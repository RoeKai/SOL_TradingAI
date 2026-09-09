"""Stage 7 strict, deterministic source compiler; no runtime configuration IO."""

import builtins
from decimal import Decimal as D, Inexact, ROUND_UP, localcontext
import json
from pathlib import Path
import socket
import sqlite3
import time

import pytest
import yaml

from app.configuration.compiler import compile_bundle, main_values, policy_from, unit_convert, verify_bundle
from app.configuration.encoding import canonical, parse_text
from app.configuration.models import ConfigurationError
from app.configuration.registry import main_specs

ROOT=Path(__file__).resolve().parents[1]
FILES={'main':'config.yaml','admission':'admission.yaml','exit':'exit-policy.yaml','manifest':'configuration.yaml'}


def texts():
    return {key+'_text':(ROOT/file).read_text() for key,file in FILES.items()}


class Dumper(yaml.SafeDumper):
    pass


Dumper.add_representer(D,lambda dumper,value:dumper.represent_scalar('tag:yaml.org,2002:float',str(value)))


def changed(sources, module, path, value, *, remove=False):
    result=dict(sources)
    raw=parse_text(result[module+'_text'])
    bits=path.split('.')
    node=raw
    for bit in bits[:-1]: node=node[int(bit)] if isinstance(node,list) else node[bit]
    leaf=int(bits[-1]) if isinstance(node,list) else bits[-1]
    if remove: del node[leaf]
    else: node[leaf]=value
    result[module+'_text']=yaml.dump(raw,Dumper=Dumper,sort_keys=False)
    return result


def compiled(sources=None):
    result=compile_bundle(**(texts() if sources is None else sources))
    assert result.parsing==result.consistency=='PASS',result.model_dump_json()
    return result


def codes(result):
    return {i.reason_code for i in result.issues}


def test_defaults_reproducible_frozen_and_each_effective_leaf_has_origin():
    data=texts()
    result=compiled(data)
    assert compiled(data)==result
    bundle=result.bundle
    assert verify_bundle(bundle)==result
    assert len({p.path for p in bundle.parameters})==len(bundle.parameters)>200
    assert {s.name for s in bundle.sources}=={'main','admission','exit','manifest'}
    assert all(p.source_digest==next(s.raw_digest for s in bundle.sources if s.name==p.source) for p in bundle.parameters)
    assert all(p.unit and p.constraint and p.authority and p.override_rule for p in bundle.parameters)
    with pytest.raises(ValueError): bundle.bundle_digest='0'*64
    view=main_values(bundle);view['risk']['max_loss_per_trade']='999'
    assert main_values(bundle)['risk']['max_loss_per_trade']=='5'
    assert bundle.live_allowed is False and bundle.execution_authority=='none'


@pytest.mark.parametrize('module,path,value',[
    ('main','risk.max_loss_per_trade',4),('admission','max_loss_per_trade_usdt',4),
    ('exit','runner_trail_r',D('.8')),('manifest','score_context_max_age_seconds',299),
])
def test_same_versions_changed_content_changes_bundle_and_effective_hash(module,path,value):
    before=compiled();after=compiled(changed(texts(),module,path,value))
    assert after.bundle.manifest.bundle_revision==before.bundle.manifest.bundle_revision
    assert after.bundle.bundle_digest!=before.bundle.bundle_digest
    a=next(s for s in after.bundle.sources if s.name==module);b=next(s for s in before.bundle.sources if s.name==module)
    assert a.raw_digest!=b.raw_digest and a.effective_digest!=b.effective_digest


def test_raw_whitespace_changes_raw_binding_not_effective_semantics():
    original=compiled()
    inputs=texts();inputs['main_text']+='\n# same effective configuration\n'
    other=compiled(inputs)
    assert original.bundle.bundle_digest!=other.bundle.bundle_digest
    a,b=original.bundle.sources[0],other.bundle.sources[0]
    assert a.effective_digest==b.effective_digest and a.raw_digest!=b.raw_digest


