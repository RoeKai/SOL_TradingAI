"""Stage 3: deterministic arithmetic on synthetic plans, never real accounts."""

import ast
import builtins
from dataclasses import asdict
from decimal import Decimal, Inexact, ROUND_UP, localcontext
from pathlib import Path
import socket
import sqlite3
import time

import pytest
from pydantic import ValidationError

from app.models import Signal
from app.setups import TradeSetup, adapt_legacy_signal
from app.setups.models import CostAssumptions
from app.setups.rr import calculate_rr
from app.setups.rr_models import RRCalculation

D = Decimal
NOW = 1_800_000_000.0


def raw_plan(side='LONG', *, fractions=(.2, .3, .5), stop_distance=2,
             rewards=(2, 4, 6), lower=100, upper=100, costs=None, order_type='MARKET'):
    sign = 1 if side == 'LONG' else -1
    return dict(plan_version='rr-synthetic/v1', setup_id='rr-fixture', symbol='SOLUSDT',
        strategy_name='synthetic_fixture', strategy_type='custom', side=side,
        entry=dict(order_type=order_type, reference_price=100, lower_price=lower, upper_price=upper),
        initial_stop=dict(price=100-sign*stop_distance, basis='Synthetic stop; not market advice'),
        targets=[dict(target_id=f'tp-{i}', price=100+sign*reward, fraction=fraction,
                      kind='unverified', basis='Synthetic target; not observed market')
                 for i, (reward, fraction) in enumerate(zip(rewards, fractions), 1)],
        cost_assumptions=costs if costs is not None else dict(entry_fee_rate=0,
            exit_fee_rate=0, entry_slippage_bps=0, exit_slippage_bps=0, funding_cost_usdt=0),
        created_at=NOW, data_as_of=NOW-1, valid_until=NOW+30)


def plan(side='LONG', **kwargs):
    return TradeSetup.model_validate(raw_plan(side, **kwargs))


def near(value, expected):
    assert value is not None
    assert abs(value - expected) < D('1e-25')


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('order_type', ['MARKET', 'LIMIT'])
def test_gross_rr_uses_real_prices_and_weighted_targets(side, order_type):
    p = plan(side, order_type=order_type)
    result = calculate_rr(p)
    r = result.reference
    assert r.gross_risk_per_unit == 2
    assert [t.gross_rr for t in r.targets] == [1, 2, 3]
    assert [t.weighted_gross_rr for t in r.targets] == [D('.2'), D('.6'), D('1.5')]
    assert r.gross_rr == D('2.3')  # Not the farthest target and not an unweighted mean.
    assert r.net_rr == r.gross_rr and result.status == 'complete'
    assert r.gross_pnl_usdt is None and r.gross_risk_usdt is None
    assert result.hypothetical_quantity is None
    assert p.targets[0].theoretical_rr is None  # No write-back into Stage 2.


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_single_target_and_explicit_hypothetical_amounts(side):
    r = calculate_rr(plan(side, fractions=(1,), rewards=(7,), stop_distance=2), quantity=3).reference
    assert r.gross_rr == D('3.5') and r.net_rr == D('3.5')
    assert r.gross_risk_usdt == 6 and r.gross_pnl_usdt == 21 and r.net_pnl_usdt == 21
    assert r.targets[0].hypothetical_quantity == 3


