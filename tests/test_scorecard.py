"""Stage 4: synthetic inputs only; no market/account/network access."""

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
from app.setups.models import COVERAGE_KEYS, DataCoverage
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from app.setups.scorecard_models import DIMENSIONS, Scorecard, ScoreCheck

NOW = 1_800_000_000.0
D = Decimal


def raw_plan(side='LONG', order_type='MARKET'):
    sign = 1 if side == 'LONG' else -1
    evidence = [dict(evidence_id='entry', kind='range_boundary', price=100),
        dict(evidence_id='stop', kind='swing_low' if side == 'LONG' else 'swing_high', price=100-sign*4)]
    targets = []
    for i, (reward, fraction) in enumerate(zip((5, 10, 15), (.2, .3, .5)), 1):
        key = f'target-{i}'
        evidence.append(dict(evidence_id=key, kind='resistance' if side == 'LONG' else 'support', price=100+sign*reward))
        targets.append(dict(target_id=key, price=100+sign*reward, fraction=fraction,
            basis='Synthetic structural level', kind='structure', evidence_ids=[key]))
    for item in evidence:
        item.update(description='Synthetic evidence only', status='available', source='synthetic-fixture', observed_at=NOW-1)
    return dict(plan_version='score-fixture/v1', setup_id='score-fixture', symbol='SOLUSDT',
        strategy_name='synthetic', strategy_type='custom', side=side,
        structure_evidence=evidence,
        entry=dict(order_type=order_type, reference_price=100, lower_price=99.5, upper_price=100.5,
                   basis='structure_zone', evidence_ids=['entry']),
        initial_stop=dict(price=100-sign*5, basis='Synthetic swing invalidation', evidence_ids=['stop']),
        invalidation_conditions=[dict(condition_id='price', kind='price', description='Synthetic invalidation',
                                     operator='lte' if sign == 1 else 'gte', price=100-sign*5)],
        targets=targets, confidence=dict(value=.9, basis='Caller-supplied evidence confidence, not win rate'),
        market_state=dict(status='available', source='synthetic-fixture', observed_at=NOW-1, reference_price=100),
        cost_assumptions=dict(entry_fee_rate=0, exit_fee_rate=0, entry_slippage_bps=0,
            exit_slippage_bps=0, funding_cost_usdt=0, source='synthetic-fixture', observed_at=NOW-1),
        position_limit_advice=dict(max_quantity=2, max_notional_usdt=200),
        risk_budget=dict(max_loss_usdt=10), created_at=NOW, data_as_of=NOW-1, valid_until=NOW+60)


def plan(raw=None, **kwargs):
    raw = raw_plan(**kwargs) if raw is None else raw
    # Unused Stage 2 market-factor data remain explicitly missing, not filled in.
    raw.setdefault('data_coverage', dict(items=[dict(name=k, status='available', source='synthetic-fixture',
        observed_at=NOW-1) if k in ('structure', 'market_price') else dict(name=k) for k in COVERAGE_KEYS],
        coverage_ratio=.2, missing_items=[k for k in COVERAGE_KEYS if k not in ('structure', 'market_price')]))
    return TradeSetup.model_validate(raw)


def evaluate(p=None, quantity=1, **kwargs):
    p = plan() if p is None else p
    return score_trade_setup(p, calculate_rr(p, quantity=quantity), evaluated_at=NOW, **kwargs)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('order_type', ['MARKET', 'LIMIT'])
def test_exact_user_dimensions_hand_computed_complete_card(side, order_type):
    card = evaluate(plan(side=side, order_type=order_type))
    dims = [getattr(card, key) for key in DIMENSIONS]
    assert [x.score for x in dims] == [90, 96, 100, 100, 80, 100, 100]
    assert card.overall_trade_quality.score == 95.14
    assert card.overall_trade_quality.label == 'excellent'
    assert card.overall_trade_quality.coverage == 1
    assert card.overall_trade_quality.summary and card.overall_trade_quality.explanation
    assert len(DIMENSIONS) == 7  # Eighth is overall, not included in its own mean.
    assert {'trend', 'volume', 'rsi', 'funding_rate', 'news'}.isdisjoint(Scorecard.model_fields)
    for dim in (*dims, card.overall_trade_quality):
        assert 0 <= dim.score <= 100 and dim.explanation and dim.status == 'complete'
    assert card.admission_status == 'not_evaluated' and card.execution_authority == 'none'
    assert 'news' in card.input_missing_items  # Completeness is rubric-only, not all market data.


