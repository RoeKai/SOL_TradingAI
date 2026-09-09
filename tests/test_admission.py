"""Stage 5 synthetic Paper snapshots only. No private/real account operations."""

import ast
import builtins
from decimal import Decimal, Inexact, ROUND_UP, localcontext
from pathlib import Path
import socket
import sqlite3
import time

import pytest
import yaml
from pydantic import ValidationError

from app.admission.engine import admit_trade
from app.admission.contract import require_paper_admission
from app.admission.models import (AccountHardLimits, AdmissionContractError, AdmissionDecision,
    AdmissionRequest, EvidenceConfirmation, ExchangeConstraints, InvalidationReview, PaperRiskSnapshot, fingerprint)
from app.admission.policy import AdmissionPolicy, parse_admission_policy
from app.models import Signal
from app.setups.models import COVERAGE_KEYS, TradeSetup
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from test_scorecard import raw_plan, NOW

D=Decimal
ROOT=Path(__file__).resolve().parents[1]


def raw(side='LONG',order_type='MARKET'):
    data=raw_plan(side,order_type)
    sign=1 if side=='LONG' else -1
    for i,t in enumerate(data['targets']):
        t['price']=100+sign*(9+i*7)
        data['structure_evidence'][i+2]['price']=t['price']
    data['confidence']['value']=.95
    data['market_state'].update(regime='trend',bid=99.99,ask=100.01,volatility_pct=.5,
                               btc_return_3m_pct=.2,eth_return_3m_pct=.3)
    data['cost_assumptions'].update(entry_fee_rate=.0005,exit_fee_rate=.0005,
        entry_slippage_bps=10,exit_slippage_bps=10,assumed_holding_seconds=3600)
    data['risk_budget']['max_loss_usdt']=10
    data['position_limit_advice'].update(max_notional_usdt=500,max_margin_usdt=100,leverage_cap=5)
    data['data_coverage']=dict(items=[dict(name=k,status='available',source='synthetic',observed_at=NOW-1)
        for k in COVERAGE_KEYS],coverage_ratio=1,missing_items=[])
    return data


def case(data=None,*,side='LONG',order_type='MARKET',q=1):
    setup=TradeSetup.model_validate(raw(side,order_type) if data is None else data)
    rr=calculate_rr(setup,quantity=q)
    score=score_trade_setup(setup,rr,evaluated_at=NOW)
    policy=AdmissionPolicy()
    limits=AccountHardLimits(max_loss_per_trade_usdt=5,daily_loss_limit_usdt=20,max_trades_per_day=3,
        max_consecutive_losses=2,max_positions=1,max_leverage=5,max_margin_ratio='.2',max_margin_usdt=100,
        max_position_notional_usdt=500,max_position_quantity=100)
    account=PaperRiskSnapshot(instance_id='synthetic-paper-fixture',snapshot_revision=1,mode='paper',
        status='confirmed',source='paper-ledger-snapshot/v1',observed_at=NOW-1,day_started_at=NOW-1000,day_ends_at=NOW+86400,
        equity_usdt=500,available_margin_usdt=500,margin_used_usdt=0,day_realized_loss_usdt=0,
        unrealized_loss_usdt=0,reserved_risk_usdt=0,trades_today=0,consecutive_losses=0,positions=(),pending_entries=(),
        paused=False,reconciliation_clear=True,margin_mode='ISOLATED',configured_leverage=5,
        auto_add_margin_enabled=False,martingale_enabled=False,limits=limits)
    venue=ExchangeConstraints(exchange='BINANCE_USDT_M',symbol=setup.symbol,order_type=setup.entry.order_type,
        contract_type='linear_usdt',status='confirmed',source='synthetic-rules',observed_at=NOW-1,
        quantity_step='.001',min_quantity='.001',max_quantity=1000,min_notional_usdt=5,price_tick='.01',max_leverage=20)
    request=AdmissionRequest(request_id='synthetic-entry-request',risk_budget_usdt=5,action='OPEN',leverage=5,
        margin_mode='ISOLATED',auto_add_margin=False,loss_recovery_sizing=False,sizing_basis='quality_risk_budget',
        invalidation_review=InvalidationReview(setup_digest=fingerprint(setup),all_conditions_clear=True,
                                               verifier='paper-structure-review/v1',checked_at=NOW),
        confirmations=tuple(EvidenceConfirmation(evidence_id=e.evidence_id,evidence_digest=fingerprint(e),
            verified=True,verifier='paper-structure-review/v1',checked_at=NOW) for e in setup.structure_evidence))
    return dict(setup=setup,rr=rr,scorecard=score,account=account,exchange=venue,request=request,policy=policy,evaluated_at=NOW)


