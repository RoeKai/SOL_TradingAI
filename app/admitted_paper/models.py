"""Versioned synthetic contracts, never network/account/order capabilities."""
from typing import Literal
from decimal import Decimal
from pydantic import Field, model_validator
from app.setups.models import Record, Text, TradeSetup
from app.exits.models import Positive, Amount, Signed, Digest, ExitVenueRules
from app.exits.policy import ExitPolicy
from app.offline_paper.models import RunSettings

PROVIDER='synthetic-swing-provider/v1'
FUNDING='synthetic-no-funding/v1'


class AdmittedSettings(RunSettings):
    application: Literal['sol-admitted-paper/8b']='sol-admitted-paper/8b'
    schema_version: Literal[2]=2


class MarketInput(Record):
    scope: Literal['SYNTHETIC_OFFLINE']='SYNTHETIC_OFFLINE'
    sequence: int=Field(strict=True,ge=1)
    observed_at: int=Field(strict=True,ge=0)
    available_at: int=Field(strict=True,ge=0)
    sol: Positive
    btc: Positive
    eth: Positive
    volume: Positive

    @model_validator(mode='after')
    def chronology(self):
        if self.observed_at>self.available_at: raise ValueError('Observation cannot be available before it occurs')
        return self


class EvidenceSnapshot(Record):
    version: Literal['paper-evidence/v1']='paper-evidence/v1'
    scope: Literal['SYNTHETIC_OFFLINE']='SYNTHETIC_OFFLINE'
    provider: Literal['synthetic-swing-provider/v1']=PROVIDER
    instance_id: Text
    input_digest: Digest
    input_count: int=Field(strict=True,gt=0)
    last_sequence: int=Field(strict=True,gt=0)
    observed_at: int=Field(strict=True,ge=0)
    available_at: int=Field(strict=True,ge=0)
    evaluated_at: int=Field(strict=True,ge=0)
    expires_at: int=Field(strict=True,ge=0)
    funding_model: Literal['synthetic-no-funding/v1']=FUNDING
    funding_basis: Literal['this_simulated_contract_has_no_periodic_funding']='this_simulated_contract_has_no_periodic_funding'
    funding_horizon_seconds: int=Field(default=3600,strict=True,gt=0)
    direction_checks: tuple[tuple[Text,bool],...]
    structure_sequences: tuple[tuple[Text,int,int,int],...]


class Candidate(Record):
    version: Literal['paper-candidate/v1']='paper-candidate/v1'
    candidate_id: Digest
    setup: TradeSetup
    evidence: EvidenceSnapshot
    bundle_digest: Digest


class Trigger(Record):
    name: Literal['TP1','TP2']
    r_multiple: Positive
    original_fraction: Positive
    reference_price: Positive
    corridor_evidence_ids: tuple[Text,...]
    mapping: Literal['risk_trigger_inside_observed_structure_corridor']='risk_trigger_inside_observed_structure_corridor'


class ExitPlan(Record):
    schema_version: Literal['paper-exit-plan/v1']='paper-exit-plan/v1'
    plan_id: Digest
    plan_version: Literal['1']='1'
    setup_digest: Digest
    evidence_digest: Digest
    bundle_digest: Digest
    symbol: Literal['SOLUSDT']='SOLUSDT'
    side: Literal['LONG','SHORT']
    entry_method: Literal['MARKET']='MARKET'
    reference_entry: Positive
    entry_lower: Positive
    entry_upper: Positive
    geometry: Literal['pre_fill_reference_not_frozen_r']='pre_fill_reference_not_frozen_r'
    initial_stop: Positive
    stop_evidence_ids: tuple[Text,...]
    triggers: tuple[Trigger,...]
    runner_fraction: Positive
    runner_reference_evidence_id: Text | None
    runner_reference_price: Positive | None
    policy: ExitPolicy
    policy_digest: Digest
    rules: ExitVenueRules
    rules_digest: Digest
    costs: tuple[tuple[Text,Decimal],...]
    funding_model: Literal['synthetic-no-funding/v1']=FUNDING
    holding_seconds: int=Field(strict=True,gt=0)
    evidence_version: Literal['paper-evidence/v1']='paper-evidence/v1'
    created_at: int=Field(strict=True,ge=0)
    valid_until: int=Field(strict=True,ge=0)


class ScenarioLeg(Record):
    action: Text
    quantity: Positive
    quote: Positive
    fill_price: Positive
    gross_cash_flow: Signed
    fee_usdt: Amount
    occurred_at: int=Field(strict=True,ge=0)
    event_order: int=Field(strict=True,ge=1)


class Scenario(Record):
    scenario_id: Literal['S0','S1','S2','S3']
    status: Literal['SUPPORTED','UNAVAILABLE','UNSUPPORTED']
    assumptions: tuple[Text,...]
    reason_codes: tuple[Text,...]=()
    quantity: Positive
    entry_quote: Positive
    legs: tuple[ScenarioLeg,...]=()
    gross_pnl: Signed | None=None
    fees_usdt: Amount | None=None
    net_pnl: Signed | None=None
    initial_stop_risk_usdt: Amount | None=None
    scenario_net_rr: Signed | None=None
    frozen_initial_r: Positive | None=None
    final_stop: Positive | None=None
    reference_structure_price: Positive | None=None


class ScenarioEvaluation(Record):
    version: Literal['conditional-exit-scenarios/v1']='conditional-exit-scenarios/v1'
    scope: Literal['SYNTHETIC_OFFLINE']='SYNTHETIC_OFFLINE'
    evaluation_id: Digest
    exit_plan_digest: Digest
    setup_digest: Digest
    quantity: Positive
    conservative_initial_risk_usdt: Amount
    scenarios: tuple[Scenario,...]
    static_rr_digest: Digest
    full_policy_expected_return: None=None
    win_probability: None=None
    limitations: tuple[Text,...]
