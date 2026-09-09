"""All protection-loss inputs use the same receipt evidence contract; no I/O."""

from decimal import Decimal as D
from itertools import permutations
import json

import pytest

from app.exits.engine import apply_event, digest
from app.exits.models import (ActionReceipt, ExitFill, ExitContractError, ExitState,
                              MarketEvent, ProtectionLost, RecoveryRequired)
from app.exits.persistence import checkpoint, restore_checkpoint
from test_stage06_r2_protection_lost import Probe


class RecordedProbe(Probe):
    def __init__(self, side):
        self.journal = []
        super().__init__(side)

    def send(self, cls, **values):
        self.n += 1
        self.now += 1.0
        event = cls(event_id=f'r3-event-{self.n}', position_id=self.state.position_id,
                    received_at=self.now, **values)
        self.result = apply_event(self.seed, self.policy, self.state, event)
        self.state = self.result.state
        self.journal.append(event)
        return self.result

    def latest(self, kind):
        return next(a for a in reversed(self.state.actions) if a.kind == kind)

    def restart(self):
        saved = check_replay(self)
        self.n += 1
        self.now += 1.0
        event = RecoveryRequired(event_id=f'r3-restart-{self.n}', position_id=self.state.position_id,
                                 received_at=self.now)
        restored = restore_checkpoint(saved.model_dump_json(), recovery_event=event)
        self.state = restored.state
        self.journal = list(restored.journal)


def check_replay(h):
    saved = checkpoint(h.seed, h.policy, tuple(h.journal))
    assert saved.state == h.state
    duplicate = apply_event(h.seed, h.policy, h.state, h.journal[-1])
    assert duplicate.state == h.state and duplicate.actions == ()
    return saved


def redeliver(h, event):
    return h.send(type(event), **event.model_dump(exclude={'event_id', 'received_at', 'position_id', 'kind'}))


def loss(h, quantity, status='UNKNOWN', target=None):
    return h.send(ProtectionLost, action_id=target or h.stop_id, status=status,
                  cumulative_filled_quantity=quantity)


def receipt(h, quantity, status='ACCEPTED', target=None):
    return h.send(ActionReceipt, action_id=target or h.stop_id, status=status,
                  cumulative_filled_quantity=quantity, reduce_only_verified=True,
                  stop_price=h.stop().stop_price, covers_remaining=True)


def confirmed_fill(h, quantity, fill_id, target=None, price=None):
    return h.send(ExitFill, action_id=target or h.stop_id, fill_id=fill_id, quantity=quantity,
                  price=price or (95 if h.state.side == 'LONG' else 105), fee_usdt='.02',
                  occurred_at=h.seed.first_fill.occurred_at + 1)


def queries(h, target=None):
    return [a for a in h.state.actions if a.kind == 'RECONCILE' and a.target_action_id == (target or h.stop_id)]


def assert_waiting(h, known, remaining=10):
    assert h.stop().known_filled_quantity == h.stop().acknowledged_quantity == D(known)
    assert h.stop().status in ('UNKNOWN', 'SETTLING')
    assert h.state.remaining_quantity == D(remaining)
    assert queries(h) and not any(a.target_confirmed for a in queries(h))
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('status', ['UNKNOWN', 'CANCELED', 'FAILED'])
@pytest.mark.parametrize('prior', [0, 2])
def test_no_quantity_is_unknown_not_an_implicit_terminal_zero(side, status, prior):
    h = RecordedProbe(side)
    if prior: receipt(h, prior, 'UNKNOWN')
    loss(h, None, status)
    assert h.journal[-1].cumulative_filled_quantity is None
    assert_waiting(h, prior)
    assert h.stop().terminal_status is None and h.stop().terminal_quantity is None
    assert not h.state.faults and h.state.realized_net_pnl == 0
    assert len(h.state.fill_facts) == 1
    assert h.state.fill_facts[0].action_id == h.state.entry_action_id and h.state.fill_facts[0].quantity == 10
    check_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('status', ['UNKNOWN', 'CANCELED', 'FAILED'])