@pytest.mark.parametrize('module,path',[
    ('main','instance_id'),('main','dry_run'),('main','live.enabled'),('main','risk.max_loss_per_trade'),
    ('main','risk.daily_loss_limit'),('main','risk.max_leverage'),('main','risk.max_margin_ratio'),
    ('main','risk.max_positions'),('main','risk.max_consecutive_losses'),('main','risk.max_trades_per_day'),
    ('admission','policy_version'),('admission','mode'),('exit','version'),('exit','mode'),('manifest','live_allowed'),
])
def test_critical_absence_never_assumed_safe(module,path):
    result=compile_bundle(**changed(texts(),module,path,None,remove=True))
    assert result.parsing=='FAIL' and result.bundle is None


def test_explicit_noncritical_defaults_and_strategy_fallback_semantics():
    source=changed(texts(),'exit','runner_trail_r',None,remove=True)
    result=compiled(source)
    value=next(p for p in result.bundle.parameters if p.path=='exit.runner_trail_r')
    assert value.default_applied and value.value_json=='"1"'
    no_section=compiled(changed(texts(),'main','strategies',None,remove=True))
    empty_section=compiled(changed(texts(),'main','strategies',{}))
    assert all(s['enabled'] for s in main_values(no_section.bundle)['strategies'].values())
    assert [n for n,s in main_values(empty_section.bundle)['strategies'].items() if s['enabled']]==['panic_rebound']
    assert main_values(empty_section.bundle)['strategies']['panic_rebound']['order_type']=='MARKET'


@pytest.mark.parametrize('value',[3,True,'bad',D('NaN'),D('Infinity'),-1])
def test_fraction_invalid_types_or_ranges(value):
    result=compile_bundle(**changed(texts(),'exit','tp1_fraction',value))
    assert result.parsing=='FAIL'


@pytest.mark.parametrize('path',['risk.taker_fee_rate','dashboard.require_auth','live.dedicated_account_confirmed',
    'strategies.trend_breakout.enabled','symbols'])
def test_empty_mapping_is_not_absent_scalar_and_cannot_activate_default(path):
    assert compile_bundle(**changed(texts(),'main',path,{})).parsing=='FAIL'


@pytest.mark.parametrize('module,path',[
    ('admission','minimum_net_rr'),('admission','minimum_entry_fee_rate'),('admission','max_cost_age_seconds'),
    ('admission','max_position_quantity'),('admission','tiers.0.minimum_total'),
    ('exit','expected_exit_fee_rate'),('exit','market_max_age_seconds'),
])
@pytest.mark.parametrize('value',[True,'NaN','Infinity','1e10000'])
def test_all_policy_numeric_units_reject_boolean_nonfinite_and_unbounded_exponents(module,path,value):
    assert compile_bundle(**changed(texts(),module,path,value)).parsing=='FAIL'


def test_legacy_float_policy_rounding_not_silent_and_decimal_money_exact():
    ttl=D('15.000000000000000000000000000001')
    result=compile_bundle(**changed(texts(),'admission','max_market_age_seconds',ttl))
    assert result.parsing=='FAIL' and 'LEGACY_NUMERIC_REPRESENTATION_UNSUPPORTED' in result.issues[0].suggestion
    budget=D('4.000000000000000000000000000001')
    b=compiled(changed(texts(),'admission','max_loss_per_trade_usdt',str(budget))).bundle
    assert policy_from(b,'admission').max_loss_per_trade_usdt==budget


@pytest.mark.parametrize('spelling',['012','0x10','1:30'])
def test_nondecimal_yaml_integer_not_mistaken_for_units(spelling):
    inputs=texts();inputs['main_text']=inputs['main_text'].replace('max_trades_per_day: 3','max_trades_per_day: '+spelling)
    assert compile_bundle(**inputs).parsing=='FAIL'


@pytest.mark.parametrize('module,path,values',[
    ('main','risk.max_trades_per_day',[True,3.0,'3',0]),
    ('admission','max_leverage',[True,5.0,'5',0]),
    ('exit','max_control_attempts',[True,3.0,'3',0]),
    ('main','dry_run',[1,'true',False]),
    ('main','risk.max_loss_per_trade',[True,'not-a-number',0]),
    ('manifest','live_allowed',[True,0,'false']),
])
def test_strict_types_not_silent_coercion(module,path,values):
    for value in values:
        assert compile_bundle(**changed(texts(),module,path,value)).parsing=='FAIL'


