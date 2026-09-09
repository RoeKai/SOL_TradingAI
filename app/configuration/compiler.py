"""Compile explicit source texts; no files, secrets, account state or runtime."""

from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import json
import yaml

from pydantic import ValidationError
from app.admission.policy import AdmissionPolicy
from app.exits.policy import ExitPolicy

from .encoding import canonical, content_hash, flatten, parse_text, text_hash
from .models import (BundleManifest, ComparableLimit, ConfigBundle, ConfigCompilation,
    ConfigurationError, ContractIssue, FreshnessRule, ParameterSource, SourceSnapshot)
from .registry import POLICY_UNITS, TIME_FIELDS, field_unit, prepare_main


def issue(code, path, source, actual, expected, suggestion, severity='ERROR'):
    return ContractIssue(reason_code=code,severity=severity,field_path=path,source=source,
        actual=canonical(actual),expected=expected,suggestion=suggestion)


def unit_convert(value, source, target):
    """Only dimensionless rates, fractions, percent points and bps interconvert."""
    from .models import decimal_input
    factors = {'fraction':Decimal(1),'fee_rate':Decimal(1),'percent_points':Decimal(100),'bps':Decimal(10000)}
    if source not in factors or target not in factors:
        raise ConfigurationError('Cannot convert amount, quantity, margin, leverage or time as a ratio')
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)):
        return decimal_input(value)*factors[target]/factors[source]


def policy_from(bundle, name):
    source = next(s for s in bundle.sources if s.name == name)
    model = {'admission':AdmissionPolicy,'exit':ExitPolicy}[name]
    return model.model_validate_json(source.effective_json)


def main_values(bundle):
    # Fresh mutable view for the caller; the bundle stores only immutable text.
    return json.loads(next(s for s in bundle.sources if s.name=='main').effective_json)


def _strict_numeric_policy(raw, validated=None):
    """Retain Decimal inputs and refuse lossy legacy float representations.

    Accepted policies still own their ranges/defaults. This boundary adds no
    trading rule; it rejects bool, nonfinite and rounded declarations rather
    than silently feeding another effective amount or TTL to those policies.
    """
    from .models import decimal_input
    values=None if validated is None else dict(flatten(validated))
    for path,value in flatten(raw):
        if path.split('.')[-1] not in set(POLICY_UNITS)|TIME_FIELDS: continue
        number=decimal_input(value)
        if values is not None and path in values and number!=decimal_input(values[path]):
            raise ConfigurationError('LEGACY_NUMERIC_REPRESENTATION_UNSUPPORTED: '+path)


def _schema_constraint(schema, path):
    node=schema
    def resolve(value):
        while '$ref' in value:
            value=schema['$defs'][value['$ref'].split('/')[-1]]
        return value
    for part in path.split('.'):
        node=resolve(node)
        node=node.get('items',{}) if part.isdigit() else node.get('properties',{}).get(part,{})
    node=resolve(node)
    return canonical({k:v for k,v in node.items() if k not in ('title','default','description')})+'; plus accepted model cross-field validators'


def _trace(name, raw, effective, raw_digest, provenance=None):
    supplied = dict(flatten(raw))
    records = []
    schema=None if name=='main' else {'admission':AdmissionPolicy,'exit':ExitPolicy,'manifest':BundleManifest}[name].model_json_schema()
    for path,value in flatten(effective):
        if name=='main' and path.startswith('symbols.'): continue
        if name=='main':
            origin,default,spec = provenance[path]
            unit,authority,constraint = spec.unit,spec.authority,spec.constraint
            category = ('strategy_policy' if path.startswith('strategies.') else
                        'configured_hard_ceiling' if path.startswith('risk.') else 'main_declaration')
            override = 'explicit field or documented original default; legacy aliases must agree after sign conversion'
        else:
            origin,default = path,path not in supplied
            unit = field_unit(path,value)
            authority = {'admission':'app/admission/policy.py','exit':'app/exits/policy.py',
                         'manifest':'config-catalog/v1 + accepted module versions'}[name]
            category = {'admission':'admission_policy','exit':'position_exit_policy','manifest':'scope_and_versions'}[name]
            constraint = _schema_constraint(schema,path)
            override = 'explicit file or documented model default; no host/environment/last-writer override'
        records.append(ParameterSource(path=name+'.'+path,value_json=canonical(value),unit=unit,
            category=category,authority=authority,source=name,source_path=origin,source_digest=raw_digest,
            default_applied=default,constraint=constraint,override_rule=override))
    if name=='main':
        origin,default,spec = provenance['symbols']
        records.append(ParameterSource(path='main.symbols',value_json=canonical(effective['symbols']),unit='symbol_set',
            category='main_declaration',authority=spec.authority,source=name,source_path=origin,
            source_digest=raw_digest,default_applied=default,constraint=spec.constraint,override_rule='exact symbol set, no duplicates'))
    return records