def test_explicit_zero_differs_from_absent_final_quantity(side, status):
    missing, zero = RecordedProbe(side), RecordedProbe(side)
    loss(missing, None, status)
    loss(zero, 0, status)
    assert missing.journal[-1].model_dump()['cumulative_filled_quantity'] is None
    assert zero.journal[-1].cumulative_filled_quantity == 0
    assert digest(missing.journal[-1]) != digest(zero.journal[-1])
    assert_waiting(missing, 0)
    if status == 'UNKNOWN':
        assert_waiting(zero, 0)
        assert zero.stop().terminal_status is None
    else:
        assert zero.stop().terminal_quantity == 0 and zero.stop().terminal_status == 'CANCELED'
        assert zero.latest('CLOSE_ALL').quantity == 10  # Only an explicit consistent final zero permits this.
    assert zero.state.remaining_quantity == missing.state.remaining_quantity == 10
    assert zero.state.realized_net_pnl == missing.state.realized_net_pnl == 0
    check_replay(missing)
    check_replay(zero)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('terminal_source', ['receipt', 'loss_canceled', 'loss_failed'])
def test_conflicting_zero_stays_quarantined_and_later_details_still_book(side, restart, terminal_source):
    h = RecordedProbe(side)
    loss(h, 2)
    if restart: h.restart()
    receipt(h, 0)
    assert_waiting(h, 2)
    if restart: assert h.stop_id in h.state.recovery_pending_action_ids
    if terminal_source == 'receipt': receipt(h, 0, 'CANCELED')
    else: loss(h, 0, 'CANCELED' if terminal_source == 'loss_canceled' else 'FAILED')
    assert 'TERMINAL_TOTAL_BEHIND_KNOWN_CUMULATIVE' in h.state.faults
    assert h.stop().terminal_status is None and h.state.phase == 'PROTECTION_REQUIRED'
    assert_waiting(h, 2)
    confirmed_fill(h, 1, 'late-one')
    assert_waiting(h, 2, 9)
    confirmed_fill(h, 1, 'late-two')
    last = h.journal[-1]
    assert h.stop().filled_quantity == h.stop().known_filled_quantity == 2
    assert h.state.remaining_quantity == 8 and h.state.realized_gross_pnl == -10
    assert h.state.realized_net_pnl == D('-10.04') and h.state.exit_fees == D('.04')
    assert h.state.faults and not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)
    if restart: assert h.stop_id in h.state.recovery_pending_action_ids
    redeliver(h, last)
    assert h.state.remaining_quantity == 8 and h.state.realized_net_pnl == D('-10.04')
    assert len(h.state.fill_facts) == 3
    check_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('terminal_source', ['receipt', 'loss_canceled', 'loss_failed'])