@pytest.mark.parametrize('module,path,value',[
    ('main','risk.max_leverage',6),('main','risk.max_margin_ratio',D('.21')),
    ('main','risk.max_positions',2),('main','symbols',['SOLUSDT','BTCUSDT','ETHUSDT','SOLUSDT']),
    ('exit','tp2_r',1),('exit','runner_activation_r',1),('exit','runner_trail_r',0),
    ('exit','market_max_age_seconds',0),('exit','tp1_fraction',D('.30000000000000000000000000000000000000000000000001')),
    ('admission','max_cost_age_seconds',0),('admission','maximum_exit_slippage_bps',5),
    ('admission','tiers.0.minimum_total',20),('admission','minimum_risk_budget_usdt',10),
])
def test_invalid_ranges_and_exact_fraction_sum(module,path,value):
    assert compile_bundle(**changed(texts(),module,path,value)).parsing=='FAIL'


@pytest.mark.parametrize('module',['main','admission','exit','manifest'])
@pytest.mark.parametrize('suffix',[
    '\nunknown_field: {}\n','\nunknown_field: 10\n',
    '\nunknown_field: &secret {a: 1}\nalias: *secret\n',
    '\nunknown_field: !!str value\n',
    '\nunknown_field: "$HOST_SECRET"\n','\nunknown_field: "{{ token }}"\n',
    '\nunknown_field: [broken\n',
])
def test_unknown_fields_aliases_tags_interpolation_and_bad_yaml_fail(module,suffix):
    source=texts();source[module+'_text']+=suffix
    assert compile_bundle(**source).parsing=='FAIL'


@pytest.mark.parametrize('module,key',[('main','dry_run'),('admission','enabled'),('exit','trend_exit_enabled'),('manifest','live_allowed')])
def test_duplicate_top_level_keys_and_nonliteral_bool_refused(module,key):
    data=texts();data[module+'_text']+='\n'+key+': true\n'
    assert compile_bundle(**data).parsing=='FAIL'
    for value in ('yes','on','TRUE','False'):
        data=texts();raw=parse_text(data[module+'_text']);raw[key]=True
        data[module+'_text']=yaml.dump(raw,Dumper=Dumper).replace(key+': true',key+': '+value)
        assert compile_bundle(**data).parsing=='FAIL'


@pytest.mark.parametrize('field',[
    'schema_version','catalog_version','main_contract_version','setup_schema_version','rr_calculation_version',
    'scorecard_schema_version','scoring_rule_version','admission_policy_version','exit_policy_version',
    'exit_state_version','exit_checkpoint_version','mode','exchange','contract_type','quote_currency',
])
def test_unknown_manifest_versions_or_modes_do_not_mean_latest(field):
    assert compile_bundle(**changed(texts(),'manifest',field,'unknown/v99')).parsing=='FAIL'


@pytest.mark.parametrize('module,path',[('admission','policy_version'),('exit','version')])
def test_original_policy_free_text_version_is_closed_by_bundle_contract(module,path):
    assert compile_bundle(**changed(texts(),module,path,'future/v99')).parsing=='FAIL'


@pytest.mark.parametrize('source',['fraction','fee_rate','percent_points','bps'])
@pytest.mark.parametrize('target',['fraction','fee_rate','percent_points','bps'])
def test_unit_conversion_explicit_and_roundtrips(source,target):
    value=D('.125')
    assert unit_convert(unit_convert(value,source,target),target,source)==value
    assert unit_convert('.01','fraction','percent_points')==1
    assert unit_convert('.0005','fee_rate','percent_points')==D('.05')
    assert unit_convert(10,'bps','fraction')==D('.001')


@pytest.mark.parametrize('unit',['USDT','base_quantity','USDT_notional','USDT_margin','seconds','leverage_multiple'])
def test_money_quantity_margin_leverage_time_not_ratio_conversions(unit):
    with pytest.raises(ConfigurationError): unit_convert(1,unit,'fraction')