def change(data,record,**values):
    data[record]=data[record].model_copy(update=values)
    return data


def rejected(data,code):
    decision=admit_trade(**data)
    assert decision.result=='REJECT', decision.model_dump_json(indent=2)
    assert code in decision.reason_codes, decision.reason_codes
    assert decision.max_quantity==decision.allowed_risk_budget_usdt==decision.max_notional_usdt==0
    assert decision.live_allowed is False and decision.execution_authority=='none_until_paper_integration'
    return decision


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('order_type',['MARKET','LIMIT'])
def test_high_quality_plan_approved_with_risk_first_size(side,order_type):
    data=case(side=side,order_type=order_type);result=admit_trade(**data)
    assert result.result=='APPROVE',result.reason_codes
    assert result.opportunity_tier=='S' and result.hard_gates_passed
    assert 0<result.modeled_stop_loss_usdt<=result.allowed_risk_budget_usdt<=5
    assert result.max_quantity%D('.001')==0
    assert result.max_quantity==result.final_rr.hypothetical_quantity
    assert result.final_rr==calculate_rr(data['setup'],quantity=float(result.max_quantity))
    assert result.final_min_net_rr>=result.required_net_rr>=data['policy'].minimum_net_rr
    assert result.max_initial_margin_usdt+result.entry_fee_reserve_usdt<=100
    assert result.leverage==5 and result.valid_until==NOW+4
    assert result.binding.setup==fingerprint(data['setup'])
    assert len(result.constraints)>=6 and result.reason_codes


def test_high_score_cannot_override_low_net_rr():
    data=case();assert data['scorecard'].overall_trade_quality.score>90
    change(data,'policy',minimum_net_rr=D(4))
    result=rejected(data,'NET_RR_BELOW_HARD_FLOOR')
    assert not result.hard_gates_passed


def test_high_rr_with_missing_structure_rejected():
    data=raw()
    for i,t in enumerate(data['targets']):
        t['price']=200+100*i;data['structure_evidence'][i+2]['price']=t['price']
    data['initial_stop']['evidence_ids']=[]
    args=case(data);assert args['rr'].reference.net_rr>30
    rejected(args,'STOP_STRUCTURE_MISSING')


def test_high_overall_score_with_bad_stop_quality_rejected():
    data=raw();data['invalidation_conditions'][0]['operator']='gte'
    args=case(data)
    assert args['scorecard'].overall_trade_quality.score>85
    assert args['scorecard'].stop_loss_quality.score<80
    rejected(args,'STOP_INVALIDATION_INCONSISTENT')


def test_high_total_does_not_override_configured_critical_dimension_floor():
    args=case()
    floors=tuple(x.model_copy(update={'minimum':97}) if x.dimension=='entry_quality' else x
                 for x in args['policy'].dimension_floors)
    change(args,'policy',dimension_floors=floors)
    result=rejected(args,'DIMENSION_SCORE_BELOW_MINIMUM')
    assert result.hard_gates_passed and args['scorecard'].overall_trade_quality.score>90


@pytest.mark.parametrize('field',['market_state','structure_evidence','cost_assumptions','data_coverage'])
@pytest.mark.parametrize('status',['missing','stale','unverified'])
def test_required_stale_missing_unverified_inputs_never_default_safe(field,status):
    data=raw()
    if field=='market_state': data[field]['status']=status
    elif field=='structure_evidence': data[field][0]['status']=status
    elif field=='cost_assumptions': data[field]['source']=None
    else:
        data[field]['items'][0]['status']=status
        data[field]['coverage_ratio']=.9;data[field]['missing_items']=['market_price']
    rejected(case(data),'UNCONFIRMED_DATA')


@pytest.mark.parametrize('record,field,value',[
    ('account','observed_at',NOW-6),('exchange','observed_at',NOW-3601),
    ('account','observed_at',NOW+1),('account','day_ends_at',NOW),('account','day_started_at',NOW+1)])
def test_state_freshness_and_daily_epoch(record,field,value):
    args=change(case(),record,**{field:value})
    rejected(args,'RISK_DAY_MISMATCH' if field.startswith('day_') else 'STALE_OR_FUTURE_DATA')


@pytest.mark.parametrize('loss,unrealized',[(20,0),(19,1),(21,0)])
def test_daily_loss_limit_cannot_be_overridden(loss,unrealized):
    rejected(change(case(),'account',day_realized_loss_usdt=D(loss),unrealized_loss_usdt=D(unrealized)),'DAILY_LOSS_LIMIT')


