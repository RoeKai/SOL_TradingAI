"""Versioned price/cost records; never permissions or real venue claims."""
from decimal import Decimal as D
from typing import Literal
from pydantic import Field
from app.setups.models import Record
from app.offline_paper.models import RunSettings
from app.historical_replay.models import HistoricalExitPlan, HistoricalScenarios


class QuantifiedSettings(RunSettings):
    application: Literal['quantified-paper/8d']='quantified-paper/8d'
    schema_version: Literal[4]=4
    mode: Literal['quantified_historical_offline']='quantified_historical_offline'
    allow_fixtures: Literal[False]=False


class QuantificationPolicy(Record):
    version: Literal['bounded-lattice-quantification/v1']='bounded-lattice-quantification/v1'
    max_evaluations: int=Field(default=32,strict=True,gt=0,le=10000)
    adverse_quote_drift_bps: Literal[0]=0
    funding_rate_allowance: D=Field(default=D('.001'),ge=D('.001'),allow_inf_nan=False)
    funding_event_allowance: int=Field(default=2,strict=True,ge=2,le=24)
    fixed_fee_usdt: D=Field(default=D(0),ge=0,allow_inf_nan=False)
    fixed_fee_basis: Literal['DECLARED_MODEL_HAS_NO_PER_ORDER_FIXED_CHARGE']='DECLARED_MODEL_HAS_NO_PER_ORDER_FIXED_CHARGE'


class PriceContract(Record):
    version: Literal['execution-price-layers/v1']='execution-price-layers/v1'
    contract_id: str
    original_setup_digest: str
    side: Literal['LONG','SHORT']
    signal_reference_price: D
    market_trade_price: D
    executable_quote: D
    modeled_fill_price: D
    signal_at: float
    quote_at: float
    source_event_id: str
    available_at: float
    spread_bps: D
    slippage_bps: D
    price_tick: D
    fee_rate: D
    rounding: Literal['BUY_CEILING_SELL_FLOOR']='BUY_CEILING_SELL_FLOOR'
    provenance: Literal['MODELLED_TRADE_PRICE_PLUS_DECLARED_SPREAD']='MODELLED_TRADE_PRICE_PLUS_DECLARED_SPREAD'
    extra_adverse_drift_bps: Literal[0]=0
    source_digest: str
    model_digest: str


class CostFunction(Record):
    version: Literal['quantity-funding-budget/v1','fixed-ex-ante-funding-budget/v1']
    function_id: str
    fixed_usdt: D=Field(ge=0,allow_inf_nan=False)
    per_unit_usdt: D=Field(ge=0,allow_inf_nan=False)
    budget_price: D=Field(gt=0,allow_inf_nan=False)
    rate_allowance: D=Field(ge=D('.001'),allow_inf_nan=False)
    event_allowance: int=Field(strict=True,ge=2)
    price_basis: tuple[tuple[str,D],...]
    fixed_cost_basis: str
    known_at: float
    original_setup_digest: str
    price_contract_digest: str
    holding_seconds: int


class QuantifiedExitPlan(HistoricalExitPlan):
    schema_version: Literal['quantified-exit-plan/v1']='quantified-exit-plan/v1'
    funding_model: Literal['quantity-funding-budget/v1','fixed-ex-ante-funding-budget/v1']='quantity-funding-budget/v1'
    original_setup_digest: str
    price_contract_digest: str
    cost_function_digest: str
    materialized_quantity: D
    geometry_entry_price: D


class QuantifiedScenarios(HistoricalScenarios):
    version: Literal['quantity-consistent-scenarios/v1']='quantity-consistent-scenarios/v1'
    scope: Literal['OFFLINE_EXECUTION_PRICE_COST_MODEL']='OFFLINE_EXECUTION_PRICE_COST_MODEL'
