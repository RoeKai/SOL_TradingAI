"""Author-written 8D regressions. All observations are explicitly synthetic."""
from decimal import Decimal as D
import hashlib
import json
import pytest
from test_stage08c_historical import workspace, observation
from app.historical_replay.data import START, seal
from app.historical_replay.models import ExecutionModel, HistoricalCandidate
from app.execution_costs.configuration import configuration, run_manifest
from app.execution_costs.storage import QuantifiedStore, hget, hput
from app.execution_costs.engine import QuantifiedPaper
from app.execution_costs.replay import Replay, saved_cursor
from app.execution_costs.prices import fill_from_mid, linear_feasibility, check_quote
from app.offline_paper.storage import get, put, rows


def system(tmp,*,run_id='test-quantified',model=None):
    workspace(tmp);model=model or ExecutionModel()
    b,settings=configuration(tmp,model)
    dataset=seal(dict(version='historical-dataset/v1',status='COMPLETE',market='SYNTHETIC_TEST_NOT_HISTORICAL',
        files=[dict(file=s+'.test.csv',sha256=hashlib.sha256(s.encode()).hexdigest()) for s in ('SOLUSDT','BTCUSDT','ETHUSDT')]))
    run=run_manifest(experiment_id=run_id,code_commit='b'*40,code_digest='c'*64,dataset=dataset,bundle=b,settings=settings,model=model,
        kind='engineering',end_ms=START+3600000)
    store=QuantifiedStore.initialize(tmp,run_id,settings,b,dataset,run)
    paper=QuantifiedPaper(store);paper.recover();return paper,Replay(paper)


def supply(paper,replay,side='LONG',*,values=None):
    values=values or [150,155,149,158,160,150,100,90,95,96,97,98,99,100]
    sign=1 if side=='LONG' else -1
    with paper.store.transaction() as db:
        c=hget(db,'history_cursor','cursor')
        for i,p in enumerate(values):
            at=START-(len(values)-1-i)*15000
            for sym,price in (('SOLUSDT',p if sign==1 else 200-p),('BTCUSDT',10000+sign*i),('ETHUSDT',2000+sign*i)):
                replay.trade(db,c,observation(sym,price,at-1,i+1,qty=str(100+i)),active=False)
            replay.sample(db,c,at)
        saved_cursor(db,c)
        return [r for _,r in rows(db,'requests') if 'entry_action_id' in r]


def market(paper,replay,price,at,qty='10000',*,active=True):
    with paper.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');last=c.get('sequences',{}).get('SOLUSDT');seq=last[0]+1 if last else 100
        e=observation('SOLUSDT',price,at,seq,qty)
        replay.trade(db,c,e,active=active);saved_cursor(db,c)
    return e


@pytest.mark.parametrize('side,q,fill,fee',[('LONG','100.01','100.12','.050060'),('SHORT','99.99','99.89','.049945')])
def test_declared_price_arithmetic(side,q,fill,fee):
    model=ExecutionModel();actual,price=fill_from_mid(D(100),side,model,D('.01'))
    assert actual==D(q) and price==D(fill) and price*model.fee_rate==D(fee)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_new_normal_entry_does_not_need_fixture_or_point_price_tolerance(tmp_path,side):
    p,r=system(tmp_path);results=supply(p,r,side)
    assert len(results)==1
    result=results[0]
    assert result['result'] in ('APPROVE','REDUCE'), result
    with p.store.transaction() as db:
        assert not p.store.settings(db).allow_fixtures and not rows(db,'positions')
        body=hget(db,'history_approvals',result['approval_id'])['body']
        assert body['candidate']['setup']['entry']['lower_price']==body['candidate']['setup']['entry']['upper_price']==100
        assert body['price_contract']['executable_quote']!=str(100)
        assert body['quantity']==body['lineage']['materialization']['quantity']==body['scenarios']['quantity']
        assert body['full_policy_expected_return'] is None
    market(p,r,100,START+1000)
    market(p,r,100,START+2000)
    assert p.summary()['positions'],p.summary()


@pytest.mark.parametrize('f0',['0','.2','1'])
@pytest.mark.parametrize('q',['.01','.1','.2','1','10'])
def test_linear_equivalence(f0,q):
    a,b=linear_feasibility('5','1',f0,'2',q);assert a==b


def test_fixed_cost_probe_failure_does_not_prove_domain_failure():
    assert not linear_feasibility('5','1','1','2','.1')[0]
    assert linear_feasibility('5','1','1','2','1')[0]