@pytest.mark.parametrize('field,value,code',[
    ('consecutive_losses',2,'CONSECUTIVE_LOSS_HALT'),('trades_today',3,'DAILY_TRADE_LIMIT'),
    ('paused',True,'ACCOUNT_HALTED_OR_UNRECONCILED'),('reconciliation_clear',False,'ACCOUNT_HALTED_OR_UNRECONCILED'),
    ('margin_mode','CROSS','ISOLATED_MARGIN_REQUIRED'),('auto_add_margin_enabled',True,'AUTO_MARGIN_FORBIDDEN'),
    ('martingale_enabled',True,'MARTINGALE_OR_RECOVERY_FORBIDDEN'),('configured_leverage',6,'LEVERAGE_LIMIT_OR_MISMATCH'),
    ('mode','live','PAPER_ONLY'),('source','untrusted-fixture','UNCONFIRMED_DATA')])
def test_account_hard_conditions(field,value,code):
    rejected(change(case(),'account',**{field:value}),code)


@pytest.mark.parametrize('field',[
    'day_started_at','day_ends_at','equity_usdt','available_margin_usdt','margin_used_usdt',
    'day_realized_loss_usdt','unrealized_loss_usdt','reserved_risk_usdt','trades_today','consecutive_losses',
    'positions','pending_entries','paused','reconciliation_clear','margin_mode','configured_leverage',
    'auto_add_margin_enabled','martingale_enabled'])
def test_each_unknown_account_state_is_rejected(field):
    rejected(change(case(),'account',**{field:None}),'ACCOUNT_STATE_UNKNOWN')


@pytest.mark.parametrize('field',list(AccountHardLimits.model_fields))
def test_each_unknown_account_hard_limit_rejected(field):
    args=case();limits=args['account'].limits.model_copy(update={field:None})
    rejected(change(args,'account',limits=limits),'ACCOUNT_HARD_LIMIT_UNKNOWN')


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_any_same_symbol_position_conflicts_and_prevents_adding(side):
    from app.admission.models import PositionExposure
    pos=PositionExposure(symbol='SOLUSDT',side=side,quantity=1)
    rejected(change(case(),'account',positions=(pos,)),'CONFLICTING_POSITION_OR_ORDER')


def test_pending_intent_counts_as_conflict_and_reserved_slot():
    from app.admission.models import PendingEntry
    pending=PendingEntry(intent_id='synthetic-pending',symbol='SOLUSDT',side='LONG')
    args=change(case(),'account',pending_entries=(pending,),trades_today=2)
    result=rejected(args,'CONFLICTING_POSITION_OR_ORDER')
    assert {'DAILY_TRADE_LIMIT','MAX_POSITIONS_LIMIT'}.issubset(result.reason_codes)


@pytest.mark.parametrize('field,value,code',[
    ('action','ADD','ADDING_FORBIDDEN'),('margin_mode','CROSS','ISOLATED_MARGIN_REQUIRED'),
    ('auto_add_margin',True,'AUTO_MARGIN_FORBIDDEN'),('loss_recovery_sizing',True,'MARTINGALE_OR_RECOVERY_FORBIDDEN'),
    ('sizing_basis','martingale','MARTINGALE_OR_RECOVERY_FORBIDDEN'),('sizing_basis','unknown','MARTINGALE_OR_RECOVERY_FORBIDDEN'),
    ('risk_budget_usdt',D('5.01'),'SINGLE_TRADE_RISK_LIMIT'),('leverage',6,'LEVERAGE_LIMIT_OR_MISMATCH')])
def test_request_hard_limits(field,value,code):
    rejected(change(case(),'request',**{field:value}),code)


@pytest.mark.parametrize('field',['risk_budget_usdt','action','leverage','margin_mode','auto_add_margin','loss_recovery_sizing'])
def test_unknown_intent_safety_never_defaults_to_allowed(field):
    rejected(change(case(),'request',**{field:None}),'REQUEST_STATE_UNKNOWN')


def test_remaining_daily_risk_reduces_without_changing_geometry():
    args=case();before=args['setup'].model_dump_json()
    normal=admit_trade(**args)
    change(args,'account',day_realized_loss_usdt=D(17),reserved_risk_usdt=D(1))
    reduced=admit_trade(**args)
    assert reduced.result=='REDUCE' and 'RISK_REDUCED_DAILY_REMAINING' in reduced.reason_codes
    assert reduced.allowed_risk_budget_usdt<=2 and reduced.max_quantity<normal.max_quantity
    assert reduced.plan_terms==normal.plan_terms
    assert args['setup'].model_dump_json()==before
    assert reduced.leverage==normal.leverage  # Score/remaining budget never select leverage.


