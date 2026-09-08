"""Explicit caller-supplied Paper risk state, never an account client or ledger."""

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from app.setups.models import Record, Text, Timestamp
from app.setups.rr_models import RRCalculation

Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveAmount = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Ratio = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Count = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Digest = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]


def fingerprint(record: Record) -> str:
    """Content binding, NOT a signature or proof of a trusted state supplier."""
    raw = json.dumps(record.model_dump(mode='json'), sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class AccountHardLimits(Record):
    # No guessed limits. All must be supplied by a future trusted Paper adapter.
    max_loss_per_trade_usdt: PositiveAmount | None = None
    daily_loss_limit_usdt: PositiveAmount | None = None
    max_trades_per_day: PositiveInt | None = None
    max_consecutive_losses: PositiveInt | None = None
    max_positions: PositiveInt | None = None
    max_leverage: PositiveInt | None = None
    max_margin_ratio: Ratio | None = None
    max_margin_usdt: Amount | None = None
    max_position_notional_usdt: Amount | None = None
    max_position_quantity: Amount | None = None


class PositionExposure(Record):
    symbol: Text
    side: Literal['LONG', 'SHORT']
    quantity: PositiveAmount


class PendingEntry(Record):
    intent_id: Text
    symbol: Text
    side: Literal['LONG', 'SHORT']


class PaperRiskSnapshot(Record):
    instance_id: Text
    snapshot_revision: Count
    mode: Literal['paper', 'live'] | None = None
    status: Literal['confirmed', 'unverified', 'incomplete'] | None = None
    source: Text | None = None
    observed_at: Timestamp | None = None
    day_started_at: Timestamp | None = None
    day_ends_at: Timestamp | None = None
    equity_usdt: PositiveAmount | None = None
    available_margin_usdt: Amount | None = None  # Available for margin AND entry fees.
    margin_used_usdt: Amount | None = None
    day_realized_loss_usdt: Amount | None = None  # Sum of losing net outcomes, never profit credit.
    unrealized_loss_usdt: Amount | None = None
    reserved_risk_usdt: Amount | None = None  # Outstanding open/pending initial-stop risk.
    trades_today: Count | None = None
    consecutive_losses: Count | None = None
    positions: tuple[PositionExposure, ...] | None = None  # None != known flat ().
    pending_entries: tuple[PendingEntry, ...] | None = None
    paused: StrictBool | None = None
    reconciliation_clear: StrictBool | None = None
    margin_mode: Literal['ISOLATED', 'CROSS'] | None = None
    configured_leverage: PositiveInt | None = None
    auto_add_margin_enabled: StrictBool | None = None
    martingale_enabled: StrictBool | None = None
    limits: AccountHardLimits = Field(default_factory=AccountHardLimits)


class ExchangeConstraints(Record):
    exchange: Text
    symbol: Text
    order_type: Literal['MARKET', 'LIMIT']
    contract_type: Literal['linear_usdt', 'inverse', 'unknown'] = 'unknown'
    status: Literal['confirmed', 'unverified', 'stale'] | None = None
    source: Text | None = None
    observed_at: Timestamp | None = None
    quantity_step: PositiveAmount | None = None
    min_quantity: Amount | None = None
    max_quantity: PositiveAmount | None = None
    min_notional_usdt: Amount | None = None
    price_tick: PositiveAmount | None = None
    max_leverage: PositiveInt | None = None


class EvidenceConfirmation(Record):
    evidence_id: Text
    evidence_digest: Digest
    verified: StrictBool | None = None
    verifier: Text | None = None
    checked_at: Timestamp | None = None


class InvalidationReview(Record):
    setup_digest: Digest
    all_conditions_clear: StrictBool | None = None
    verifier: Text | None = None
    checked_at: Timestamp | None = None


class AdmissionRequest(Record):
    request_id: Text
    risk_budget_usdt: PositiveAmount | None = None  # Desired LOSS budget, not desired order notional.
    action: Literal['OPEN', 'ADD'] | None = None
    leverage: PositiveInt | None = None
    margin_mode: Literal['ISOLATED', 'CROSS'] | None = None
    auto_add_margin: StrictBool | None = None
    loss_recovery_sizing: StrictBool | None = None
    sizing_basis: Literal['quality_risk_budget', 'martingale', 'unknown'] = 'unknown'
    confirmations: tuple[EvidenceConfirmation, ...] = ()
    invalidation_review: InvalidationReview | None = None

    @model_validator(mode='after')
    def unique_confirmations(self):
        ids = [c.evidence_id for c in self.confirmations]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate evidence confirmation')
        return self


class DecisionReason(Record):
    code: Text
    layer: Literal['contract', 'hard', 'soft', 'sizing', 'information']
    explanation: Text
    fields: tuple[Text, ...] = ()


class ConstraintResult(Record):
    name: Text
    quantity_cap: Amount
    explanation: Text


class PlanTerms(Record):
    setup_id: Text
    plan_version: Text
    symbol: Text
    side: Literal['LONG', 'SHORT']
    order_type: Literal['MARKET', 'LIMIT']
    entry_reference: PositiveAmount
    entry_lower: PositiveAmount
    entry_upper: PositiveAmount
    initial_stop: PositiveAmount
    targets: tuple[tuple[Text, PositiveAmount, Ratio], ...]


class InputBinding(Record):
    setup: Digest
    rr: Digest
    scorecard: Digest
    account: Digest
    exchange: Digest
    request: Digest
    policy: Digest
    instance_id: Text
    snapshot_revision: Count


class AdmissionDecision(Record):
    schema_version: Literal['paper-admission/v1'] = 'paper-admission/v1'
    decision_id: Digest
    policy_version: Text
    result: Literal['APPROVE', 'REDUCE', 'REJECT']
    evaluated_at: Timestamp
    valid_until: Timestamp | None
    binding: InputBinding | None
    plan_terms: PlanTerms | None
    hard_gates_passed: StrictBool
    opportunity_tier: Literal['S', 'A', 'B', 'C'] | None = None
    tier_interpretation: Literal['risk_budget_band_not_win_probability'] = 'risk_budget_band_not_win_probability'
    reason_codes: tuple[Text, ...]
    reasons: tuple[DecisionReason, ...]
    policy_risk_ceiling_usdt: Amount = Decimal(0)
    allowed_risk_budget_usdt: Amount = Decimal(0)
    max_quantity: Amount = Decimal(0)
    max_notional_usdt: Amount = Decimal(0)
    max_initial_margin_usdt: Amount = Decimal(0)
    entry_fee_reserve_usdt: Amount = Decimal(0)
    modeled_stop_loss_usdt: Amount = Decimal(0)
    leverage: PositiveInt | None = None
    required_net_rr: Amount | None = None
    final_min_net_rr: Annotated[Decimal, Field(allow_inf_nan=False)] | None = None
    final_rr: RRCalculation | None = None
    constraints: tuple[ConstraintResult, ...] = ()
    eligibility_scope: Literal['paper_only'] = 'paper_only'
    execution_authority: Literal['none_until_paper_integration'] = 'none_until_paper_integration'
    live_allowed: Literal[False] = False
    limitations: tuple[Text, ...] = (
        'Input digests bind content, not source authenticity; future adapters must own trusted state',
        'Pure decision only: no risk reservation, ledger mutation, order, or runtime integration',
        'A future Paper consumer must revalidate fresh state under its execution lock and reserve atomically',
        'A smaller quantity also needs a new decision: fixed funding can change net RR after downsizing',
        'Modeled stop loss is not a guarantee against gaps, liquidity shocks or unmodeled costs',
    )

    @model_validator(mode='after')
    def decision_shape(self):
        if not self.reasons or self.reason_codes != tuple(dict.fromkeys(r.code for r in self.reasons)):
            raise ValueError('Every decision must carry consistent reason codes and explanations')
        amounts = (self.policy_risk_ceiling_usdt, self.allowed_risk_budget_usdt, self.max_quantity,
                   self.max_notional_usdt, self.max_initial_margin_usdt, self.entry_fee_reserve_usdt,
                   self.modeled_stop_loss_usdt)
        if self.result == 'REJECT':
            if any(amounts) or self.leverage is not None or self.final_rr is not None:
                raise ValueError('Rejected decisions cannot authorize risk, size, or leverage')
        elif (not self.hard_gates_passed or self.binding is None or self.plan_terms is None
              or self.valid_until is None or self.valid_until <= self.evaluated_at
              or self.max_quantity <= 0 or self.allowed_risk_budget_usdt <= 0
              or self.allowed_risk_budget_usdt > self.policy_risk_ceiling_usdt
              or self.modeled_stop_loss_usdt > self.allowed_risk_budget_usdt
              or self.leverage is None or self.final_rr is None or self.required_net_rr is None
              or self.final_min_net_rr is None or self.final_min_net_rr < self.required_net_rr):
            raise ValueError('Paper eligibility requires bounded size, risk, matching calculation and lifetime')
        return self


class AdmissionContractError(ValueError):
    """Fail closed: invalid caller contract must never be treated as permission."""
