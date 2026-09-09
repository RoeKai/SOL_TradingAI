"""Receipt-order adversaries: synthetic facts, no network or execution adapter."""

from decimal import Decimal as D
from itertools import permutations
import json

import pytest

from app.exits.engine import apply_event, digest
from app.exits.models import ActionReceipt, ExitFill, ExitContractError, ProtectionLost, RecoveryRequired
from app.exits.persistence import checkpoint, restore_checkpoint
from test_stage06_r1_receipt_ordering import RecordedProbe


def action(h, original):
    return next(a for a in h.state.actions if a.action_id == original.action_id)


def queries(h, original):
    return [a for a in h.state.actions if a.kind == 'RECONCILE' and a.target_action_id == original.action_id]


def target_ack(h, original, quantity, status='ACCEPTED'):
    return h.send(ActionReceipt, action_id=original.action_id, status=status,
                  cumulative_filled_quantity=quantity, reduce_only_verified=True,
                  stop_price=original.stop_price, covers_remaining=True)


def replace_ack(h, new, retired_quantity=1):
    return h.send(ActionReceipt, action_id=new.action_id, status='ACCEPTED',
                  reduce_only_verified=True, stop_price=new.stop_price, covers_remaining=True,
                  old_stop_retired=True, retired_stop_cumulative_filled=retired_quantity)


def fill(h, original, quantity, fill_id, price=None):
    # The business timestamp predates receipt delivery. It is not an ACK timestamp.
    return h.send(ExitFill, action_id=original.action_id, fill_id=fill_id, quantity=quantity,
                  price=price or (95 if h.state.side == 'LONG' else 105), fee_usdt='.02',
                  occurred_at=h.seed.first_fill.occurred_at + 1)


def assert_replay(h):
    saved = checkpoint(h.seed, h.policy, tuple(h.journal))
    assert saved.state == h.state
    # Duplicate delivery also runs the reducer's persisted-state validation.
    result = apply_event(h.seed, h.policy, h.state, h.journal[-1])
    assert result.state == h.state and result.actions == ()
    return saved


def redeliver(h, event):
    return h.send(type(event), **event.model_dump(exclude={'event_id', 'position_id', 'received_at', 'kind'}))


def replacing(side):
    h = RecordedProbe(side).arm()
    old = h.latest('ARM_STOP')
    h.add(6)
    h.seal()
    return h, old, h.latest('MOVE_STOP')


