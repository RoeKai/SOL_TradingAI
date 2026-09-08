"""Stage 2: immutable, versioned descriptions, never an execution capability.

No prices, targets, RR, scores, grades, position sizes or trading decisions are
calculated here. Supplied estimates are unverified claims, not approvals.
Validation only checks types, units, geometry and internal record consistency.
Timestamps are UTC Unix seconds; rates/fractions use [0, 1], not percent points.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Text = Annotated[str, Field(strict=True, min_length=1, max_length=4096, pattern=r'\S')]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Positive = Annotated[Number, Field(gt=0)]
NonNegative = Annotated[Number, Field(ge=0)]
Fraction = Annotated[Number, Field(ge=0, le=1)]
Score = Annotated[Number, Field(ge=0, le=100)]
Timestamp = NonNegative
Availability = Literal['available', 'missing', 'stale', 'unverified', 'not_applicable']
StrategyType = Literal['trend_breakout', 'pullback_entry', 'panic_rebound',
                       'fake_breakout_reverse', 'legacy_unknown', 'custom']
ScoreDimension = Literal['trend', 'volume', 'volatility', 'market_resonance',
                         'structure', 'funding_rate', 'liquidation_zones', 'news']
SCORE_DIMENSIONS = ('trend', 'volume', 'volatility', 'market_resonance',
                    'structure', 'funding_rate', 'liquidation_zones', 'news')
COVERAGE_KEYS = ('market_price', 'trend', 'volume', 'volatility', 'btc_reference',
                 'eth_reference', 'structure', 'funding_rate', 'liquidation_zones', 'news')


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False,
                              validate_default=True, revalidate_instances='always')


class Evidence(Record):
    evidence_id: Text
    kind: Literal['support', 'resistance', 'swing_low', 'swing_high', 'range_boundary',
                  'indicator', 'market_event', 'legacy_unspecified', 'other']
    description: Text
    status: Availability = 'unverified'
    source: Text | None = None
    timeframe: Text | None = None
    observed_at: Timestamp | None = None
    price: Positive | None = None

    @model_validator(mode='after')
    def available_needs_provenance(self):
        if self.status == 'available' and (self.source is None or self.observed_at is None):
            raise ValueError('Available evidence requires source and observed_at')
        return self


class EntryPlan(Record):
    order_type: Literal['MARKET', 'LIMIT']
    reference_price: Positive
    lower_price: Positive
    upper_price: Positive
    basis: Literal['structure_zone', 'legacy_point', 'unverified'] = 'unverified'
    evidence_ids: tuple[Text, ...] = ()

    @model_validator(mode='after')
    def ordered_range(self):
        if not self.lower_price <= self.reference_price <= self.upper_price:
            raise ValueError('Entry range must contain the reference price')
        return self


class InvalidationCondition(Record):
    condition_id: Text
    kind: Literal['price', 'structure', 'time', 'data', 'operator']
    description: Text
    operator: Literal['lt', 'lte', 'gt', 'gte', 'eq'] | None = None
    price: Positive | None = None
    at: Timestamp | None = None
    evidence_ids: tuple[Text, ...] = ()

    @model_validator(mode='after')
    def condition_shape(self):
        if self.kind == 'price' and (self.price is None or self.operator is None):
            raise ValueError('Price invalidation requires price and operator')
        if self.kind == 'time' and self.at is None:
            raise ValueError('Time invalidation requires at')
        return self


class InitialStop(Record):
    price: Positive
    basis: Text
    evidence_ids: tuple[Text, ...] = ()


class TargetLevel(Record):
    target_id: Text
    price: Positive
    fraction: Annotated[Fraction, Field(gt=0)]
    basis: Text
    kind: Literal['structure', 'legacy_unspecified', 'unverified'] = 'unverified'
    evidence_ids: tuple[Text, ...] = ()
    theoretical_rr: NonNegative | None = None
    net_rr: Number | None = None  # Can be negative after costs.


class RewardRiskEstimates(Record):
    gross_rr: NonNegative | None = None
    net_rr: Number | None = None
    calculation_method: Text | None = None
    # No grade-to-RR mapping, threshold, arithmetic or endorsement in this schema.
    verification: Literal['not_calculated_or_verified_in_stage2'] = 'not_calculated_or_verified_in_stage2'


class MarketSnapshot(Record):
    status: Availability = 'missing'
    regime: Literal['unknown', 'trend', 'range', 'high_volatility'] = 'unknown'
    observed_at: Timestamp | None = None
    source: Text | None = None
    reference_price: Positive | None = None
    bid: Positive | None = None
    ask: Positive | None = None
    return_1m_pct: Number | None = None
    return_3m_pct: Number | None = None
    return_5m_pct: Number | None = None
    return_15m_pct: Number | None = None
    btc_return_3m_pct: Number | None = None
    eth_return_3m_pct: Number | None = None
    volume_ratio: NonNegative | None = None
    volatility_pct: NonNegative | None = None
    buy_pressure: Fraction | None = None

    @model_validator(mode='after')
    def snapshot_shape(self):
        if self.status == 'available' and (self.observed_at is None or self.source is None or self.reference_price is None):
            raise ValueError('Available market snapshot requires time, source and price')
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError('Market bid must not exceed ask')
        return self


class ScoreComponent(Record):
    dimension: ScoreDimension
    status: Availability = 'missing'
    points: Score | None = None
    max_points: Score | None = None  # Weights are deliberately not decided here.
    evidence_ids: tuple[Text, ...] = ()
    explanation: str = ''

    @model_validator(mode='after')
    def points_shape(self):
        if self.points is not None:
            if self.max_points is None or self.points > self.max_points:
                raise ValueError('Supplied points require a sufficient max_points')
            if self.status not in ('available', 'unverified'):
                raise ValueError('Missing, stale or inapplicable data cannot carry points')
        return self


class ScoreBreakdown(Record):
    total: Score | None = None
    components: tuple[ScoreComponent, ...] = Field(
        default_factory=lambda: tuple(ScoreComponent(dimension=d) for d in SCORE_DIMENSIONS))
    method_version: Text | None = None
    legacy_score: Score | None = None  # Separate scale/provenance, never promoted to total.
    interpretation: Literal['heuristic_not_win_probability'] = 'heuristic_not_win_probability'

    @model_validator(mode='after')
    def exact_dimensions(self):
        dimensions = [item.dimension for item in self.components]
        if len(dimensions) != len(SCORE_DIMENSIONS) or set(dimensions) != set(SCORE_DIMENSIONS):
            raise ValueError('Exactly eight unique scoring dimensions are required')
        return self


class ConfidenceEstimate(Record):
    value: Fraction | None = None
    basis: Text | None = None
    interpretation: Literal['evidence_confidence_not_win_probability'] = 'evidence_confidence_not_win_probability'

    @model_validator(mode='after')
    def confidence_needs_basis(self):
        if self.value is not None and self.basis is None:
            raise ValueError('Supplied confidence requires a basis; it is not a win probability')
        return self


class PositionLimitAdvice(Record):
    max_quantity: NonNegative | None = None
    max_notional_usdt: NonNegative | None = None
    max_margin_usdt: NonNegative | None = None
    max_margin_fraction_of_equity: Fraction | None = None
    leverage_cap: Annotated[int, Field(strict=True, ge=1)] | None = None
    basis: Text | None = None
    authority: Literal['advice_only_not_order_quantity'] = 'advice_only_not_order_quantity'


class RiskBudget(Record):
    max_loss_usdt: NonNegative | None = None
    remaining_daily_loss_usdt: NonNegative | None = None
    max_risk_fraction_of_equity: Fraction | None = None
    basis: Text | None = None


class CostAssumptions(Record):
    entry_fee_rate: Fraction | None = None
    exit_fee_rate: Fraction | None = None
    entry_slippage_bps: Annotated[NonNegative, Field(le=10000)] | None = None
    exit_slippage_bps: Annotated[NonNegative, Field(le=10000)] | None = None
    funding_cost_usdt: Number | None = None  # Positive=cost, negative=credit; None=unknown.
    assumed_holding_seconds: NonNegative | None = None
    source: Text | None = None
    observed_at: Timestamp | None = None


class DataAvailability(Record):
    name: Text
    status: Availability = 'missing'
    required: StrictBool = True
    source: Text | None = None
    observed_at: Timestamp | None = None
    note: str = ''

    @model_validator(mode='after')
    def availability_shape(self):
        if self.status == 'available' and (self.source is None or self.observed_at is None):
            raise ValueError('Available input requires source and timestamp; it does not prove freshness')
        if self.required and self.status == 'not_applicable':
            raise ValueError('Required data cannot be marked not_applicable')
        return self


def _coverage_summary(items):
    # Record metadata only. Not a score, freshness check or trading admission.
    applicable = [item for item in items if item.status != 'not_applicable']
    ratio = sum(item.status == 'available' for item in applicable) / len(applicable) if applicable else None
    missing = tuple(item.name for item in applicable if item.status != 'available')
    return ratio, missing


class DataCoverage(Record):
    items: tuple[DataAvailability, ...] = Field(
        default_factory=lambda: tuple(DataAvailability(name=name) for name in COVERAGE_KEYS))
    coverage_ratio: Fraction | None = 0.0
    missing_items: tuple[Text, ...] = COVERAGE_KEYS  # Includes stale/unverified; see each status.

    @classmethod
    def from_items(cls, items: tuple[DataAvailability, ...]):
        ratio, missing = _coverage_summary(items)
        return cls(items=items, coverage_ratio=ratio, missing_items=missing)

    @model_validator(mode='after')
    def consistent_summary(self):
        names = [item.name for item in self.items]
        if len(names) != len(set(names)) or not set(COVERAGE_KEYS).issubset(names):
            raise ValueError('Coverage must contain each baseline input exactly once')
        ratio, missing = _coverage_summary(self.items)
        if self.missing_items != missing or ((ratio is None) != (self.coverage_ratio is None)):
            raise ValueError('Coverage summary contradicts input statuses')
        if ratio is not None and abs(ratio - self.coverage_ratio) > 1e-12:
            raise ValueError('Coverage ratio contradicts input statuses')
        return self


class StopMoveRule(Record):
    after_target_id: Text
    move_to: Literal['entry', 'cost_adjusted_entry', 'fixed_price', 'trailing']
    fixed_price: Positive | None = None
    trailing_distance_r: Positive | None = None
    buffer_bps: NonNegative | None = None
    requires_confirmed_fill: StrictBool = True

    @model_validator(mode='after')
    def descriptive_move_shape(self):
        if not self.requires_confirmed_fill:
            raise ValueError('Stop movement declarations require confirmed target fills')
        if self.move_to == 'fixed_price' and self.fixed_price is None:
            raise ValueError('Fixed stop destination requires fixed_price')
        if self.move_to == 'trailing' and self.trailing_distance_r is None:
            raise ValueError('Trailing declaration requires a distance; no trailing algorithm runs here')
        return self


class StopMovementPlan(Record):
    rules: tuple[StopMoveRule, ...] = ()
    allow_widening: StrictBool = False

    @model_validator(mode='after')
    def no_widening_declaration(self):
        if self.allow_widening:
            raise ValueError('Stop widening cannot be declared by a TradeSetup')
        return self


class RejectionReason(Record):
    code: Text
    message: Text
    source: Text
    recorded_at: Timestamp | None = None


class TradeSetup(Record):
    schema_version: Literal['trade-setup/v1'] = 'trade-setup/v1'
    plan_version: Text
    setup_id: Text
    origin: Literal['native_plan', 'legacy_signal'] = 'native_plan'
    symbol: Annotated[str, Field(strict=True, pattern=r'^[A-Z0-9]{2,32}$')]
    strategy_name: Text
    strategy_type: StrategyType
    side: Literal['LONG', 'SHORT']
    structure_evidence: tuple[Evidence, ...] = ()
    entry: EntryPlan
    invalidation_conditions: tuple[InvalidationCondition, ...] = ()
    initial_stop: InitialStop
    targets: tuple[TargetLevel, ...] = ()  # Empty means not yet supplied, not no-TP permission.
    reward_risk: RewardRiskEstimates = Field(default_factory=RewardRiskEstimates)
    market_state: MarketSnapshot = Field(default_factory=MarketSnapshot)
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    grade: Literal['S', 'A', 'B', 'C'] | None = None
    grade_interpretation: Literal['policy_tier_not_win_probability'] = 'policy_tier_not_win_probability'
    confidence: ConfidenceEstimate = Field(default_factory=ConfidenceEstimate)
    position_limit_advice: PositionLimitAdvice = Field(default_factory=PositionLimitAdvice)
    risk_budget: RiskBudget = Field(default_factory=RiskBudget)
    cost_assumptions: CostAssumptions = Field(default_factory=CostAssumptions)
    data_coverage: DataCoverage = Field(default_factory=DataCoverage)
    stop_movement: StopMovementPlan = Field(default_factory=StopMovementPlan)
    created_at: Timestamp
    data_as_of: Timestamp | None = None
    valid_until: Timestamp | None = None  # None means unknown validity, never unlimited validity.
    rejection_reasons: tuple[RejectionReason, ...] = ()
    compatibility_notes: tuple[Text, ...] = ()
    admission_status: Literal['not_evaluated'] = 'not_evaluated'
    execution_authority: Literal['none'] = 'none'

    @model_validator(mode='after')
    def internally_consistent_description(self):
        if self.data_as_of is not None and self.data_as_of > self.created_at:
            raise ValueError('Data timestamp cannot be later than plan creation')
        if self.valid_until is not None and self.valid_until <= self.created_at:
            raise ValueError('Explicit validity must end after plan creation')
        cutoff = self.data_as_of if self.data_as_of is not None else self.created_at
        stamps = (self.market_state.observed_at, self.cost_assumptions.observed_at,
                  *(item.observed_at for item in self.structure_evidence),
                  *(item.observed_at for item in self.data_coverage.items))
        if any(stamp is not None and stamp > cutoff for stamp in stamps):
            raise ValueError('Evidence cannot be newer than the declared data cutoff')
        direction = 1 if self.side == 'LONG' else -1
        edge = self.entry.lower_price if direction == 1 else self.entry.upper_price
        if (edge - self.initial_stop.price) * direction <= 0:
            raise ValueError('Initial stop must be outside the entry zone on the loss side')
        ids = [item.target_id for item in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate target ID')
        if self.targets and abs(sum(item.fraction for item in self.targets) - 1) > 1e-9:
            raise ValueError('Provided target fractions must sum to one')
        edge = self.entry.upper_price if direction == 1 else self.entry.lower_price
        for target in self.targets:
            if (target.price - edge) * direction <= 0:
                raise ValueError('Targets must be beyond the entry zone and ordered toward profit')
            edge = target.price
            if target.kind == 'structure' and not target.evidence_ids:
                raise ValueError('A structure target must reference its evidence')
        evidence_ids = [item.evidence_id for item in self.structure_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError('Duplicate evidence ID')
        references = (*self.entry.evidence_ids, *self.initial_stop.evidence_ids,
                      *(key for item in self.targets for key in item.evidence_ids),
                      *(key for item in self.invalidation_conditions for key in item.evidence_ids),
                      *(key for item in self.score.components for key in item.evidence_ids))
        if set(references) - set(evidence_ids):
            raise ValueError('Evidence reference is absent from the plan')
        if self.entry.basis == 'structure_zone' and not self.entry.evidence_ids:
            raise ValueError('A structure entry zone must reference its evidence')
        condition_ids = [item.condition_id for item in self.invalidation_conditions]
        if len(condition_ids) != len(set(condition_ids)):
            raise ValueError('Duplicate invalidation condition ID')
        triggers = [rule.after_target_id for rule in self.stop_movement.rules]
        if len(triggers) != len(set(triggers)):
            raise ValueError('Ambiguous duplicate stop movement trigger')
        for rule in self.stop_movement.rules:
            if rule.after_target_id not in ids:
                raise ValueError('Stop movement references an absent target')
        return self