@pytest.mark.parametrize('confidence,label', [(0,'poor'),(.4999,'poor'),(.5,'fair'),(.6999,'fair'),
                                               (.7,'good'),(.8499,'good'),(.85,'excellent'),(1,'excellent')])
def test_label_boundaries_are_descriptive_not_win_rates(confidence, label):
    raw=raw_plan(); raw['confidence']['value']=confidence
    card=evaluate(plan(raw))
    assert card.direction_confidence.score == confidence*100
    assert card.direction_confidence.label == label
    assert card.interpretation == 'descriptive_heuristic_not_win_probability'
    assert card.admission_status == 'not_evaluated'


def test_unknown_confidence_is_not_inferred_from_side_rr_or_old_score():
    raw=raw_plan(); raw['confidence']={}; raw.update(score={'total':100,'legacy_score':100},grade='S')
    card=evaluate(plan(raw))
    assert card.direction_confidence.score is None and card.direction_confidence.label=='invalid'
    assert any(i.field=='confidence.value' for i in card.direction_confidence.issues)
    assert card.overall_trade_quality.score is None
    assert card.overall_trade_quality.known_points==82.29
    assert card.overall_trade_quality.coverage==pytest.approx(6/7)
    assert card.overall_trade_quality.label=='invalid'  # No available-only normalization.


@pytest.mark.parametrize('status',['missing','stale','unverified','not_applicable'])
def test_unavailable_evidence_is_structured_not_safe(status):
    raw=raw_plan(); raw['structure_evidence'][0]['status']=status
    card=evaluate(plan(raw))
    assert card.direction_confidence.score is None
    assert card.entry_quality.score is None
    assert status in [i.code for i in card.entry_quality.issues]
    assert card.entry_quality.known_points==56
    assert card.entry_quality.coverage==.6


@pytest.mark.parametrize('where', ['evidence','market','costs','coverage'])
def test_age_window_is_explicit_and_not_a_wall_clock_or_trading_gate(where):
    raw=raw_plan()
    if where=='evidence': raw['structure_evidence'][0]['observed_at']=NOW-301
    elif where=='market': raw['market_state']['observed_at']=NOW-301
    elif where=='costs': raw['cost_assumptions']['observed_at']=NOW-301
    p=plan(raw)
    if where=='coverage':
        payload=p.model_dump()
        next(i for i in payload['data_coverage']['items'] if i['name']=='structure')['observed_at']=NOW-301
        p=TradeSetup.model_validate(payload)
    card=evaluate(p)
    assert any(i.code=='stale' for i in card.overall_trade_quality.issues)
    assert card.overall_trade_quality.score is None
    assert evaluate(p, max_data_age_seconds=301).overall_trade_quality.score is not None


def test_unknown_structure_coverage_not_overridden_by_available_evidence():
    raw=raw_plan(); raw['data_coverage']={}
    card=evaluate(plan(raw))
    assert any(i.field=='data_coverage.structure' for i in card.direction_confidence.issues)
    assert card.direction_confidence.score is None


@pytest.mark.parametrize('status', ['missing', 'stale', 'unverified'])
def test_market_coverage_problem_not_overridden_by_snapshot(status):
    p=plan()
    coverage=DataCoverage.from_items(tuple(item.model_copy(update={'status':status})
        if item.name=='market_price' else item for item in p.data_coverage.items))
    card=evaluate(p.model_copy(update={'data_coverage':coverage}))
    assert card.entry_quality.score is None
    assert any(i.field=='data_coverage.market_price' and i.code==status for i in card.entry_quality.issues)


@pytest.mark.parametrize('reference,score', [(100,96),(101.5,88),(103,76),(110,56)])
def test_entry_distance_and_range_use_declared_r_not_new_indicators(reference, score):
    raw=raw_plan();raw['market_state']['reference_price']=reference
    assert evaluate(plan(raw)).entry_quality.score==score


def test_entry_structure_mismatch_is_observed_zero_not_missing():
    raw=raw_plan();raw['structure_evidence'][0]['price']=98
    result=evaluate(plan(raw)).entry_quality
    assert result.score==56 and result.checks[0].points==0 and not result.issues