@pytest.mark.parametrize('reserved',[D('19.9'),D(20),D(21)])
def test_reserved_risk_prevents_spending_same_daily_budget_twice(reserved):
    rejected(change(case(),'account',reserved_risk_usdt=reserved),'RISK_BUDGET_EXHAUSTED')


def test_score_tier_soft_reduction_configurable_without_rr_or_leverage_mapping():
    args=case();normal=admit_trade(**args)
    tiers=tuple(t.model_copy(update={'minimum_total':99}) if t.name=='S' else t for t in args['policy'].tiers)
    change(args,'policy',tiers=tiers)
    result=admit_trade(**args)
    assert result.result=='REDUCE' and result.opportunity_tier=='A'
    assert 'RISK_REDUCED_SCORE' in result.reason_codes and result.allowed_risk_budget_usdt<=D('3.75')
    assert result.max_quantity<normal.max_quantity and result.leverage==normal.leverage
    assert result.plan_terms==normal.plan_terms


def test_range_regime_reduces_risk_and_unknown_regime_rejects():
    data=raw();data['market_state']['regime']='range'
    result=admit_trade(**case(data))
    assert result.result=='REDUCE' and result.allowed_risk_budget_usdt<=D('2.5')
    assert 'RISK_REDUCED_MARKET' in result.reason_codes
    data['market_state']['regime']='unknown'
    rejected(case(data),'MARKET_REGIME_NOT_ALLOWED')


def test_policy_cannot_relax_account_hard_risk_leverage_or_margin_caps():
    args=case();change(args,'policy',max_loss_per_trade_usdt=D(500),max_leverage=100,
        max_margin_ratio=D(1),max_margin_usdt=D(500),max_position_quantity=D(10000),max_position_notional_usdt=D(100000))
    result=admit_trade(**args)
    assert result.allowed_risk_budget_usdt<=5 and result.max_initial_margin_usdt<=100 and result.leverage==5
    rejected(change(args,'request',risk_budget_usdt=D(6)),'SINGLE_TRADE_RISK_LIMIT')
    args=change(args,'request',risk_budget_usdt=D(5),leverage=6)
    rejected(args,'LEVERAGE_LIMIT_OR_MISMATCH')


@pytest.mark.parametrize('field,value,reason',[
    ('available_margin_usdt',D(10),'SIZE_REDUCED_AVAILABLE_MARGIN'),
    ('margin_used_usdt',D(95),'SIZE_REDUCED_AVAILABLE_MARGIN')])
def test_margin_headroom_reduces_quantity_instead_of_raising_leverage(field,value,reason):
    args=case()
    if field=='margin_used_usdt': change(args,'account',available_margin_usdt=D(405))
    change(args,'account',**{field:value})
    result=admit_trade(**args)
    assert result.result=='REDUCE',result.reason_codes
    assert reason in result.reason_codes and result.leverage==5
    room=min(args['account'].available_margin_usdt,100-args['account'].margin_used_usdt)
    assert result.max_initial_margin_usdt+result.entry_fee_reserve_usdt<=room


def test_margin_inconsistency_is_not_treated_as_available_cash():
    rejected(change(case(),'account',margin_used_usdt=D(50)),'ACCOUNT_BALANCE_INCONSISTENT')


@pytest.mark.parametrize('quantity,step,minimum,expected',[
    (D('.4'),D('.03'),D('.001'),'REDUCE'),(D('.04'),D('.03'),D('.001'),'REJECT'),
    (D('.4'),D(1),D('.001'),'REJECT')])
def test_exchange_quantity_precision_and_minimums_never_round_up(quantity,step,minimum,expected):
    args=change(case(),'exchange',max_quantity=quantity,quantity_step=step,min_quantity=minimum)
    result=admit_trade(**args)
    assert result.result==expected,result.reason_codes
    if expected=='REDUCE':
        assert result.max_quantity==D('.39') and result.max_quantity<=quantity
    else: assert 'EXCHANGE_MINIMUM_EXCEEDS_BUDGET' in result.reason_codes


@pytest.mark.parametrize('field',['quantity_step','min_quantity','max_quantity','min_notional_usdt','price_tick','max_leverage'])
def test_unknown_exchange_constraints_block(field):
    rejected(change(case(),'exchange',**{field:None}),'EXCHANGE_RULES_UNKNOWN')


