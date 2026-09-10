"""Historical observations, simulated accounts and assumed costs are distinct."""
from decimal import Decimal as D
from typing import Literal
from pydantic import Field
from app.setups.models import Record, TradeSetup
from app.offline_paper.models import RunSettings
from app.admitted_paper.models import ExitPlan, ScenarioEvaluation


class HistoricalSettings(RunSettings):
    application: Literal['historical-paper/8c']='historical-paper/8c'
    schema_version: Literal[3]=3
    mode: Literal['historical_offline']='historical_offline'
    allow_fixtures: Literal[False]=False


class ExecutionModel(Record):
    version: Literal['trade-price-spread-participation/v1']='trade-price-spread-participation/v1'
    quote_provenance: Literal['MODELLED_NOT_HISTORICAL_BOOK']='MODELLED_NOT_HISTORICAL_BOOK'
    rule_provenance: Literal['ASSUMED_NOT_HISTORICAL_EXCHANGE_RULES']='ASSUMED_NOT_HISTORICAL_EXCHANGE_RULES'
    spread_bps: D=Field(default=D('2'),gt=0,le=20,allow_inf_nan=False)
    slippage_bps: D=Field(default=D('10'),ge=10,le=30,allow_inf_nan=False)
    participation: D=Field(default=D('.01'),gt=0,le=D('.1'),allow_inf_nan=False)
    acceptance_delay_ms: int=Field(default=1000,strict=True,ge=1,le=4000)
    observation_delay_ms: int=Field(default=250,strict=True,ge=1,le=5000)
    fee_rate: D=Field(default=D('.0005'),ge=D('.0005'),le=D('.01'),allow_inf_nan=False)
    funding_rate_allowance: D=Field(default=D('.001'),gt=0,le=D('.01'),allow_inf_nan=False)
    funding_event_allowance: int=Field(default=2,strict=True,ge=1,le=24)
    funding_model: Literal['fixed-ex-ante-funding-budget/v1']='fixed-ex-ante-funding-budget/v1'
    sampling_seconds: Literal[15]=15
    structure_seconds: Literal[300]=300
    reference_age_seconds: Literal[15]=15
    same_time_order: Literal['FUNDING_BEFORE_MARKET_AND_SIMULATED_FILLS']='FUNDING_BEFORE_MARKET_AND_SIMULATED_FILLS'
    maintenance_model: Literal['UNSUPPORTED_STOP_AT_ISOLATED_MARGIN_EXHAUSTION']='UNSUPPORTED_STOP_AT_ISOLATED_MARGIN_EXHAUSTION'


class HistoricalCandidate(Record):
    version: Literal['historical-swing-candidate/v1']='historical-swing-candidate/v1'
    provenance: Literal['HISTORICAL_OBSERVATIONS_ASSUMED_EXECUTION','SYNTHETIC_TEST_OBSERVATIONS_ASSUMED_EXECUTION']='HISTORICAL_OBSERVATIONS_ASSUMED_EXECUTION'
    candidate_id: str
    setup: TradeSetup
    evidence: dict
    bundle_digest: str
    dataset_digest: str
    run_digest: str


class HistoricalExitPlan(ExitPlan):
    schema_version: Literal['historical-exit-plan/v1']='historical-exit-plan/v1'
    funding_model: Literal['fixed-ex-ante-funding-budget/v1']='fixed-ex-ante-funding-budget/v1'
    evidence_version: Literal['historical-visible-evidence/v1']='historical-visible-evidence/v1'
    dataset_digest: str
    run_digest: str
    spread_bps: D=Field(gt=0,allow_inf_nan=False)
    pre_entry_funding_budget_usdt: D=Field(gt=0,allow_inf_nan=False)


class HistoricalScenarios(ScenarioEvaluation):
    version: Literal['historical-conditional-scenarios/v1']='historical-conditional-scenarios/v1'
    scope: Literal['HISTORICAL_OBSERVATIONS_MODELLED_COSTS']='HISTORICAL_OBSERVATIONS_MODELLED_COSTS'
    pre_entry_funding_budget_usdt: D=Field(ge=0,allow_inf_nan=False)
