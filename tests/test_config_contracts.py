"""Cross-module content, units, path-dependence and safety boundary regressions."""

import builtins
from decimal import Decimal as D, Inexact, localcontext
from pathlib import Path
import socket
import sqlite3
import time

import pytest

from app.admission.engine import admit_trade
from app.admission.models import fingerprint
from app.configuration.compiler import compile_bundle, policy_from
from app.configuration.contracts import declare_plan_binding, validate_contract
from app.configuration.examples import example_plan
from app.configuration.inputs import BoundPosition, PlanInputs
from app.configuration.models import ConfigurationError
from app.exits.models import ExitVenueRules
from app.setups.models import TradeSetup
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from test_admission import case, raw, NOW
from test_config_bundle import texts, changed, compiled
from test_exit_policy import Harness


def scenario(side='LONG'):
    data=raw(side)
    data.update(strategy_name='trend_breakout',strategy_type='trend_breakout')
    c=case(data,side=side)
    c['account']=c['account'].model_copy(update={'instance_id':'sol-ai-local'})
    bundle=compiled().bundle
    args={k:c[k] for k in ('setup','rr','scorecard','account','exchange','request')}
    args['exit_rules']=ExitVenueRules(symbol='SOLUSDT',verified=True,quantity_step='.001',price_tick='.01',
        min_quantity='.001',min_notional=5,max_quantity=1000,reduce_only_min_quantity_exempt=True,
        reduce_only_min_notional_exempt=True,exact_close_remainder=True,atomic_stop_replace=True)
    inputs=PlanInputs(**args)
    binding=declare_plan_binding(bundle,inputs,declared_at=NOW)
    decision=admit_trade(**c)
    assert decision.result=='APPROVE'
    return bundle,inputs.model_copy(update={'admission':decision,'configuration_binding':binding})


def result_codes(result):
    return {i.reason_code for i in result.issues}


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_defaults_do_not_silently_match_original_structure_allocation(side):
    b,p=scenario(side)
    before=p.model_dump_json()
    r=validate_contract(b,p,evaluated_at=NOW)
    assert r.config_parsing==r.config_consistency=='PASS' and r.plan_consistency=='FAIL'
    assert {'EXIT_ALLOCATION_MISMATCH','STRUCTURE_TARGET_DIFFERS_FROM_EXIT_TRIGGER'}<=result_codes(r)
    assert r.runtime_metadata=='PASS' and r.runtime_trust=='INCOMPLETE'
    assert r.execution=='NOT_INTEGRATED' and r.execution_authority=='none' and not r.live_allowed
    assert p.model_dump_json()==before


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('name',['allocation-mismatch','runner-unmodeled'])
def test_minimal_examples_do_not_fabricate_scores_sources_funding_or_runner_return(side,name):
    b=compiled().bundle;p=example_plan(name,side)
    assert p.scorecard is None and p.account is None and p.setup.confidence.value is None
    r=validate_contract(b,p,evaluated_at=1800000000)
    assert r.plan_consistency==('FAIL' if name=='allocation-mismatch' else 'UNSUPPORTED')
    assert r.runtime_metadata=='INCOMPLETE' and r.runtime_trust=='INCOMPLETE'
    x=r.exit_comparison
    assert x.runner_activation_price==(115 if side=='LONG' else 85)
    assert x.runner_exit_price is x.full_policy_net_rr is None
    assert 'FUNDING_HORIZON_INCOMPLETE' in result_codes(r)
    assert p.setup.cost_assumptions.funding_cost_usdt is None and p.rr.reference.net_rr is None
    assert 'STATIC_RR_NOT_EXIT_POLICY_RETURN' in result_codes(r)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('mutation',['stop','target','fraction','cost','entry','strategy','side'])