def _freshness(main, admission, exit_policy, manifest):
    rows = [
        ('main.market',main['data']['stale_after_seconds'],'original RiskEngine',('original_market_readiness',)),
        ('market',admission.max_market_age_seconds,'Admission/plan assembly',('plan','admission')),
        ('structure',admission.max_structure_age_seconds,'structure provider/Admission',('structure','admission')),
        ('account',admission.max_account_age_seconds,'trusted ledger/locked recheck',('admission','risk_reservation')),
        ('exchange',admission.max_exchange_age_seconds,'trusted venue rules/locked recheck',('sizing','admission')),
        ('costs',admission.max_cost_age_seconds,'cost assumptions/Admission',('net_rr_applicability','admission')),
        ('scorecard',admission.max_scorecard_age_seconds,'Admission result age check',('admission',)),
        ('score_inputs',manifest.score_context_max_age_seconds,'unchanged descriptive rubric',('scorecard',)),
        ('admission',admission.decision_ttl_seconds,'future locked consumer',('new_entry_eligibility',)),
        ('exit.market',exit_policy.market_max_age_seconds,'original bound ExitPolicy',('new_price_exit_proposal',)),
        ('exit.evidence',exit_policy.evidence_max_age_seconds,'original bound ExitPolicy',('new_runner_proposal',)),
    ]
    return tuple(FreshnessRule(field_path=p,ttl_seconds=ttl,owner=owner,invalidates=invalidates)
                 for p,ttl,owner,invalidates in rows)