@pytest.mark.parametrize('ordering', list(permutations('TAB')))
def test_known_two_terminal_and_details_orderings_close_only_confirmed_eight(side, terminal_source, ordering):
    h = RecordedProbe(side)
    loss(h, 2)
    receipt(h, 0)
    assert_waiting(h, 2)
    seen = set()
    for step in ordering:
        if step == 'T':
            if terminal_source == 'receipt': receipt(h, 2, 'CANCELED')
            else: loss(h, 2, 'CANCELED' if terminal_source == 'loss_canceled' else 'FAILED')
        else: confirmed_fill(h, 1, 'detail-' + step)
        seen.add(step)
        actual = len(seen & {'A', 'B'})
        assert h.state.remaining_quantity == 10 - actual
        assert h.stop().acknowledged_quantity == 2 and h.stop().filled_quantity == actual
        assert h.state.realized_gross_pnl == -5 * actual
        assert h.state.exit_fees == D('.02') * actual and not h.state.faults
        if actual < 2:
            assert not h.stop().settlement_complete
            assert not any(a.target_confirmed for a in queries(h))
            assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)
        if 'T' in seen:
            assert h.stop().terminal_status == 'CANCELED' and h.stop().terminal_quantity == 2
            assert h.stop().status == ('CANCELED' if actual == 2 else 'SETTLING')
        closes = [a for a in h.state.actions if a.kind == 'CLOSE_ALL']
        assert len(closes) == int(len(seen) == 3)
        assert all(a.quantity == 8 for a in closes)
        check_replay(h)
    assert all(a.target_confirmed for a in queries(h))
    close = h.latest('CLOSE_ALL')
    confirmed_fill(h, 8, 'remaining-eight', target=close.action_id, price=90 if side == 'LONG' else 110)
    entries = [f for f in h.state.fill_facts if f.action_id == h.state.entry_action_id]
    exits = [f for f in h.state.fill_facts if f.action_id != h.state.entry_action_id]
    gross = (1 if side == 'LONG' else -1) * (sum(f.price * f.quantity for f in exits)
                                            - sum(f.price * f.quantity for f in entries))
    assert h.state.remaining_quantity == h.state.remaining_entry_cost == 0 and h.state.phase == 'CLOSED'
    assert h.state.realized_gross_pnl == gross == -90
    assert h.state.realized_net_pnl == gross - sum(f.fee_usdt for f in h.state.fill_facts) == D('-90.06')
    before = tuple(a.action_id for a in h.state.actions)
    for event in list(h.journal[-4:]): redeliver(h, event)
    h.restart()
    assert tuple(a.action_id for a in h.state.actions) == before
    assert h.state.phase == 'CLOSED' and h.state.realized_net_pnl == D('-90.06')
    check_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('quantity', [None, 0, 1, 2, 3])
@pytest.mark.parametrize('settled', [False, True])
@pytest.mark.parametrize('status', ['UNKNOWN', 'CANCELED', 'FAILED'])
def test_retired_protection_loss_never_rebinds_identity_and_keeps_quantity_conflicts(side, quantity, settled, status):
    h = RecordedProbe(side)
    price = D(106 if side == 'LONG' else 94)
    h.send(MarketEvent, observed_at=h.now + 1, bid=price, ask=price + D('.01'), confirmed=True)
    tp = h.latest('TP1')
    confirmed_fill(h, 3, 'tp-one', target=tp.action_id, price=105 if side == 'LONG' else 95)
    replacement = h.latest('MOVE_STOP')
    h.send(ActionReceipt, action_id=replacement.action_id, status='ACCEPTED', reduce_only_verified=True,
           stop_price=replacement.stop_price, covers_remaining=True, old_stop_retired=True,
           retired_stop_cumulative_filled=2)
    if settled: confirmed_fill(h, 2, 'retired-real-fill')
    loss(h, quantity, status)
    assert h.state.protection_action_id == replacement.action_id
    assert h.stop().lifecycle_terminal and h.stop().terminal_status == 'CANCELED'
    assert h.stop().terminal_quantity == h.stop().known_filled_quantity == 2
    assert h.stop().status == ('CANCELED' if settled else 'SETTLING')
    assert h.state.remaining_quantity == (5 if settled else 7)
    conflict = quantity is not None and (quantity > 2 if status == 'UNKNOWN' else quantity != 2)
    if conflict:
        expected = 'NONTERMINAL_RECEIPT_EXCEEDS_TERMINAL_TOTAL' if status == 'UNKNOWN' else 'CONFLICTING_TERMINAL_RECEIPT'
        assert expected in h.state.faults
        assert any(not a.target_confirmed for a in queries(h))  # Even a retired/settled order is queried by original ID.
    else: assert not h.state.faults
    assert not any(a.kind in ('MOVE_STOP', 'TP1', 'TP2', 'CLOSE_ALL') for a in h.result.actions)
    if not settled: confirmed_fill(h, 2, 'retired-real-fill')
    assert h.state.remaining_quantity == 5 and h.state.realized_gross_pnl == 5
    assert h.state.realized_net_pnl == D('4.96')
    check_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('lower', [None, 0, 1, 2])