def test_same_id_and_version_altered_content_cannot_reuse_upstream_or_approval(side,mutation):
    b,p=scenario(side);data=p.setup.model_dump()
    sign=1 if side=='LONG' else -1
    if mutation=='stop': data['initial_stop']['price']-=sign
    if mutation=='target': data['targets'][0]['price']+=sign
    if mutation=='fraction':
        data['targets'][0]['fraction']=.25;data['targets'][1]['fraction']=.25
    if mutation=='cost': data['cost_assumptions']['exit_fee_rate']=.001
    if mutation=='entry': data['entry']['reference_price']=100.1
    if mutation=='strategy': data['strategy_name']='same-id-altered-strategy-description'
    if mutation=='side':
        data=raw('SHORT' if side=='LONG' else 'LONG')
        data.update(strategy_name='trend_breakout',strategy_type='trend_breakout')
    setup=TradeSetup.model_validate(data)
    assert setup.setup_id==p.setup.setup_id and setup.plan_version==p.setup.plan_version
    r=validate_contract(b,p.model_copy(update={'setup':setup}),evaluated_at=NOW)
    assert r.plan_consistency=='FAIL'
    assert {'CONFIG_PLAN_BINDING_MISMATCH','ADMISSION_INPUT_CONTENT_MISMATCH'}<=result_codes(r)
    if mutation=='strategy':
        # A description is not an RR input. Full-plan binding must catch it
        # without coupling the accepted price/cost arithmetic to strategy text.
        assert calculate_rr(setup,quantity=1)==p.rr
        assert 'RR_CONTENT_MISMATCH' not in result_codes(r)
    else:
        assert 'RR_CONTENT_MISMATCH' in result_codes(r)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('part',['rr','scorecard','admission'])
def test_result_tampering_cannot_hide_behind_matching_ids(side,part):
    b,p=scenario(side)
    if part=='rr':
        rr=p.rr.model_copy(update={'reference':p.rr.reference.model_copy(update={'net_rr':D('999')})})
        p=p.model_copy(update={'rr':rr})
    elif part=='scorecard':
        p=p.model_copy(update={'scorecard':p.scorecard.model_copy(update={'rubric_version':'unknown/v9'})})
        with pytest.raises(ValueError): validate_contract(b,p,evaluated_at=NOW)
        return
    else:
        p=p.model_copy(update={'admission':p.admission.model_copy(update={'max_quantity':p.admission.max_quantity/D(2)})})
    r=validate_contract(b,p,evaluated_at=NOW)
    assert r.plan_consistency=='FAIL'
    assert 'ADMISSION_CONTENT_DIGEST_MISMATCH' in result_codes(r) if part=='admission' else 'RR_CONTENT_MISMATCH' in result_codes(r)


@pytest.mark.parametrize('module,path,value',[
    ('main','risk.max_loss_per_trade',4),('admission','max_loss_per_trade_usdt',4),
    ('exit','runner_trail_r',D('.5')),('manifest','score_context_max_age_seconds',200),
])
def test_old_approval_cannot_bind_changed_configuration_even_same_versions(module,path,value):
    _,p=scenario()
    new=compiled(changed(texts(),module,path,value)).bundle
    r=validate_contract(new,p,evaluated_at=NOW)
    assert 'CONFIG_PLAN_BINDING_MISMATCH' in result_codes(r) and r.plan_consistency=='FAIL'
    assert new.manifest.bundle_revision=='sol-paper-config/v1'


