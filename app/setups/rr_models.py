"""Stage 3 arithmetic results, not amended plans or execution permissions.

Decimal values serialize as JSON strings to retain calculation precision.
Results are immutable and carry no risk approval or executable order quantity.
"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from .models import CostAssumptions, Positive, Record, Text, Timestamp

FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
ScenarioName = Literal['reference', 'lower', 'upper']


class CalculationQuantity(Record):
    """Explicit hypothetical BASE-asset quantity, never taken from advice."""
    quantity: Positive | None = None


class RRIssue(Record):
    code: Text
    scope: Literal['inputs', 'reference', 'lower', 'upper'] = 'inputs'
    message: Text
    fields: tuple[Text, ...] = ()


class RRCostBreakdown(Record):
    """USDT cost per one base unit; funding may be a signed credit."""
    entry_fee_per_unit: FiniteDecimal | None = None
    exit_fee_per_unit: FiniteDecimal | None = None
    entry_slippage_per_unit: FiniteDecimal | None = None
    exit_slippage_per_unit: FiniteDecimal | None = None
    funding_per_unit: FiniteDecimal | None = None
    total_per_unit: FiniteDecimal | None = None


class RRTargetResult(Record):
    target_id: Text
    target_price: FiniteDecimal
    fraction: FiniteDecimal  # As supplied, not silently normalized.
    target_kind: Literal['structure', 'legacy_unspecified', 'unverified']
    gross_reward_per_unit: FiniteDecimal
    gross_rr: FiniteDecimal
    weighted_gross_rr: FiniteDecimal
    effective_exit_price: FiniteDecimal | None
    costs: RRCostBreakdown
    net_reward_per_unit: FiniteDecimal | None
    net_rr: FiniteDecimal | None
    weighted_net_rr: FiniteDecimal | None
    hypothetical_quantity: FiniteDecimal | None
    gross_pnl_usdt: FiniteDecimal | None
    net_pnl_usdt: FiniteDecimal | None


class RRScenario(Record):
    name: ScenarioName
    entry_price: FiniteDecimal
    initial_stop_price: FiniteDecimal
    effective_entry_price: FiniteDecimal | None
    effective_stop_price: FiniteDecimal | None
    gross_risk_per_unit: FiniteDecimal
    stop_costs: RRCostBreakdown
    net_stop_loss_per_unit: FiniteDecimal | None
    gross_risk_usdt: FiniteDecimal | None
    net_stop_loss_usdt: FiniteDecimal | None
    targets: tuple[RRTargetResult, ...]
    weighted_gross_reward_per_unit: FiniteDecimal | None
    weighted_net_reward_per_unit: FiniteDecimal | None
    gross_rr: FiniteDecimal | None
    net_rr: FiniteDecimal | None
    gross_pnl_usdt: FiniteDecimal | None
    net_pnl_usdt: FiniteDecimal | None


class RRCalculation(Record):
    calculation_version: Literal['linear-usdt-rr/v1'] = 'linear-usdt-rr/v1'
    setup_id: Text
    plan_version: Text
    symbol: Text
    side: Literal['LONG', 'SHORT']
    status: Literal['complete', 'partial', 'unavailable']
    hypothetical_quantity: FiniteDecimal | None
    fraction_sum: FiniteDecimal
    cost_assumptions: CostAssumptions
    reference: RRScenario | None
    entry_lower: RRScenario | None
    entry_upper: RRScenario | None
    adverse_entry: Literal['lower', 'upper']
    issues: tuple[RRIssue, ...] = ()
    input_missing_items: tuple[Text, ...]
    data_as_of: Timestamp | None
    valid_until: Timestamp | None
    quantity_authority: Literal['hypothetical_only_not_order_quantity'] = 'hypothetical_only_not_order_quantity'
    weights_policy: Literal['as_supplied_no_normalization'] = 'as_supplied_no_normalization'
    net_rr_definition: Literal['net_reward_over_cost_adjusted_initial_stop_loss'] = 'net_reward_over_cost_adjusted_initial_stop_loss'
    verification: Literal['arithmetic_only_not_market_verified'] = 'arithmetic_only_not_market_verified'
    admission_status: Literal['not_evaluated'] = 'not_evaluated'
    execution_authority: Literal['none'] = 'none'
    limitations: tuple[Text, ...] = (
        'Linear USDT-quoted contracts only; not inverse or quanto contracts',
        'Supplied stop and targets are not authenticated, generated or adjusted',
        'All targets reached is a scenario, not expected value or a win probability',
        'Initial stop only; partial-exit paths and stop movement are not simulated',
        'One supplied funding total is reused for stop and target scenarios, allocated pro rata',
        'No order-book impact, price gaps, liquidation, contract rounding or account state model',
        'Input data freshness, evidence, score, grade and risk admission are not evaluated',
    )
