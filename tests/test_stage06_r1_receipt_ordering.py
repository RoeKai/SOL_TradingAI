"""Independent R1 receipt-ordering regression tests (synthetic data only).

Commit under review: def0c6e103bad3ba78cecbc90a56a213c723806c.
Current result: four failures, two scenarios in LONG and SHORT.
Place in the reviewed repository root, then run:
    python -m pytest -q -s test_stage06_r1_receipt_ordering.py
No tests import the repository's own Harness; no network/account access.
"""
from decimal import Decimal as D
import pytest
from app.exits.engine import initialize_exit, apply_event
from app.exits.models import (
    ActionReceipt, EntryFill, EntrySealed, ExitFill, ExitSeed, ExitVenueRules,
    MarketEvent, PlanSnapshot, RecoveryRequired, StopCoverage, ExitContractError,
)
from app.exits.policy import ExitPolicy
from app.exits.persistence import checkpoint, restore_checkpoint

class Probe:
    def __init__(self, side='LONG', quantity='4'):
        self.now = 1000.0
        self.n = 0
        self.policy = ExitPolicy()
        self.plan = PlanSnapshot(
            setup_id='synthetic-review-plan', plan_version='v1', symbol='SOLUSDT',
            side=side, original_stop='95' if side == 'LONG' else '105',
            original_targets=(), setup_digest='0'*64, rr_digest='0'*64,
            scorecard_digest='0'*64, admission_digest='0'*64,
            admission_result='APPROVE',
        )
        self.rules = ExitVenueRules(
            symbol='SOLUSDT', verified=True, quantity_step='0.001',
            price_tick='0.01', min_quantity='0.001', min_notional='0',
            max_quantity='1000', reduce_only_min_quantity_exempt=False,
            reduce_only_min_notional_exempt=False,
            exact_close_remainder=True, atomic_stop_replace=True,
        )
        first = EntryFill(
            event_id='first-entry', position_id='synthetic-position',
            received_at=self.now, fill_id='first-fill', entry_action_id='opening-leg',
            quantity=quantity, price='100', fee_usdt='0', occurred_at=self.now,
        )
        self.seed = ExitSeed(position_id='synthetic-position', plan=self.plan,
                             rules=self.rules, first_fill=first)
        self.result = initialize_exit(self.seed, self.policy)
        self.state = self.result.state

    def send(self, cls, **values):
        self.now += 1.0
        self.n += 1
        event = cls(event_id=f'event-{self.n}', position_id=self.state.position_id,
                    received_at=self.now, **values)
        self.result = apply_event(self.seed, self.policy, self.state, event)
        self.state = self.result.state
        return self.result

    def latest(self, kind):
        return next(a for a in reversed(self.state.actions) if a.kind == kind)

    def arm(self):
        stop = self.latest('ARM_STOP')
        self.send(ActionReceipt, action_id=stop.action_id, status='ACCEPTED',
                  cumulative_filled_quantity='0', reduce_only_verified=True,
                  stop_price=stop.stop_price, covers_remaining=True)
        return self

    def seal(self):
        self.send(EntrySealed, entry_action_id=self.state.entry_action_id,
                  total_filled_quantity=self.state.original_quantity)
        return self

    def fill(self, action, quantity, price, fill_id):
        return self.send(ExitFill, action_id=action.action_id, fill_id=fill_id,
                         quantity=quantity, price=price, fee_usdt='0', occurred_at=self.now)

    def quote(self, price):
        bid=D(price)
        return self.send(MarketEvent, observed_at=self.now+1,
                         bid=bid, ask=bid+D('.01'), confirmed=True)