def test_missing_binding_and_retroactive_wrapper_are_not_permission():
    b,p=scenario()
    r=validate_contract(b,p.model_copy(update={'configuration_binding':None}),evaluated_at=NOW)
    assert 'PREAPPROVAL_CONFIG_BINDING_MISSING' in result_codes(r)
    with pytest.raises(ConfigurationError): declare_plan_binding(b,p,declared_at=NOW+1)
    forged=p.configuration_binding.model_copy(update={'declared_at':D(str(NOW+1))})
    assert 'CONFIG_BINDING_TIME_INVALID' in result_codes(validate_contract(b,p.model_copy(update={'configuration_binding':forged}),evaluated_at=NOW+1))


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('field,value,expected',[
    ('entry_fee_rate',0,'COST_ASSUMPTION_UNDERBUDGETED'),
    ('exit_fee_rate',0,'COST_ASSUMPTION_UNDERBUDGETED'),
    ('exit_slippage_bps',None,'COST_ASSUMPTION_MISSING'),
    ('funding_cost_usdt',None,'FUNDING_HORIZON_INCOMPLETE'),
    ('assumed_holding_seconds',300,'FUNDING_HORIZON_INCOMPATIBLE'),
    ('assumed_holding_seconds',90000,'FUNDING_HORIZON_INCOMPATIBLE'),
    ('entry_slippage_bps',31,'COST_ASSUMPTION_OUTSIDE_POLICY'),
    ('exit_slippage_bps',31,'COST_ASSUMPTION_OUTSIDE_POLICY'),
])
def test_cost_units_and_horizon_mismatch_are_not_hidden(side,field,value,expected):
    b,p=scenario(side);raw=p.setup.model_dump();raw['cost_assumptions'][field]=value
    s=TradeSetup.model_validate(raw)
    r=validate_contract(b,PlanInputs(setup=s,rr=calculate_rr(s,quantity=1)),evaluated_at=NOW)
    assert expected in result_codes(r)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_conservative_cost_budget_is_not_mechanically_equal_or_double_counted(side):
    b,p=scenario(side);raw=p.setup.model_dump()
    raw['cost_assumptions'].update(exit_fee_rate=.001,exit_slippage_bps=20,assumed_holding_seconds=7200)
    s=TradeSetup.model_validate(raw);rr=calculate_rr(s,quantity=1)
    before=rr.model_dump_json()
    r=validate_contract(b,PlanInputs(setup=s,rr=rr),evaluated_at=NOW)
    assert not {'COST_ASSUMPTION_UNDERBUDGETED','FUNDING_HORIZON_INCOMPATIBLE'} & result_codes(r)
    assert 'FUNDING_PATH_NOT_MODELED' in result_codes(r) and rr.model_dump_json()==before


@pytest.mark.parametrize('field,ttl',[
    ('market',15),('structure',300),('account',5),('exchange',3600),('costs',300),('scorecard',30),
])
def test_each_freshness_domain_uses_own_ttl_and_reports_invalidation(field,ttl):
    b,p=scenario()
    if field in ('account','exchange'): p=p.model_copy(update={field:getattr(p,field).model_copy(update={'observed_at':NOW-ttl-1})})
    elif field=='scorecard': p=p.model_copy(update={'scorecard':p.scorecard.model_copy(update={'context':p.scorecard.context.model_copy(update={'evaluated_at':NOW-ttl-1})})})
    else:
        data=p.setup.model_dump()
        if field=='market': data['market_state']['observed_at']=NOW-ttl-1
        if field=='costs': data['cost_assumptions']['observed_at']=NOW-ttl-1
        if field=='structure': data['structure_evidence'][0]['observed_at']=NOW-ttl-1
        p=p.model_copy(update={'setup':TradeSetup.model_validate(data)})
    r=validate_contract(b,p,evaluated_at=NOW)
    prefix={'market':'setup.market_state','structure':'structure.entry'}.get(field,field)
    observation=next(x for x in r.freshness if x.field_path==prefix)
    assert observation.status=='FAIL' and observation.ttl_seconds==ttl and observation.invalidates
    assert r.runtime_metadata=='FAIL' and r.runtime_trust=='INCOMPLETE'


def test_quote_structure_score_input_and_exit_windows_are_not_forced_equal():
    b=compiled().bundle;windows={r.field_path:r.ttl_seconds for r in b.freshness_rules}
    assert windows['market']==15 and windows['structure']==300 and windows['account']==5
    assert windows['exchange']==3600 and windows['costs']==300 and windows['scorecard']==30
    assert windows['score_inputs']==300 and windows['exit.market']==5 and windows['exit.evidence']==30


