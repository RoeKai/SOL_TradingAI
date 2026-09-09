"""Synthetic review regressions; the first six also run against daa68658."""

from decimal import Decimal
import json

import pytest
from pydantic import ValidationError

from app.exits.engine import initialize_exit, apply_event
from app.exits.models import (ActionReceipt, EntryFill, EntrySealed, ExitFill,
                             ExitContractError, MarketEvent, RecoveryRequired)
from app.exits.persistence import checkpoint, restore_checkpoint
from app.exits.policy import ExitPolicy
from app.exits.runner import break_even_price
from test_exit_policy import Harness, NOW

D = Decimal


def zero_fee_open(side, quantity=4):
    h = Harness(side, q=quantity)
    h.seed = h.seed.model_copy(update={
        'first_fill': h.seed.first_fill.model_copy(update={'fee_usdt': D(0)})})
    h.result = initialize_exit(h.seed, h.policy)
    h.state = h.result.state
    return h


def confirmed_exit(h, action, quantity, price, fee=0, occurred_at=None):
    return h.send(ExitFill, action_id=action.action_id, fill_id='review-exit-' + str(h.counter),
                  quantity=quantity, price=price, fee_usdt=fee,
                  occurred_at=h.now if occurred_at is None else occurred_at)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_review_new_entry_cannot_reuse_four_unit_stop_as_ten_unit_coverage(side):
    h = zero_fee_open(side)
    stop = h.action('ARM_STOP')
    h.ack_stop(stop)
    assert h.state.protection_status == 'ACTIVE' and stop.quantity == 4
    h.send(EntryFill, fill_id='review-extra-six', entry_action_id=h.state.entry_action_id,
           quantity=6, price=100, fee_usdt=0, occurred_at=h.now)
    assert h.state.remaining_quantity == 10
    assert h.state.protection_status != 'ACTIVE', 'A four-unit ACK cannot protect ten units'
    assert not any(a.kind == 'CANCEL' and a.target_action_id == stop.action_id
                   for a in h.result.actions), 'Do not remove valid protection before replacement'
    assert h.result.actions or h.state.faults, 'Missing coverage must not wait silently'


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_review_late_entry_final_realized_pnl_equals_confirmed_cashflows(side):
    h = zero_fee_open(side)
    h.send(ActionReceipt, action_id=h.action('ARM_STOP').action_id,
           status='REJECTED', cumulative_filled_quantity=0)
    exit_price = 90 if side == 'LONG' else 110
    confirmed_exit(h, h.action('CLOSE_ALL'), 4, exit_price)
    assert h.state.realized_gross_pnl == -40 and h.state.remaining_quantity == 0
    r = h.state.frozen_initial_r
    h.send(EntryFill, fill_id='review-late-one', entry_action_id=h.state.entry_action_id,
           quantity=1, price=99 if side == 'LONG' else 101, fee_usdt=0, occurred_at=h.now)
    assert h.state.realized_gross_pnl == -40 and h.state.frozen_initial_r == r
    h.send(EntrySealed, entry_action_id=h.state.entry_action_id, total_filled_quantity=5)
    confirmed_exit(h, h.action('CLOSE_ALL'), 1, exit_price)
    assert h.state.phase == 'CLOSED'
    entries = [f for f in h.state.fill_facts if f.action_id == h.state.entry_action_id]
    exits = [f for f in h.state.fill_facts if f.action_id != h.state.entry_action_id]
    cashflow = (sum(f.price * f.quantity for f in exits) -
                sum(f.price * f.quantity for f in entries)) * (1 if side == 'LONG' else -1)
    assert cashflow == -49
    assert h.state.realized_gross_pnl == cashflow
    assert h.state.realized_net_pnl == cashflow


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_review_rejected_cancel_cannot_silently_stall_protection(side):
    h = Harness(side).arm()
    h.at_r(-2)
    cancel = h.action('CANCEL')
    before = len(h.state.actions)
    h.send(ActionReceipt, action_id=cancel.action_id, status='REJECTED')
    for _ in range(3):
        h.at_r(-2)
    new_controls = [a for a in h.state.actions[before:]
                    if a.kind in ('CANCEL', 'RECONCILE') and a.target_action_id == cancel.target_action_id]
    assert new_controls or h.state.faults, 'Known CANCEL failure must retry, reconcile, or escalate'
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions), 'No unconfirmed double exit'