@pytest.mark.parametrize('side', ['LONG','SHORT'])
def test_stop_structure_and_invalidation_wrong_geometry_lower_description_only(side):
    raw=raw_plan(side)
    raw['structure_evidence'][1]['price']=94 if side=='LONG' else 106
    raw['invalidation_conditions'][0]['operator']='gte' if side=='LONG' else 'lte'
    result=evaluate(plan(raw))
    assert result.stop_loss_quality.score==20 and result.stop_loss_quality.label=='poor'
    assert result.execution_clarity.score==80
    assert result.admission_status=='not_evaluated'


@pytest.mark.parametrize('field', ['price','kind'])
def test_stop_evidence_without_usable_anchor_not_scored_as_good(field):
    raw=raw_plan();raw['structure_evidence'][1][field]=None if field=='price' else 'indicator'
    result=evaluate(plan(raw)).stop_loss_quality
    assert result.score is None
    assert any(i.field.endswith('.'+field) for i in result.issues)


def test_absent_invalidation_is_unknown_and_not_invented_from_stop():
    raw=raw_plan();raw['invalidation_conditions']=[]
    result=evaluate(plan(raw))
    assert result.stop_loss_quality.score is None and result.stop_loss_quality.known_points==70
    assert result.execution_clarity.score is None and result.execution_clarity.known_points==80


@pytest.mark.parametrize('side', ['LONG','SHORT'])
def test_target_evidence_and_original_fraction_weighting(side):
    raw=raw_plan(side)
    raw['structure_evidence'][-1]['price']=110 if side=='LONG' else 90
    result=evaluate(plan(raw))
    assert result.take_profit_quality.score==75  # Half the target weight lacks structural reach.
    assert result.rr_quality.score==80  # Structure description does not rewrite RR.


def test_no_targets_returns_unknown_not_a_synthetic_target():
    raw=raw_plan();raw['targets']=[]
    result=evaluate(plan(raw))
    assert result.take_profit_quality.score is None and result.take_profit_quality.coverage==0
    assert result.rr_quality.score is None


def test_unverified_target_kind_is_explicitly_missing_quality():
    raw=raw_plan();raw['targets'][0]['kind']='unverified'
    result=evaluate(plan(raw))
    assert result.take_profit_quality.score is None
    assert any(i.field=='targets.target-1.kind' for i in result.take_profit_quality.issues)


def test_fraction_roundoff_is_not_silently_normalized():
    raw=raw_plan();raw['targets'][-1]['fraction']=.5000000001
    result=evaluate(plan(raw))
    assert result.take_profit_quality.checks[2].points==0
    assert result.execution_clarity.checks[3].points==0
    assert result.rr_quality.score is None


def rr_plan(value, side='LONG'):
    raw=raw_plan(side);sign=1 if side=='LONG' else -1
    raw['entry'].update(lower_price=100, upper_price=100)
    raw['targets']=raw['targets'][:1]
    raw['targets'][0].update(price=100+sign*5*value, fraction=1)
    raw['structure_evidence'][2]['price']=100+sign*5*value
    return raw


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('rr,score',[(.1,25),(.99,25),(1,45),(1.49,45),(1.5,60),(1.99,60),
                                   (2,80),(2.99,80),(3,100),(4,100),(8,100)])
def test_net_rr_quality_bands_no_admission_and_no_bonus_above_three(rr,score,side):
    result=evaluate(plan(rr_plan(rr,side)))
    assert result.rr_quality.score==score
    assert result.admission_status=='not_evaluated' and result.execution_authority=='none'


def test_range_quality_uses_worst_edge_not_only_reference_rr():
    raw=rr_plan(3);raw['entry'].update(lower_price=99,upper_price=101)
    result=evaluate(plan(raw))
    assert result.rr_quality.checks[0].points==50
    assert result.rr_quality.checks[1].points==40  # 14 / 6 = 2.333R.
    assert result.rr_quality.score==90


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_negative_net_reward_low_score_not_rejection(side):
    raw=rr_plan(.1,side);raw['cost_assumptions'].update(entry_fee_rate=.01,exit_fee_rate=.01)
    result=evaluate(plan(raw))
    assert result.rr_quality.score==0 and result.rr_quality.label=='poor'
    assert result.take_profit_quality.checks[-1].points==0
    assert result.admission_status=='not_evaluated'


@pytest.mark.parametrize('field',['entry_fee_rate','exit_fee_rate','entry_slippage_bps','exit_slippage_bps',
                                 'funding_cost_usdt','source','observed_at'])
