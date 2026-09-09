"""Real child process death, WAL recovery, atomic reservations and quarantine."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.offline_paper.engine import OfflinePaper
from app.offline_paper.storage import Store, get, put, rows
from app.offline_paper.models import OfflineError
from app.offline_paper.fixtures import opening, quote, example_settings, example_bundle
from test_offline_paper import paper, entered, state, conservation, ROOT


def cli(paper,*args):
    return subprocess.run([sys.executable,'-m','app.offline_paper.cli',*args,
        '--workspace',str(paper.store.paths.root),'--run','test-run'],cwd=ROOT,text=True,capture_output=True,timeout=30)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('point',['before_intent_commit','after_intent_commit','after_broker_execution','after_receipt_commit'])
def test_real_process_crash_reopen_reconcile_no_duplicate_orders_or_cash(paper,side,point):
    result=cli(paper,'crash-probe','--point',point,'--side',side)
    assert result.returncode==91,(result.stdout,result.stderr)
    resumed=cli(paper,'resume');assert resumed.returncode==0,(resumed.stdout,resumed.stderr)
    once=json.loads(resumed.stdout)
    twice=cli(paper,'resume');assert twice.returncode==0,twice.stdout
    again=json.loads(twice.stdout)
    assert once['account']['cash']==again['account']['cash']
    assert once['fills']==again['fills'] and set(once['orders'])==set(again['orders'])
    entry_orders=[o for o in once['orders'].values() if o['action']['kind']=='ENTRY']
    assert len(entry_orders)==(0 if point=='before_intent_commit' else 1)
    assert len(once['fills'])==(1 if point=='after_receipt_commit' else 0)
    if point!='before_intent_commit':
        assert D(once['risk_snapshot']['reserved_risk_usdt'])==5
    if point=='after_receipt_commit':
        assert D(next(iter(once['positions'].values()))['protection_covered_quantity'])==D('.5')
    conservation(OfflinePaper(Store(paper.store.paths.root,'test-run','offline-paper-demo')))


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_concurrent_fixture_reservations_serialize_version_and_slot(paper,side):
    workers=[OfflinePaper(Store(paper.store.paths.root,'test-run','offline-paper-demo')) for _ in range(2)]
    for worker in workers:worker.recover()
    quote(paper,'100','price');one=opening(paper,side,'first');two=opening(paper,side,'second')
    # Both see the same revision outside the execution lock; only one reserves.
    def submit(pair):
        worker,req=pair
        try:return worker.fixture_entry(req)
        except OfflineError as error:return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(submit,zip(workers,[one,two])))
    assert sum('ACCOUNT_VERSION_CHANGED' in x for x in results)==1
    with paper.store.transaction() as db:
        assert len(rows(db,'reservations'))==len(rows(db,'outbox'))==1
    assert D(paper.summary()['risk_snapshot']['reserved_risk_usdt'])==5


@pytest.mark.parametrize('failure',['before_intent','during_receipt'])
def test_database_write_failure_rolls_back_atomic_unit_and_latches_store(paper,failure):
    quote(paper,'100','price');request=opening(paper)
    if failure=='during_receipt':
        entry=paper.fixture_entry(request);paper.pump();paper.broker.fill(entry,'.5','fail-fill')
        with paper.store.transaction() as db:
            event=next(k for k,e in rows(db,'broker_events') if e['payload']['kind']=='ENTRY_FILL')
    with paper.store.transaction() as db:
        db.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON inbox BEGIN SELECT RAISE(ABORT,'injected write failure'); END" if failure=='during_receipt'
            else "CREATE TRIGGER injected_failure BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'injected write failure'); END")
    with pytest.raises(sqlite3.Error):
        if failure=='during_receipt':paper.deliver(event)
        else:paper.fixture_entry(request)
    assert paper.store.failed
    with pytest.raises(OfflineError,match='STORE_FAILED'): paper.fixture_entry(request)
    # Inspect the failed transaction with a separate raw read-only connection.
    db=sqlite3.connect(paper.store.path.as_uri()+'?mode=ro',uri=True)
    assert db.execute('SELECT count(*) FROM fills').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==0
    if failure=='before_intent':
        assert db.execute('SELECT count(*) FROM reservations').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM requests').fetchone()[0]==0
    else:
        assert db.execute('SELECT count(*) FROM broker_fills').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM reservations').fetchone()[0]==1
    db.close()


@pytest.mark.parametrize('field',['cash','checkpoint','fill','outbox','policy','bundle'])
def test_corruption_quarantined_without_deleting_history(paper,field):
    entered(paper)
    with paper.store.transaction() as db:
        pid,p=rows(db,'positions')[0]
        if field=='cash':
            a=get(db,'account','account');a['cash']='999999';put(db,'account','account',a)
        elif field=='checkpoint':
            p['checkpoint']['state']['remaining_quantity']='1000';put(db,'positions',pid,p)
        elif field=='fill':
            key,f=rows(db,'fills')[0];f['price']='1';put(db,'fills',key,f)
        elif field=='outbox':
            key=state_id=p['checkpoint']['state']['actions'][0]['action_id']
            item=get(db,'outbox',key);item['action']['quantity']='999';put(db,'outbox',key,item)
        elif field=='policy':
            r=get(db,'reservations',pid);r['exit_policy']['runner_trail_r']='100';put(db,'reservations',pid,r)
        elif field=='bundle':
            key,b=rows(db,'configurations')[0];b['bundle_digest']='0'*64;put(db,'configurations',key,b)
        before={t:len(rows(db,t)) for t in ('fills','broker_fills','outbox','positions','reservations')}
    fresh=OfflinePaper(Store(paper.store.paths.root,'test-run','offline-paper-demo'))
    with pytest.raises(ValueError): fresh.recover()
    assert not fresh.ready and fresh.store.path.exists()
    with fresh.store.transaction() as db:
        assert get(db,'account','account')['quarantined']
        assert before=={t:len(rows(db,t)) for t in before}
        assert rows(db,'quarantine')


@pytest.mark.parametrize('wrong',['instance','schema','application'])
def test_unknown_database_identity_refused_no_adoption(paper,wrong):
    if wrong=='instance':
        with pytest.raises(OfflineError,match='Foreign'): Store(paper.store.paths.root,'test-run','foreign-instance').connect()
    else:
        with paper.store.transaction() as db:
            db.execute('PRAGMA user_version=99' if wrong=='schema' else 'PRAGMA application_id=123')
        with pytest.raises(OfflineError,match='Unknown database'): paper.store.connect()
    assert paper.store.path.exists()


def test_existing_run_never_reinitializes_or_tops_up(paper):
    entered(paper);before=paper.summary()['account']['cash']
    with pytest.raises(OfflineError,match='already exists'):
        Store.create(paper.store.paths.root,'test-run',example_settings(fixtures=True),example_bundle(paper.store.paths.root))
    assert paper.summary()['account']['cash']==before


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_process_restart_after_confirmed_exit_no_double_fees_or_release(paper,side):
    entered(paper,side);quote(paper,'90' if side=='LONG' else '110','stop');paper.pump()
    from app.offline_paper.fixtures import action
    close=action(paper,'CLOSE_ALL');paper.broker.fill(close,'.5','close-before-death')
    with paper.store.transaction() as db:
        event=next(k for k,e in rows(db,'broker_events') if not e['consumed'] and e['payload']['kind']=='EXIT_FILL')
    code='from app.offline_paper.storage import Store; from app.offline_paper.engine import OfflinePaper; import sys; OfflinePaper(Store(sys.argv[1],"test-run","offline-paper-demo")).deliver(sys.argv[2],fault="after_receipt_commit")'
    died=subprocess.run([sys.executable,'-c',code,str(paper.store.paths.root),event],cwd=ROOT,capture_output=True,timeout=30)
    assert died.returncode==91,died.stderr
    first=cli(paper,'resume');assert first.returncode==0,first.stdout
    final=json.loads(first.stdout);assert next(iter(final['positions'].values()))['phase']=='CLOSED'
    second=cli(paper,'resume');assert second.returncode==0,second.stdout
    again=json.loads(second.stdout)
    assert final['fills']==again['fills'] and final['account']['cash']==again['account']['cash']
    assert D(again['risk_snapshot']['reserved_risk_usdt'])==0


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_two_real_processes_contend_for_same_snapshot_revision(paper,side):
    code='''
import json,sys
from app.offline_paper.storage import Store
from app.offline_paper.engine import OfflinePaper
from app.offline_paper.models import FixtureEntry,OfflineError
r=OfflinePaper(Store(sys.argv[1],"test-run","offline-paper-demo"));r.recover()
print("READY",flush=True)
request=FixtureEntry.model_validate(json.loads(sys.stdin.readline()))
try: print(json.dumps({"reserved":r.fixture_entry(request)}),flush=True)
except OfflineError as e: print(json.dumps({"rejected":str(e)}),flush=True)
'''
    children=[subprocess.Popen([sys.executable,'-c',code,str(paper.store.paths.root)],cwd=ROOT,
        text=True,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
    try:
        for child in children:assert child.stdout.readline().strip()=='READY'
        requests=[opening(paper,side,'process-'+str(i)) for i in range(2)]
        assert requests[0].expected_revision==requests[1].expected_revision
        for child,request in zip(children,requests):
            child.stdin.write(request.model_dump_json()+'\n');child.stdin.flush()
        results=[]
        for child in children:
            stdout,stderr=child.communicate(timeout=20)
            assert child.returncode==0,stderr
            results.append(json.loads(stdout))
        assert sum('reserved' in result for result in results)==1
        assert [r['rejected'] for r in results if 'rejected' in r]==['ACCOUNT_VERSION_CHANGED']
        assert D(paper.summary()['risk_snapshot']['reserved_risk_usdt'])==5
    finally:
        for child in children:
            if child.poll() is None:child.kill();child.wait(timeout=5)
