"""Synthetic receipts only: every quantity change requires a confirmed fill fact."""

import ast
import builtins
from decimal import Decimal, Context, ROUND_UP, localcontext
import json
from pathlib import Path
import socket
import sqlite3
import time

import pytest
from pydantic import ValidationError

from app.exits.bindings import capture_plan
from app.exits.engine import initialize_exit, apply_event, digest
from app.exits.models import (ActionReceipt, EntryFill, EntrySealed, ExitContractError,
    ExitFill, ExitSeed, ExitVenueRules, MarketEvent, ProtectionLost, RecoveryRequired, RunnerEvidence)
from app.exits.persistence import checkpoint, restore_checkpoint
from app.exits.policy import ExitPolicy, parse_exit_policy
from app.admission.engine import admit_trade
from test_admission import case, change, NOW

D=Decimal
ROOT=Path(__file__).resolve().parents[1]


class Harness:
    def __init__(self,side='LONG',q='10',policy=None,rules=None,entry_price='100',rejected=False):
        args=case(side=side)
        if rejected: change(args,'account',paused=True)
        decision=admit_trade(**args)
        plan=capture_plan(args['setup'],args['rr'],args['scorecard'],decision)
        self.policy=policy or ExitPolicy()
        self.rules=rules or ExitVenueRules(symbol='SOLUSDT',verified=True,quantity_step='.001',price_tick='.01',
            min_quantity='.001',min_notional=5,max_quantity=1000,reduce_only_min_quantity_exempt=False,
            reduce_only_min_notional_exempt=True,exact_close_remainder=True,atomic_stop_replace=True)
        self.counter=0;self.now=NOW;self.journal=[]
        first=EntryFill(event_id='open-receipt',position_id='synthetic-position',received_at=NOW,
            fill_id='open-fill',entry_action_id='synthetic-open',quantity=q,price=entry_price,fee_usdt='.1',occurred_at=NOW)
        self.seed=ExitSeed(position_id=first.position_id,plan=plan,rules=self.rules,first_fill=first)
        self.result=initialize_exit(self.seed,self.policy);self.state=self.result.state

    def send(self,cls,**values):
        self.counter+=1;self.now=round(self.now+.01,2)
        event=cls(event_id='event-'+str(self.counter),position_id=self.state.position_id,received_at=self.now,**values)
        self.result=apply_event(self.seed,self.policy,self.state,event)
        self.state=self.result.state;self.journal.append(event)
        return self.result

    def action(self,kind):
        return next(a for a in reversed(self.state.actions) if a.kind==kind)

    def arm(self):
        self.send(EntrySealed,entry_action_id=self.state.entry_action_id,total_filled_quantity=self.state.original_quantity)
        self.ack_stop(self.action('ARM_STOP'))
        return self

    def ack_stop(self,action):
        return self.send(ActionReceipt,action_id=action.action_id,status='ACCEPTED',reduce_only_verified=True,
            stop_price=action.stop_price,covers_remaining=True,old_stop_retired=action.kind=='MOVE_STOP',
            retired_stop_cumulative_filled=0 if action.kind=='MOVE_STOP' else None)

    def tick(self,price,**kwargs):
        p=D(str(price));bid=p if self.state.side=='LONG' else p-D('.02');ask=p+D('.02') if self.state.side=='LONG' else p
        observed=kwargs.pop('observed_at',round(self.now+.01,2))
        return self.send(MarketEvent,observed_at=observed,bid=bid,ask=ask,confirmed=True,**kwargs)

    def at_r(self,r,**kwargs):
        sign=1 if self.state.side=='LONG' else -1
        return self.tick(self.state.frozen_r_anchor_entry+sign*D(str(r))*self.state.frozen_initial_r,**kwargs)

    def fill(self,action,quantity=None,price=None,fill_id=None):
        q=quantity if quantity is not None else action.quantity-action.filled_quantity
        price=price if price is not None else (self.state.last_market.bid if self.state.side=='LONG' else self.state.last_market.ask)
        return self.send(ExitFill,action_id=action.action_id,fill_id=fill_id or 'fill-'+str(self.counter+1),
                         quantity=q,price=price,fee_usdt='.01',occurred_at=round(self.now+.01,2))

    def tp1(self):
        self.at_r(1);self.fill(self.action('TP1'));self.ack_stop(self.action('MOVE_STOP'))
        return self

    def runner(self):
        self.tp1();self.at_r(2);self.fill(self.action('TP2'))
        # Extra confirmed TP fee can require one more inward cost-cover tick.
        if self.state.protection_status=='PENDING': self.ack_stop(self.action('MOVE_STOP'))
        return self

    def retire_stop(self):
        return self.send(ProtectionLost,action_id=self.state.protection_action_id,status='CANCELED',
                         cumulative_filled_quantity=0)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_full_mirror_lifecycle_and_runner_not_sold_at_three_r(side):
    h=Harness(side).arm();r=h.state.frozen_initial_r;old_stop=h.state.original_stop
    assert h.state.tp1_planned==3 and h.state.tp2_planned==4 and h.state.runner_planned==3
    h.runner()
    assert h.state.phase=='RUNNER' and h.state.remaining_quantity==h.state.runner_quantity==3
    assert h.state.milestones==('TP1_FILLED','TP2_FILLED')
    assert h.state.frozen_initial_r==r and h.state.original_stop==old_stop
    result=h.at_r(3)
    assert [a.kind for a in result.actions]==['MOVE_STOP']
    h.ack_stop(h.action('MOVE_STOP'));h.at_r(5);h.ack_stop(h.action('MOVE_STOP'))
    assert h.state.remaining_quantity==3  # Not a fixed TP3 liquidation.
    h.at_r(3.5)
    assert not any(a.kind=='CLOSE_ALL' for a in h.result.actions)  # Retire native protection first.
    h.retire_stop();close=h.action('CLOSE_ALL');h.fill(close)
    assert h.state.phase=='CLOSED' and h.state.remaining_quantity==0
    assert h.state.realized_net_pnl==h.state.realized_gross_pnl-h.state.entry_fees-h.state.exit_fees


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_touch_is_intent_not_fill_or_break_even(side):
    h=Harness(side).arm();old=h.state.current_stop
    h.at_r(1);assert h.state.phase=='TP1_PENDING'
    assert h.state.remaining_quantity==10 and h.state.tp1_filled==0 and h.state.current_stop==old
    assert not any(a.kind=='MOVE_STOP' for a in h.state.actions)
    for r in (1,1.1,1.5,2,3):
        h.at_r(r)
        assert len([a for a in h.state.actions if a.kind=='TP1'])==1
        assert h.state.remaining_quantity==10 and h.state.current_stop==old


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('kind',['TP1','TP2'])
def test_partial_fill_is_not_full_completion(side,kind):
    h=Harness(side).arm()
    if kind=='TP2': h.tp1()
    h.at_r(1 if kind=='TP1' else 2);action=h.action(kind);stop=h.state.current_stop
    h.fill(action,quantity='1')
    assert not (h.state.tp1_complete if kind=='TP1' else h.state.tp2_complete)
    assert h.state.current_stop==stop
    assert h.state.remaining_quantity==(9 if kind=='TP1' else 6)
    h.fill(h.action(kind))
    assert h.state.tp1_complete if kind=='TP1' else h.state.tp2_complete


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['entry_price','cost_covered'])
def test_break_even_costs_and_inward_tick(side,mode):
    h=Harness(side,policy=ExitPolicy(break_even_mode=mode)).arm()
    h.at_r(1);h.fill(h.action('TP1'));move=h.action('MOVE_STOP')
    assert h.state.current_stop==h.state.original_stop  # Intent not confirmation.
    h.ack_stop(move)
    q=h.state.remaining_quantity;entry=h.state.actual_average_entry;stop=h.state.current_stop
    if mode=='entry_price': assert stop==entry
    elif side=='LONG':
        net=(stop*(1-D('.001'))*(1-D('.0005'))-entry)*q
        assert net>=h.state.entry_fees+h.state.exit_fees
    else:
        net=(entry-stop*(1+D('.001'))*(1+D('.0005')))*q
        assert net>=h.state.entry_fees+h.state.exit_fees


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_stop_never_widens_and_r_never_drifts(side):
    h=Harness(side).arm().runner();h.at_r(5);h.ack_stop(h.action('MOVE_STOP'))
    tight=h.state.current_stop;r=h.state.frozen_initial_r
    h.at_r(4.5)
    assert not any(a.kind=='MOVE_STOP' for a in h.result.actions)
    assert h.state.current_stop==tight and h.state.frozen_initial_r==r
    altered=h.state.model_copy(update={'frozen_initial_r':r+1})
    with pytest.raises(ExitContractError): apply_event(h.seed,h.policy,altered,h.journal[-1])