def test_same_semantic_alias_conflict_not_last_writer_wins():
    result=compile_bundle(**changed(texts(),'main','strategies.panic_rebound.drop_pct',3))
    assert result.parsing=='FAIL'
    assert 'SEMANTIC_ALIAS_CONFLICT' in result.issues[0].suggestion
    equal=compiled(changed(texts(),'main','strategies.panic_rebound.drop_pct',2))
    p=next(p for p in equal.bundle.parameters if p.path=='main.strategies.panic_rebound.drop_threshold_pct')
    assert 'drop_pct' in p.source_path and p.value_json=='"-2"'


def test_comparable_minima_and_distinct_daily_quantity_units():
    result=compiled(changed(texts(),'admission','max_loss_per_trade_usdt',3))
    limits={p.semantic:p for p in result.bundle.comparable_limits}
    assert limits['max_loss_per_trade_usdt'].effective_value==3
    assert 'daily_loss_limit_usdt' not in limits and 'max_trades_per_day' not in limits
    assert 'STRICTER_COMPARABLE_CEILING' in codes(result) and 'DISTINCT_DAILY_ACCOUNTING_NOT_MERGED' in codes(result)
    values={p.path:p for p in result.bundle.parameters}
    assert len({values['admission.'+p].unit for p in ('max_margin_usdt','max_position_notional_usdt','max_position_quantity')})==3
    for p in result.bundle.parameters:
        if p.path.startswith('derived.minimum_net_rr.'): assert D(json.loads(p.value_json))>=D('1.5')


@pytest.mark.parametrize('module,path,value,code',[
    ('manifest','instance_id','different-paper-instance','SCOPE_OR_VERSION_CONFLICT'),
    ('admission','allowed_symbols',['BTCUSDT'],'SCOPE_OR_VERSION_CONFLICT'),
    ('admission','max_leverage',3,'EXECUTION_LEVERAGE_EXCEEDS_EFFECTIVE_CAP'),
    ('exit','expected_exit_fee_rate',0,'EXIT_COST_COVER_UNDERBUDGETED'),
    ('exit','expected_exit_slippage_bps',0,'EXIT_COST_COVER_UNDERBUDGETED'),
    ('exit','max_holding_seconds',90000,'EXIT_HORIZON_EXCEEDS_COST_POLICY'),
    ('exit','expected_exit_slippage_bps',40,'EXIT_COST_OUTSIDE_ADMISSION_RANGE'),
    ('main','strategies.trend_breakout.order_type','LIMIT','STRATEGY_ORDER_RULE_SCOPE_CONFLICT'),
])
def test_cross_config_errors_parse_but_are_not_compatible(module,path,value,code):
    result=compile_bundle(**changed(texts(),module,path,value))
    assert result.parsing=='PASS' and result.consistency=='FAIL' and code in codes(result)


def test_core_no_io_clock_env_or_account_access_and_decimal_context_independence(monkeypatch):
    data=texts();expected=compiled(data)
    def denied(*args,**kwargs): raise AssertionError('No IO/clock/environment or credentials in pure compiler')
    monkeypatch.setenv('BINANCE_API_KEY','synthetic-poison-never-inherited')
    monkeypatch.setenv('DRY_RUN','false')
    monkeypatch.setattr(builtins,'open',denied);monkeypatch.setattr(Path,'read_text',denied)
    monkeypatch.setattr(socket,'socket',denied);monkeypatch.setattr(sqlite3,'connect',denied);monkeypatch.setattr(time,'time',denied)
    with localcontext() as ctx:
        ctx.prec=2;ctx.rounding=ROUND_UP;ctx.traps[Inexact]=True
        assert compile_bundle(**data)==expected
        assert verify_bundle(expected.bundle)==expected
        assert ctx.prec==2 and ctx.traps[Inexact]
    assert 'synthetic-poison' not in expected.model_dump_json()


def test_bundle_and_provenance_forgery_not_accepted():
    b=compiled().bundle
    for forged in (b.model_copy(update={'bundle_digest':'0'*64}),b.model_copy(update={'parameters':b.parameters[:-1]}),
                   b.model_copy(update={'sources':b.sources[:-1]}),b.model_copy(update={'live_allowed':True})):
        with pytest.raises(ConfigurationError): verify_bundle(forged)
