"""Immutable, serializable event-sourced protection records; synthetic/Paper only."""

from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import Field, StrictBool, model_validator

from app.setups.models import Record, Text, Timestamp

Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Signed = Annotated[Decimal, Field(allow_inf_nan=False)]
Ratio = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Count = Annotated[int, Field(strict=True, ge=0)]
Digest = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
Phase = Literal['OPEN', 'TP1_PENDING', 'TP1_FILLED', 'TP2_PENDING', 'TP2_FILLED',
                'RUNNER', 'STOP_PENDING', 'CLOSED', 'PROTECTION_REQUIRED', 'ERROR']


class ExitContractError(ValueError):
    """Caller must quarantine invalid input, never interpret failure as permission."""


class PlanSnapshot(Record):
    setup_id: Text
    plan_version: Text
    symbol: Text
    side: Literal['LONG', 'SHORT']
    original_stop: Positive
    original_targets: tuple[tuple[Text, Positive, Ratio], ...]
    setup_digest: Digest
    rr_digest: Digest
    scorecard_digest: Digest
    admission_digest: Digest
    admission_result: Literal['APPROVE', 'REDUCE', 'REJECT']


class ExitVenueRules(Record):
    # Explicit adapter assertions, not independently authenticated exchange facts.
    symbol: Text
    verified: StrictBool
    quantity_step: Positive
    price_tick: Positive
    min_quantity: Amount
    min_notional: Amount
    max_quantity: Positive
    reduce_only_min_quantity_exempt: StrictBool
    reduce_only_min_notional_exempt: StrictBool
    exact_close_remainder: StrictBool
    atomic_stop_replace: StrictBool
    dynamic_full_position_stop: StrictBool = False
    dynamic_stop_contract_id: Text | None = None

    @model_validator(mode='after')
    def ordered(self):
        if self.min_quantity > self.max_quantity:
            raise ValueError('Invalid venue quantity bounds')
        if self.dynamic_full_position_stop != (self.dynamic_stop_contract_id is not None):
            raise ValueError('Dynamic protection needs an explicit future-fill coverage contract')
        return self


class BaseEvent(Record):
    event_id: Text
    position_id: Text
    received_at: Timestamp


class EntryFill(BaseEvent):
    kind: Literal['ENTRY_FILL'] = 'ENTRY_FILL'
    fill_id: Text
    entry_action_id: Text
    quantity: Positive
    price: Positive
    fee_usdt: Amount
    occurred_at: Timestamp
    confirmed: Literal[True] = True


class EntrySealed(BaseEvent):
    kind: Literal['ENTRY_SEALED'] = 'ENTRY_SEALED'
    entry_action_id: Text
    total_filled_quantity: Positive


class RunnerEvidence(Record):
    kind: Literal['swing_low', 'swing_high', 'atr', 'trend_invalid']
    evidence_id: Text
    value: Positive | None = None
    invalid: StrictBool | None = None
    confirmed: StrictBool = False
    observed_at: Timestamp


class MarketEvent(BaseEvent):
    kind: Literal['MARKET'] = 'MARKET'
    observed_at: Timestamp
    bid: Positive | None = None
    ask: Positive | None = None
    confirmed: StrictBool = False
    evidence: tuple[RunnerEvidence, ...] = ()
    # Informational only; new-entry gates MUST NOT disable existing protection.
    new_entries_paused: StrictBool = False
    daily_halted: StrictBool = False
    admission_rejected: StrictBool = False
    score_available: StrictBool = True


class StopCoverage(Record):
    """Adapter assertion bound to one position/order, not network authentication.

    quantity is total order capacity including already filled quantity. Dynamic
    mode explicitly includes future fills of THIS position until order retirement.
    """
    mode: Literal['fixed_quantity', 'dynamic_position'] = 'fixed_quantity'
    quantity: Positive
    quantity_version: Count
    evidence_id: Text
    dynamic_contract_id: Text | None = None


class ActionReceipt(BaseEvent):
    kind: Literal['RECEIPT'] = 'RECEIPT'
    action_id: Text
    status: Literal['ACCEPTED', 'FILLED', 'CANCELED', 'REJECTED', 'UNKNOWN']
    cumulative_filled_quantity: Amount = Decimal(0)
    reduce_only_verified: StrictBool = False
    stop_price: Positive | None = None
    covers_remaining: StrictBool = False
    old_stop_retired: StrictBool = False
    retired_stop_cumulative_filled: Amount | None = None
    coverage: StopCoverage | None = None


class ExitFill(BaseEvent):
    kind: Literal['EXIT_FILL'] = 'EXIT_FILL'
    action_id: Text
    fill_id: Text
    quantity: Positive
    price: Positive
    fee_usdt: Amount
    occurred_at: Timestamp
    confirmed: Literal[True] = True


class ProtectionLost(BaseEvent):
    kind: Literal['PROTECTION_LOST'] = 'PROTECTION_LOST'
    action_id: Text
    status: Literal['CANCELED', 'FAILED', 'UNKNOWN']
    cumulative_filled_quantity: Amount | None = None