@pytest.mark.parametrize('record,updates,code',[
    ('account',{'instance_id':'other-synthetic-instance'},'ACCOUNT_SCOPE_CONFLICT'),
    ('account',{'mode':'live'},'ACCOUNT_SCOPE_CONFLICT'),
    ('account',{'margin_mode':'CROSS'},'UNSAFE_DECLARED_ACCOUNT_MODE'),
    ('account',{'auto_add_margin_enabled':True},'UNSAFE_DECLARED_ACCOUNT_MODE'),
    ('account',{'martingale_enabled':True},'UNSAFE_DECLARED_ACCOUNT_MODE'),
    ('account',{'reconciliation_clear':None},'ACCOUNT_SAFETY_METADATA_INCOMPLETE'),
    ('exchange',{'symbol':'BTCUSDT'},'EXCHANGE_RULE_SCOPE_CONFLICT'),
    ('exchange',{'contract_type':'inverse'},'EXCHANGE_RULE_SCOPE_CONFLICT'),
    ('exchange',{'quantity_step':None},'EXCHANGE_RULES_INCOMPLETE'),
    ('request',{'leverage':10},'REQUEST_LEVERAGE_CONFLICT'),
    ('request',{'auto_add_margin':True},'REQUEST_MODE_CONFLICT'),
    ('request',{'loss_recovery_sizing':True},'REQUEST_MODE_CONFLICT'),
    ('exit_rules',{'quantity_step':D('.1')},'ENTRY_EXIT_RULES_CONFLICT'),
    ('exit_rules',{'verified':False},'EXIT_RULE_METADATA_UNVERIFIED'),
])
def test_runtime_declarations_never_implicitly_safe_or_authenticated(record,updates,code):
    b,p=scenario();p=p.model_copy(update={record:getattr(p,record).model_copy(update=updates)})
    r=validate_contract(b,p,evaluated_at=NOW)
    assert code in result_codes(r) and r.runtime_trust=='INCOMPLETE' and not r.live_allowed


def test_approval_sized_rr_uses_original_calculator_not_farthest_target():
    b,p=scenario()
    rr=p.admission.final_rr
    assert rr.hypothetical_quantity==p.admission.max_quantity
    assert calculate_rr(p.setup,quantity=float(p.admission.max_quantity))==rr
    amended=p.admission.model_copy(update={'final_rr':rr.model_copy(update={'hypothetical_quantity':D('1000')})})
    r=validate_contract(b,p.model_copy(update={'admission':amended}),evaluated_at=NOW)
    assert 'ADMISSION_SIZED_RR_MISMATCH' in result_codes(r)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_invalid_new_config_never_changes_existing_exit_policy_state_or_frozen_r(side):
    b,p=scenario(side);h=Harness(side=side).arm()
    bound=BoundPosition(seed=h.seed,policy=h.policy,state=h.state)
    p=p.model_copy(update={'bound_position':bound})
    before=bound.model_dump_json()
    wrong=compile_bundle(**changed(texts(),'manifest','instance_id','incompatible-new-instance'))
    r=validate_contract(wrong,p,evaluated_at=NOW)
    assert r.config_consistency=='FAIL'
    assert r.existing_position_rule=='continue_original_bound_policy_independent_of_new_config'
    assert bound.model_dump_json()==before and bound.state.frozen_initial_r==h.state.frozen_initial_r
    normal=validate_contract(b,p,evaluated_at=NOW)
    assert 'EXISTING_POSITION_POLICY_RETAINED' in result_codes(normal) and bound.model_dump_json()==before
    assert not hasattr(normal,'actions')


def test_pure_contract_does_not_connect_or_change_global_decimal_context(monkeypatch):
    b,p=scenario();expected=validate_contract(b,p,evaluated_at=NOW)
    def denied(*args,**kwargs): raise AssertionError('Contract verification cannot perform IO')
    monkeypatch.setattr(builtins,'open',denied);monkeypatch.setattr(Path,'read_text',denied)
    monkeypatch.setattr(socket,'socket',denied);monkeypatch.setattr(sqlite3,'connect',denied);monkeypatch.setattr(time,'time',denied)
    with localcontext() as ctx:
        ctx.prec=2;ctx.traps[Inexact]=True
        assert validate_contract(b,p,evaluated_at=NOW)==expected
        assert ctx.prec==2 and ctx.traps[Inexact]