def add_entry(h, quantity, price=100, fee=0, occurred_at=None):
    return h.send(EntryFill, fill_id='extra-entry-' + str(h.counter), entry_action_id=h.state.entry_action_id,
                  quantity=quantity, price=price, fee_usdt=fee,
                  occurred_at=h.now if occurred_at is None else occurred_at)


def resume(h):
    saved = checkpoint(h.seed, h.policy, tuple(h.journal))
    event = RecoveryRequired(event_id='review-recovery-' + str(h.counter),
                             position_id=h.state.position_id, received_at=h.now + 1)
    restored = restore_checkpoint(saved.model_dump_json(), recovery_event=event)
    h.state = restored.state
    h.journal = list(restored.journal)
    h.now = event.received_at
    return saved


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('ack_before_extra', [True, False])
def test_fixed_coverage_repaired_atomically_without_canceling_old_stop(side, ack_before_extra):
    h = zero_fee_open(side)
    old = h.action('ARM_STOP')
    if ack_before_extra:
        h.ack_stop(old)
    add_entry(h, 6)
    if not ack_before_extra:
        h.ack_stop(old)  # A delayed four-unit ACK still does not cover ten units.
    move = h.action('MOVE_STOP')
    assert move.reason_code == 'REPAIR_PROTECTION_COVERAGE'
    assert move.quantity == 10 and move.replaces_action_id == old.action_id
    assert move.stop_price == old.stop_price and move.position_quantity_version == 1
    assert h.state.protection_covered_quantity == 4 and h.state.protection_status == 'PENDING'
    assert h.state.protection_coverage_version == 0
    assert not any(a.kind == 'CANCEL' for a in h.state.actions)
    h.ack_stop(move)
    assert h.state.protection_status == 'ACTIVE' and h.state.protection_covered_quantity == 10
    assert h.state.protection_coverage_version == 1
    assert next(a for a in h.state.actions if a.action_id == old.action_id).status == 'CANCELED'


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_entry_growth_while_replacement_pending_requires_another_verified_capacity(side):
    h = zero_fee_open(side)
    h.ack_stop(h.action('ARM_STOP'))
    add_entry(h, 6)
    first = h.action('MOVE_STOP')
    add_entry(h, 2)
    assert len([a for a in h.state.actions if a.kind == 'MOVE_STOP']) == 1
    h.ack_stop(first)
    assert h.state.protection_status == 'PENDING' and h.state.protection_covered_quantity == 10
    second = h.action('MOVE_STOP')
    assert second.quantity == 12 and second.replaces_action_id == first.action_id
    assert second.position_quantity_version == 2
    h.ack_stop(second)
    assert h.state.protection_covered_quantity == 12 and h.state.protection_status == 'ACTIVE'
    assert h.state.frozen_initial_r == 5
    assert not any(a.kind in ('CANCEL', 'TP1', 'TP2') for a in h.state.actions)


def dynamic_harness(side):
    rules = Harness().rules.model_copy(update={
        'dynamic_full_position_stop': True, 'dynamic_stop_contract_id': 'synthetic-future-fill-contract/v1'})
    return Harness(side, q=4, rules=rules)