@pytest.mark.parametrize('field,value',[
    ('exchange','OTHER'),('symbol','ETHUSDT'),('order_type','LIMIT'),('contract_type','inverse')])
def test_rules_must_match_current_exchange_symbol_and_order_type(field,value):
    rejected(change(case(),'exchange',**{field:value}),'EXCHANGE_RULES_MISMATCH')


@pytest.mark.parametrize('field',['entry_fee_rate','exit_fee_rate','entry_slippage_bps','exit_slippage_bps'])
def test_missing_or_optimistic_costs_cannot_be_hidden_by_high_rr(field):
    data=raw();data['cost_assumptions'][field]=0
    rejected(case(data),'COST_ASSUMPTION_OUT_OF_BOUNDS')
    data['cost_assumptions'][field]=None
    result=rejected(case(data),'COSTS_UNKNOWN')
    assert 'NET_RR_UNAVAILABLE' in result.reason_codes


@pytest.mark.parametrize('field',['funding_cost_usdt','assumed_holding_seconds'])
def test_funding_horizon_must_be_explicit(field):
    data=raw();data['cost_assumptions'][field]=None
    rejected(case(data),'FUNDING_HORIZON_UNKNOWN')


def test_positive_fixed_funding_resized_rr_must_be_rechecked():
    data=raw();data['cost_assumptions']['funding_cost_usdt']=2
    args=case(data,q=2)
    # High score at the larger arithmetic reference quantity cannot hide worse net RR at final Q.
    assert args['scorecard'].overall_trade_quality.score>85 and min(args['rr'].entry_lower.net_rr,args['rr'].entry_upper.net_rr)>2
    result=admit_trade(**args)
    assert result.result=='REJECT' and 'RESIZED_NET_RR_BELOW_FLOOR' in result.reason_codes


def test_expected_funding_credit_cannot_fund_larger_size():
    args=case();base=admit_trade(**args)
    data=raw();data['cost_assumptions']['funding_cost_usdt']=-.1
    credit=admit_trade(**case(data))
    assert credit.result in ('APPROVE','REDUCE')
    assert credit.max_quantity<=base.max_quantity
    assert credit.modeled_stop_loss_usdt<credit.allowed_risk_budget_usdt


def test_requested_risk_and_quality_do_not_use_loss_multipliers():
    args=case();normal=admit_trade(**args)
    later=admit_trade(**change(args,'account',consecutive_losses=1,day_realized_loss_usdt=D(1)))
    assert later.max_quantity<=normal.max_quantity and later.leverage==normal.leverage


@pytest.mark.parametrize('field,value,code',[
    ('btc_return_3m_pct',-.8,'BTC_CRASH_LONG_BLOCK'),('volatility_pct',3.01,'ABNORMAL_VOLATILITY'),
    ('reference_price',120,'ENTRY_DEVIATION_LIMIT'),('ask',101,'ABNORMAL_SPREAD')])
def test_market_hard_limits(field,value,code):
    data=raw();data['market_state'][field]=value
    rejected(case(data),code)


@pytest.mark.parametrize('field',['bid','ask','reference_price','volatility_pct','btc_return_3m_pct','eth_return_3m_pct'])
def test_unknown_market_state_never_defaults_to_low_volatility_or_safe_btc(field):
    data=raw();data['market_state'][field]=None
    if field=='reference_price': data['market_state']['status']='missing'
    rejected(case(data),'MARKET_STATE_UNKNOWN')


def test_stop_target_precision_is_not_fixed_by_changing_prices():
    data=raw();data['initial_stop']['price']=95.005
    data['invalidation_conditions'][0]['price']=95.005
    rejected(case(data),'PRICE_PRECISION_INVALID')


@pytest.mark.parametrize('field,value',[
    ('verified',False),('verified',None),('verifier','self-asserted'),('evidence_digest','f'*64)])
def test_confirmation_cannot_be_forged_or_reused_for_changed_evidence(field,value):
    args=case();confirmations=list(args['request'].confirmations)
    confirmations[0]=confirmations[0].model_copy(update={field:value})
    rejected(change(args,'request',confirmations=tuple(confirmations)),'EVIDENCE_NOT_CONFIRMED')


@pytest.mark.parametrize('field,value',[
    ('all_conditions_clear',False),('all_conditions_clear',None),('verifier','untrusted'),('setup_digest','f'*64)])
def test_invalidation_review_is_bound_to_current_plan(field,value):
    args=case();review=args['request'].invalidation_review.model_copy(update={field:value})
    rejected(change(args,'request',invalidation_review=review),'INVALIDATION_STATE_UNCONFIRMED')