def test_duplicate_event_and_fill_identifiers_do_not_reduce_twice():
    h=Harness().arm();h.at_r(1);h.fill(h.action('TP1'),quantity=1,fill_id='one-trade')
    event=h.journal[-1];before=h.state
    result=apply_event(h.seed,h.policy,before,event)
    assert result.state==before and result.actions==()
    duplicate=event.model_copy(update={'event_id':'new-delivery','received_at':h.now+.1})
    result=apply_event(h.seed,h.policy,h.state,duplicate)
    assert result.state.remaining_quantity==9 and result.actions==()
    bad=event.model_copy(update={'quantity':D(2)})
    with pytest.raises(ExitContractError): apply_event(h.seed,h.policy,h.state,bad)


def test_gap_across_both_tps_serializes_confirmed_allocations():
    h=Harness().arm();h.at_r(4)
    assert [a.kind for a in h.result.actions]==['TP1']
    h.fill(h.action('TP1'))
    assert [a.kind for a in h.result.actions]==['MOVE_STOP']
    h.ack_stop(h.action('MOVE_STOP'))
    assert [a.kind for a in h.result.actions]==['TP2']
    assert h.action('TP2').quantity==4 and h.state.remaining_quantity==7


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_stop_cross_with_pending_tp_waits_for_authoritative_cancel_and_fills(side):
    h=Harness(side).arm();h.at_r(1);tp=h.action('TP1');h.at_r(-3)
    assert any(a.kind=='CANCEL' and a.target_action_id==tp.action_id for a in h.result.actions)
    cancel=h.action('CANCEL')
    h.send(ActionReceipt,action_id=cancel.action_id,status='ACCEPTED')
    assert not any(a.kind=='CLOSE_ALL' for a in h.state.actions)
    h.send(ActionReceipt,action_id=tp.action_id,status='CANCELED',cumulative_filled_quantity=1)
    assert h.action('TP1').status=='SETTLING'
    h.fill(tp,quantity=1,price=100)
    assert h.state.remaining_quantity==9 and not h.state.tp1_complete
    h.retire_stop();close=h.action('CLOSE_ALL')
    assert close.quantity==9
    gap=80 if side=='LONG' else 120
    h.fill(close,price=gap)
    assert h.state.phase=='CLOSED' and h.state.realized_net_pnl<0
    assert h.state.realized_gross_pnl==9*(D(gap)-100)*(1 if side=='LONG' else -1)


