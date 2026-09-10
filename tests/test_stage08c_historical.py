"""Synthetic faults use labeled test sources; real-history runs are separate."""
from decimal import Decimal as D
from pathlib import Path
import hashlib
import json
import shutil
import zipfile
import pytest

from app.historical_replay.models import ExecutionModel, HistoricalSettings
from app.historical_replay.configuration import configuration, run_manifest, INSTANCE
from app.historical_replay.data import seal, verify_seal, archive_rows, funding_records, HistoricalError, WARMUP, START, END
from app.historical_replay.storage import HistoricalStore, hget, hput
from app.historical_replay.engine import HistoricalPaper, ORIGIN
from app.historical_replay.replay import Replay, saved_cursor
from app.historical_replay.provider import describe
from app.historical_replay.evidence import review
from app.historical_replay.funding import settle, snapshot, net_funding
from app.offline_paper.storage import get, put, rows

ROOT=Path(__file__).resolve().parents[1]


def workspace(tmp):
    for file in ('isolation-policy.json','admission.yaml','exit-policy.yaml','examples/admitted-paper/main.yaml','examples/admitted-paper/manifest.yaml'):
        target=tmp/file;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/file,target)
    return tmp


def system(tmp,*,run_id='test-historical',model=None):
    workspace(tmp)
    model=model or ExecutionModel()
    bundle,settings=configuration(tmp,model)
    dataset=seal(dict(version='historical-dataset/v1',status='COMPLETE',market='SYNTHETIC_TEST_NOT_HISTORICAL',
        files=[dict(file=s+'.test.csv',sha256=hashlib.sha256(s.encode()).hexdigest()) for s in ('SOLUSDT','BTCUSDT','ETHUSDT')]))
    run=run_manifest(experiment_id=run_id,code_commit='b'*40,code_digest='c'*64,dataset=dataset,bundle=bundle,settings=settings,model=model,
        kind='engineering',end_ms=START+3600000)
    store=HistoricalStore.initialize(tmp,run_id,settings,bundle,dataset,run)
    paper=HistoricalPaper(store);paper.recover()
    return paper,Replay(paper)


def observation(symbol,price,at,seq,qty='200'):
    return dict(kind='TRADE',symbol=symbol,event_id=symbol+':agg:'+str(seq),raw_sequence=seq,event_time_ms=at-250,
        available_at_ms=at,price=str(price),quantity=str(qty),source_file=symbol+'.test.csv',
        source_digest=hashlib.sha256(symbol.encode()).hexdigest(),downloaded_at='TEST_NOT_REAL_DOWNLOAD',
        stream_file=symbol+'.csv',next_offset=seq*100)


def supply(paper,replay,side='LONG'):
    values=[150,155,149,158,160,150,100,90,95,96,97,98,99,100]
    sign=1 if side=='LONG' else -1
    with paper.store.transaction() as db:
        c=hget(db,'history_cursor','cursor')
        for i,p in enumerate(values):
            at=START-(13-i)*15000
            for sym,price in (('SOLUSDT',p if sign==1 else 200-p),('BTCUSDT',10000+sign*i),('ETHUSDT',2000+sign*i)):
                e=observation(sym,price,at-1,i+1,qty=str(100+i))
                replay.trade(db,c,e,active=False)
            replay.sample(db,c,at)
        saved_cursor(db,c)
        records=rows(db,'requests')
        approvals=[r for r in records if r[0].startswith('historical-admitted:')]
        return approvals


def market(paper,replay,price,at,seq=100,qty='10000',*,auto=True):
    with paper.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');last=c.get('sequences',{}).get('SOLUSDT')
        seq=last[0]+1 if last else seq;e=observation('SOLUSDT',price,at,seq,qty)
        replay.trade(db,c,e,active=auto)
        saved_cursor(db,c)
    return e


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_original_point_entry_conflicts_with_required_nonzero_spread(tmp_path,side):
    p,r=system(tmp_path);result=supply(p,r,side)
    assert len(result)==1,result
    # An initial author-written positive test exposed this real contract
    # conflict. Preserve the ORIGINAL gate, assert the blocker explicitly.
    assert result[0][1]['result']=='REJECT',result
    assert 'EXECUTABLE_QUOTE_OUTSIDE_PLAN' in result[0][1]['reason_codes']
    with p.store.transaction() as db:
        assert not rows(db,'reservations')
        assert not p.store.settings(db).allow_fixtures
        assert not rows(db,'positions') and not rows(db,'broker_orders')
        candidate=next(iter(db.execute('SELECT payload FROM history_candidates')))[0]
        assert json.loads(candidate)['provenance']=='SYNTHETIC_TEST_OBSERVATIONS_ASSUMED_EXECUTION'
    # Rejected normal entry never falls back to fixture/old Signal even when
    # future quotes would have delivered a profitable path.
    market(p,r,100,START+1000)
    assert not p.summary()['positions']
    market(p,r,100,START+2000)
    assert not p.summary()['positions']