def test_missing_invalidation_confirmation_is_not_assumed_clear():
    rejected(change(case(),'request',invalidation_review=None),'INVALIDATION_STATE_UNCONFIRMED')


@pytest.mark.parametrize('stamp,code',[(NOW-1,'INVALIDATION_REVIEW_PREDATES_PLAN'),(NOW-16,'STALE_OR_FUTURE_DATA'),(NOW+1,'STALE_OR_FUTURE_DATA')])
def test_invalidation_review_timestamp(stamp,code):
    args=case();review=args['request'].invalidation_review.model_copy(update={'checked_at':stamp})
    rejected(change(args,'request',invalidation_review=review),code)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('kind',['time','quote'])
def test_triggered_invalidation_not_overridden_by_clear_claim(side,kind):
    data=raw(side)
    if kind=='time':
        data['invalidation_conditions'].append(dict(condition_id='deadline',kind='time',description='Synthetic expired condition',at=NOW))
    else:
        quote=94 if side=='LONG' else 106
        data['market_state'].update(bid=quote-.01,ask=quote+.01)
    result=rejected(case(data),'PLAN_INVALIDATED')
    assert not result.hard_gates_passed


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_executable_quote_not_just_reference_price_must_fit_market_plan(side):
    data=raw(side);data['market_state'].update(bid=100.69,ask=100.71)
    # Reference remains 100 and spread is tiny, but executable quotes exceed the modeled interval.
    rejected(case(data),'EXECUTABLE_QUOTE_OUTSIDE_PLAN')


def test_unreferenced_optional_evidence_does_not_shorten_validity():
    data=raw();data['structure_evidence'].append(dict(evidence_id='unused',kind='other',description='Unused old note',
        status='unverified',source='synthetic-old',observed_at=NOW-99999))
    decision=admit_trade(**case(data))
    assert decision.result=='APPROVE' and decision.valid_until==NOW+4


def test_optional_not_applicable_data_vs_policy_required_data():
    data=raw()
    for item in data['data_coverage']['items']:
        if item['name']=='news': item.update(status='not_applicable',required=False)
    args=case(data)
    assert admit_trade(**args).result=='APPROVE'
    change(args,'policy',required_data=(*args['policy'].required_data,'news'))
    rejected(args,'UNCONFIRMED_DATA')


def test_missing_required_key_and_low_coverage_are_distinct_hard_gates():
    args=case();change(args,'policy',required_data=(*args['policy'].required_data,'extra_required'))
    rejected(args,'REQUIRED_DATA_MISSING')
    data=raw()
    for item in data['data_coverage']['items']:
        if item['name'] in ('news','funding_rate','liquidation_zones'): item.update(status='missing',required=False)
    data['data_coverage'].update(coverage_ratio=.7,missing_items=['funding_rate','liquidation_zones','news'])
    rejected(case(data),'DATA_COVERAGE_LOW')


@pytest.mark.parametrize('budget',[D(1),D(3),D(5)])
@pytest.mark.parametrize('leverage',[1,2,5])
@pytest.mark.parametrize('margin',[D(1),D(10),D(100)])
def test_sizing_constraint_grid_never_exceeds_any_cap(budget,leverage,margin):
    args=case();change(args,'request',risk_budget_usdt=budget,leverage=leverage)
    change(args,'account',available_margin_usdt=margin,configured_leverage=leverage)
    decision=admit_trade(**args)
    if decision.result=='REJECT':
        assert decision.allowed_risk_budget_usdt==decision.max_quantity==0
        return
    assert decision.allowed_risk_budget_usdt<=budget
    assert decision.max_initial_margin_usdt+decision.entry_fee_reserve_usdt<=margin
    assert all(decision.max_quantity<=limit.quantity_cap for limit in decision.constraints)
    assert decision.leverage==leverage and decision.final_min_net_rr>=decision.required_net_rr
    assert decision.plan_terms.initial_stop==D(str(args['setup'].initial_stop.price))
    assert decision.plan_terms.side==args['setup'].side


def test_stage3_reference_quantity_is_not_mistaken_for_an_order_size():
    small=admit_trade(**case(q=.25));large=admit_trade(**case(q=2))
    assert small.result==large.result=='APPROVE'
    assert small.max_quantity==large.max_quantity>D('.25')
    # Stage 3's quantity is hypothetical; request.risk_budget is the actual admission ceiling.