@pytest.mark.parametrize('status',['UNKNOWN','FILLED'])
def test_unknown_and_terminal_ahead_of_fills_never_resend(status):
    h=Harness().arm();h.at_r(1);tp=h.action('TP1')
    h.send(ActionReceipt,action_id=tp.action_id,status=status,cumulative_filled_quantity=3 if status=='FILLED' else 0)
    for r in (1,2,4):
        h.at_r(r)
        assert not h.result.actions
    assert len([a for a in h.state.actions if a.kind=='TP1'])==1
    assert h.state.remaining_quantity==10 and not h.state.tp1_complete
    h.fill(tp,quantity=3)
    assert h.state.remaining_quantity==7 and h.state.tp1_complete


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_late_order_ack_cannot_undo_fill(side):
    h=Harness(side).arm();h.at_r(1);tp=h.action('TP1');h.fill(tp,quantity=1)
    h.send(ActionReceipt,action_id=tp.action_id,status='ACCEPTED',reduce_only_verified=True)
    assert not h.state.faults and h.state.remaining_quantity==9
    h.fill(h.action('TP1'));h.ack_stop(h.action('MOVE_STOP'))
    h.send(ActionReceipt,action_id=tp.action_id,status='ACCEPTED',reduce_only_verified=True)
    assert h.action('TP1').status=='FILLED' and not h.state.faults