def dynamic_ack(h, **changes):
    from app.exits.models import StopCoverage
    stop = h.action('ARM_STOP')
    fields = dict(mode='dynamic_position', quantity=h.state.remaining_quantity,
                  quantity_version=h.state.position_quantity_version, evidence_id='synthetic-adapter-proof',
                  dynamic_contract_id='synthetic-future-fill-contract/v1')
    fields.update(changes)
    return h.send(ActionReceipt, action_id=stop.action_id, status='ACCEPTED',
                  reduce_only_verified=True, stop_price=stop.stop_price, coverage=StopCoverage(**fields))


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_dynamic_coverage_requires_explicit_capability_and_matching_ack(side):
    h = dynamic_harness(side)
    assert h.action('ARM_STOP').protection_mode == 'dynamic_position'
    dynamic_ack(h)
    add_entry(h, 6)
    assert h.state.protection_status == 'ACTIVE' and h.state.protection_covered_quantity == 10
    assert h.state.protection_coverage_version == 0 and h.state.position_quantity_version == 1
    assert not any(a.kind == 'MOVE_STOP' for a in h.state.actions)
    saved = resume(h)
    assert saved.state.protection_covered_quantity == 10
    assert h.state.phase == 'PROTECTION_REQUIRED'  # Restart still requires original-order reconciliation.


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('bad', ['legacy_boolean', 'wrong_contract', 'stale_version', 'unconfigured_dynamic', 'wrong_quantity'])
def test_dynamic_coverage_cannot_be_inferred_or_forged_by_legacy_boolean(side, bad):
    h = zero_fee_open(side) if bad == 'unconfigured_dynamic' else dynamic_harness(side)
    if bad == 'legacy_boolean':
        h.ack_stop(h.action('ARM_STOP'))
    elif bad == 'stale_version':
        add_entry(h, 6)
        dynamic_ack(h, quantity_version=0)
    elif bad == 'wrong_contract':
        dynamic_ack(h, dynamic_contract_id='wrong-contract')
    elif bad == 'wrong_quantity':
        dynamic_ack(h, quantity=1)
    else:
        dynamic_ack(h)
    assert 'STOP_COVERAGE_CONTRACT_INVALID' in h.state.faults
    assert h.state.protection_status != 'ACTIVE'
    assert h.state.protection_covered_quantity == 0


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('failure', ['no_atomic_replace', 'rejected', 'unknown'])
def test_coverage_repair_failure_never_cancels_prior_valid_protection(side, failure):
    rules = Harness().rules.model_copy(update={'atomic_stop_replace': failure != 'no_atomic_replace'})
    h = Harness(side, q=4, rules=rules)
    old = h.action('ARM_STOP')
    h.ack_stop(old)
    add_entry(h, 6)
    if failure != 'no_atomic_replace':
        move = h.action('MOVE_STOP')
        h.send(ActionReceipt, action_id=move.action_id,
               status='REJECTED' if failure == 'rejected' else 'UNKNOWN')
    assert h.state.protection_status != 'ACTIVE' and h.state.protection_covered_quantity == 4
    assert h.state.phase == 'PROTECTION_REQUIRED'
    assert not any(a.kind in ('CANCEL', 'CLOSE_ALL') for a in h.state.actions)
    if failure == 'unknown':
        for _ in range(3):
            h.tick(100)
        assert len([a for a in h.state.actions if a.kind == 'MOVE_STOP']) == 1
        assert any(a.kind == 'RECONCILE' and a.target_action_id == move.action_id for a in h.state.actions)
    else:
        assert h.state.faults


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_pending_coverage_and_duplicate_entry_survive_restart_without_extra_exit(side):
    h = zero_fee_open(side)
    h.ack_stop(h.action('ARM_STOP'))
    add_entry(h, 6)
    fill = h.journal[-1]
    h.send(EntryFill, **fill.model_dump(exclude={'event_id', 'position_id', 'received_at', 'kind'}))
    assert h.state.position_quantity_version == 1 and h.state.remaining_quantity == 10
    saved = resume(h)
    assert h.state.protection_covered_quantity == 4 and h.state.protection_status == 'PENDING'
    assert len([a for a in h.state.actions if a.kind == 'MOVE_STOP']) == 1
    assert all(a.kind == 'RECONCILE' for a in h.state.actions[len(saved.state.actions):])
    assert h.state.remaining_entry_cost == 1000


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('first_exit', [1, 2, 4])
@pytest.mark.parametrize('earlier_occurrence', [False, True])
@pytest.mark.parametrize('fee', [D(0), D('.013')])
def test_remaining_cost_and_final_cashflow_conservation_after_partial_or_full_exit(side, first_exit, earlier_occurrence, fee):
    h = zero_fee_open(side)
    h.send(ActionReceipt, action_id=h.action('ARM_STOP').action_id, status='REJECTED')
    price = D(90 if side == 'LONG' else 110)
    late_price = D(99 if side == 'LONG' else 101)
    confirmed_exit(h, h.action('CLOSE_ALL'), first_exit, price, fee)
    old_pnl = h.state.realized_gross_pnl
    frozen = h.state.frozen_initial_r
    add_entry(h, 1, late_price, fee, NOW - 1 if earlier_occurrence else h.now)
    assert h.state.realized_gross_pnl == old_pnl
    assert h.state.remaining_entry_cost == (4 - first_exit) * 100 + late_price
    assert h.state.actual_average_entry == (400 + late_price) / 5  # Historical VWAP is still historical.
    assert h.state.frozen_initial_r == frozen and h.state.frozen_r_anchor_entry == 100
    h.send(EntrySealed, entry_action_id=h.state.entry_action_id, total_filled_quantity=5)
    while h.state.remaining_quantity:
        action = h.action('CLOSE_ALL')
        confirmed_exit(h, action, action.quantity - action.filled_quantity, price, fee,
                       NOW + .01 if earlier_occurrence else None)
    state = h.state
    entries = [f for f in state.fill_facts if f.action_id == state.entry_action_id]
    exits = [f for f in state.fill_facts if f.action_id != state.entry_action_id]
    # Independent oracle: no historical/remaining average from the engine is used.
    cashflow = (sum(f.quantity * f.price for f in exits) - sum(f.quantity * f.price for f in entries)) * (
        1 if side == 'LONG' else -1)
    fees = sum(f.fee_usdt for f in state.fill_facts)
    assert cashflow == -49 and state.realized_gross_pnl == cashflow
    assert state.realized_net_pnl == cashflow - fees
    assert state.remaining_entry_cost == 0 and state.remaining_average_entry is None
    assert state.phase == 'CLOSED'
    assert checkpoint(h.seed, h.policy, tuple(h.journal)).state == state
    resume(h)
    assert h.state.realized_gross_pnl == cashflow and h.state.remaining_entry_cost == 0


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('kind', ['CANCEL', 'RECONCILE'])
@pytest.mark.parametrize('limit', [1, 2, 3])
@pytest.mark.parametrize('failure', ['REJECTED', 'CANCELED'])
def test_known_control_failure_retries_are_bounded_by_target_and_survive_recovery(side, kind, limit, failure):
    h = Harness(side, policy=ExitPolicy(max_control_attempts=limit)).arm()
    h.at_r(-2)
    target = h.action('ARM_STOP').action_id
    if kind == 'RECONCILE':
        h.send(ActionReceipt, action_id=target, status='UNKNOWN')
    for _ in range(limit):
        action = next(a for a in reversed(h.state.actions) if a.kind == kind and a.target_action_id == target)
        h.send(ActionReceipt, action_id=action.action_id, status=failure)
        # A redelivered terminal failure cannot create another retry.
        duplicate = apply_event(h.seed, h.policy, h.state, h.journal[-1])
        assert not duplicate.actions and duplicate.state == h.state
    for _ in range(5):
        h.at_r(-2)
    attempts = [a for a in h.state.actions if a.kind == kind and a.target_action_id == target]
    assert len(attempts) == limit and len({a.action_id for a in attempts}) == limit
    assert kind + '_CONTROL_ATTEMPTS_EXHAUSTED' in h.state.faults
    assert h.state.phase == 'PROTECTION_REQUIRED' and h.state.remaining_quantity == 10
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)
    resume(h)
    assert len([a for a in h.state.actions if a.kind == kind and a.target_action_id == target]) == limit


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_unknown_cancel_queries_original_target_never_reissues_exit(side):
    h = Harness(side).arm()
    h.at_r(-2)
    cancel = h.action('CANCEL')
    h.send(ActionReceipt, action_id=cancel.action_id, status='UNKNOWN')
    query = h.action('RECONCILE')
    assert query.target_action_id == cancel.target_action_id
    for _ in range(3):
        h.at_r(-2)
    assert len([a for a in h.state.actions if a.kind == 'CANCEL']) == 1
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)
    h.send(ActionReceipt, action_id=cancel.target_action_id, status='CANCELED', cumulative_filled_quantity=0)
    assert h.action('CLOSE_ALL').quantity == 10
    assert all(next(a for a in h.state.actions if a.action_id == key).target_confirmed
               for key in (cancel.action_id, query.action_id))


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_unknown_exit_remains_same_intent_while_reconciliation_fails(side):
    h = Harness(side).arm()
    h.at_r(1)
    tp = h.action('TP1')
    h.send(ActionReceipt, action_id=tp.action_id, status='UNKNOWN')
    for _ in range(h.policy.max_control_attempts):
        query = next(a for a in reversed(h.state.actions) if a.kind == 'RECONCILE' and a.target_action_id == tp.action_id)
        h.send(ActionReceipt, action_id=query.action_id, status='REJECTED')
    assert len([a for a in h.state.actions if a.kind == 'TP1']) == 1
    assert h.state.remaining_quantity == 10 and not h.state.tp1_complete
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('receipt', ['UNKNOWN', 'FILLED'])
def test_uncertain_or_payloadless_query_result_escalates_without_inventing_target_state(side, receipt):
    h = Harness(side).arm()
    stop = h.action('ARM_STOP')
    h.send(ActionReceipt, action_id=stop.action_id, status='UNKNOWN')
    query = h.action('RECONCILE')
    h.send(ActionReceipt, action_id=query.action_id, status=receipt)
    assert h.state.faults and h.state.phase == 'PROTECTION_REQUIRED'
    assert h.state.protection_status == 'UNKNOWN'
    assert len([a for a in h.state.actions if a.kind == 'ARM_STOP']) == 1


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_successful_target_reconciliation_does_not_block_a_later_unknown_episode(side):
    h = Harness(side).arm()
    stop = h.action('ARM_STOP')
    h.send(ActionReceipt, action_id=stop.action_id, status='UNKNOWN')
    first_query = h.action('RECONCILE')
    h.ack_stop(stop)
    assert next(a for a in h.state.actions if a.action_id == first_query.action_id).target_confirmed
    h.send(ActionReceipt, action_id=stop.action_id, status='UNKNOWN')
    second_query = h.action('RECONCILE')
    assert second_query.action_id != first_query.action_id
    assert second_query.target_action_id == stop.action_id
    assert len([a for a in h.state.actions if a.kind == 'ARM_STOP']) == 1