@pytest.mark.parametrize('side,entry_fill,exit_fill,stop_fill,net_reward,net_risk', [
    ('LONG', '100.1', '109.78', '94.81', '8.86034', '6.07972'),
    ('SHORT', '99.9', '90.18', '105.21', '8.93974', '6.12032'),
])
def test_hand_calculated_fees_slippage_funding_and_net_denominator(
        side, entry_fill, exit_fill, stop_fill, net_reward, net_risk):
    costs=dict(entry_fee_rate=.001, exit_fee_rate=.002,
               entry_slippage_bps=10, exit_slippage_bps=20, funding_cost_usdt=1)
    r=calculate_rr(plan(side, fractions=(1,), rewards=(10,), stop_distance=5, costs=costs), quantity=2).reference
    t=r.targets[0]
    assert r.effective_entry_price == D(entry_fill)
    assert r.effective_stop_price == D(stop_fill)
    assert t.effective_exit_price == D(exit_fill)
    assert t.net_reward_per_unit == D(net_reward)
    assert r.net_stop_loss_per_unit == D(net_risk)
    assert t.costs.entry_fee_per_unit == D(entry_fill)*D('.001')
    assert t.costs.exit_fee_per_unit == D(exit_fill)*D('.002')
    assert t.costs.funding_per_unit == D('.5')
    assert r.gross_rr == 2
    near(r.net_rr, D(net_reward)/D(net_risk))
    assert r.net_pnl_usdt == D(net_reward)*2
    assert r.net_stop_loss_usdt == D(net_risk)*2


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_weighting_allocates_entry_fee_and_funding_once(side):
    costs=dict(entry_fee_rate=.001, exit_fee_rate=.002,
               entry_slippage_bps=10, exit_slippage_bps=20, funding_cost_usdt=3)
    p=plan(side, costs=costs)
    r=calculate_rr(p, quantity=10).reference
    assert sum(t.hypothetical_quantity for t in r.targets) == 10
    near(sum(t.gross_pnl_usdt for t in r.targets), r.gross_pnl_usdt)
    near(sum(t.net_pnl_usdt for t in r.targets), r.net_pnl_usdt)
    near(sum(t.weighted_gross_rr for t in r.targets), r.gross_rr)
    near(sum(t.weighted_net_rr for t in r.targets), r.net_rr)
    near(sum(t.hypothetical_quantity*t.costs.entry_fee_per_unit for t in r.targets),
         r.effective_entry_price*D('.001')*10)
    near(sum(t.hypothetical_quantity*t.costs.funding_per_unit for t in r.targets), D('3'))
    near(r.net_rr, r.net_pnl_usdt/r.net_stop_loss_usdt)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_entry_range_reports_both_edges_without_optimizing_plan(side):
    p=plan(side, fractions=(.3,.4,.3), lower=99, upper=101)
    before=p.model_dump_json()
    result=calculate_rr(p)
    assert result.reference.gross_rr == 2
    adverse=result.entry_upper if side=='LONG' else result.entry_lower
    favorable=result.entry_lower if side=='LONG' else result.entry_upper
    assert adverse.gross_rr == 1 and favorable.gross_rr == 5
    assert result.adverse_entry == ('upper' if side=='LONG' else 'lower')
    assert p.model_dump_json() == before


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('rr', [.1, .5, 1.49, 1.5, 2, 3, 4, 8])
def test_any_structurally_valid_rr_is_calculated_without_admission(side, rr):
    p=plan(side, fractions=(1,), rewards=(rr*10,), stop_distance=10)
    result=calculate_rr(p)
    near(result.reference.gross_rr, D(str(rr)))
    assert result.status=='complete' and not result.issues
    assert result.admission_status=='not_evaluated' and result.execution_authority=='none'


@pytest.mark.parametrize('grade,score', [('S',99),('A',85),('B',71),('C',0),(None,None)])
def test_grade_score_confidence_budget_and_size_advice_do_not_affect_rr(grade,score):
    raw=raw_plan()
    before=calculate_rr(TradeSetup.model_validate(raw))
    raw.update(grade=grade,score={'total':score},confidence={'value':.99,'basis':'Synthetic only'},
        risk_budget={'max_loss_usdt':0,'remaining_daily_loss_usdt':0},
        position_limit_advice={'max_quantity':999999,'leverage_cap':1000},
        market_state={'regime':'high_volatility'})
    assert calculate_rr(TradeSetup.model_validate(raw)) == before


def test_supplied_stale_rr_claims_are_ignored_not_used_as_inputs():
    raw=raw_plan()
    for t in raw['targets']: t.update(theoretical_rr=888,net_rr=777)
    raw['reward_risk']={'gross_rr':666,'net_rr':555}
    p=TradeSetup.model_validate(raw)
    result=calculate_rr(p)
    assert result.reference.gross_rr==D('2.3')
    assert p.reward_risk.gross_rr==666 and p.targets[0].net_rr==777