def test_unknown_score_never_inferred_from_old_signal_score():
    data=raw();data['confidence']={};data['score']={'total':100,'legacy_score':100};data['grade']='S'
    rejected(case(data),'SCORECARD_INCOMPLETE')


@pytest.mark.parametrize('record,field,value,reason',[
    ('rr','setup_id','different','RR_PLAN_MISMATCH'),('rr','plan_version','v2','RR_PLAN_MISMATCH'),
    ('rr','hypothetical_quantity',D(2),'RR_PLAN_MISMATCH'),
    ('scorecard','plan_version','v2','SCORECARD_PLAN_MISMATCH'),('scorecard','symbol','ETHUSDT','SCORECARD_PLAN_MISMATCH')])
def test_plan_version_content_binding(record,field,value,reason):
    rejected(change(case(),record,**{field:value}),reason)


def test_forged_high_overall_score_is_not_trusted():
    args=case();card=args['scorecard']
    fake=card.overall_trade_quality.model_copy(update={'score':100,'known_points':100})
    rejected(change(args,'scorecard',overall_trade_quality=fake),'SCORECARD_PLAN_MISMATCH')


def test_invalid_model_copy_bypasses_are_rejected():
    args=case();p=args['setup'];change(args,'setup',initial_stop=p.initial_stop.model_copy(update={'price':120}))
    rejected(args,'INPUT_CONTRACT_INVALID')


def test_config_template_matches_model_and_thresholds_are_editable():
    policy=parse_admission_policy((ROOT/'admission.yaml').read_text())
    assert policy==AdmissionPolicy()
    args=case();change(args,'policy',minimum_total_score=99)
    rejected(args,'TOTAL_SCORE_BELOW_MINIMUM')
    change(args,'policy',minimum_total_score=50)
    assert admit_trade(**args).result=='APPROVE'


@pytest.mark.parametrize('text',[
    'mode: live','max_leverage: true','enabled: "false"','minimum_total_score: "70"',
    'minimum_net_rr: -1','max_margin_ratio: 1.1','max_positions: 0','allow_live: true',
    'enabled: true\nenabled: false','a: &x 1\nb: *x','!!python/object/apply:os.system ["false"]',
    '[]','', 'minimum_net_rr: ${SOME_ENV_VALUE}'])
def test_invalid_policy_duplicate_yaml_and_live_overrides_rejected(text):
    with pytest.raises((ValueError,TypeError,yaml.YAMLError)):
        parse_admission_policy(text)


def test_tiers_cannot_lower_rr_floor_for_higher_scores():
    policy=AdmissionPolicy();raw_policy=policy.model_dump()
    raw_policy['tiers'][0]['minimum_net_rr']='1'
    with pytest.raises(ValidationError): AdmissionPolicy.model_validate(raw_policy)
    args=case(); tiers=tuple(t.model_copy(update={'minimum_net_rr':D('1')}) for t in policy.tiers)
    markets=tuple(m.model_copy(update={'minimum_net_rr':D('1')}) for m in policy.markets)
    change(args,'policy',tiers=tiers,markets=markets,minimum_net_rr=D(4))
    rejected(args,'NET_RR_BELOW_HARD_FLOOR')


def test_decisions_round_trip_immutable_and_reject_has_no_capability():
    decision=admit_trade(**case())
    assert AdmissionDecision.model_validate_json(decision.model_dump_json())==decision
    with pytest.raises(ValidationError): decision.max_quantity=100
    raw_decision=decision.model_dump();raw_decision['live_allowed']=True
    with pytest.raises(ValidationError): AdmissionDecision.model_validate(raw_decision)
    raw_decision=decision.model_dump();raw_decision['result']='REJECT'
    with pytest.raises(ValidationError): AdmissionDecision.model_validate(raw_decision)


def test_no_plan_rr_score_or_snapshot_mutation_and_independent_decimal_context():
    args=case();before={k:v.model_dump_json() for k,v in args.items() if hasattr(v,'model_dump_json')}
    expected=admit_trade(**args)
    with localcontext() as c:
        c.prec=4;c.rounding=ROUND_UP;c.traps[Inexact]=True
        assert admit_trade(**args)==expected
    assert before=={k:v.model_dump_json() for k,v in args.items() if hasattr(v,'model_dump_json')}


