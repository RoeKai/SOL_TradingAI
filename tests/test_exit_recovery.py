"""Adversarial ordering, precision and restart invariants for the pure exit reducer."""

from decimal import Decimal, Context, ROUND_UP, localcontext
import json

import pytest
from pydantic import ValidationError

from app.exits.engine import apply_event, initialize_exit
from app.exits.models import (ActionReceipt, EntryFill, EntrySealed, ExitContractError, ExitFill,
    MarketEvent, ProtectionLost, RecoveryRequired, RunnerEvidence)
from app.exits.persistence import checkpoint, restore_checkpoint
from app.exits.policy import ExitPolicy
from test_exit_policy import Harness, NOW

D=Decimal


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('q',['.01','.101','1.007','10','19.999'])
@pytest.mark.parametrize('parts',[('.3','.4','.3'),('.2','.5','.3'),('.1','.2','.7')])
def test_quantity_conservation_full_exit_matrix(side,q,parts):
    policy=ExitPolicy(tp1_fraction=parts[0],tp2_fraction=parts[1],runner_fraction=parts[2])
    h=Harness(side,q=q,policy=policy).arm()
    for level in (1,2,4):
        h.at_r(level)
        for _ in range(8):
            active=[a for a in h.state.actions if a.status=='INTENT' and a.kind in ('TP1','TP2','TP_COMBINED','MOVE_STOP')]
            if not active: break
            a=active[-1]
            if a.kind=='MOVE_STOP': h.ack_stop(a)
            else: h.fill(a)
            assert 0<=h.state.remaining_quantity<=D(q)
        assert h.state.tp1_planned+h.state.tp2_planned+h.state.runner_planned==D(q)
    h.at_r(-3)
    if h.state.protection_action_id: h.retire_stop()
    while h.state.remaining_quantity:
        a=h.action('CLOSE_ALL');assert a.quantity<=h.state.remaining_quantity
        h.fill(a,price=85 if side=='LONG' else 115)
    exits=[f for f in h.state.fill_facts if f.action_id!=h.state.entry_action_id]
    assert sum((f.quantity for f in exits),D(0))==D(q)
    assert h.state.phase=='CLOSED'
    saved=checkpoint(h.seed,h.policy,tuple(h.journal))
    assert saved.state==h.state


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_true_combined_tp_allocates_original_goals_after_confirmation(side):
    policy=ExitPolicy(tp1_fraction='.1',tp2_fraction='.4',runner_fraction='.5')
    rules=Harness().rules.model_copy(update={'min_quantity':D(2)})
    h=Harness(side,policy=policy,rules=rules).arm();h.at_r(1)
    assert not h.result.actions and h.state.tp1_filled==0
    h.at_r(2);action=h.action('TP_COMBINED');assert action.quantity==5
    h.fill(action,quantity=2)
    assert h.state.tp1_filled==1 and h.state.tp2_filled==1 and h.state.tp1_complete and not h.state.tp2_complete
    assert h.state.current_stop==h.state.original_stop  # Serial exit settlement before stop replacement.
    h.fill(h.action('TP_COMBINED'))
    assert h.state.tp2_complete and h.state.remaining_quantity==5


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_native_stop_fill_races_pending_tp_without_double_quantity(side):
    h=Harness(side).arm();h.at_r(1);stop=h.action('ARM_STOP');tp=h.action('TP1')
    h.fill(stop,quantity=8,price=94 if side=='LONG' else 106)
    assert h.state.remaining_quantity==2
    assert any(a.kind=='CANCEL' and a.target_action_id==tp.action_id for a in h.state.actions)
    # A TP fill which was already in flight is still an authoritative fact.
    h.fill(tp,quantity=1,price=105 if side=='LONG' else 95)
    assert h.state.remaining_quantity==1
    h.send(ActionReceipt,action_id=tp.action_id,status='CANCELED',cumulative_filled_quantity=1)
    h.send(ActionReceipt,action_id=stop.action_id,status='CANCELED',cumulative_filled_quantity=8)
    close=h.action('CLOSE_ALL');assert close.quantity==1
    h.fill(close,price=90 if side=='LONG' else 110)
    assert h.state.phase=='CLOSED'


