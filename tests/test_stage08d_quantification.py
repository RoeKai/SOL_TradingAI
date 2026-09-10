"""Pure quantity/cost proofs plus exact isolated-DB permission boundaries."""
from decimal import Decimal as D
import copy
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
import pytest
from test_stage08d_contracts import system,supply
from test_stage08c_historical import system as old_system
from app.execution_costs.models import QuantificationPolicy
from app.execution_costs.storage import QuantifiedStore,hget,hput
from app.execution_costs.engine import QuantifiedPaper,grant
from app.execution_costs.prices import prices,cost_function,derive,materialize,check_quote,fill_from_mid
from app.execution_costs.gate import quantify
from app.execution_costs.evidence import review,admission_request
from app.execution_costs.funding import snapshot,exchange_snapshot
from app.execution_costs.attribution import compare
from app.historical_replay.models import HistoricalCandidate,ExecutionModel
from app.offline_paper.storage import get,put,rows,digest
from app.setups.rr import calculate_rr
from app.admitted_paper.provider import number


def inputs(tmp,side='LONG',values=None):
    p,r=system(tmp);p.ready=False;p._recovered=False;supply(p,r,side,values=values);p.recover()
    with p.store.transaction() as db:
        cid=db.execute('SELECT id FROM history_candidates').fetchone()[0];now=get(db,'account','account')['now']
        c,_=review(p.store,db,cid,now=now);a=snapshot(p.store,db);v=exchange_snapshot(now)
        req=admission_request(p.store,db,cid,'pure',D(5),5,now)
        pc=prices(c,r.model,get(db,'account','account')['quote'],v.price_tick,now=now)
        args=(c,r.bundle,r.model,p.store.settings(db),a,v,req,pc,QuantificationPolicy())
    return p,r,args,now


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_linear_cost_scale_invariance_and_single_charge(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side);c,b,m,settings,a,v,req,pc,qp=args
    f=cost_function(c,pc,m,qp);ratios=[]
    for q in (D('.05'),D('.2'),D('.469'),D(2)):
        s,lineage=derive(c,pc,f,q,m);rr=calculate_rr(s,quantity=number(q));ratios.append(rr.reference.net_rr)
        assert D(lineage['materialization']['funding_budget_usdt'])==q*f.per_unit_usdt
        assert rr.reference.stop_costs.funding_per_unit==f.per_unit_usdt
        sign=1 if side=='LONG' else -1
        entry_quote,entry_fill=fill_from_mid(D(100),side,m,v.price_tick)
        stop_quote,stop_fill=fill_from_mid(D(str(s.initial_stop.price)),side,m,v.price_tick,entry=False)
        actual=(entry_fill-stop_fill)*sign*q+(entry_fill+stop_fill)*m.fee_rate*q+q*f.per_unit_usdt
        assert rr.reference.net_stop_loss_usdt>=actual
        assert abs(rr.reference.net_stop_loss_usdt-actual)<D('.02')*q
        assert pc.executable_quote==entry_quote and pc.modeled_fill_price==entry_fill
    assert len(set(ratios))==1


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_old_fixed_entire_one_usdt_and_new_quantity_budget_are_separate(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side);c,b,m,settings,a,v,req,pc,qp=args
    old=cost_function(c,pc,m,qp,old_fixed=True);new=cost_function(c,pc,m,qp)
    for q in (D('.05'),D('.4'),D(5)):
        assert materialize(old,q)['funding_budget_usdt']=='1.000000' or D(materialize(old,q)['funding_budget_usdt'])==1
        assert D(materialize(new,q)['funding_budget_usdt'])==q*new.budget_price*D('.001')*2
    assert new.budget_price>=D(str(c.setup.initial_stop.price))
    assert new.budget_price>=max(D(str(e.price)) for e in c.setup.structure_evidence if e.price is not None)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_full_solver_fixed_cost_probe_reject_larger_size_can_pass(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side,values=[180,185,179,188,190,180,100,90,95,96,97,98,99,100])
    tiny=quantify(*args,now=now,old_fixed=True,only_quantity=D('.05'))
    solved=quantify(*args,now=now,old_fixed=True)
    assert tiny['result']=='REJECT'
    assert solved['result'] in ('APPROVE','REDUCE'),solved['reason_codes']
    assert D(solved['quantity'])>D('.05')
    assert D(solved['lineage']['materialization']['funding_budget_usdt'])==1
    assert D(solved['risk'])<=5


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_quantity_independent_rejections_and_caps(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side)
    for changes,reason in (({'day_realized_loss_usdt':D(20)},'DAILY_LOSS_LIMIT'),
        ({'consecutive_losses':2},'CONSECUTIVE_LOSS_HALT'),({'trades_today':3},'DAILY_TRADE_LIMIT'),
        ({'reserved_risk_usdt':D(20)},'RISK_OR_EXCHANGE_MINIMUM_EXCEEDS_BUDGET'),
        ({'available_margin_usdt':D(0)},'RISK_OR_EXCHANGE_MINIMUM_EXCEEDS_BUDGET'),
        ({'configured_leverage':6},'LEVERAGE_LIMIT_OR_MISMATCH'),
        ({'reconciliation_clear':False},'ACCOUNT_HALTED_OR_UNRECONCILED')):
        current=list(args);current[4]=args[4].model_copy(update=changes)
        out=quantify(*current,now=now)
        assert out['result']=='REJECT' and reason in out['reason_codes'],out


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_resized_results_all_recomputed_and_bound(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side)
    large=quantify(*args,now=now)
    smallargs=list(args);smallargs[6]=args[6].model_copy(update={'risk_budget_usdt':D(2)})
    small=quantify(*smallargs,now=now)
    for out in (large,small):
        assert out['result'] in ('APPROVE','REDUCE'),out['reason_codes']
        q=D(out['quantity'])
        assert q==D(out['rr']['hypothetical_quantity'])==D(out['scenarios']['quantity'])==D(out['exit_plan']['materialized_quantity'])
        assert D(out['lineage']['materialization']['quantity'])==q
        assert D(out['risk'])<=D(out['risk_ceiling'])
    assert D(small['quantity'])<D(large['quantity'])
    assert small['lineage']['derived_setup_digest']!=large['lineage']['derived_setup_digest']


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_readonly_abcd_not_independent_samples_or_orders(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side);c,b,m,settings,*_=args
    with p.store.transaction() as db:before=(get(db,'account','account'),rows(db,'broker_orders'),rows(db,'reservations'))
    out=compare(c,b,m,settings,QuantificationPolicy())
    assert out['independent_samples']==1
    assert 'EXECUTABLE_QUOTE_OUTSIDE_PLAN' in out['groups']['A']['reason_codes']
    assert 'EXECUTABLE_QUOTE_OUTSIDE_PLAN' in out['groups']['C']['reason_codes']
    assert out['groups']['D']['result'] in ('APPROVE','REDUCE')
    assert 'RUNNER_FULL_POLICY_VALUATION_UNSUPPORTED' in str(out['groups']['A']['legacy_validation'])
    with p.store.transaction() as db:assert before==(get(db,'account','account'),rows(db,'broker_orders'),rows(db,'reservations'))