@pytest.mark.parametrize('field,value', [
    ('remaining_entry_cost', D(1)), ('remaining_average_entry', D(1)), ('exit_notional', D(1)),
    ('position_quantity_version', 99), ('protection_covered_quantity', D(99)), ('protection_coverage_version', 99)])
def test_cost_and_coverage_state_forgery_is_rejected(field, value):
    h = Harness().arm()
    bad = h.state.model_copy(update={field: value})
    event = MarketEvent(event_id='forged-review', position_id=h.state.position_id, received_at=h.now + 1,
                        observed_at=h.now + 1, bid=100, ask=101, confirmed=True)
    with pytest.raises(ExitContractError):
        apply_event(h.seed, h.policy, bad, event)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('mode', ['entry_price', 'cost_covered'])
def test_break_even_uses_remaining_cost_not_closed_historical_inventory(side, mode):
    h = zero_fee_open(side)
    h.send(ActionReceipt, action_id=h.action('ARM_STOP').action_id, status='REJECTED')
    confirmed_exit(h, h.action('CLOSE_ALL'), 4, 90 if side == 'LONG' else 110, D('.02'))
    price = D(99 if side == 'LONG' else 101)
    add_entry(h, 1, price, D('.03'))
    policy = h.policy.model_copy(update={'break_even_mode': mode})
    if mode == 'entry_price':
        expected = price
    else:
        f, s = policy.expected_exit_fee_rate, policy.expected_exit_slippage_bps / 10000
        expected = (price + D('.05')) / ((1-s)*(1-f)) if side == 'LONG' else (price-D('.05')) / ((1+s)*(1+f))
    assert break_even_price(h.state, policy) == expected
    assert h.state.remaining_average_entry == price and h.state.actual_average_entry != price


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_legacy_checkpoint_is_rejected_not_silently_rebased_or_resent(side):
    h = Harness(side).arm()
    saved = json.loads(checkpoint(h.seed, h.policy, tuple(h.journal)).model_dump_json())
    saved['schema_version'] = 'exit-checkpoint/v1'
    saved['state']['schema_version'] = 'position-exit/v1'
    with pytest.raises(ValidationError):
        restore_checkpoint(json.dumps(saved), recovery_event=RecoveryRequired(event_id='old-format',
            position_id=h.state.position_id, received_at=h.now+1))


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('kind', ['CANCEL', 'RECONCILE'])
def test_control_ack_stays_in_flight_until_original_target_fact(side, kind):
    h = Harness(side).arm()
    h.at_r(-2)
    stop = h.action('ARM_STOP')
    if kind == 'RECONCILE':
        h.send(ActionReceipt, action_id=stop.action_id, status='UNKNOWN')
    control = h.action(kind)
    h.send(ActionReceipt, action_id=control.action_id, status='ACCEPTED')
    for _ in range(3):
        h.at_r(-2)
    assert len([a for a in h.state.actions if a.kind == kind and a.target_action_id == stop.action_id]) == 1
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)
    h.send(ActionReceipt, action_id=stop.action_id, status='CANCELED', cumulative_filled_quantity=0)
    assert h.action('CLOSE_ALL').quantity == 10


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_terminal_native_stop_never_gets_another_cancel_after_target_confirmation(side):
    h = Harness(side).arm()
    h.at_r(-2)
    cancel = h.action('CANCEL')
    confirmed_exit(h, h.action('ARM_STOP'), 10, 90 if side == 'LONG' else 110)
    for _ in range(3):
        h.at_r(-2)
    assert h.state.phase == 'CLOSED' and h.state.remaining_quantity == 0
    assert len([a for a in h.state.actions if a.kind == 'CANCEL']) == 1
    assert next(a for a in h.state.actions if a.action_id == cancel.action_id).target_confirmed