def test_confirmed_overfill_is_quarantined_not_negative_or_reported_closed():
    h=Harness().arm();h.at_r(1);stop=h.action('ARM_STOP');tp=h.action('TP1')
    h.fill(stop,quantity=9,price=94)
    h.fill(tp,quantity=2,price=105)
    assert h.state.remaining_quantity==1 and h.state.phase=='PROTECTION_REQUIRED'
    assert 'CONFIRMED_FILL_EXCEEDS_REMAINING' in h.state.faults
    assert all(a.kind=='RECONCILE' for a in h.result.actions)


@pytest.mark.parametrize('retired_total',[None,D(1)])
def test_move_stop_requires_retired_order_cumulative_fill_proof(retired_total):
    h=Harness().arm();h.at_r(1);h.fill(h.action('TP1'));move=h.action('MOVE_STOP');old=h.action('ARM_STOP')
    h.send(ActionReceipt,action_id=move.action_id,status='ACCEPTED',reduce_only_verified=True,
        stop_price=move.stop_price,covers_remaining=True,old_stop_retired=True,
        retired_stop_cumulative_filled=retired_total)
    if retired_total is None:
        assert 'STOP_ACK_NOT_AUTHORITATIVE' in h.state.faults
    else:
        assert next(a for a in h.state.actions if a.action_id==old.action_id).status=='SETTLING'
        h.at_r(3);assert not any(a.kind=='TP2' for a in h.state.actions)
        h.fill(old,quantity=1,price=95)
        assert h.state.remaining_quantity==6 and h.state.emergency_reason=='CONFIRMED_STOP_EXECUTION'


def test_late_old_stop_ack_does_not_resurrect_canceled_stop():
    h=Harness().arm();old=h.action('ARM_STOP');h.tp1();current=h.state.protection_action_id
    h.send(ActionReceipt,action_id=old.action_id,status='ACCEPTED',reduce_only_verified=True,
        stop_price=old.stop_price,covers_remaining=True)
    assert h.state.protection_action_id==current and h.state.current_stop>old.stop_price


def test_action_accepted_ahead_of_fills_waits_for_trade_details():
    h=Harness().arm();h.at_r(1);tp=h.action('TP1')
    h.send(ActionReceipt,action_id=tp.action_id,status='ACCEPTED',reduce_only_verified=True,cumulative_filled_quantity=1)
    assert h.action('TP1').status=='SETTLING' and h.state.remaining_quantity==10
    h.fill(tp,quantity=1)
    assert h.action('TP1').status=='ACCEPTED' and h.state.remaining_quantity==9


@pytest.mark.parametrize('status,total',[('FILLED',1),('CANCELED',4),('REJECTED',4)])
def test_invalid_terminal_report_never_fakes_a_full_tp(status,total):
    h=Harness().arm();h.at_r(1);h.send(ActionReceipt,action_id=h.action('TP1').action_id,
                                   status=status,cumulative_filled_quantity=total)
    assert h.state.faults and h.state.remaining_quantity==10 and not h.state.tp1_complete


def test_canceled_partial_tp_retries_only_remaining_original_allocation():
    h=Harness().arm();h.at_r(1);first=h.action('TP1');h.fill(first,quantity=1)
    h.send(ActionReceipt,action_id=first.action_id,status='CANCELED',cumulative_filled_quantity=1)
    retry=h.action('TP1')
    assert retry.action_id!=first.action_id and retry.quantity==2
    h.fill(retry)
    assert h.state.tp1_filled==3 and h.state.remaining_quantity==7