@pytest.mark.parametrize('field', ['entry_fee_rate','exit_fee_rate','entry_slippage_bps',
                                    'exit_slippage_bps','funding_cost_usdt'])
def test_each_missing_cost_retains_gross_and_marks_net_unknown(field):
    raw=raw_plan(); raw['cost_assumptions'][field]=None
    result=calculate_rr(TradeSetup.model_validate(raw),quantity=10)
    assert result.status=='partial' and result.reference.gross_rr==D('2.3')
    assert result.reference.net_rr is None
    assert all(t.net_rr is None for t in result.reference.targets)
    missing=next(i for i in result.issues if i.code=='MISSING_COST_ASSUMPTIONS')
    assert missing.fields==('cost_assumptions.'+field,)


def test_all_costs_unknown_do_not_mean_fee_free_or_no_funding():
    p=plan(costs={})
    result=calculate_rr(p)
    assert len(result.issues[0].fields)==5
    assert result.reference.targets[0].costs.total_per_unit is None
    assert result.reference.net_stop_loss_per_unit is None
    assert result.reference.gross_rr==D('2.3')
    assert result.cost_assumptions==CostAssumptions()


def test_available_metadata_is_not_required_or_promoted_to_freshness_verification():
    p=plan()  # No external evidence available; explicit numerical assumptions only.
    result=calculate_rr(p)
    assert result.status=='complete'
    assert result.input_missing_items==p.data_coverage.missing_items
    assert 'funding_rate' in result.input_missing_items
    assert result.verification=='arithmetic_only_not_market_verified'
    assert result.data_as_of==NOW-1 and result.valid_until==NOW+30


@pytest.mark.parametrize('funding', [-2,2])
def test_nonzero_total_funding_requires_quantity_not_max_quantity_advice(funding):
    raw=raw_plan();raw['cost_assumptions']['funding_cost_usdt']=funding
    raw['position_limit_advice']={'max_quantity':10000}
    result=calculate_rr(TradeSetup.model_validate(raw))
    assert result.reference.gross_rr==D('2.3') and result.reference.net_rr is None
    assert result.reference.targets[0].costs.funding_per_unit is None
    assert 'QUANTITY_REQUIRED_FOR_FUNDING' in [i.code for i in result.issues]
    assert result.hypothetical_quantity is None


@pytest.mark.parametrize('side', ['LONG','SHORT'])
@pytest.mark.parametrize('quantity', [.01,1,10,1000])
def test_rr_is_scale_invariant_when_funding_explicitly_zero(side,quantity):
    costs=dict(entry_fee_rate=.001,exit_fee_rate=.002,
               entry_slippage_bps=3,exit_slippage_bps=7,funding_cost_usdt=0)
    p=plan(side,costs=costs)
    a,b=calculate_rr(p),calculate_rr(p,quantity=quantity)
    assert a.reference.net_rr==b.reference.net_rr
    assert a.reference.gross_rr==b.reference.gross_rr
    assert b.quantity_authority=='hypothetical_only_not_order_quantity'


def test_nonzero_funding_dilution_uses_explicit_quantity():
    raw=raw_plan();raw['cost_assumptions']['funding_cost_usdt']=1
    p=TradeSetup.model_validate(raw)
    small,large=calculate_rr(p,quantity=1),calculate_rr(p,quantity=10)
    assert small.reference.gross_rr==large.reference.gross_rr
    assert small.reference.net_rr < large.reference.net_rr


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_signed_funding_credit_is_not_lost_or_assumed_guaranteed(side):
    costs=dict(entry_fee_rate=0,exit_fee_rate=0,entry_slippage_bps=0,exit_slippage_bps=0,funding_cost_usdt=-1)
    r=calculate_rr(plan(side,fractions=(1,),rewards=(10,),stop_distance=5,costs=costs),quantity=2).reference
    assert r.targets[0].costs.funding_per_unit==D('-.5')
    assert r.targets[0].net_reward_per_unit==D('10.5')
    assert r.net_stop_loss_per_unit==D('4.5')
    near(r.net_rr,D('10.5')/D('4.5'))