def test_missing_cost_values_or_provenance_never_fallback_to_gross_rr(field):
    raw=raw_plan();raw['cost_assumptions'][field]=None
    result=evaluate(plan(raw))
    assert result.rr_quality.score is None and result.rr_quality.label=='invalid'
    assert result.execution_clarity.score is None
    assert result.take_profit_quality.score is None
    assert result.position_quality.score is None


def test_nonpositive_net_risk_from_funding_credit_is_unscorable():
    raw=raw_plan();raw['cost_assumptions']['funding_cost_usdt']=-10
    result=evaluate(plan(raw))
    assert result.rr_quality.score is None and result.position_quality.score is None


def test_nonzero_funding_without_explicit_quantity_remains_unknown():
    raw=raw_plan();raw['cost_assumptions']['funding_cost_usdt']=1
    result=evaluate(plan(raw), quantity=None)
    assert result.position_quality.score is None and result.rr_quality.score is None
    assert result.position_quality.coverage==0


def test_unknown_quantity_is_not_replaced_by_max_quantity_advice():
    result=evaluate(quantity=None)
    assert result.position_quality.score is None and result.position_quality.coverage==0
    assert result.rr_quality.score==80  # Zero funding permits per-unit arithmetic independently.
    assert any(i.field=='rr.hypothetical_quantity' for i in result.position_quality.issues)


@pytest.mark.parametrize('field,value,expected',[('max_quantity',1,100),('max_quantity',.99,65),
    ('max_quantity',0,65),('max_notional_usdt',100.5,100),('max_notional_usdt',100,65),
    ('max_loss_usdt',5.5,100),('max_loss_usdt',5,70),('max_loss_usdt',0,70)])
def test_position_quality_describes_hypothetical_limits_without_sizing(field,value,expected):
    raw=raw_plan()
    raw['risk_budget' if field=='max_loss_usdt' else 'position_limit_advice'][field]=value
    result=evaluate(plan(raw))
    assert result.position_quality.score==expected
    assert result.admission_status=='not_evaluated'


@pytest.mark.parametrize('field',['max_quantity','max_notional_usdt','max_loss_usdt'])
def test_each_unknown_size_limit_remains_unknown(field):
    raw=raw_plan();raw['risk_budget' if field=='max_loss_usdt' else 'position_limit_advice'][field]=None
    result=evaluate(plan(raw))
    assert result.position_quality.score is None
    assert any(i.field.endswith('.'+field) for i in result.position_quality.issues)


def test_expired_plan_changes_clarity_only_and_does_not_block_description():
    p=plan();result=score_trade_setup(p,calculate_rr(p,quantity=1),evaluated_at=NOW+60)
    assert result.execution_clarity.score==80
    assert result.overall_trade_quality.score is not None
    assert result.admission_status=='not_evaluated'


@pytest.mark.parametrize('field',['valid_until','data_as_of'])
def test_unknown_timing_never_means_unlimited_validity(field):
    raw=raw_plan();raw[field]=None
    result=evaluate(plan(raw))
    assert result.execution_clarity.score is None
    assert any(i.field==field for i in result.execution_clarity.issues)


@pytest.mark.parametrize('key,value',[('evaluated_at',NOW-1),('evaluated_at',True),
    ('evaluated_at','1800000000'),('evaluated_at',float('nan')),('evaluated_at',float('inf')),
    ('max_data_age_seconds',0),('max_data_age_seconds',-1),('max_data_age_seconds',True),
    ('max_data_age_seconds','300'),('max_data_age_seconds',float('inf'))])
def test_invalid_scoring_context_is_contract_error_not_trading_rejection(key,value):
    p=plan();kwargs={'evaluated_at':NOW,key:value}
    with pytest.raises((ValueError,TypeError)):
        score_trade_setup(p,calculate_rr(p),**kwargs)


