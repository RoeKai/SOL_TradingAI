"""Synthetic file streams and real process death; NOT historical performance."""
from decimal import Decimal as D
import json
import subprocess
import sys
import pytest
from test_stage08c_historical import workspace, ROOT, system, component_inventory
from app.historical_replay.data import START,WARMUP,seal
from app.historical_replay.models import ExecutionModel
from app.historical_replay.configuration import configuration,run_manifest,INSTANCE
from app.historical_replay.storage import HistoricalStore,hget,hput
from app.historical_replay.engine import HistoricalPaper
from app.historical_replay.replay import Replay,saved_cursor
from app.historical_replay.funding import net_funding
from app.offline_paper.storage import get,rows


def stream_system(root,kind='engineering'):
    workspace(root);data=root/'test-data';stream=data/'stream-v1';stream.mkdir(parents=True)
    files=[]
    for symbol in ('SOLUSDT','BTCUSDT','ETHUSDT'):
        name=symbol+'.csv';lines=[]
        for i in range(450):
            lines.append(f'{i+1},100,10,{i+1},{i+1},{WARMUP+i*3000},true\n')
        (stream/name).write_text(''.join(lines))
        files.append(dict(file=name,kind='aggTrades',source_file=name,source_digest='f'*64,symbol=symbol,downloaded_at='SYNTHETIC_TEST'))
    fund=[dict(symbol='SOLUSDT',fundingTime=START+1,fundingRate='.001',markPrice='100')]
    (data/'funding.json').write_text(json.dumps(fund))
    dataset=seal(dict(version='historical-dataset/v1',status='COMPLETE',market='SYNTHETIC_TEST_NOT_HISTORICAL',
        files=[*files,dict(file='funding.json',kind='fundingRate',sha256='f'*64)]))
    model=ExecutionModel();bundle,settings=configuration(root,model)
    run=run_manifest(experiment_id='stream-test',code_commit='b'*40,code_digest='c'*64,
        dataset=dataset,bundle=bundle,settings=settings,model=model,kind=kind,end_ms=START+3600000)
    store=HistoricalStore.initialize(root,'stream-test',settings,bundle,dataset,run)
    (data/'index.json').write_text(json.dumps(dict(dataset_digest=dataset['content_digest'],files=files)))
    p=HistoricalPaper(store);p.recover();return p,data


CHILD='''
import json,sys
from pathlib import Path
from app.historical_replay.storage import HistoricalStore
from app.historical_replay.engine import HistoricalPaper
from app.historical_replay.replay import Replay
from app.historical_replay.configuration import INSTANCE
p=HistoricalPaper(HistoricalStore(sys.argv[1],"stream-test",INSTANCE));p.recover()
root=Path(sys.argv[1])/"test-data"
Replay(p).run_stream(root,json.loads((root/"index.json").read_text()),fault=None if sys.argv[2]=="none" else sys.argv[2])
'''


@pytest.mark.parametrize('fault',['before_market_cursor_commit','after_market_cursor_commit','before_funding_cursor_commit','scheduled_restart'])
def test_real_process_death_resumes_same_cursor_funding_cash_and_events(tmp_path,fault):
    kind='missing_restart' if fault=='scheduled_restart' else 'engineering'
    p,data=stream_system(tmp_path/'interrupted',kind);continuous,other=stream_system(tmp_path/'continuous',kind)
    died=subprocess.run([sys.executable,'-c',CHILD,str(p.store.paths.root),fault],cwd=ROOT,capture_output=True,timeout=30)
    assert died.returncode==91,(died.stdout,died.stderr)
    for _ in range(2):
        resumed=subprocess.run([sys.executable,'-c',CHILD,str(p.store.paths.root),'none'],cwd=ROOT,capture_output=True,timeout=30)
        assert resumed.returncode==0,(resumed.stdout,resumed.stderr)
    Replay(continuous).run_stream(other,json.loads((other/'index.json').read_text()))
    def facts(p):
        with p.store.transaction() as db:
            return dict(cursor=hget(db,'history_cursor','cursor'),funding=net_funding(db),
                account=get(db,'account','account'),fills=rows(db,'fills'),orders=rows(db,'broker_orders'),
                count=hget(db,'history_meta','funding')['count'],stats=hget(db,'history_meta','statistics'))
    a,b=facts(p),facts(continuous)
    # Recovery adds versions but must not change economic facts or input prefix.
    for key in ('version','recovery_count'):
        a['account'].pop(key,None);b['account'].pop(key,None)
    assert a==b
    assert a['count']==1 and a['account']['cash']=='500' and a['fills']==a['orders']==[]


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('after_commit',[False,True])
def test_real_process_component_funding_cash_and_cursor_commit_together(tmp_path,side,after_commit):
    p,r=system(tmp_path);component_inventory(p,side)
    # Deliberately component-only inventory; never recover as admitted history.
    child='''
import os,sys
from app.historical_replay.storage import HistoricalStore,hget,hput
from app.historical_replay.engine import HistoricalPaper
from app.historical_replay.replay import Replay,saved_cursor
from app.historical_replay.configuration import INSTANCE
from app.historical_replay.data import START
from app.historical_replay.funding import settle
p=HistoricalPaper(HistoricalStore(sys.argv[1],"test-historical",INSTANCE));r=Replay(p)
with p.store.transaction() as db:
 c=hget(db,"history_cursor","cursor");r._set_clock(db,c,START+3000)
 f=dict(at_ms=START+3000,rate=".001",mark_price="100",source_digest="f"*64)
 hput(db,"history_meta","active_funding",f);settle(p,db,f);saved_cursor(db,c)
 if sys.argv[2]=="False": os._exit(91)
os._exit(91)
'''
    died=subprocess.run([sys.executable,'-c',child,str(tmp_path),str(after_commit)],cwd=ROOT,capture_output=True,timeout=30)
    assert died.returncode==91,died.stderr
    fresh=HistoricalPaper(HistoricalStore(tmp_path,'test-historical',INSTANCE))
    for _ in range(2):
        with fresh.store.transaction() as db:
            fresh._assert_cash(db)
            expected=(-D('.05') if side=='LONG' else D('.05')) if after_commit else D(0)
            assert net_funding(db)==expected
            assert D(get(db,'account','account')['cash'])==D('499.975')+expected
            assert hget(db,'history_cursor','cursor')['at_ms']==(START+3000 if after_commit else WARMUP)


def test_corrupt_cursor_is_quarantined_before_ready(tmp_path):
    p,data=stream_system(tmp_path)
    Replay(p).run_stream(data,json.loads((data/'index.json').read_text()),max_events=10)
    with p.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');c['event_count']+=1;hput(db,'history_cursor','cursor',c)
    fresh=HistoricalPaper(HistoricalStore(tmp_path,'stream-test',INSTANCE))
    with pytest.raises(ValueError): fresh.recover()
    assert not fresh.ready
    with fresh.store.transaction() as db: assert get(db,'account','account')['quarantined']