@pytest.mark.parametrize('q',['.001','.009','.01','.101','1.007','10.999'])
def test_original_quantity_allocation_precision_and_tail(q):
    h=Harness(q=q).arm()
    assert h.state.tp1_planned+h.state.tp2_planned+h.state.runner_planned==D(q)
    assert h.state.tp1_planned%h.rules.quantity_step==0 and h.state.tp2_planned%h.rules.quantity_step==0
    h.at_r(-2);h.retire_stop();close=h.action('CLOSE_ALL')
    assert close.quantity==D(q)
    h.fill(close,price=90)
    assert h.state.remaining_quantity==0 and h.state.phase=='CLOSED'


def test_substep_tail_uses_explicit_exact_remainder_never_uplift():
    h=Harness(q='.0004').arm();h.at_r(-2);h.retire_stop();close=h.action('CLOSE_ALL')
    assert close.quantity==D('.0004') and close.close_exact_remainder
    h.fill(close,price=90);assert h.state.phase=='CLOSED'


def test_missing_exact_close_capability_reports_unmanageable_tail_not_fake_closed():
    rules=Harness().rules.model_copy(update={'exact_close_remainder':False})
    h=Harness(q='.0004',rules=rules).arm();h.at_r(-2);h.retire_stop()
    assert 'UNMANAGEABLE_REMAINDER_REQUIRES_ADAPTER' in h.state.faults
    assert h.state.remaining_quantity==D('.0004') and h.state.phase=='PROTECTION_REQUIRED'
    assert not any(a.kind=='CLOSE_ALL' for a in h.state.actions)


def test_subminimum_tp_deferred_then_combined_not_marked_filled():
    rules=Harness().rules.model_copy(update={'min_quantity':D('.004')})
    h=Harness(q='.01',rules=rules).arm();h.at_r(1)
    assert not h.result.actions and not h.state.tp1_complete
    h.at_r(2)
    # The combined TP would leave only .003, so request a full dust sweep instead.
    assert h.state.emergency_reason=='DUST_SWEEP_FULL_REMAINDER'
    h.retire_stop();assert h.action('CLOSE_ALL').quantity==D('.01')


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('flag',['new_entries_paused','daily_halted','admission_rejected','score_available'])
def test_new_entry_gates_never_disable_existing_position_protection(side,flag):
    h=Harness(side,rejected=True).arm()
    assert h.seed.plan.admission_result=='REJECT'
    h.at_r(-2,**{flag:False if flag=='score_available' else True})
    assert h.state.emergency_reason=='STOP_CROSSED_OR_GAPPED'
    h.retire_stop();assert h.action('CLOSE_ALL').quantity==10


def test_time_exit_works_without_market_data():
    h=Harness(policy=ExitPolicy(max_holding_seconds='1')).arm()
    h.now=NOW+2;h.send(MarketEvent,observed_at=NOW,bid=None,ask=None,confirmed=False)
    assert h.state.emergency_reason=='TIME_EXIT'
    h.retire_stop();assert h.action('CLOSE_ALL').quantity==10


@pytest.mark.parametrize('confirmed,age,should_exit',[(True,0,True),(False,0,False),(True,100,False)])
def test_trend_invalidation_requires_confirmed_fresh_evidence(confirmed,age,should_exit):
    h=Harness().arm()
    evidence=RunnerEvidence(kind='trend_invalid',evidence_id='trend-1',invalid=True,confirmed=confirmed,observed_at=h.now-age)
    h.tick(100,evidence=(evidence,))
    assert (h.state.emergency_reason=='CONFIRMED_TREND_INVALIDATION')==should_exit


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('strategy',['swing','atr'])
@pytest.mark.parametrize('confirmed',[True,False])
def test_runner_extension_inputs_and_tightening(side,strategy,confirmed):
    h=Harness(side,policy=ExitPolicy(runner_strategy=strategy)).arm().runner()
    kind='atr' if strategy=='atr' else 'swing_low' if side=='LONG' else 'swing_high'
    value=D(2) if strategy=='atr' else D(110) if side=='LONG' else D(90)
    evidence=RunnerEvidence(kind=kind,evidence_id='structure-1',value=value,confirmed=confirmed,observed_at=h.now)
    stop=h.state.current_stop;h.at_r(4,evidence=(evidence,))
    moves=[a for a in h.result.actions if a.kind=='MOVE_STOP']
    assert bool(moves)==confirmed
    if confirmed:
        h.ack_stop(moves[0]);assert h.state.current_stop>stop if side=='LONG' else h.state.current_stop<stop