@pytest.mark.parametrize('mutation',['setup_id','plan_version','symbol','side','cost','price','quantity','rr_value','edge','weights'])
def test_wrong_or_forged_rr_result_cannot_describe_a_different_plan(mutation):
    p=plan();rr=calculate_rr(p,quantity=1)
    if mutation in ('setup_id','plan_version','symbol'):
        rr=rr.model_copy(update={mutation:'OTHER'})
    elif mutation=='side': rr=rr.model_copy(update={'side':'SHORT'})
    elif mutation=='cost': rr=rr.model_copy(update={'cost_assumptions':rr.cost_assumptions.model_copy(update={'exit_fee_rate':.001})})
    elif mutation=='price': rr=rr.model_copy(update={'reference':rr.reference.model_copy(update={'entry_price':D(102)})})
    elif mutation=='quantity': rr=rr.model_copy(update={'hypothetical_quantity':D(2)})
    elif mutation=='rr_value': rr=rr.model_copy(update={'reference':rr.reference.model_copy(update={'net_rr':D(900)})})
    elif mutation=='edge': rr=rr.model_copy(update={'entry_upper':rr.entry_lower})
    elif mutation=='weights': rr=rr.model_copy(update={'fraction_sum':D('.9')})
    with pytest.raises(ValueError,match='RR input does not match'):
        score_trade_setup(p,rr,evaluated_at=NOW)


@pytest.mark.parametrize('malformation',['dict','subclass','geometry','confidence','nan'])
def test_bypassed_or_non_record_setup_is_revalidated(malformation):
    p=plan();rr=calculate_rr(p)
    if malformation=='dict': p=p.model_dump()
    elif malformation=='subclass':
        class FakeSetup(TradeSetup): pass
        p=FakeSetup.model_validate(p.model_dump())
    elif malformation=='geometry': p=p.model_copy(update={'initial_stop':p.initial_stop.model_copy(update={'price':120})})
    elif malformation=='confidence': p=p.model_copy(update={'confidence':p.confidence.model_copy(update={'value':2})})
    else: p=p.model_copy(update={'created_at':float('nan')})
    with pytest.raises((TypeError,ValueError)):
        score_trade_setup(p,rr,evaluated_at=NOW)


def test_unsupported_quote_does_not_invent_net_rr():
    raw=raw_plan();raw['symbol']='SOLUSD'
    result=evaluate(plan(raw))
    assert result.rr_status=='unavailable' and result.rr_quality.score is None
    assert result.position_quality.score is None


def test_frozen_json_round_trip_and_no_admission_override():
    card=evaluate()
    assert Scorecard.model_validate_json(card.model_dump_json())==card
    with pytest.raises(ValidationError): card.side='SHORT'
    with pytest.raises(ValidationError): card.rr_quality.score=0
    for field,value in [('admission_status','approved'),('execution_authority','live'),('eligible',True),('rsi',70)]:
        payload=card.model_dump();payload[field]=value
        with pytest.raises(ValidationError): Scorecard.model_validate(payload)


def test_checks_cannot_present_unknown_points_as_known():
    with pytest.raises(ValidationError):
        ScoreCheck(code='x',max_points=20,points=21,explanation='Synthetic',inputs=())
    with pytest.raises(ValidationError):
        ScoreCheck(code='x',max_points=20,points=None,explanation='Synthetic',inputs=())


def test_old_signal_and_stage2_score_remain_separate_unmodified():
    signal=Signal(id='synthetic-old',strategy='panic_rebound',symbol='SOLUSDT',side='LONG',
        entry_price=100,stop_price=95,take_profits=[dict(price=110,fraction=1)],created_at=NOW,score=88)
    before=asdict(signal);p=adapt_legacy_signal(signal);original=p.model_dump_json()
    card=score_trade_setup(p,calculate_rr(p),evaluated_at=NOW)
    assert p.score.legacy_score==88 and p.score.total is None
    assert card.direction_confidence.score is None and card.overall_trade_quality.score is None
    assert p.model_dump_json()==original and asdict(signal)==before
    assert len(p.score.components)==8 and p.score.components[0].dimension=='trend'


def test_scoring_does_not_mutate_rr_setup_targets_size_or_stops():
    p=plan();rr=calculate_rr(p,quantity=1)
    before=(p.model_dump_json(),rr.model_dump_json())
    evaluate(p)
    assert (p.model_dump_json(),rr.model_dump_json())==before
    assert p.score.total is None and p.grade is None and p.reward_risk.net_rr is None