@pytest.mark.parametrize('funding',[-10,-12])
def test_nonpositive_cost_adjusted_denominator_is_undefined_not_infinite_rr(funding):
    raw=raw_plan(fractions=(1,),rewards=(10,),stop_distance=5)
    raw['cost_assumptions']['funding_cost_usdt']=funding
    result=calculate_rr(TradeSetup.model_validate(raw),quantity=2)
    assert result.reference.gross_rr==2
    assert result.reference.net_rr is None and result.reference.targets[0].net_rr is None
    assert result.status=='partial'
    assert 'NON_POSITIVE_NET_STOP_LOSS' in [i.code for i in result.issues]


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_costs_can_make_net_reward_negative_without_clamping(side):
    raw=raw_plan(side,fractions=(1,),rewards=(.01,),stop_distance=1)
    raw['cost_assumptions'].update(entry_fee_rate=.001,exit_fee_rate=.001)
    result=calculate_rr(TradeSetup.model_validate(raw))
    assert result.status=='complete'
    assert result.reference.net_rr<0 and result.reference.gross_rr==D('.01')
    assert result.reference.targets[0].net_rr<0 and not result.issues


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_explicit_zero_net_reward_is_zero_not_missing(side):
    raw=raw_plan(side,fractions=(1,),rewards=(1,),stop_distance=1)
    raw['cost_assumptions']['funding_cost_usdt']=1
    result=calculate_rr(TradeSetup.model_validate(raw),quantity=1)
    assert result.reference.net_rr==0 and result.status=='complete'


@pytest.mark.parametrize('side,field',[('LONG','exit_slippage_bps'),('SHORT','entry_slippage_bps')])
def test_nonpositive_slipped_price_yields_partial_result(side,field):
    raw=raw_plan(side);raw['cost_assumptions'][field]=10000
    result=calculate_rr(TradeSetup.model_validate(raw))
    assert result.reference.gross_rr==D('2.3') and result.reference.net_rr is None
    assert 'NON_POSITIVE_EFFECTIVE_PRICE' in [i.code for i in result.issues]
    assert result.status=='partial'


def test_no_targets_is_unavailable_and_does_not_create_fixed_rr_targets():
    raw=raw_plan();raw['targets']=[]
    result=calculate_rr(TradeSetup.model_validate(raw))
    assert result.status=='unavailable' and result.reference.targets==()
    assert result.reference.gross_rr is None and result.reference.net_rr is None
    assert result.reference.gross_risk_per_unit==2
    assert [i.code for i in result.issues]==['NO_TARGETS']


@pytest.mark.parametrize('fractions',[(.2,.3,.5000000001), (1/3,1/3,1/3)])
def test_schema_tolerated_fraction_residue_not_silently_renormalized(fractions):
    p=plan(fractions=fractions)
    result=calculate_rr(p)
    assert result.reference.gross_rr is None and result.reference.net_rr is None
    assert [t.fraction for t in result.reference.targets]==[D(str(f)) for f in fractions]
    assert all(t.gross_rr is not None and t.net_rr is not None for t in result.reference.targets)
    assert 'FRACTIONS_NOT_EXACTLY_ONE' in [i.code for i in result.issues]
    assert result.weights_policy=='as_supplied_no_normalization'


@pytest.mark.parametrize('quantity',[0,-1,True,'1',float('nan'),float('inf'),float('-inf')])
def test_bad_hypothetical_quantity_is_validation_error_not_order_action(quantity):
    with pytest.raises(ValidationError): calculate_rr(plan(),quantity=quantity)