class RecoveryRequired(BaseEvent):
    kind: Literal['RECOVERY_REQUIRED'] = 'RECOVERY_REQUIRED'


Event = Annotated[Union[EntryFill, EntrySealed, MarketEvent, ActionReceipt, ExitFill, ProtectionLost, RecoveryRequired],
                  Field(discriminator='kind')]


class ExitAction(Record):
    action_id: Digest
    position_id: Text
    sequence: Count
    kind: Literal['ARM_STOP', 'MOVE_STOP', 'TP1', 'TP2', 'TP_COMBINED', 'CLOSE_ALL', 'CANCEL', 'RECONCILE']
    reason_code: Text
    quantity: Amount = Decimal(0)
    stop_price: Positive | None = None
    target_action_id: Text | None = None
    replaces_action_id: Text | None = None
    close_exact_remainder: StrictBool = False
    reduce_only: Literal[True] = True
    side: Literal['SELL', 'BUY']
    position_quantity_version: Count = 0
    protection_mode: Literal['fixed_quantity', 'dynamic_position'] = 'fixed_quantity'
    status: Literal['INTENT', 'ACCEPTED', 'UNKNOWN', 'SETTLING', 'FILLED', 'CANCELED', 'REJECTED'] = 'INTENT'
    filled_quantity: Amount = Decimal(0)
    acknowledged_quantity: Amount = Decimal(0)
    stop_confirmed: StrictBool = False
    terminal_status: Literal['FILLED', 'CANCELED', 'REJECTED'] | None = None
    terminal_quantity: Amount | None = None
    confirmed_coverage: StopCoverage | None = None
    target_confirmed: StrictBool = False
    execution_authority: Literal['none_until_paper_integration'] = 'none_until_paper_integration'


class FillFact(Record):
    fill_id: Text
    action_id: Text
    quantity: Positive
    price: Positive
    fee_usdt: Amount
    occurred_at: Timestamp
    entry_basis_price: Positive | None = None
    entry_basis_notional: Amount | None = None


class ExitSeed(Record):
    mode: Literal['paper'] = 'paper'
    position_id: Text
    plan: PlanSnapshot
    rules: ExitVenueRules
    first_fill: EntryFill

    @model_validator(mode='after')
    def identity(self):
        if self.position_id != self.first_fill.position_id or self.plan.symbol != self.rules.symbol:
            raise ValueError('Position/symbol mismatch')
        return self


class ExitState(Record):
    schema_version: Literal['position-exit/v2'] = 'position-exit/v2'
    mode: Literal['paper'] = 'paper'
    seed_digest: Digest
    policy_digest: Digest
    position_id: Text
    symbol: Text
    side: Literal['LONG', 'SHORT']
    version: Count
    phase: Phase
    entry_action_id: Text
    opened_at: Timestamp
    entry_sealed: StrictBool = False
    original_quantity: Positive
    remaining_quantity: Amount
    actual_average_entry: Positive
    entry_notional: Positive
    remaining_entry_cost: Amount
    remaining_average_entry: Positive | None
    exit_notional: Amount = Decimal(0)
    position_quantity_version: Count = 0
    frozen_r_anchor_entry: Positive
    frozen_initial_r: Amount
    original_stop: Positive
    current_stop: Positive
    protection_status: Literal['MISSING', 'PARTIAL', 'PENDING', 'ACTIVE', 'UNKNOWN'] = 'MISSING'
    protection_action_id: Text | None = None
    protection_covered_quantity: Amount = Decimal(0)
    protection_coverage_version: Count | None = None
    tp1_planned: Amount = Decimal(0)
    tp2_planned: Amount = Decimal(0)
    runner_planned: Amount = Decimal(0)
    tp1_filled: Amount = Decimal(0)
    tp2_filled: Amount = Decimal(0)
    runner_quantity: Amount = Decimal(0)
    tp1_complete: StrictBool = False
    tp2_complete: StrictBool = False
    entry_fees: Amount
    exit_fees: Amount = Decimal(0)
    realized_gross_pnl: Signed = Decimal(0)
    realized_net_pnl: Signed = Decimal(0)  # Gross exits minus ALL fees spent to date.
    favorable_extreme: Positive
    last_market: MarketEvent | None = None
    last_received_at: Timestamp
    last_confirmation_event: Text
    event_receipts: tuple[tuple[Text, Digest], ...]
    fill_facts: tuple[FillFact, ...]
    actions: tuple[ExitAction, ...] = ()
    completed_action_ids: tuple[Text, ...] = ()
    confirmed_fill_action_ids: tuple[Text, ...] = ()
    milestones: tuple[Text, ...] = ()
    emergency_reason: Text | None = None
    faults: tuple[Text, ...] = ()
    recovery_pending_action_ids: tuple[Text, ...] = ()
    live_allowed: Literal[False] = False


class ExitResult(Record):
    state: ExitState
    actions: tuple[ExitAction, ...]
    reason_codes: tuple[Text, ...]
    execution_authority: Literal['none_until_paper_integration'] = 'none_until_paper_integration'