def component_inventory(p,side):
    """Explicit COMPONENT fixture; not a historical normal-entry success.

    Tests funding arithmetic/exit-side modeled Broker semantics independently
    while the real point-entry contract remains rejected. Never an approval.
    """
    with p.store.transaction() as db:
        put(db,'positions','component-position',dict(checkpoint=dict(state=dict(symbol='SOLUSDT',side=side,
            remaining_quantity='.5',remaining_entry_cost='50',realized_net_pnl='-.025')),closed_at=None))
        fact=dict(kind='ENTRY_FILL',fill_id='component-entry',action_id='component-entry',position_id='component-position',
            quantity='.5',price='100',fee_usdt='.025',occurred_at=START//1000,requested_quantity='.5')
        put(db,'fills','component-entry',fact);put(db,'broker_fills','component-entry',fact)
        put(db,'reservations','component-position',dict(origin='COMPONENT_FIXTURE_NOT_ADMITTED',side=side,quantity='.5',risk='5',
            margin='10',fee_reserve='.025',released=False,entry_sealed=True,entry_high_water='.5',entry_terminal='FILLED',
            entry_terminal_quantity='.5',entry_status_unknown=False,entry_faults=[],entry_reconciliation_required=False,
            entry_action_id='component-entry',exit_policy=p.store.bundle(db).sources[0].name))
        r=get(db,'reservations','component-position')
        from app.configuration.compiler import policy_from
        r['exit_policy']=policy_from(p.store.bundle(db),'exit').model_dump(mode='json')
        put(db,'reservations','component-position',r);p._cash(db)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_fill_delay_participation_and_no_duplicate(tmp_path,side):
    p,r=system(tmp_path);component_inventory(p,side);key='component-exit'
    with p.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');r._set_clock(db,c,START)
        put(db,'outbox',key,dict(origin='COMPONENT_FIXTURE_NOT_ADMITTED',status='PENDING',attempts=0,
            action=dict(action_id=key,kind='CLOSE_ALL',position_id='component-position',quantity='.5',side='SELL' if side=='LONG' else 'BUY')))
        p._schedule(db,key);saved_cursor(db,c)
        other=get(db,'outbox',key)
        other['action']['action_id']='different-exit'
        put(db,'outbox','different-exit',other);p._schedule(db,'different-exit')
    with pytest.raises(HistoricalError,match='DELAY'): p.broker.execute(key)
    with p.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');r._set_clock(db,c,START+1000);saved_cursor(db,c)
    p.broker.execute(key)
    p.broker.execute('different-exit')
    with pytest.raises(HistoricalError,match='SAME_EVENT|FILL_REQUIRES'):
        p.broker.fill(key,'.1','arbitrary')
    event=observation('SOLUSDT',100,START+2000,100,qty='10')
    with p.store.transaction() as db:
        from app.historical_replay.replay import quote_from
        c=hget(db,'history_cursor','cursor');r._set_clock(db,c,event['available_at_ms'])
        a=get(db,'account','account');a['quote']=quote_from(event,r.model);put(db,'account','account',a)
        hput(db,'history_meta','active_market',event);hput(db,'history_meta','liquidity',dict(event_id=event['event_id'],used='0'))
    p.broker.fill(key,'.1',event['event_id'],defer_details=True,defer_receipt=True)
    with p.store.transaction() as db:
        facts=[x for x in rows(db,'broker_fills') if x[1]['kind']=='EXIT_FILL'];assert len(facts)==1
        assert D(facts[0][1]['quantity'])<=D('.1')
        cash=get(db,'account','account')['cash']
    p.broker.fill(key,facts[0][1]['requested_quantity'],event['event_id'])
    with p.store.transaction() as db:
        assert len(rows(db,'broker_fills'))==2 and get(db,'account','account')['cash']==cash
    with pytest.raises(HistoricalError,match='PARTICIPATION'):
        # Unknown action still cannot spend the already consumed raw volume.
        p.broker.fill('different-exit','.101',event['event_id'])


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('rate',['.001','-.001'])
def test_signed_funding_cash_and_idempotence(tmp_path,side,rate):
    p,r=system(tmp_path);component_inventory(p,side)
    with p.store.transaction() as db:
        q=D(rows(db,'positions')[0][1]['checkpoint']['state']['remaining_quantity'])
        old=D(get(db,'account','account')['cash']);f=dict(at_ms=START+3000,rate=rate,mark_price='100',source_digest='f'*64)
        c=hget(db,'history_cursor','cursor');r._set_clock(db,c,f['at_ms'])
        hput(db,'history_meta','active_funding',f);settle(p,db,f);saved_cursor(db,c)
        cash=-q*100*D(rate)*(1 if side=='LONG' else -1)
        assert net_funding(db)==cash
        assert D(get(db,'account','account')['cash'])==old+cash
        before=rows(db,'fills');settle(p,db,f)
        assert before==rows(db,'fills') and net_funding(db)==cash
        assert snapshot(p.store,db).day_realized_loss_usdt>=max(D(0),-cash)
    restored=HistoricalPaper(HistoricalStore(tmp_path,'test-historical',INSTANCE))
    with restored.store.transaction() as db:
        restored._assert_cash(db)
        assert net_funding(db)==cash and D(get(db,'account','account')['cash'])==old+cash
    # This arithmetic fixture deliberately has no approval: full recovery MUST
    # refuse it instead of laundering it into the ordinary historical path.
    with pytest.raises(HistoricalError,match='MIXED_INSTANCE'): restored.recover()


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_future_or_mutated_evidence_cannot_self_certify(tmp_path,side):
    p,r=system(tmp_path);supply(p,r,side)
    with p.store.transaction() as db:
        c=json.loads(db.execute('SELECT payload FROM history_candidates').fetchone()[0]);key=c['candidate_id']
        review(p.store,db,key,now=START//1000)
        c['evidence']['window'][-1]['last']['BTCUSDT']['available_at_ms']=START+100000
        hput(db,'history_candidates',key,c)
        with pytest.raises(HistoricalError,match='SAMPLE|FUTURE|RECOMPUTATION'):
            review(p.store,db,key,now=START//1000)


def test_stale_reference_and_exact_180_endpoint(tmp_path):
    p,r=system(tmp_path);supply(p,r)
    with p.store.transaction() as db:
        c=json.loads(db.execute('SELECT payload FROM history_candidates').fetchone()[0]);w=c['evidence']['window'];level=c['evidence']['level']
        w[-1]['last']['BTCUSDT']['event_time_ms']-=16000
        with pytest.raises(HistoricalError,match='STALE'): describe(r.bundle,r.model,r.run,'LONG',w,level,now=START//1000)
        w=c['evidence']['window'][:-3]
        with pytest.raises(HistoricalError,match='180_SECOND'): describe(r.bundle,r.model,r.run,'LONG',w,level,now=START//1000)


@pytest.mark.parametrize('bad', ['nan','Infinity','-1','0'])
def test_nonfinite_execution_model(bad):
    with pytest.raises(ValueError): ExecutionModel(spread_bps=bad)


@pytest.mark.parametrize('mode',['live','synthetic_offline'])
def test_no_live_or_8b_instance_adoption(tmp_path,mode):
    p,r=system(tmp_path)
    with p.store.transaction() as db: settings=p.store.settings(db).model_dump()
    settings['mode']=mode
    with pytest.raises(ValueError): HistoricalSettings.model_validate(settings)
    settings['mode']='historical_offline';settings['live_allowed']=True
    with pytest.raises(ValueError): HistoricalSettings.model_validate(settings)


@pytest.mark.parametrize('member',['../data.csv','/data.csv','dir/data.csv'])
def test_zip_traversal(tmp_path,member):
    z=tmp_path/'x.zip'
    with zipfile.ZipFile(z,'w') as f: f.writestr(member,'1,100,1,1,1,1785542400000,true\n')
    with pytest.raises(HistoricalError,match='ZIP_UNSAFE'):
        list(archive_rows(z,expected_csv='data.csv',symbol='SOLUSDT',start_ms=START,end_ms=END))


@pytest.mark.parametrize('stamp',[1785542400,1785542400000000,END])
def test_wrong_millisecond_unit(tmp_path,stamp):
    z=tmp_path/'x.zip'
    with zipfile.ZipFile(z,'w') as f: f.writestr('data.csv',f'1,100,1,1,1,{stamp},true\n')
    with pytest.raises(HistoricalError,match='MILLISECOND'):
        list(archive_rows(z,expected_csv='data.csv',symbol='SOLUSDT',start_ms=START,end_ms=END))


def test_funding_exact_millisecond_preserved_and_not_defaulted():
    v=list(funding_records([dict(symbol='SOLUSDT',fundingTime=START+26,fundingRate='.001',markPrice='100')]))
    assert v[0]['event_time_ms']==START+26
    with pytest.raises(HistoricalError): list(funding_records([]))


def test_frozen_digest_content_change():
    v=seal({'a':1});v['a']=2
    with pytest.raises(HistoricalError): verify_seal(v)