@pytest.mark.parametrize('defect',['stop','fraction','target','price','cost'])
def test_unvalidated_model_copy_cannot_bypass_input_validation(defect):
    p=plan()
    if defect=='stop': p=p.model_copy(update={'initial_stop':p.initial_stop.model_copy(update={'price':100})})
    elif defect=='fraction': p=p.model_copy(update={'targets':(p.targets[0].model_copy(update={'fraction':.9}),*p.targets[1:])})
    elif defect=='target': p=p.model_copy(update={'targets':tuple(reversed(p.targets))})
    elif defect=='price': p=p.model_copy(update={'entry':p.entry.model_copy(update={'reference_price':float('nan')})})
    elif defect=='cost': p=p.model_copy(update={'cost_assumptions':p.cost_assumptions.model_copy(update={'exit_fee_rate':-1})})
    with pytest.raises(ValidationError): calculate_rr(p)


@pytest.mark.parametrize('symbol',['SOLUSD','SOLUSDC','BTCUSD','ETHBTC'])
def test_non_usdt_quotes_not_silently_converted_or_treated_as_linear_usdt(symbol):
    raw=raw_plan();raw['symbol']=symbol
    result=calculate_rr(TradeSetup.model_validate(raw))
    assert result.status=='unavailable' and result.reference is None
    assert result.entry_lower is None and result.entry_upper is None
    assert 'UNSUPPORTED_QUOTE' in [i.code for i in result.issues]


def test_json_roundtrip_immutability_and_no_order_permission():
    result=calculate_rr(plan(),quantity=2)
    assert RRCalculation.model_validate_json(result.model_dump_json())==result
    assert '"gross_rr":"2.3"' in result.model_dump_json()
    with pytest.raises(ValidationError): result.reference.gross_rr=D('99')
    for key,value in [('execution_authority','live'),('admission_status','approved')]:
        with pytest.raises(ValidationError): RRCalculation.model_validate({**result.model_dump(),key:value})


def test_deterministic_decimal_context_and_no_global_context_mutation():
    raw=raw_plan();raw['cost_assumptions'].update(entry_fee_rate=.001,exit_fee_rate=.002,funding_cost_usdt=1)
    p=TradeSetup.model_validate(raw)
    expected=calculate_rr(p,quantity=3).model_dump_json()
    with localcontext() as context:
        context.prec=3;context.rounding=ROUND_UP;context.traps[Inexact]=True
        assert calculate_rr(p,quantity=3).model_dump_json()==expected
        assert context.prec==3 and context.rounding==ROUND_UP and context.traps[Inexact]


@pytest.mark.parametrize('quantity',[1e-300,1e300])
def test_extreme_finite_scales_keep_decimal_outputs_finite(quantity):
    raw=raw_plan();raw['cost_assumptions']['funding_cost_usdt']=1
    result=calculate_rr(TradeSetup.model_validate(raw),quantity=quantity)
    assert result.reference.net_rr.is_finite()
    assert result.reference.net_stop_loss_usdt.is_finite()
    assert RRCalculation.model_validate_json(result.model_dump_json())==result


def test_small_structural_risk_not_rounded_to_zero_or_to_policy_rr():
    p=plan(fractions=(1,),stop_distance=.00000001,rewards=(.00000002,))
    result=calculate_rr(p)
    assert result.reference.gross_risk_per_unit==D('0.00000001')
    assert result.reference.gross_rr==2


def test_no_plan_mutation_even_when_stop_move_rules_and_rejections_exist():
    raw=raw_plan()
    raw['stop_movement']={'rules':[{'after_target_id':'tp-1','move_to':'entry'},
                                  {'after_target_id':'tp-2','move_to':'trailing','trailing_distance_r':1}]}
    raw['rejection_reasons']=[{'code':'SYNTHETIC_ONLY','message':'Prior descriptive note','source':'fixture'}]
    p=TradeSetup.model_validate(raw);before=p.model_dump_json()
    assert calculate_rr(p).reference.gross_rr==D('2.3')
    assert p.model_dump_json()==before and p.initial_stop.price==98