def taking_profit(side):
    h = RecordedProbe(side, quantity='10').arm().seal()
    h.quote('106' if side == 'LONG' else '94')
    return h, h.latest('TP1')


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('ordering', list(permutations('NTFA')))
def test_retired_stop_terminal_fill_ack_permutation_conserves_identity_and_cashflow(side, ordering):
    """N=new protection, T=old terminal, F=old fill, A=stale old ACK."""
    h, old, new = replacing(side)
    seen = set()
    high_water = D(0)
    for item in ordering:
        if item == 'N': replace_ack(h, new)
        elif item == 'T': target_ack(h, old, 1, 'CANCELED')
        elif item == 'F': fill(h, old, 1, 'late-retired-fill')
        else: target_ack(h, old, 0)
        seen.add(item)
        retired = action(h, old)
        assert retired.acknowledged_quantity >= high_water
        high_water = retired.acknowledged_quantity
        assert not h.state.faults
        assert h.state.remaining_quantity == (9 if 'F' in seen else 10)
        assert h.state.realized_gross_pnl == (-5 if 'F' in seen else 0)
        assert h.state.realized_net_pnl == (D('-5.02') if 'F' in seen else 0)
        if 'N' in seen:
            assert h.state.protection_action_id == new.action_id
        if seen & {'N', 'T'}:
            assert retired.lifecycle_terminal and retired.terminal_status == 'CANCELED'
            assert retired.terminal_quantity == retired.acknowledged_quantity == 1
            assert retired.status == ('CANCELED' if 'F' in seen else 'SETTLING')
            assert retired.settlement_complete == ('F' in seen)
        assert h.state.current_stop == h.state.original_stop and h.state.frozen_initial_r == 5
        assert not any(a.kind in ('TP1', 'TP2', 'CLOSE_ALL') for a in h.result.actions)
        assert len([a for a in h.state.actions if a.kind == 'MOVE_STOP']) == 1
        assert_replay(h)
    # Exact duplicates and new delivery IDs must not rebook quantities or fees.
    delivered = list(h.journal[-4:])
    for event in delivered:
        redeliver(h, event)
        assert h.state.protection_action_id == new.action_id
        assert h.state.remaining_quantity == 9 and h.state.realized_net_pnl == D('-5.02')
        assert len(h.state.fill_facts) == 3  # Two opening facts, one real exit.
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('late_kind', ['ACK', 'UNKNOWN', 'LOSS_UNKNOWN', 'LOSS_CANCELED'])
def test_terminal_settling_survives_late_nonterminal_events_and_restart(side, restart, late_kind):
    h, old, new = replacing(side)
    replace_ack(h, new)
    if restart:
        h.restart()
        assert old.action_id in h.state.recovery_pending_action_ids
        replace_ack(h, new)  # Fresh current protection confirmation, not old fill details.
    original_queries = queries(h, old)
    assert original_queries and not any(a.target_confirmed for a in original_queries)
    for _ in range(2):
        if late_kind == 'ACK': target_ack(h, old, 0)
        elif late_kind == 'UNKNOWN': target_ack(h, old, 0, 'UNKNOWN')
        else:
            h.send(ProtectionLost, action_id=old.action_id,
                   status='UNKNOWN' if late_kind == 'LOSS_UNKNOWN' else 'CANCELED',
                   cumulative_filled_quantity=None if late_kind == 'LOSS_UNKNOWN' else 1)
        assert h.state.protection_action_id == new.action_id
        assert action(h, old).status == 'SETTLING' and action(h, old).lifecycle_terminal
        assert action(h, old).acknowledged_quantity == 1 and not action(h, old).settlement_complete
        assert not any(a.target_confirmed for a in queries(h, old))
        assert h.state.remaining_quantity == 10 and h.state.realized_net_pnl == 0
        if restart: assert old.action_id in h.state.recovery_pending_action_ids
        assert not h.state.faults and not h.result.actions
    # Partial details do not settle the terminal total or release the restart fence.
    fill(h, old, '.4', 'old-detail-a')
    assert action(h, old).status == 'SETTLING' and h.state.remaining_quantity == D('9.6')
    assert not any(a.target_confirmed for a in queries(h, old))
    if restart: assert old.action_id in h.state.recovery_pending_action_ids
    target_ack(h, old, 0)
    fill(h, old, '.6', 'old-detail-b')
    assert action(h, old).status == 'CANCELED' and action(h, old).settlement_complete
    assert action(h, old).filled_quantity == action(h, old).acknowledged_quantity == 1
    assert all(a.target_confirmed for a in queries(h, old))
    assert old.action_id not in h.state.recovery_pending_action_ids
    assert h.state.remaining_quantity == 9 and h.state.realized_net_pnl == D('-5.04')
    assert h.state.protection_action_id == new.action_id
    assert not any(a.kind == 'CANCEL' and a.target_action_id == old.action_id for a in h.state.actions)
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)  # Current stop still exists.
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('ordering', list(permutations('KTFA')))
def test_tp_known_total_terminal_fill_and_stale_ack_all_orderings(side, ordering):
    """K=known cumulative one, T=canceled one, F=one fill, A=old zero ACK."""
    h, tp = taking_profit(side)
    seen = set()
    high_water = D(0)
    for item in ordering:
        if item == 'K': target_ack(h, tp, 1)
        elif item == 'T': target_ack(h, tp, 1, 'CANCELED')
        elif item == 'F': fill(h, tp, 1, 'tp-detail', price=105 if side == 'LONG' else 95)
        else: target_ack(h, tp, 0)
        seen.add(item)
        current = action(h, tp)
        assert current.acknowledged_quantity >= high_water
        high_water = current.acknowledged_quantity
        assert h.state.remaining_quantity == (9 if 'F' in seen else 10)
        assert h.state.tp1_filled == (1 if 'F' in seen else 0)
        assert not h.state.tp1_complete and h.state.current_stop == h.state.original_stop
        if seen & {'K', 'T'} and 'F' not in seen:
            assert current.status == 'SETTLING' and not current.settlement_complete
            assert not any(a.target_confirmed for a in queries(h, tp))
        if 'T' in seen:
            assert current.lifecycle_terminal and current.terminal_status == 'CANCELED'
            assert current.terminal_quantity == current.acknowledged_quantity == 1
        retries = [a for a in h.state.actions if a.kind == 'TP1' and a.action_id != tp.action_id]
        assert len(retries) == int({'T', 'F'} <= seen)
        assert all(a.quantity == 2 for a in retries)  # Only confirmed remainder; never another three.
        assert not h.state.faults
        assert_replay(h)
    assert h.state.realized_gross_pnl == 5 and h.state.realized_net_pnl == D('4.98')
    assert action(h, tp).settlement_complete and action(h, tp).status == 'CANCELED'
    before = len(h.state.actions)
    for event in list(h.journal[-4:]): redeliver(h, event)
    assert len(h.state.actions) == before and h.state.remaining_quantity == 9
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('late_total', [0, 1, 2])
@pytest.mark.parametrize('late_status', ['ACCEPTED', 'UNKNOWN'])
def test_high_water_and_recovery_only_release_after_all_known_details(side, restart, late_total, late_status):
    h, tp = taking_profit(side)
    target_ack(h, tp, 2)
    if restart:
        h.restart()
        h.ack(h.latest('ARM_STOP'))
        assert h.state.recovery_pending_action_ids == (tp.action_id,)
    target_ack(h, tp, late_total, late_status)
    assert action(h, tp).acknowledged_quantity == 2 and not action(h, tp).settlement_complete
    assert action(h, tp).status in ('SETTLING', 'UNKNOWN')
    assert h.state.remaining_quantity == 10 and not any(a.target_confirmed for a in queries(h, tp))
    fill(h, tp, 1, 'tp-first', price=105 if side == 'LONG' else 95)
    target_ack(h, tp, 0)
    assert action(h, tp).acknowledged_quantity == 2 and action(h, tp).filled_quantity == 1
    assert action(h, tp).status == 'SETTLING' and not any(a.target_confirmed for a in queries(h, tp))
    if restart: assert tp.action_id in h.state.recovery_pending_action_ids
    fill(h, tp, 1, 'tp-second', price=105 if side == 'LONG' else 95)
    assert action(h, tp).settlement_complete and action(h, tp).status == 'ACCEPTED'
    assert all(a.target_confirmed for a in queries(h, tp))
    assert tp.action_id not in h.state.recovery_pending_action_ids
    assert h.state.remaining_quantity == 8 and h.state.tp1_filled == 2 and not h.state.tp1_complete
    assert h.state.current_stop == h.state.original_stop and h.state.realized_net_pnl == D('9.96')
    assert not h.state.faults and len([a for a in h.state.actions if a.kind == 'TP1']) == 1
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('terminal', ['CANCELED', 'REJECTED'])
def test_terminal_total_behind_known_execution_is_quarantined_not_settled(side, terminal):
    h, tp = taking_profit(side)
    target_ack(h, tp, 2)
    target_ack(h, tp, 1, terminal)
    assert 'TERMINAL_TOTAL_BEHIND_KNOWN_CUMULATIVE' in h.state.faults
    assert action(h, tp).terminal_status is None and action(h, tp).acknowledged_quantity == 2
    assert action(h, tp).status == 'SETTLING' and not any(a.target_confirmed for a in queries(h, tp))
    assert h.state.remaining_quantity == 10 and h.state.realized_net_pnl == 0
    # A fault never discards the later real fill facts or fabricates the omitted one.
    fill(h, tp, 2, 'details-despite-fault', price=105 if side == 'LONG' else 95)
    assert h.state.remaining_quantity == 8 and h.state.realized_net_pnl == D('9.98')
    assert h.state.phase == 'PROTECTION_REQUIRED' and h.state.faults
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('bad_kind', ['ACK_HIGH', 'UNKNOWN_HIGH', 'TERMINAL_HIGH', 'TERMINAL_DIFFERENT'])
def test_terminal_latch_rejects_conflicting_receipts_but_books_true_late_fill(side, bad_kind):
    h, old, new = replacing(side)
    replace_ack(h, new)
    status = {'ACK_HIGH': 'ACCEPTED', 'UNKNOWN_HIGH': 'UNKNOWN',
              'TERMINAL_HIGH': 'CANCELED', 'TERMINAL_DIFFERENT': 'FILLED'}[bad_kind]
    target_ack(h, old, 1 if bad_kind == 'TERMINAL_DIFFERENT' else 2, status)
    assert h.state.faults and h.state.phase == 'PROTECTION_REQUIRED'
    assert action(h, old).terminal_status == 'CANCELED' and action(h, old).terminal_quantity == 1
    assert action(h, old).status == 'SETTLING' and h.state.protection_action_id == new.action_id
    assert h.state.remaining_quantity == 10 and not any(a.target_confirmed for a in queries(h, old))
    fill(h, old, 1, 'real-not-the-contradictory-ack')
    assert h.state.remaining_quantity == 9 and h.state.realized_net_pnl == D('-5.02')
    assert action(h, old).status == 'CANCELED' and action(h, old).settlement_complete
    assert h.state.protection_action_id == new.action_id and h.state.faults
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_replacement_terminal_proof_cannot_erase_old_stop_high_water(side):
    h, old, new = replacing(side)
    target_ack(h, old, 2)
    replace_ack(h, new, retired_quantity=1)
    assert 'RETIRED_STOP_TOTAL_BEHIND_KNOWN_CUMULATIVE' in h.state.faults
    assert action(h, old).acknowledged_quantity == 2 and action(h, old).terminal_status is None
    assert action(h, old).status == 'SETTLING' and h.state.protection_action_id != new.action_id
    assert not action(h, new).stop_confirmed and h.state.remaining_quantity == 10
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('control_status', ['ACCEPTED', 'FILLED'])
def test_control_ack_cannot_substitute_for_missing_target_fill_details(side, control_status):
    h, tp = taking_profit(side)
    target_ack(h, tp, 1)
    h.restart()
    query = queries(h, tp)[-1]
    target_ack(h, query, 0, control_status)
    target_ack(h, tp, 0)
    assert not action(h, query).target_confirmed
    assert action(h, tp).acknowledged_quantity == 1 and action(h, tp).status == 'SETTLING'
    assert tp.action_id in h.state.recovery_pending_action_ids
    assert h.state.remaining_quantity == 10 and h.state.realized_net_pnl == 0
    assert len([a for a in h.state.actions if a.kind == 'TP1']) == 1
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_actual_fill_over_terminal_total_is_booked_once_then_quarantined(side):
    h, old, new = replacing(side)
    replace_ack(h, new)
    fill(h, old, 1, 'expected-one')
    fill(h, old, '.5', 'unexpected-but-confirmed')
    extra = h.journal[-1]
    assert 'LATE_FILL_CONTRADICTS_TERMINAL_TOTAL' in h.state.faults
    assert action(h, old).terminal_quantity == 1 and action(h, old).terminal_status == 'CANCELED'
    assert action(h, old).acknowledged_quantity == action(h, old).filled_quantity == D('1.5')
    assert not action(h, old).settlement_complete and h.state.remaining_quantity == D('8.5')
    assert h.state.realized_net_pnl == D('-7.54') and h.state.protection_action_id == new.action_id
    redeliver(h, extra)
    assert h.state.remaining_quantity == D('8.5') and h.state.realized_net_pnl == D('-7.54')
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('field,value', [('status', 'ACCEPTED'), ('acknowledged_quantity', '0'),
                                        ('terminal_status', None), ('terminal_quantity', None)])