def _compile_bundle(*, main_text: str, admission_text: str, exit_text: str, manifest_text: str) -> ConfigCompilation:
    texts = dict(main=main_text,admission=admission_text,exit=exit_text,manifest=manifest_text)
    parsed, effective, snapshots, traces, issues = {}, {}, [], [], []
    provenance = None
    for name,text in texts.items():
        try:
            raw = parse_text(text)
            parsed[name] = raw
            if name=='main':
                effective[name],provenance = prepare_main(raw)
            else:
                required = {'admission':('policy_version','mode'),'exit':('version','mode'),
                            'manifest':('live_allowed',)}[name]
                if any(k not in raw for k in required): raise ConfigurationError('Critical version/mode/safety field missing')
                if name=='manifest' and raw.get('live_allowed') is not False:
                    raise ConfigurationError('LIVE_HARD_DISABLED')
                model = {'admission':AdmissionPolicy,'exit':ExitPolicy,'manifest':BundleManifest}[name]
                _strict_numeric_policy(raw)
                if name=='exit':
                    from .models import decimal_input
                    with localcontext(Context(prec=2200)):
                        total=sum((decimal_input(raw.get(k,v)) for k,v in
                            (('tp1_fraction','.3'),('tp2_fraction','.4'),('runner_fraction','.3'))),Decimal(0))
                    if total!=1: raise ConfigurationError('Exit fractions must sum exactly to one, without rounding')
                obj = model.model_validate(raw)
                _strict_numeric_policy(raw,obj.model_dump())
                effective[name] = obj.model_dump(mode='json')
                if name=='admission' and obj.policy_version!='paper-risk-admission/v1': raise ConfigurationError('Unsupported admission version')
                if name=='exit' and obj.version!='paper-exit-policy/v2': raise ConfigurationError('Unsupported exit version')
            digest = text_hash(text)
            snapshot = SourceSnapshot(name=name,raw_text=text,raw_digest=digest,effective_json=canonical(effective[name]),
                                      effective_digest=content_hash(effective[name]))
            snapshots.append(snapshot)
            traces.extend(_trace(name,raw,effective[name],digest,provenance if name=='main' else None))
        except (ValueError,TypeError,ArithmeticError,RecursionError,yaml.YAMLError) as error:
            # Never include an arbitrary offending input value/credential in diagnostics.
            if isinstance(error,ValidationError):
                details = ', '.join('.'.join(map(str,e['loc']))+':'+e['type'] for e in error.errors(include_input=False))
            elif isinstance(error,ConfigurationError):
                details = str(error)
            else:
                details = type(error).__name__
            issues.append(issue('CONFIG_INPUT_INVALID',name,name,'rejected; raw values omitted',
                'Explicit supported schema, finite types and safety contract',details))
    if len(snapshots)!=4:
        return ConfigCompilation(parsing='FAIL',consistency='NOT_EVALUATED',bundle=None,issues=tuple(issues))
    main=effective['main']
    admission=AdmissionPolicy.model_validate(effective['admission'])
    exit_policy=ExitPolicy.model_validate(effective['exit'])
    manifest=BundleManifest.model_validate(effective['manifest'])
    for path,actual,expected in (
        ('instance_id',main['instance_id'],manifest.instance_id),
        ('trade_symbol',main['trade_symbol'],manifest.trade_symbol),
        ('admission.allowed_symbols',admission.allowed_symbols,(manifest.trade_symbol,)),
        ('admission.allowed_exchanges',admission.allowed_exchanges,(manifest.exchange,)),
        ('admission.policy_version',admission.policy_version,manifest.admission_policy_version),
        ('exit.version',exit_policy.version,manifest.exit_policy_version)):
        if actual!=expected:
            issues.append(issue('SCOPE_OR_VERSION_CONFLICT',path,'explicit module declarations',actual,str(expected),
                                'Choose consistent explicit scope; do not overwrite the previous declaration'))
    limits=[]
    for key,main_key,unit in (
        ('max_loss_per_trade_usdt','max_loss_per_trade','USDT_loss_budget'),
        ('max_leverage','max_leverage','leverage_multiple'),
        ('max_margin_ratio','max_margin_ratio','equity_fraction'),
        ('max_positions','max_positions','count')):
        a,b=Decimal(str(main['risk'][main_key])),Decimal(str(getattr(admission,key)))
        limits.append(ComparableLimit(semantic=key,unit=unit,effective_value=min(a,b),
            sources=('main.risk.'+main_key,'admission.'+key),rule='minimum_comparable_configured_ceiling'))
        if a!=b:
            issues.append(issue('STRICTER_COMPARABLE_CEILING',key,'main + admission',(a,b),str(min(a,b)),
                                'Keep both origins; future account limits may be stricter',severity='WARNING'))
    if Decimal(str(main['execution']['leverage']))>next(l.effective_value for l in limits if l.semantic=='max_leverage'):
        issues.append(issue('EXECUTION_LEVERAGE_EXCEEDS_EFFECTIVE_CAP','main.execution.leverage','main',
                            main['execution']['leverage'],'<= both configured ceilings','Lower the declared request, never raise leverage automatically'))
    if admission.minimum_risk_budget_usdt>next(l.effective_value for l in limits if l.semantic=='max_loss_per_trade_usdt'):
        issues.append(issue('MINIMUM_BUDGET_EXCEEDS_CEILING','admission.minimum_risk_budget_usdt','admission',
                            admission.minimum_risk_budget_usdt,'<= effective single-trade ceiling','Use coherent policies, not a relaxed account limit'))
    for name,settings in main['strategies'].items():
        if settings['enabled'] and settings['order_type']!=main['execution']['entry_order_type']:
            issues.append(issue('STRATEGY_ORDER_RULE_SCOPE_CONFLICT','main.strategies.'+name+'.order_type','main',
                settings['order_type'],main['execution']['entry_order_type'],'Original rules lookup uses execution order type; do not assemble mismatched rule scope'))
    if exit_policy.max_holding_seconds>Decimal(str(admission.max_holding_assumption_seconds)):
        issues.append(issue('EXIT_HORIZON_EXCEEDS_COST_POLICY','exit.max_holding_seconds','exit + admission',
            exit_policy.max_holding_seconds,'<= maximum supported funding assumption horizon','Require a new supported horizon; do not fill funding with zero'))
    for leaf,value,floor in (
        ('expected_exit_fee_rate',exit_policy.expected_exit_fee_rate,max(admission.minimum_exit_fee_rate,Decimal(str(main['risk']['taker_fee_rate'])))),
        ('expected_exit_slippage_bps',exit_policy.expected_exit_slippage_bps,max(admission.minimum_exit_slippage_bps,Decimal(str(main['risk']['slippage_bps']))))):
        if value<floor:
            issues.append(issue('EXIT_COST_COVER_UNDERBUDGETED','exit.'+leaf,'exit + admission',value,str(floor),
                                'Cost-covered protection cannot budget below the declared exit-cost floor'))
    if Decimal(str(main['risk']['slippage_bps']))>min(admission.maximum_entry_slippage_bps,admission.maximum_exit_slippage_bps):
        issues.append(issue('COST_BUDGET_OUTSIDE_POLICY','main.risk.slippage_bps','main + admission',
            main['risk']['slippage_bps'],'within declared Admission cost interval','Do not silently reduce slippage estimates'))
    if exit_policy.expected_exit_slippage_bps>admission.maximum_exit_slippage_bps:
        issues.append(issue('EXIT_COST_OUTSIDE_ADMISSION_RANGE','exit.expected_exit_slippage_bps','exit + admission',
            exit_policy.expected_exit_slippage_bps,str(admission.maximum_exit_slippage_bps),
            'No compatible plan can budget above the exit floor and below this Admission ceiling'))
    if exit_policy.runner_trail_r>exit_policy.runner_activation_r:
        issues.append(issue('RUNNER_TRAIL_LARGER_THAN_ACTIVATION','exit.runner_trail_r','exit',
            exit_policy.runner_trail_r,'no new widening; old engine monotonic protection remains authoritative',
            'Review intended trailing distance; this warning is not a change to the accepted algorithm',severity='WARNING'))
    for tier in admission.tiers:
        for market in admission.markets:
            floor=max(admission.minimum_net_rr,tier.minimum_net_rr,market.minimum_net_rr)
            traces.append(ParameterSource(path='derived.minimum_net_rr.'+tier.name+'.'+market.regime,
                value_json=canonical(floor),unit='net_R_multiple',category='admission_policy',authority='app/admission/engine.py',
                source='admission',source_path='minimum_net_rr + tiers.'+tier.name+' + markets.'+market.regime,
                source_digest=text_hash(admission_text),default_applied=False,constraint='maximum of three RR floors',
                override_rule='score never lowers the global net RR floor; disabled market stays disabled'))
    btc=max(Decimal(str(main['risk']['btc_crash_pct'])),Decimal(str(admission.btc_crash_3m_pct)))
    traces.append(ParameterSource(path='derived.btc_long_block_at_or_below_pct',value_json=canonical(btc),
        unit='percent_points',category='configured_hard_ceiling',authority='app/risk/engine.py + app/admission/engine.py',
        source='main',source_path='risk.btc_crash_pct + admission.btc_crash_3m_pct',source_digest=text_hash(main_text),
        default_applied=False,constraint='LONG only; return_3m_pct <= max(two negative thresholds)',
        override_rule='compose the same <= predicates using max; both sources retained in bundle; not a mechanical numeric min'))
    if Decimal(str(main['risk']['btc_crash_pct']))!=Decimal(str(admission.btc_crash_3m_pct)):
        issues.append(issue('STRICTER_BTC_THRESHOLD','derived.btc_long_block_at_or_below_pct','main + admission',
            (main['risk']['btc_crash_pct'],admission.btc_crash_3m_pct),str(btc),
            'Thresholds are negative percent points; compose predicates, never overwrite strategy-specific filters',severity='WARNING'))
    issues.append(issue('DISTINCT_DAILY_ACCOUNTING_NOT_MERGED','main.risk + admission','original models',
        (main['risk']['daily_loss_limit'],admission.daily_loss_limit_usdt),'net cash day vs cumulative losing outcomes',
        'Future provider must supply both metrics, matching risk-day boundaries and pending-entry accounting',severity='WARNING'))
    bundle=ConfigBundle(manifest=manifest,sources=tuple(snapshots),parameters=tuple(sorted(traces,key=lambda p:p.path)),
        comparable_limits=tuple(limits),freshness_rules=_freshness(main,admission,exit_policy,manifest),bundle_digest='0'*64)
    bundle=bundle.model_copy(update={'bundle_digest':content_hash(bundle.model_dump(mode='json',exclude={'bundle_digest'}))})
    return ConfigCompilation(parsing='PASS',consistency='FAIL' if any(i.severity=='ERROR' for i in issues) else 'PASS',
                             bundle=bundle,issues=tuple(issues))


def compile_bundle(*, main_text: str, admission_text: str, exit_text: str, manifest_text: str) -> ConfigCompilation:
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN,Emin=-999999,Emax=999999)):
        return _compile_bundle(main_text=main_text,admission_text=admission_text,exit_text=exit_text,manifest_text=manifest_text)


def verify_bundle(bundle):
    if type(bundle) is not ConfigBundle: raise ConfigurationError('Exact ConfigBundle required')
    texts={s.name:s.raw_text for s in bundle.sources}
    if len(texts)!=4 or len(bundle.sources)!=4: raise ConfigurationError('Missing/duplicate bundle source')
    actual=compile_bundle(**{k+'_text':v for k,v in texts.items()})
    if actual.bundle!=bundle: raise ConfigurationError('Bundle content/provenance/digest differs from strict recompilation')
    return actual
