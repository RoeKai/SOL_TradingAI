"""Immutable descriptions, never credentials, trusted-state proofs or permission."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StrictBool, StrictInt
from app.setups.models import Record, Text


def decimal_input(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError('Explicit finite decimal required; bool is not a number')
    number = Decimal(str(value))
    if not number.is_finite() or abs(number.as_tuple().exponent) > 1000 or len(number.as_tuple().digits)>50:
        raise ValueError('Nonfinite or excessive decimal exponent')
    return number


Number = Annotated[Decimal, BeforeValidator(decimal_input)]
Positive = Annotated[Number, Field(gt=0)]
Timestamp = Annotated[Number, Field(ge=0)]
Digest = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
Status = Literal['PASS', 'FAIL', 'INCOMPLETE', 'UNSUPPORTED', 'NOT_EVALUATED']


class BundleManifest(Record):
    schema_version: Literal['config-bundle/v1']
    bundle_revision: Text
    catalog_version: Literal['config-catalog/v1']
    main_contract_version: Literal['main-config-contract/v1']
    setup_schema_version: Literal['trade-setup/v1']
    rr_calculation_version: Literal['linear-usdt-rr/v1']
    scorecard_schema_version: Literal['trade-scorecard/v1']
    scoring_rule_version: Literal['plan-quality/v1']
    admission_policy_version: Literal['paper-risk-admission/v1']
    exit_policy_version: Literal['paper-exit-policy/v2']
    exit_state_version: Literal['position-exit/v2']
    exit_checkpoint_version: Literal['exit-checkpoint/v2']
    mode: Literal['paper_only']
    instance_id: Annotated[str, Field(strict=True, pattern=r'^[a-z][a-z0-9_-]{2,47}$')]
    exchange: Literal['BINANCE_USDT_M']
    contract_type: Literal['linear_usdt']
    trade_symbol: Literal['SOLUSDT']
    quote_currency: Literal['USDT']
    score_context_max_age_seconds: Positive = '300'
    live_allowed: Literal[False] = False


class ContractIssue(Record):
    reason_code: Text
    severity: Literal['ERROR', 'WARNING', 'PREREQUISITE', 'UNSUPPORTED']
    field_path: Text
    source: Text
    actual: str
    expected: Text
    suggestion: Text


class ParameterSource(Record):
    path: Text
    value_json: Text
    unit: Text
    category: Text
    authority: Text
    source: Text
    source_path: Text
    source_digest: Digest
    default_applied: StrictBool
    constraint: Text
    override_rule: Text


class SourceSnapshot(Record):
    name: Literal['main', 'admission', 'exit', 'manifest']
    raw_text: str
    raw_digest: Digest
    effective_json: Annotated[str, Field(strict=True, max_length=1000000)]
    effective_digest: Digest


class ComparableLimit(Record):
    semantic: Text
    unit: Text
    effective_value: Number
    sources: tuple[Text, ...]
    rule: Literal['minimum_comparable_configured_ceiling']
    authority: Literal['configured_ceiling_not_confirmed_account_limit'] = 'configured_ceiling_not_confirmed_account_limit'


class FreshnessRule(Record):
    field_path: Text
    ttl_seconds: Positive
    owner: Text
    invalidates: tuple[Text, ...]


class ConfigBundle(Record):
    schema_version: Literal['config-bundle/v1'] = 'config-bundle/v1'
    manifest: BundleManifest
    sources: tuple[SourceSnapshot, ...]
    parameters: tuple[ParameterSource, ...]
    comparable_limits: tuple[ComparableLimit, ...]
    freshness_rules: tuple[FreshnessRule, ...]
    bundle_digest: Digest
    mode: Literal['paper_only'] = 'paper_only'
    execution_authority: Literal['none'] = 'none'
    live_allowed: Literal[False] = False


class ConfigCompilation(Record):
    parsing: Status
    consistency: Status
    bundle: ConfigBundle | None
    issues: tuple[ContractIssue, ...]


class PlanConfigurationBinding(Record):
    """Declared BEFORE approval; content binding is NOT authenticated lineage."""
    schema_version: Literal['plan-config-binding/v1'] = 'plan-config-binding/v1'
    bundle_digest: Digest
    instance_id: Text
    setup_digest: Digest
    rr_digest: Digest
    scorecard_digest: Digest | None
    admission_policy_digest: Digest
    exit_policy_digest: Digest
    declared_at: Timestamp
    authority: Literal['declared_content_only_not_signature'] = 'declared_content_only_not_signature'


class FreshnessObservation(Record):
    field_path: Text
    status: Status
    observed_at: Timestamp | None
    ttl_seconds: Positive
    expires_at: Timestamp | None
    invalidates: tuple[Text, ...]


class ExitComparison(Record):
    reference_entry: Positive
    initial_stop: Positive
    reference_initial_r: Positive
    tp_trigger_prices: tuple[Number, Number]
    original_targets: tuple[tuple[Text, Number, Number], ...]
    proposed_allocations: tuple[Number, Number, Number]
    runner_activation_price: Number
    runner_exit_price: None = None
    full_policy_net_rr: None = None
    valuation: Literal['UNSUPPORTED'] = 'UNSUPPORTED'
    interpretation: Literal['reference_geometry_not_actual_fills_or_expected_return'] = 'reference_geometry_not_actual_fills_or_expected_return'


class ContractValidationResult(Record):
    schema_version: Literal['config-contract-result/v1'] = 'config-contract-result/v1'
    bundle_digest: Digest | None
    config_parsing: Status
    config_consistency: Status
    plan_consistency: Status
    runtime_metadata: Status
    runtime_trust: Literal['INCOMPLETE'] = 'INCOMPLETE'
    execution: Literal['NOT_INTEGRATED'] = 'NOT_INTEGRATED'
    issues: tuple[ContractIssue, ...]
    freshness: tuple[FreshnessObservation, ...] = ()
    exit_comparison: ExitComparison | None = None
    evaluated_at: Timestamp | None = None
    execution_authority: Literal['none'] = 'none'
    admission_replacement: Literal[False] = False
    live_allowed: Literal[False] = False
    existing_position_rule: Literal['continue_original_bound_policy_independent_of_new_config'] = 'continue_original_bound_policy_independent_of_new_config'


class ConfigurationError(ValueError):
    pass