def test_no_automatic_adoption_or_reinitialization(tmp_path):
    old,_=old_system(tmp_path);new,r=system(tmp_path,run_id='separate-new')
    with pytest.raises(ValueError,match='exists'):system(tmp_path,run_id='separate-new')
    target=tmp_path/'quantified-runs'/'test-historical';target.mkdir();shutil.copyfile(old.store.path,target/'ledger.sqlite3')
    with pytest.raises(ValueError,match='database/version'):
        QuantifiedStore(tmp_path,'test-historical',new.store.instance_id).connect()
    with old.store.transaction() as db:
        with pytest.raises(ValueError,match='8D_INSTANCE'):hget(db,'history_meta','run')
    with new.store.transaction() as db:
        from app.historical_replay.storage import hget as old_get
        with pytest.raises(ValueError,match='8C_INSTANCE'):old_get(db,'history_meta','run')


def test_concurrent_normal_submissions_do_not_double_reserve(tmp_path):
    p,r,args,now=inputs(tmp_path)
    a=p.prepare(args[0].candidate_id,'one');b=p.prepare(args[0].candidate_id,'two')
    assert a['body']['result'] in ('APPROVE','REDUCE') and b['body']['result'] in ('APPROVE','REDUCE')
    # Independent connection holders, same explicit recovered epoch. No gate mocks.
    def submit(rec):return p.submit(rec['approval_id'])
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,(a,b)))
    assert sum(x['entry_action_id'] is not None for x in results)==1
    with p.store.transaction() as db:
        assert len(rows(db,'reservations'))==1
        assert db.execute('SELECT count(*) FROM history_consumptions').fetchone()[0]==1


@pytest.mark.parametrize('field',['quantity','risk','price_contract','cost_function','derived_setup','scenarios'])
def test_rehashed_json_cannot_forge_instance_approval(tmp_path,field):
    p,r,args,now=inputs(tmp_path);rec=p.prepare(args[0].candidate_id,'good')
    with p.store.transaction() as db:
        raw=copy.deepcopy(rec);body=raw['body']
        if field in ('quantity','risk'):body[field]='999'
        elif field=='price_contract':body[field]['modeled_fill_price']='101'
        elif field=='cost_function':body[field]['per_unit_usdt']='0'
        elif field=='derived_setup':body[field]['initial_stop']['price']=80
        else:body[field]['quantity']='999'
        key=digest({'issuer':body['version'],'body':body});raw.update(approval_id=key,body_digest=digest(body));hput(db,'history_approvals',key,raw)
        with pytest.raises(ValueError):grant(p.store,db,key)


@pytest.mark.parametrize('field,value',[('schema_version',3),('mode','live'),('allow_fixtures',True),('application','historical-paper/8c')])
def test_new_runtime_identity_and_live_fence(tmp_path,field,value):
    p,r=system(tmp_path)
    with p.store.transaction() as db:settings=p.store.settings(db)
    raw=settings.model_dump();raw[field]=value
    with pytest.raises(ValueError):type(settings).model_validate(raw)