def test_old_scores_grades_daily_budget_leverage_and_rejections_are_not_admission_inputs():
    p=plan();base=evaluate(p);raw=p.model_dump()
    raw.update(score={'total':0,'legacy_score':0},grade='C')
    raw['risk_budget'].update(remaining_daily_loss_usdt=0,max_risk_fraction_of_equity=0)
    raw['position_limit_advice'].update(leverage_cap=1000,max_margin_fraction_of_equity=0)
    raw['rejection_reasons']=[dict(code='EXISTING_RECORD',message='Synthetic record only',source='fixture')]
    q=TradeSetup.model_validate(raw);card=evaluate(q)
    for name in (*DIMENSIONS,'overall_trade_quality'):
        assert getattr(card,name)==getattr(base,name)
    assert card.recorded_rejection_codes==('EXISTING_RECORD',)
    assert q.rejection_reasons and calculate_rr(p,quantity=1)==calculate_rr(q,quantity=1)


def test_confidence_changes_score_only_and_never_rr():
    p=plan();raw=p.model_dump();raw['confidence']['value']=.2;q=TradeSetup.model_validate(raw)
    assert calculate_rr(p,quantity=1)==calculate_rr(q,quantity=1)
    assert evaluate(p).direction_confidence.score > evaluate(q).direction_confidence.score


def test_market_indicator_claims_do_not_create_extra_scoring_dimensions():
    p=plan();raw=p.model_dump()
    raw['market_state'].update(return_1m_pct=-99, return_3m_pct=99, return_5m_pct=-99,
        return_15m_pct=99, btc_return_3m_pct=-99, eth_return_3m_pct=99,
        volume_ratio=999, volatility_pct=999, buy_pressure=0, regime='high_volatility')
    assert evaluate(TradeSetup.model_validate(raw))==evaluate(p)


def test_stop_movement_declarations_are_not_executed_or_required_by_scorecard():
    p=plan();raw=p.model_dump()
    raw['stop_movement']={'rules':[dict(after_target_id='target-1',move_to='entry'),
        dict(after_target_id='target-2',move_to='trailing',trailing_distance_r=1)]}
    q=TradeSetup.model_validate(raw);before=q.model_dump_json()
    assert evaluate(q)==evaluate(p)
    assert q.model_dump_json()==before and q.initial_stop.price==95


def test_independent_decimal_context_and_determinism():
    p=plan();expected=evaluate(p)
    with localcontext() as context:
        context.prec=4;context.rounding=ROUND_UP;context.traps[Inexact]=True
        assert evaluate(p)==expected
    assert evaluate(p).model_dump_json()==expected.model_dump_json()


def test_evaluation_uses_no_io_network_accounts_configuration_or_clock(monkeypatch):
    p=plan();rr=calculate_rr(p,quantity=1)
    def forbidden(*args,**kwargs): raise AssertionError('Unexpected external capability')
    monkeypatch.setattr(builtins,'open',forbidden)
    monkeypatch.setattr(Path,'open',forbidden)
    monkeypatch.setattr(socket,'socket',forbidden)
    monkeypatch.setattr(sqlite3,'connect',forbidden)
    monkeypatch.setattr(time,'time',forbidden)
    monkeypatch.setenv('DATABASE_URL','forbidden-fixture')
    monkeypatch.setenv('SOL_LIVE_TRADING','true')
    assert score_trade_setup(p,rr,evaluated_at=NOW).execution_authority=='none'


def test_new_modules_are_pure_and_not_imported_into_existing_runtime():
    root=Path(__file__).resolve().parents[1]
    allowed={'__future__','decimal','typing','pydantic','models','rr','rr_models','scorecard_models'}
    new={'scorecard.py','scorecard_models.py'}
    for file in new:
        tree=ast.parse((root/'app/setups'/file).read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Import): assert all(x.name in allowed for x in node.names)
            if isinstance(node,ast.ImportFrom): assert node.module in allowed
    for path in [root/'main.py',*(root/'app').rglob('*.py')]:
        if path.name in new: continue
        # Stage 5 is another opt-in pure sidecar, not an existing runtime entry.
        if path.relative_to(root).as_posix() in {
            'app/admission/engine.py','app/admission/models.py',
            'app/admission/policy.py','app/admission/contract.py',
            'app/exits/bindings.py','app/exits/models.py',
            # Stage 7 exact offline descriptors/validators, no runtime imports.
            'app/configuration/models.py','app/configuration/inputs.py','app/configuration/contracts.py',
            'app/configuration/compiler.py','app/configuration/registry.py',
            'app/offline_paper/engine.py','app/admitted_paper/engine.py','app/admitted_paper/gate.py'}: continue
        assert 'scorecard' not in path.read_text().lower(), str(path.relative_to(root))