@pytest.mark.parametrize('kind',['TP1','CLOSE_ALL'])
def test_known_rejections_have_bounded_attempts(kind):
    h=Harness().arm();h.at_r(1 if kind=='TP1' else -2)
    if kind=='CLOSE_ALL': h.retire_stop()
    for _ in range(3):
        a=h.action(kind);h.send(ActionReceipt,action_id=a.action_id,status='REJECTED',cumulative_filled_quantity=0)
    assert len([a for a in h.state.actions if a.kind==kind])==3
    if kind=='TP1':
        assert h.state.protection_status=='ACTIVE'
        assert 'TP_RETRY_BUDGET_EXHAUSTED_KEEP_PROTECTION' in h.result.reason_codes
        h.at_r(-2);h.retire_stop();assert h.action('CLOSE_ALL').quantity==10
    else: assert 'EMERGENCY_EXIT_REPEATED_REJECTION' in h.state.faults


def test_final_exit_above_venue_max_is_serially_chunked_then_exact_tail():
    rules=Harness().rules.model_copy(update={'max_quantity':D(4)})
    h=Harness(q='10.0004',rules=rules).arm();h.at_r(-2);h.retire_stop()
    quantities=[]
    while h.state.remaining_quantity:
        a=h.action('CLOSE_ALL');quantities.append(a.quantity)
        assert a.quantity<=4
        h.fill(a,price=90)
    assert quantities==[D(4),D(4),D('2.0004')]
    assert h.action('CLOSE_ALL').close_exact_remainder and h.state.phase=='CLOSED'


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_recovery_confirmations_release_fence_without_reexecuting_finished_tp(side):
    h=Harness(side).arm().tp1();snap=checkpoint(h.seed,h.policy,tuple(h.journal))
    recovery=RecoveryRequired(event_id='resume',position_id=h.state.position_id,received_at=h.now+1)
    restored=restore_checkpoint(snap.model_dump_json(),recovery_event=recovery)
    h.state=restored.state;h.journal=list(restored.journal);h.now=recovery.received_at
    stop=h.action('MOVE_STOP');h.ack_stop(stop)
    assert not h.state.recovery_pending_action_ids
    h.at_r(2)
    assert h.result.actions[0].kind=='TP2'
    assert len([a for a in h.state.actions if a.kind=='TP1'])==1 and h.state.tp1_filled==3


def test_two_restarts_query_again_but_never_resend_an_exit_order():
    h=Harness().arm();h.at_r(1);h.send(ActionReceipt,action_id=h.action('TP1').action_id,status='UNKNOWN')
    saved=checkpoint(h.seed,h.policy,tuple(h.journal))
    for i in (1,2):
        n=len(saved.state.actions)
        saved=restore_checkpoint(saved.model_dump_json(),recovery_event=RecoveryRequired(event_id='resume-'+str(i),
            position_id=h.state.position_id,received_at=h.now+i))
        assert saved.state.actions[n:] and all(a.kind=='RECONCILE' for a in saved.state.actions[n:])
    assert len([a for a in saved.state.actions if a.kind=='TP1'])==1


def test_entry_fill_after_emergency_exit_preserves_historical_pnl_and_initial_r():
    h=Harness(q=4);stop=h.action('ARM_STOP')
    h.send(ActionReceipt,action_id=stop.action_id,status='REJECTED',cumulative_filled_quantity=0)
    h.fill(h.action('CLOSE_ALL'),price=90)
    assert h.state.phase=='PROTECTION_REQUIRED' and h.state.remaining_quantity==0
    old_pnl=h.state.realized_gross_pnl;r=h.state.frozen_initial_r
    h.send(EntryFill,fill_id='late-entry',entry_action_id=h.state.entry_action_id,quantity=1,price=99,
        fee_usdt='.01',occurred_at=h.now)
    assert h.state.remaining_quantity==1 and h.state.realized_gross_pnl==old_pnl
    assert h.state.frozen_initial_r==r and h.action('CLOSE_ALL').quantity==1
    h.send(EntrySealed,entry_action_id=h.state.entry_action_id,total_filled_quantity=5)
    h.fill(h.action('CLOSE_ALL'),price=90)
    assert h.state.phase=='CLOSED'


