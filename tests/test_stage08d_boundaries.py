"""Author-written edge cases; normal success paths use the real issuer."""
from decimal import Decimal as D
import ast
import copy
import inspect
import textwrap
import pytest
from test_stage08d_quantification import inputs
from test_stage08d_contracts import system,supply,market
from test_stage08d_execution import enter,state
from app.execution_costs.models import QuantificationPolicy
from app.execution_costs.gate import quantify
from app.execution_costs.gate import soft_checks
from app.configuration.compiler import policy_from
from app.setups.scorecard_models import Scorecard
from app.execution_costs.prices import prices,cost_function,derive
from app.execution_costs.engine import QuantifiedPaper,grant
from app.execution_costs.storage import QuantifiedStore,hget,hput
from app.execution_costs.storage import verify_run
from app.historical_replay.data import seal
from app.execution_costs.replay import Replay,saved_cursor
from app.execution_costs.report import result
from app.historical_replay.replay import Replay as OldReplay
from app.historical_replay.data import START
from app.offline_paper.storage import get,put,rows


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_larger_declared_cost_never_improves_rr_or_risk(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side);base=quantify(*args,now=now)
    changed=list(args);changed[8]=QuantificationPolicy(funding_rate_allowance='.002')
    capped=quantify(*changed,now=now,only_quantity=D(base['quantity']))
    assert capped['result']=='REJECT' and capped['search_status']=='SELECTED_QUANTITY_REJECT'
    # Compare both cash models at the SAME smaller in-domain quantity. The
    # previous maximum now exceeds the cost-adjusted risk domain, so no RR is
    # issued for it. This is a refusal, not a missing calculator result.
    base=quantify(*args,now=now,only_quantity=D('.2'))
    costly=quantify(*changed,now=now,only_quantity=D('.2'))
    assert D(costly['rr']['reference']['net_rr'])<D(base['rr']['reference']['net_rr'])
    assert D(costly['rr']['reference']['net_stop_loss_usdt'])>D(base['rr']['reference']['net_stop_loss_usdt'])


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_incomplete_search_not_claimed_impossible(tmp_path,side):
    # Same rule, deliberately narrow evidenced corridor. No policy changes.
    p,r,args,now=inputs(tmp_path,side,values=[121,123,119,124,125,120,100,90,95,96,97,98,99,100])
    bounded=list(args);bounded[8]=QuantificationPolicy(max_evaluations=1)
    out=quantify(*bounded,now=now)
    assert out['result']=='UNRESOLVED',out['reason_codes']
    assert out['search_status']=='EVALUATION_BUDGET_EXHAUSTED' and out['proof'] is None
    assert out['legacy_full_policy_valuation']=='UNSUPPORTED' and out['full_policy_expected_return'] is None


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_whole_domain_static_reject_has_specific_proof(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side,values=[106,108,105,109,110,107,100,90,95,96,97,98,99,100])
    out=quantify(*args,now=now)
    assert out['result']=='REJECT' and out['search_status']=='PROVEN_STATIC_RR_DOMAIN_REJECT',out
    assert out['proof']['maximum_quantity']==out['domain']['high']


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_final_size_lattice_and_grade_cap_binding(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side);out=quantify(*args,now=now)
    assert out['result'] in ('APPROVE','REDUCE')
    q=D(out['quantity']);assert q%args[5].quantity_step==0
    assert q*min(args[7].signal_reference_price,args[7].modeled_fill_price)>=args[5].min_notional_usdt
    assert q<=min(D(v) for v in out['quantity_caps'].values())
    assert out['evaluated_quantities'][-1]['grade']==out['tier']
    invalid=quantify(*args,now=now,only_quantity=q+D('.0001'))
    assert invalid['search_status']=='SELECTED_QUANTITY_REJECT'


@pytest.mark.parametrize('score,tier',[(100,'S'),(84,'A'),(74,'B'),(64,'C')])
def test_grade_boundaries_are_original_policy_not_rr_mappings(tmp_path,score,tier):
    # Component inputs ONLY; never passed to issuer/Broker as an approval.
    p,r,args,now=inputs(tmp_path);out=quantify(*args,now=now)
    card=Scorecard.model_validate(out['scorecard'])
    card=card.model_copy(update={'overall_trade_quality':card.overall_trade_quality.model_copy(update={'score':score})})
    selected,reasons=soft_checks(card,policy_from(args[1],'admission'))
    assert selected.name==tier and not reasons
    assert selected.risk_fraction=={'S':D(1),'A':D('.75'),'B':D('.5'),'C':D('.25')}[tier]