def test_gate_and_contract_do_not_read_files_network_clock_or_real_accounts(monkeypatch):
    args=case();decision=admit_trade(**args)
    import app.config as config
    import app.execution.bridge_client as bridge
    def forbidden(*a,**kw): raise AssertionError('External capability reached')
    with monkeypatch.context() as m:
        m.setattr(builtins,'open',forbidden);m.setattr(Path,'read_text',forbidden)
        m.setattr(socket,'socket',forbidden);m.setattr(sqlite3,'connect',forbidden);m.setattr(time,'time',forbidden)
        m.setattr(config,'load_config',forbidden);m.setattr(config,'load_secrets',forbidden)
        m.setattr(bridge.BridgeClient,'__init__',forbidden)
        m.setenv('SOL_LIVE_TRADING','true');m.setenv('DATABASE_URL','forbidden-fixture')
        assert admit_trade(**args)==decision
        assert require_paper_admission(decision,**args,quantity=decision.max_quantity)==decision


def test_future_contract_accepts_only_exact_bound_decision_and_rechecks_freshness():
    args=case();decision=admit_trade(**args)
    assert require_paper_admission(decision,**args,quantity=decision.max_quantity)==decision
    args['evaluated_at']=NOW+1
    refreshed=require_paper_admission(decision,**args,quantity=decision.max_quantity)
    assert refreshed.evaluated_at==NOW+1 and refreshed.max_quantity==decision.max_quantity
    args['evaluated_at']=decision.valid_until
    with pytest.raises(AdmissionContractError): require_paper_admission(decision,**args,quantity=decision.max_quantity)


@pytest.mark.parametrize('mutation',['revision','balance','risk','policy','request','quantity','smaller','forged','reject'])
def test_future_contract_prevents_stale_changed_or_forged_permissions(mutation):
    args=case();decision=admit_trade(**args);quantity=decision.max_quantity
    if mutation=='revision': change(args,'account',snapshot_revision=2)
    elif mutation=='balance': change(args,'account',equity_usdt=D(600))
    elif mutation=='risk': change(args,'account',consecutive_losses=2)
    elif mutation=='policy': change(args,'policy',minimum_net_rr=D(10))
    elif mutation=='request': change(args,'request',risk_budget_usdt=D(4))
    elif mutation=='quantity': quantity+=D('.001')
    elif mutation=='smaller': quantity-=D('.001')
    elif mutation=='forged': decision=decision.model_copy(update={'max_quantity':D(100)});quantity=D(100)
    else: decision=admit_trade(**change(args,'account',paused=True))
    with pytest.raises(AdmissionContractError): require_paper_admission(decision,**args,quantity=quantity)


@pytest.mark.parametrize('kind',['signal','setup','dict'])
def test_old_signal_or_naked_setup_cannot_bypass_future_contract(kind):
    args=case();decision=admit_trade(**args)
    signal=Signal('synthetic','panic_rebound','SOLUSDT','LONG',100,95,[{'price':110,'fraction':1}],NOW)
    naked={'signal':signal,'setup':args['setup'],'dict':decision.model_dump()}[kind]
    with pytest.raises(AdmissionContractError): require_paper_admission(naked,**args,quantity=decision.max_quantity)
    if kind=='signal':
        args['setup']=signal
        with pytest.raises(AdmissionContractError): admit_trade(**args)


def test_no_runtime_wiring_and_pure_dependency_closure():
    pure={p.resolve() for p in (ROOT/'app/admission').glob('*.py')}
    allowed={'__future__','decimal','hashlib','json','typing','pydantic','yaml','app.setups.models',
             'app.setups.rr','app.setups.rr_models','app.setups.scorecard','app.setups.scorecard_models',
             'models','policy','engine'}
    for path in pure:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.ImportFrom): assert node.module in allowed
            elif isinstance(node,ast.Import): assert all(n.name in allowed for n in node.names)
    for path in [ROOT/'main.py',*(ROOT/'app').rglob('*.py')]:
        if path.resolve() in pure: continue
        # Only historical capture/schema sidecars, never an execution route.
        if path.relative_to(ROOT).as_posix() in {'app/exits/bindings.py','app/exits/models.py',
            # Stage 7 exact offline configuration/validation/CLI, not execution.
            'app/configuration/models.py','app/configuration/inputs.py','app/configuration/compiler.py',
            'app/configuration/contracts.py','app/configuration/check.py',
            'app/offline_paper/models.py','app/offline_paper/engine.py','app/offline_paper/risk.py',
            'app/offline_paper/fixtures.py','app/offline_paper/cli.py'}: continue
        assert 'admission' not in path.read_text().lower() or 'setups' in path.parts
    assert 'admission' not in (ROOT/'config.yaml').read_text()
    assert 'dry_run: true' in (ROOT/'config.yaml').read_text()
    assert '"live_runtime_allowed": false' in (ROOT/'isolation-policy.json').read_text()