@pytest.mark.parametrize('field,value',[('tp1_complete',True),('current_stop',D(100)),
    ('actual_average_entry',D(101)),('entry_fees',D(5)),('realized_net_pnl',D(10)),
    ('tp1_planned',D(4)),('version',400),('confirmed_fill_action_ids',('fake',))])
def test_forged_state_is_rejected_before_actions(field,value):
    h=Harness().arm();bad=h.state.model_copy(update={field:value})
    event=MarketEvent(event_id='bad',position_id=h.state.position_id,received_at=h.now+1,observed_at=h.now+1,
        bid=110,ask=D('110.01'),confirmed=True)
    with pytest.raises(ExitContractError): apply_event(h.seed,h.policy,bad,event)


def test_cost_cover_stop_crossed_during_tp1_fill_exits_instead_of_invalid_stop_order():
    h=Harness().arm();h.at_r(1);tp=h.action('TP1');h.tick(99)
    h.fill(tp,price=105)
    assert h.state.emergency_reason=='PROPOSED_STOP_ALREADY_CROSSED'
    assert not any(a.kind=='MOVE_STOP' for a in h.state.actions)
    h.retire_stop();assert h.action('CLOSE_ALL').quantity==7


@pytest.mark.parametrize('delta,confirmed',[(100,True),(0,False)])
def test_stale_or_unverified_market_does_not_fake_fills_or_remove_existing_stop(delta,confirmed):
    h=Harness().arm();h.now+=delta
    h.send(MarketEvent,observed_at=NOW,bid=120,ask=D('120.01'),confirmed=confirmed)
    assert not h.result.actions and h.state.remaining_quantity==10 and h.state.protection_status=='ACTIVE'


def test_old_market_delivery_does_not_rewind_favorable_extreme():
    h=Harness().arm().runner();h.at_r(5);peak=h.state.favorable_extreme
    h.send(MarketEvent,observed_at=NOW,bid=120,ask=D('120.01'),confirmed=True)
    assert h.state.favorable_extreme==peak


def test_deterministic_under_different_decimal_contexts():
    h=Harness(q='10.003').arm();event=MarketEvent(event_id='context',position_id=h.state.position_id,
        received_at=h.now+1,observed_at=h.now+1,bid=106,ask=D('106.01'),confirmed=True)
    normal=apply_event(h.seed,h.policy,h.state,event)
    with localcontext(Context(prec=8,rounding=ROUND_UP)):
        other=apply_event(h.seed,h.policy,h.state,event)
    assert normal==other


def test_policy_change_cannot_reprice_an_open_position_or_widen_stop():
    h=Harness().arm().runner();policy=h.policy.model_copy(update={'runner_trail_r':D(10)})
    with pytest.raises(ExitContractError): apply_event(h.seed,policy,h.state,h.journal[-1])


def test_final_closed_checkpoint_stays_closed_after_restart_and_duplicate_fill():
    h=Harness().arm();h.at_r(-2);h.retire_stop();h.fill(h.action('CLOSE_ALL'),price=90)
    last=h.journal[-1];saved=checkpoint(h.seed,h.policy,tuple(h.journal))
    restored=restore_checkpoint(saved.model_dump_json(),recovery_event=RecoveryRequired(event_id='closed-resume',
        position_id=h.state.position_id,received_at=h.now+1))
    assert restored.state.phase=='CLOSED' and restored.state.remaining_quantity==0
    result=apply_event(h.seed,h.policy,restored.state,last)
    assert not result.actions and result.state==restored.state


def test_future_quote_cannot_poison_following_valid_quotes():
    h=Harness().arm();h.send(MarketEvent,observed_at=h.now+100,bid=130,ask=D('130.01'),confirmed=True)
    assert h.state.last_market is None and not h.result.actions
    h.at_r(1);assert h.result.actions[0].kind=='TP1'