def test_entry_partial_fills_freeze_first_r_and_seal_actual_original_quantity():
    h=Harness(q=4);r=h.state.frozen_initial_r
    h.send(EntryFill,fill_id='opening-part-2',entry_action_id=h.state.entry_action_id,quantity=6,price=101,
           fee_usdt='.1',occurred_at=h.now)
    assert h.state.original_quantity==10 and h.state.actual_average_entry==D('100.6')
    assert h.state.frozen_initial_r==r and h.state.frozen_r_anchor_entry==100
    h.arm();assert h.state.tp1_planned==3 and h.state.tp2_planned==4


def test_zero_or_crossed_original_stop_still_requests_emergency_close():
    for price in ('95','94'):
        h=Harness(entry_price=price)
        assert h.state.emergency_reason=='FILL_ALREADY_BEYOND_INITIAL_STOP'
        assert h.action('CLOSE_ALL').quantity==10


@pytest.mark.parametrize('status',['REJECTED','CANCELED'])
def test_failed_initial_stop_requests_close_only_after_authoritative_terminal(status):
    h=Harness();stop=h.action('ARM_STOP')
    h.send(ActionReceipt,action_id=stop.action_id,status=status,cumulative_filled_quantity=0)
    assert h.action('CLOSE_ALL').quantity==10
    assert h.state.protection_status=='MISSING'


def test_canceled_stop_without_filled_total_is_not_active_or_safe_to_resend():
    h=Harness().arm();stop=h.action('ARM_STOP')
    h.send(ProtectionLost,action_id=stop.action_id,status='CANCELED')
    assert h.state.protection_status=='UNKNOWN'
    assert not any(a.kind=='CLOSE_ALL' for a in h.state.actions)
    h.send(ActionReceipt,action_id=stop.action_id,status='CANCELED',cumulative_filled_quantity=0)
    assert h.action('CLOSE_ALL').quantity==10


@pytest.mark.parametrize('stage',['initial','tp1_pending','tp1_partial','tp1_filled','runner','unknown'])
def test_restart_replays_and_fences_original_actions_without_resending(stage):
    h=Harness().arm()
    if stage.startswith('tp1') or stage=='unknown': h.at_r(1)
    if stage=='tp1_partial': h.fill(h.action('TP1'),quantity=1)
    if stage=='tp1_filled': h.fill(h.action('TP1'));h.ack_stop(h.action('MOVE_STOP'))
    if stage=='runner': h.runner()
    if stage=='unknown': h.send(ActionReceipt,action_id=h.action('TP1').action_id,status='UNKNOWN')
    snap=checkpoint(h.seed,h.policy,tuple(h.journal));assert snap.state==h.state
    recovery=RecoveryRequired(event_id='recovery-1',position_id=h.state.position_id,received_at=h.now+1)
    restored=restore_checkpoint(snap.model_dump_json(),recovery_event=recovery)
    assert restored.state.remaining_quantity==h.state.remaining_quantity
    assert restored.state.frozen_initial_r==h.state.frozen_initial_r
    added=restored.state.actions[len(h.state.actions):]
    assert all(a.kind=='RECONCILE' for a in added)
    tick=MarketEvent(event_id='after-restart',position_id=h.state.position_id,received_at=h.now+2,
                     observed_at=h.now+2,bid=120,ask=D('120.01'),confirmed=True)
    result=apply_event(h.seed,h.policy,restored.state,tick)
    assert result.actions==() and result.state.phase=='PROTECTION_REQUIRED'
    assert len([a for a in result.state.actions if a.kind=='TP1'])==len([a for a in h.state.actions if a.kind=='TP1'])