class RecordedProbe(Probe):
    def __init__(self, side='LONG', quantity='4', dynamic=False, atomic=True):
        super().__init__(side, quantity)
        changes={'atomic_stop_replace':atomic}
        if dynamic:
            changes.update(dynamic_full_position_stop=True, dynamic_stop_contract_id='test-dynamic/v1')
        self.rules=self.rules.model_copy(update=changes)
        self.seed=self.seed.model_copy(update={'rules':self.rules})
        self.result=initialize_exit(self.seed,self.policy)
        self.state=self.result.state
        self.journal=[]
    def send(self, cls, **values):
        self.now+=1.0
        self.n+=1
        event=cls(event_id=f'event-{self.n}',position_id=self.state.position_id,received_at=self.now,**values)
        self.result=apply_event(self.seed,self.policy,self.state,event)
        self.state=self.result.state
        self.journal.append(event)
        return self.result
    def add(self, q, price='100', fee='0', occurred_at=None):
        return self.send(EntryFill,entry_action_id=self.state.entry_action_id,
                         fill_id=f'add-{self.n}', quantity=str(q),price=str(price),fee_usdt=str(fee),
                         occurred_at=self.now if occurred_at is None else occurred_at)
    def ack(self, action, dynamic=False):
        extra={}
        if action.replaces_action_id:
            old=next(a for a in self.state.actions if a.action_id==action.replaces_action_id)
            extra.update(old_stop_retired=True,retired_stop_cumulative_filled=old.filled_quantity)
        if dynamic:
            extra['coverage']=StopCoverage(mode='dynamic_position',quantity=self.state.remaining_quantity,
                 quantity_version=self.state.position_quantity_version,evidence_id=f'proof-{self.n}',
                 dynamic_contract_id=self.rules.dynamic_stop_contract_id)
        return self.send(ActionReceipt,action_id=action.action_id,status='ACCEPTED',
                         reduce_only_verified=True,stop_price=action.stop_price,covers_remaining=True,**extra)
    def restart(self):
        saved=checkpoint(self.seed,self.policy,tuple(self.journal))
        assert saved.state==self.state
        self.now+=1
        self.n+=1
        rec=RecoveryRequired(event_id=f'restart-{self.n}',position_id=self.state.position_id,received_at=self.now)
        restored=restore_checkpoint(saved.model_dump_json(),recovery_event=rec)
        self.state=restored.state
        self.journal=list(restored.journal)
        return saved,restored

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_retired_stop_waiting_for_fill_detail_cannot_be_resurrected_by_late_ack(side):
    h=RecordedProbe(side).arm()
    old=h.latest('ARM_STOP')
    h.add(6)
    h.seal()
    new=h.latest('MOVE_STOP')
    # The new stop is accepted. The atomic replacement confirms that the old
    # order is canceled, with 1 unit filled. That fill detail has not arrived.
    h.send(ActionReceipt,action_id=new.action_id,status='ACCEPTED',
           reduce_only_verified=True,stop_price=new.stop_price,covers_remaining=True,
           old_stop_retired=True,retired_stop_cumulative_filled='1')
    retired=next(a for a in h.state.actions if a.action_id==old.action_id)
    assert retired.status=='SETTLING' and retired.terminal_status=='CANCELED'
    assert retired.terminal_quantity==1
    assert h.state.protection_action_id==new.action_id
    assert h.state.remaining_quantity==10  # Missing fill detail is not fabricated.
    # A delayed historical ACK arrives, with a new delivery/event ID. This is
    # not a new stop and must never undo its already confirmed retirement.
    h.send(ActionReceipt,action_id=old.action_id,status='ACCEPTED',
           reduce_only_verified=True,stop_price=old.stop_price,covers_remaining=True,
           cumulative_filled_quantity='0')
    retired=next(a for a in h.state.actions if a.action_id==old.action_id)
    print(side,'retired_after_late_ACK=',retired.status,
          'terminal=',retired.terminal_status,'terminal_quantity=',retired.terminal_quantity,
          'points_back_to_retired=',h.state.protection_action_id==old.action_id,
          'new_actions=',[(a.kind,a.reason_code) for a in h.result.actions],
          'faults=',h.state.faults)
    assert h.state.protection_action_id!=old.action_id, 'Retired stop became current protection again'
    assert retired.status!='ACCEPTED', 'Waiting for fill details must not erase order terminality'
    assert not any(a.kind in ('MOVE_STOP','TP1','TP2','CLOSE_ALL') for a in h.result.actions)

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_late_lower_cumulative_ack_cannot_clear_known_missing_fill_obligation(side):
    h=RecordedProbe(side,quantity='10').arm().seal()
    h.quote('106' if side=='LONG' else '94')
    tp=h.latest('TP1')
    h.send(ActionReceipt,action_id=tp.action_id,status='ACCEPTED',
           reduce_only_verified=True,cumulative_filled_quantity='1')
    pending=h.latest('TP1')
    assert pending.acknowledged_quantity==1 and pending.status=='SETTLING'
    assert h.state.remaining_quantity==10
    # The old zero-fill ACK arrives after the cumulative-one update.
    h.send(ActionReceipt,action_id=tp.action_id,status='ACCEPTED',
           reduce_only_verified=True,cumulative_filled_quantity='0')
    current=h.latest('TP1')
    print(side,'known_cumulative_after_late_ACK=',current.acknowledged_quantity,
          'status=',current.status,'faults=',h.state.faults,
          'queries=',[(a.status,a.target_confirmed) for a in h.state.actions if a.kind=='RECONCILE'])
    assert current.acknowledged_quantity>=1, 'Known cumulative filled amount regressed from 1 to 0'
    assert current.status in ('SETTLING','UNKNOWN') or h.state.faults
    assert h.state.remaining_quantity==10  # Still wait for the real fill detail.