@pytest.mark.parametrize('strategy',['trend_breakout','pullback_entry','panic_rebound','fake_breakout_reverse'])
def test_legacy_signal_adapter_remains_explicit_unmodified_and_unverified(strategy):
    signal=Signal('legacy',strategy,'SOLUSDT','LONG',100,98,
                  [{'price':102,'fraction':.5},{'price':104,'fraction':.5}],NOW,score=90)
    before=asdict(signal);p=adapt_legacy_signal(signal);snapshot=p.model_dump_json()
    result=calculate_rr(p)
    assert result.reference.gross_rr==D('1.5') and result.reference.net_rr is None
    assert all(t.target_kind=='legacy_unspecified' for t in result.reference.targets)
    assert asdict(signal)==before and p.model_dump_json()==snapshot
    with pytest.raises(TypeError): calculate_rr(signal)
    with pytest.raises(TypeError): calculate_rr(p.model_dump())


def test_pure_calculation_never_loads_configuration_state_clock_or_network(monkeypatch):
    import os
    import app.config as config
    import app.execution.bridge_client as bridge
    p=plan();snapshot=p.model_dump_json()
    def forbidden(*args,**kwargs): raise AssertionError('RR calculation attempted IO or runtime access')
    with monkeypatch.context() as m:
        for name in ('load_config','load_secrets'): m.setattr(config,name,forbidden)
        m.setattr(bridge.BridgeClient,'__init__',forbidden)
        m.setattr(builtins,'open',forbidden)
        m.setattr(Path,'read_text',forbidden);m.setattr(Path,'write_text',forbidden)
        m.setattr(sqlite3,'connect',forbidden);m.setattr(socket.socket,'connect',forbidden)
        m.setattr(time,'time',forbidden);m.setattr(os,'getenv',forbidden)
        assert calculate_rr(p).reference.net_rr==D('2.3')
        assert p.model_dump_json()==snapshot


def test_rr_has_no_execution_dependencies_and_is_not_imported_by_runtime():
    root=Path(__file__).resolve().parents[1]
    for name in ('rr.py','rr_models.py'):
        tree=ast.parse((root/'app/setups'/name).read_text())
        allowed={'decimal','typing','pydantic','models','rr_models'}
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom): assert node.module in allowed
            elif isinstance(node,ast.Import): assert all(n.name in allowed for n in node.names)
    for file in (root/'app').rglob('*.py'):
        if 'setups' in file.parts: continue
        # Only the explicit Stage 5 pure sidecars may depend on setup arithmetic.
        if file.relative_to(root).as_posix() in {
            'app/admission/engine.py','app/admission/models.py',
            'app/admission/policy.py','app/admission/contract.py',
            'app/exits/bindings.py','app/exits/models.py',
            'app/exits/policy.py','app/exits/runner.py',
            # Stage 7 exact offline validation modules; never runtime/execution.
            'app/configuration/models.py','app/configuration/inputs.py',
            'app/configuration/contracts.py','app/configuration/examples.py',
            # 8A independent offline composition only; old runtime remains forbidden.
            'app/offline_paper/models.py','app/offline_paper/engine.py',
            'app/admitted_paper/models.py','app/admitted_paper/provider.py',
            'app/admitted_paper/scenarios.py','app/admitted_paper/gate.py',
            # 8C exact offline descriptors/calculators, never old runtime.
            'app/historical_replay/models.py','app/historical_replay/provider.py',
            'app/historical_replay/scenarios.py','app/historical_replay/gate.py',
            # 8D exact opt-in offline composition, not main/runtime wiring.
            'app/execution_costs/models.py','app/execution_costs/prices.py',
            'app/execution_costs/gate.py','app/execution_costs/engine.py','app/execution_costs/attribution.py',
            # 8E exact read-only diagnostics, not execution or admission wiring.
            'app/scenario_diagnostics/scenarios.py','app/scenario_diagnostics/proofs.py',
            'app/scenario_diagnostics/quantities.py','app/signal_research/costs.py'}: continue
        tree=ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom): assert not (node.module or '').startswith('app.setups')
            elif isinstance(node,ast.Import): assert not any(n.name.startswith('app.setups') for n in node.names)
    assert 'rr' not in (root/'app/setups/__init__.py').read_text()