def test_no_implicit_clock_or_bare_signal_contract():
    b,p=scenario()
    assert 'EVALUATION_TIME_REQUIRED' in result_codes(validate_contract(b,p))
    with pytest.raises(ConfigurationError): validate_contract(b,p.setup,evaluated_at=NOW)
    with pytest.raises(ConfigurationError): validate_contract(b,p,evaluated_at=True)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_stricter_main_ceiling_is_checked_even_with_new_bundle_binding(side):
    _,p=scenario(side)
    approved=p.admission.model_dump_json()
    b=compiled(changed(texts(),'main','risk.max_loss_per_trade',4)).bundle
    p=p.model_copy(update={'configuration_binding':declare_plan_binding(b,p,declared_at=NOW)})
    r=validate_contract(b,p,evaluated_at=NOW)
    assert 'CONFIG_PLAN_BINDING_MISMATCH' not in result_codes(r)
    assert 'APPROVAL_EXCEEDS_BUNDLE_CEILING' in result_codes(r)
    assert 4<p.admission.allowed_risk_budget_usdt<=5
    assert p.admission.model_dump_json()==approved
    assert r.plan_consistency=='FAIL' and r.execution_authority=='none'


@pytest.mark.parametrize('field',['confirmations','invalidation_review'])
def test_required_structure_review_metadata_missing_is_not_ready(field):
    b,p=scenario()
    request=p.request.model_copy(update={field:() if field=='confirmations' else None})
    r=validate_contract(b,p.model_copy(update={'request':request}),evaluated_at=NOW)
    assert r.runtime_metadata=='INCOMPLETE'
    assert any(o.status=='INCOMPLETE' for o in r.freshness if o.field_path.startswith(('confirmation.','invalidation_review')))


@pytest.mark.parametrize('field,ttl',[('confirmations',300),('invalidation_review',15)])
def test_confirmation_and_invalidation_metadata_have_separate_expiry(field,ttl):
    b,p=scenario()
    value=getattr(p.request,field)
    value=(tuple(c.model_copy(update={'checked_at':NOW-ttl}) for c in value) if field=='confirmations'
           else value.model_copy(update={'checked_at':NOW-ttl}))
    p=p.model_copy(update={'request':p.request.model_copy(update={field:value})})
    r=validate_contract(b,p,evaluated_at=NOW)
    assert r.runtime_metadata=='FAIL'
    assert any(o.status=='FAIL' and o.ttl_seconds==ttl for o in r.freshness
        if o.field_path.startswith(('confirmation.','invalidation_review')))


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_explicit_stop_movement_declaration_not_silently_replaced(side):
    b,p=scenario(side);data=p.setup.model_dump()
    data['stop_movement']={'rules':[dict(after_target_id=data['targets'][0]['target_id'],move_to='entry')]}
    s=TradeSetup.model_validate(data)
    r=validate_contract(b,PlanInputs(setup=s,rr=calculate_rr(s,quantity=1)),evaluated_at=NOW)
    assert 'STOP_MOVEMENT_POLICY_CONFLICT' in result_codes(r)
    assert s.stop_movement.rules[0].move_to=='entry'


def test_short_r_trigger_cannot_imply_negative_exit_price():
    p=example_plan('runner-unmodeled','SHORT');data=p.setup.model_dump()
    data['initial_stop']['price']=200
    s=TradeSetup.model_validate(data)
    r=validate_contract(compiled().bundle,PlanInputs(setup=s,rr=calculate_rr(s,quantity=1)),evaluated_at=1800000000)
    assert 'EXIT_TRIGGER_PRICE_NONPOSITIVE' in result_codes(r) and r.plan_consistency=='FAIL'
    assert r.exit_comparison.runner_exit_price is None


def test_negative_btc_threshold_compares_predicate_not_numeric_min():
    b=compiled(changed(texts(),'admission','btc_crash_3m_pct',D('-1.2'))).bundle
    row=next(p for p in b.parameters if p.path=='derived.btc_long_block_at_or_below_pct')
    assert row.value_json=='"-0.8"' and 'max' in row.override_rule