def test_tampered_checkpoint_rejected_and_original_ids_restored():
    h=Harness().arm().tp1();snap=checkpoint(h.seed,h.policy,tuple(h.journal));payload=json.loads(snap.model_dump_json())
    payload['state']['tp1_filled']='0'
    recovery=RecoveryRequired(event_id='resume',position_id=h.state.position_id,received_at=h.now+1)
    with pytest.raises(ExitContractError): restore_checkpoint(json.dumps(payload),recovery_event=recovery)
    payload=json.loads(snap.model_dump_json());payload['journal']=payload['journal'][:-1]
    with pytest.raises(ExitContractError): restore_checkpoint(json.dumps(payload),recovery_event=recovery)


def test_policy_template_and_invalid_policy():
    assert parse_exit_policy((ROOT/'exit-policy.yaml').read_text())==ExitPolicy()
    for raw in ('mode: live','tp1_fraction: 0.4','tp2_r: 0.5','runner_trail_r: 0',
                'mode: paper_only\nmode: paper_only','x: &one 1\ny: *one','auto_live: true'):
        with pytest.raises((ValidationError,ValueError)): parse_exit_policy(raw)


def test_pure_reducer_under_io_database_network_clock_poison(monkeypatch):
    h=Harness().arm();event=MarketEvent(event_id='poison-test',position_id=h.state.position_id,
        received_at=h.now+1,observed_at=h.now+1,bid=110,ask=D('110.01'),confirmed=True)
    def denied(*args,**kwargs): raise AssertionError('No IO is allowed')
    for obj,name in ((builtins,'open'),(sqlite3,'connect'),(socket,'socket'),(time,'time')):
        monkeypatch.setattr(obj,name,denied)
    result=apply_event(h.seed,h.policy,h.state,event)
    assert result.actions[0].kind=='TP1' and result.state.live_allowed is False


def test_no_live_mode_no_upstream_mutation_and_no_runtime_wiring():
    h=Harness().arm();before=(h.seed.model_dump_json(),h.policy.model_dump_json())
    h.runner();assert before==(h.seed.model_dump_json(),h.policy.model_dump_json())
    with pytest.raises(ValidationError): ExitSeed.model_validate({**h.seed.model_dump(),'mode':'live'})
    paths=list((ROOT/'app/exits').glob('*.py'))
    allowed={'__future__','decimal','typing','hashlib','json','pydantic','yaml','app.setups.models',
        'app.setups.rr_models','app.setups.scorecard_models','app.admission.models','models','policy','engine','runner'}
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.Import): assert all(a.name in allowed for a in node.names)
            if isinstance(node,ast.ImportFrom): assert node.module in allowed
    for path in [ROOT/'main.py',*(ROOT/'app').rglob('*.py')]:
        if 'exits' in path.parts: continue
        # Stage 7 exact offline content checks; no Paper/Live wiring permitted.
        if path.relative_to(ROOT).as_posix() in {
            'app/configuration/contracts.py','app/configuration/inputs.py','app/configuration/compiler.py',
            'app/offline_paper/models.py','app/offline_paper/engine.py','app/offline_paper/broker.py',
            'app/admitted_paper/models.py','app/admitted_paper/scenarios.py',
            'app/admitted_paper/engine.py','app/admitted_paper/gate.py',
            'app/historical_replay/scenarios.py','app/historical_replay/gate.py',
            'app/historical_replay/engine.py','app/historical_replay/replay.py',
            # 8D exact isolated composition; the original main remains excluded.
            'app/execution_costs/gate.py','app/execution_costs/engine.py','app/execution_costs/replay.py',
            'app/scenario_diagnostics/scenarios.py','app/scenario_diagnostics/quantities.py'}: continue
        assert 'app.exits' not in path.read_text()
    assert 'exit-policy' not in (ROOT/'config.yaml').read_text()
    assert 'dry_run: true' in (ROOT/'config.yaml').read_text()
    assert '"live_runtime_allowed": false' in (ROOT/'isolation-policy.json').read_text()