def test_forged_terminal_or_high_water_checkpoint_cannot_resume(side, field, value):
    h, old, new = replacing(side)
    replace_ack(h, new)
    saved = assert_replay(h)
    bad = json.loads(saved.model_dump_json())
    next(a for a in bad['state']['actions'] if a['action_id'] == old.action_id)[field] = value
    # Even recomputing the untrusted state checksum cannot bypass full journal replay.
    from app.exits.models import ExitState
    bad['state_digest'] = digest(ExitState.model_validate(bad['state']))
    recovery = RecoveryRequired(event_id='reject-forged-restore', position_id=h.state.position_id,
                                received_at=h.now + 1)
    with pytest.raises(ExitContractError):
        restore_checkpoint(json.dumps(bad), recovery_event=recovery)
    assert h.state.protection_action_id == new.action_id and h.state.remaining_quantity == 10


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_second_restart_keeps_missing_detail_debt_without_reissuing_exit(side):
    h, old, new = replacing(side)
    replace_ack(h, new)
    original_orders = [a.action_id for a in h.state.actions if a.kind not in ('CANCEL', 'RECONCILE')]
    for _ in range(2):
        h.restart()
        target_ack(h, old, 0)
        replace_ack(h, new)
        assert h.state.recovery_pending_action_ids == (old.action_id,)
        assert action(h, old).acknowledged_quantity == 1 and action(h, old).status == 'SETTLING'
        assert not any(a.target_confirmed for a in queries(h, old))
        assert [a.action_id for a in h.state.actions if a.kind not in ('CANCEL', 'RECONCILE')] == original_orders
    fill(h, old, 1, 'after-two-restarts')
    assert not h.state.recovery_pending_action_ids and all(a.target_confirmed for a in queries(h, old))
    assert h.state.remaining_quantity == 9 and h.state.realized_net_pnl == D('-5.02')
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('restart', [False, True])
def test_late_retired_fill_then_remaining_close_matches_all_confirmed_cashflows(side, restart):
    h, old, new = replacing(side)
    replace_ack(h, new)
    if restart:
        h.restart()
        replace_ack(h, new)
    target_ack(h, old, 0)
    fill(h, old, 1, 'retired-stop-fill')
    h.quote('90' if side == 'LONG' else '110')
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)
    target_ack(h, new, 0, 'CANCELED')
    close = h.latest('CLOSE_ALL')
    assert close.quantity == h.state.remaining_quantity == 9
    fill(h, close, 9, 'final-remainder', price=90 if side == 'LONG' else 110)
    entries = [f for f in h.state.fill_facts if f.action_id == h.state.entry_action_id]
    exits = [f for f in h.state.fill_facts if f.action_id != h.state.entry_action_id]
    gross = (1 if side == 'LONG' else -1) * (sum(f.quantity * f.price for f in exits)
                                            - sum(f.quantity * f.price for f in entries))
    fees = sum(f.fee_usdt for f in h.state.fill_facts)
    assert h.state.phase == 'CLOSED' and h.state.remaining_quantity == h.state.remaining_entry_cost == 0
    assert h.state.realized_gross_pnl == gross == -95
    assert h.state.realized_net_pnl == gross - fees == D('-95.04')
    old_order_ids = tuple(a.action_id for a in h.state.actions)
    h.restart()
    assert tuple(a.action_id for a in h.state.actions) == old_order_ids
    assert h.state.phase == 'CLOSED' and h.state.realized_net_pnl == D('-95.04')
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_repeated_replacement_proof_cannot_overwrite_latched_retired_total(side):
    h, old, new = replacing(side)
    replace_ack(h, new)
    replace_ack(h, new, retired_quantity=2)
    assert 'CONFLICTING_RETIRED_STOP_TERMINAL_RECEIPT' in h.state.faults
    assert action(h, old).terminal_quantity == action(h, old).acknowledged_quantity == 1
    assert action(h, old).terminal_status == 'CANCELED' and action(h, old).status == 'SETTLING'
    assert h.state.protection_action_id == new.action_id and h.state.remaining_quantity == 10
    assert_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_checkpoint_cannot_erase_receipt_only_high_water_even_with_rehashed_state(side):
    h, tp = taking_profit(side)
    target_ack(h, tp, 1)
    saved = assert_replay(h)
    bad = json.loads(saved.model_dump_json())
    stale = next(a for a in bad['state']['actions'] if a['action_id'] == tp.action_id)
    stale.update(acknowledged_quantity='0', status='ACCEPTED')
    from app.exits.models import ExitState
    bad['state_digest'] = digest(ExitState.model_validate(bad['state']))
    recovery = RecoveryRequired(event_id='reject-erased-receipt', position_id=h.state.position_id,
                                received_at=h.now + 1)
    with pytest.raises(ExitContractError):
        restore_checkpoint(json.dumps(bad), recovery_event=recovery)
    assert action(h, tp).acknowledged_quantity == 1 and h.state.remaining_quantity == 10
