"""Independent R2 regression: preserve cumulative fills on ProtectionLost.

Reviewed commit: 58fff0ab2142a42565fc9604dc1d10e03b4650fa.
Synthetic events only. Place this file in the repository root and run:
    python -m pytest -q -s test_stage06_r2_protection_lost.py
No external test Harness, broker, network, or account is used.
"""
from decimal import Decimal as D
import pytest
from app.exits.engine import initialize_exit, apply_event
from app.exits.models import (
    ActionReceipt, EntryFill, EntrySealed, ExitSeed, ExitVenueRules,
    PlanSnapshot, ProtectionLost,
)


class Probe:
    def __init__(self, side):
        from app.exits.policy import ExitPolicy
        self.now = 1000.0
        self.n = 0
        self.policy = ExitPolicy()
        plan = PlanSnapshot(
            setup_id='r2-independent-plan', plan_version='1', symbol='SOLUSDT',
            side=side, original_stop='95' if side == 'LONG' else '105',
            original_targets=(), setup_digest='0'*64, rr_digest='0'*64,
            scorecard_digest='0'*64, admission_digest='0'*64,
            admission_result='APPROVE',
        )
        rules = ExitVenueRules(
            symbol='SOLUSDT', verified=True, quantity_step='.001', price_tick='.01',
            min_quantity='.001', min_notional='0', max_quantity='1000',
            reduce_only_min_quantity_exempt=False, reduce_only_min_notional_exempt=False,
            exact_close_remainder=True, atomic_stop_replace=True,
        )
        fill = EntryFill(
            event_id='first', position_id='r2-independent-position', received_at=self.now,
            fill_id='open-fill', entry_action_id='open-leg', quantity='10', price='100',
            fee_usdt='0', occurred_at=self.now,
        )
        self.seed = ExitSeed(position_id=fill.position_id, plan=plan, rules=rules, first_fill=fill)
        self.result = initialize_exit(self.seed, self.policy)
        self.state = self.result.state
        self.stop_id = self.result.actions[0].action_id
        self.ack_zero()
        self.send(EntrySealed, entry_action_id='open-leg', total_filled_quantity='10')
        assert self.state.protection_status == 'ACTIVE'

    def send(self, cls, **values):
        self.n += 1
        self.now += 1.0
        event = cls(event_id=f'r2-event-{self.n}', position_id=self.state.position_id,
                    received_at=self.now, **values)
        self.result = apply_event(self.seed, self.policy, self.state, event)
        self.state = self.result.state
        return self.result

    def stop(self):
        return next(a for a in self.state.actions if a.action_id == self.stop_id)

    def ack_zero(self):
        return self.send(ActionReceipt, action_id=self.stop_id, status='ACCEPTED',
                         cumulative_filled_quantity='0', reduce_only_verified=True,
                         stop_price=self.stop().stop_price, covers_remaining=True)

    def report_unknown_with_two_filled(self):
        return self.send(ProtectionLost, action_id=self.stop_id, status='UNKNOWN',
                         cumulative_filled_quantity='2')


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_protection_lost_keeps_explicit_cumulative_fill_high_water(side):
    h = Probe(side)
    h.report_unknown_with_two_filled()
    print(side, 'after ProtectionLost/UNKNOWN/2:', h.stop().status,
          'known=', h.stop().known_filled_quantity, 'remaining=', h.state.remaining_quantity)
    assert h.state.remaining_quantity == 10, 'Receipts must not manufacture fills'
    assert h.stop().known_filled_quantity >= 2 or h.state.faults, (
        'A supplied cumulative fill of 2 must be retained, or the input explicitly quarantined'
    )


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_old_zero_ack_cannot_complete_query_after_loss_reported_two(side):
    h = Probe(side)
    h.report_unknown_with_two_filled()
    query_ids = [a.action_id for a in h.state.actions
                 if a.kind == 'RECONCILE' and a.target_action_id == h.stop_id]
    assert query_ids, 'Unknown protection must query the original order'
    h.ack_zero()
    queries = [a for a in h.state.actions if a.action_id in query_ids]
    print(side, 'after old ACK/0:', h.stop().status, 'known=', h.stop().known_filled_quantity,
          'query_confirmed=', [a.target_confirmed for a in queries], 'faults=', h.state.faults)
    assert not any(a.target_confirmed for a in queries), (
        'Old ACK/0 must not clear the reported missing two-fill obligation'
    )
    assert h.stop().status in ('UNKNOWN', 'SETTLING') or h.state.faults


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_zero_terminal_cannot_release_full_exit_after_reported_two(side):
    h = Probe(side)
    h.report_unknown_with_two_filled()
    h.ack_zero()
    h.send(ActionReceipt, action_id=h.stop_id, status='CANCELED', cumulative_filled_quantity='0')
    exits = [a for a in h.result.actions if a.kind == 'CLOSE_ALL']
    print(side, 'after CANCELED/0:', 'exits=', [(a.kind, a.quantity) for a in exits],
          'known=', h.stop().known_filled_quantity, 'faults=', h.state.faults)
    assert not exits, (
        'Conflicting terminal 0 must not release a fresh 10-unit CLOSE_ALL '
        'while the previously reported two-fill obligation is unresolved'
    )
    assert h.state.faults, 'Final zero contradicts the reported cumulative two'