def test_out_of_order_opening_facts_never_redefine_first_confirmed_r():
    h=Harness(q=4);r=h.state.frozen_initial_r
    h.send(EntryFill,fill_id='earlier-confirmed-later',entry_action_id=h.state.entry_action_id,
        quantity=6,price=101,fee_usdt='.1',occurred_at=NOW-1)
    assert h.state.original_quantity==10 and h.state.actual_average_entry==D('100.6')
    assert h.state.opened_at==NOW-1 and h.state.frozen_initial_r==r


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_unknown_stop_movement_requires_original_id_reconciliation(side):
    h=Harness(side).arm();h.at_r(1);h.fill(h.action('TP1'));move=h.action('MOVE_STOP')
    old_stop=h.state.current_stop
    h.send(ActionReceipt,action_id=move.action_id,status='UNKNOWN')
    assert h.state.protection_status=='UNKNOWN' and h.state.phase=='PROTECTION_REQUIRED'
    for r in (2,3,4):
        h.at_r(r)
        assert not h.result.actions and h.state.current_stop==old_stop
    h.ack_stop(move)
    assert h.state.current_stop!=old_stop and h.state.protection_status=='ACTIVE'


def test_failed_move_preserves_confirmed_old_stop_until_retired_then_closes():
    h=Harness().arm();h.at_r(1);h.fill(h.action('TP1'));move=h.action('MOVE_STOP');old=h.action('ARM_STOP')
    h.send(ActionReceipt,action_id=move.action_id,status='REJECTED',cumulative_filled_quantity=0)
    assert h.state.current_stop==old.stop_price and h.state.protection_status=='ACTIVE'
    assert not any(a.kind=='CLOSE_ALL' for a in h.state.actions)
    h.retire_stop();assert h.action('CLOSE_ALL').quantity==7


def test_no_atomic_stop_replace_capability_uses_explicit_emergency_exit():
    rules=Harness().rules.model_copy(update={'atomic_stop_replace':False})
    h=Harness(rules=rules).arm();h.at_r(1);h.fill(h.action('TP1'))
    assert h.state.emergency_reason=='ATOMIC_STOP_REPLACE_UNAVAILABLE'
    assert not any(a.kind=='MOVE_STOP' for a in h.state.actions)
    assert h.state.protection_status=='ACTIVE'
    h.retire_stop();assert h.action('CLOSE_ALL').quantity==7


def test_unverified_venue_cannot_create_exit_intent_or_claim_protection():
    rules=Harness().rules.model_copy(update={'verified':False})
    h=Harness(rules=rules)
    assert 'EXIT_RULES_UNVERIFIED' in h.state.faults
    assert not any(a.kind in ('ARM_STOP','CLOSE_ALL') for a in h.state.actions)
    assert h.state.remaining_quantity==10 and h.state.phase=='PROTECTION_REQUIRED'


@pytest.mark.parametrize('field,value',[('entry_action_id','wrong-open'),('phase','CLOSED'),
    ('runner_quantity',D(5)),('opened_at',NOW-100),('completed_action_ids',('wrong-id',))])
def test_extra_state_invariants_reject_before_new_actions(field,value):
    h=Harness().arm();bad=h.state.model_copy(update={field:value})
    with pytest.raises(ExitContractError): apply_event(h.seed,h.policy,bad,h.journal[-1])


def test_tampered_action_filled_quantity_rejected_even_when_total_position_matches():
    h=Harness().arm();h.at_r(1);h.fill(h.action('TP1'),quantity=1)
    bad=h.state.model_copy(update={'actions':tuple(a.model_copy(update={'filled_quantity':D(0)})
        if a.kind=='TP1' else a for a in h.state.actions)})
    with pytest.raises(ExitContractError): apply_event(h.seed,h.policy,bad,h.journal[-1])


def test_accepted_report_larger_than_order_is_not_a_valid_partial_fill():
    h=Harness().arm();h.at_r(1);h.send(ActionReceipt,action_id=h.action('TP1').action_id,
        status='ACCEPTED',reduce_only_verified=True,cumulative_filled_quantity=4)
    assert 'RECEIPT_QUANTITY_EXCEEDS_INTENT' in h.state.faults
    assert h.state.remaining_quantity==10