@pytest.mark.parametrize('field',['day_ends_at','available_margin_usdt','reconciliation_clear','consecutive_losses'])
def test_missing_account_evidence_not_safe(tmp_path,field):
    p,r,args,now=inputs(tmp_path);a=list(args);a[4]=a[4].model_copy(update={field:None})
    out=quantify(*a,now=now)
    assert out['result']=='REJECT' and 'ACCOUNT_STATE_UNKNOWN' in out['reason_codes']


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_future_observations_and_funding_do_not_rewrite_prior_decision(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side);before=p.prepare(args[0].candidate_id,'past')
    with p.store.transaction() as db:
        # Available only after the current cursor, not passed to the supplier.
        hput(db,'history_samples',str(int((now+60)*1000)),{'future_market':'999999'})
        hput(db,'history_meta','unconsumed_future_funding',{'rate':'-.999','mark_price':'999999'})
        restored,_=grant(p.store,db,before['approval_id'])
        assert restored==before['body']
    forged=copy.deepcopy(args[0].model_dump(mode='json'))
    forged['evidence']['window'][-1]['last']['SOLUSDT']['available_at_ms']=int((now+1)*1000)
    candidate=type(args[0]).model_validate(forged)
    with pytest.raises(ValueError,match='STALE_OR_FUTURE'):
        prices(candidate,args[2],before['body']['quote'],args[5].price_tick,now=now)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('change',['paused','quote_missing','quote_improves'])
def test_acceptance_rechecks_nonprice_safety_and_improved_price(tmp_path,side,change):
    p,r=system(tmp_path);a=supply(p,r,side)[0]
    with p.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');r._set_clock(db,c,START+1000);saved_cursor(db,c)
        account=get(db,'account','account')
        if change=='paused':account['paused']=True
        elif change=='quote_missing':account['quote']=None
        else:account['quote']['ask' if side=='LONG' else 'bid']=str(D(99) if side=='LONG' else D(101))
        put(db,'account','account',account)
    p.pump()
    with p.store.transaction() as db:
        o=get(db,'broker_orders',a['entry_action_id'])
        assert o['status']=='REJECTED' and not rows(db,'broker_fills')
        assert o['reason_code']=={'paused':'ACCOUNT_HALTED_BEFORE_ACCEPTANCE','quote_missing':'EXECUTION_QUOTE_MISSING',
            'quote_improves':'FAVORABLE_QUOTE_REQUIRES_NEW_QUANTIFIED_REQUEST'}[change]


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('partial',[False,True])
def test_new_normal_position_competing_exits_and_shared_liquidity(tmp_path,side,partial):
    p,r=system(tmp_path);a=enter(p,r,side);market(p,r,100,START+3000);pid=a['position_id']
    market(p,r,115 if side=='LONG' else 85,START+4000)
    with p.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');r._set_clock(db,c,START+5000);saved_cursor(db,c)
    p.pump()
    with p.store.transaction() as db:
        tp=next(k for k,o in rows(db,'broker_orders') if o['action']['kind']=='TP1')
        assert get(db,'broker_orders',tp)['status']=='ACCEPTED'
        q=D(state(p,pid)['remaining_quantity'])
    e=market(p,r,85 if side=='LONG' else 115,START+6000,qty='5' if partial else '10000')
    with p.store.transaction() as db:
        assert D(get(db,'broker_orders',tp)['cumulative'])==0
        assert D(state(p,pid)['remaining_quantity'])==q-D('.05') if partial else D(state(p,pid)['remaining_quantity'])==0
        before=rows(db,'fills');c=hget(db,'history_cursor','cursor');r.trade(db,c,e,active=True);saved_cursor(db,c)
        assert rows(db,'fills')==before


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_new_normal_position_margin_breach_latches_before_stop_and_recovery(tmp_path,side):
    p,r=system(tmp_path);a=enter(p,r,side);market(p,r,100,START+3000)
    market(p,r,70 if side=='LONG' else 130,START+4000)
    out=result(p);assert out['model_limit']['at_ms']==START+4000 and out['metrics'] is None
    assert D(state(p,a['position_id'])['remaining_quantity'])==0
    p.recover();again=result(p)
    assert again['model_limit']==out['model_limit'] and again['metrics'] is None


@pytest.mark.parametrize('method',['_fills','_model_limit','trade','run_stream'])
def test_accepted_8c_scheduler_and_model_boundary_algorithms_unchanged(method):
    def syntax(cls):return ast.dump(ast.parse(textwrap.dedent(inspect.getsource(getattr(cls,method)))))
    assert syntax(Replay)==syntax(OldReplay)


@pytest.mark.parametrize('field',['version','price_contract_version','cost_function_version','admission_version','exit_plan_version','scenario_version'])
def test_unknown_rehashed_run_version_is_not_current(tmp_path,field):
    p,r=system(tmp_path)
    with p.store.transaction() as db:run=p.store.run(db)
    run.pop('content_digest');run[field]='unknown/v999'
    with pytest.raises(ValueError,match='UNKNOWN_RUN_VERSION'):verify_run(seal(run))