def test_repeated_loss_and_smaller_ack_preserve_high_water_across_two_restarts(side, lower):
    h = RecordedProbe(side)
    loss(h, 2)
    first_loss = h.journal[-1]
    for _ in range(2):
        h.restart()
        loss(h, lower)
        receipt(h, 0)
        assert_waiting(h, 2)
        assert h.stop_id in h.state.recovery_pending_action_ids
        assert h.stop().terminal_status is None and not h.state.faults
        redeliver(h, first_loss)
        assert_waiting(h, 2)
    receipt(h, 2, 'CANCELED')
    confirmed_fill(h, 1, 'recovery-a')
    assert_waiting(h, 2, 9)
    assert h.stop_id in h.state.recovery_pending_action_ids
    confirmed_fill(h, 1, 'recovery-b')
    assert h.stop_id not in h.state.recovery_pending_action_ids
    assert all(a.target_confirmed for a in queries(h))
    assert h.state.remaining_quantity == 8 and h.latest('CLOSE_ALL').quantity == 8
    assert len([a for a in h.state.actions if a.kind == 'ARM_STOP']) == 1
    assert len([a for a in h.state.actions if a.kind == 'CLOSE_ALL']) == 1
    check_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('source', ['receipt', 'loss'])
@pytest.mark.parametrize('total', [0, 1, 2])
def test_loss_and_receipt_share_known_quantity_and_terminal_decisions(side, source, total):
    h = RecordedProbe(side)
    if source == 'receipt': receipt(h, 2, 'UNKNOWN')
    else: loss(h, 2)
    assert_waiting(h, 2)
    receipt(h, total)
    assert h.stop().known_filled_quantity == 2 and h.state.remaining_quantity == 10
    assert not any(a.target_confirmed for a in queries(h))
    if source == 'receipt': receipt(h, total, 'CANCELED')
    else: loss(h, total, 'CANCELED')
    assert h.stop().known_filled_quantity == 2 and h.state.remaining_quantity == 10
    if total < 2:
        assert h.state.faults == ('TERMINAL_TOTAL_BEHIND_KNOWN_CUMULATIVE',)
        assert h.stop().terminal_status is None
    else:
        assert not h.state.faults and h.stop().terminal_quantity == 2
        assert h.stop().status == 'SETTLING'
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)
    check_replay(h)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_state_hash_rewrite_cannot_erase_loss_evidence_during_restore(side):
    h = RecordedProbe(side)
    loss(h, 2)
    saved = check_replay(h)
    raw = json.loads(saved.model_dump_json())
    target = next(a for a in raw['state']['actions'] if a['action_id'] == h.stop_id)
    target.update(acknowledged_quantity='0', status='ACCEPTED')
    raw['state_digest'] = digest(ExitState.model_validate(raw['state']))
    with pytest.raises(ExitContractError):
        restore_checkpoint(json.dumps(raw), recovery_event=RecoveryRequired(event_id='forged-resume',
            position_id=h.state.position_id, received_at=h.now + 1))
    assert_waiting(h, 2)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_unknown_or_nonstop_loss_target_is_quarantined_before_evidence_merge(side):
    h = RecordedProbe(side)
    loss(h, 2, target='not-a-known-order')
    assert 'UNRECOGNIZED_PROTECTION_LOSS' in h.state.faults
    assert h.state.remaining_quantity == 10 and h.stop().known_filled_quantity == 0
    query = h.latest('RECONCILE')
    loss(h, 2, target=query.action_id)
    assert h.state.remaining_quantity == 10 and h.stop().known_filled_quantity == 0
    assert not any(a.kind == 'CLOSE_ALL' for a in h.state.actions)
    check_replay(h)
