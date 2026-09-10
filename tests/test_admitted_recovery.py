"""Actual process death and SQLite failure, not mocks of approvals or reserves."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
import json
import sqlite3
import subprocess
import sys
import pytest
from app.admitted_paper.engine import AdmittedPaper
from app.admitted_paper.storage import AdmittedStore
from app.admitted_paper.demo import supply, normal_open, complete_exit, latest_state, action, tick, INSTANCE
from app.offline_paper.storage import get, put, rows
from app.offline_paper.models import OfflineError
from test_admitted_paper import paper, accepted, assert_conservation, reopen, ROOT


def child(code,*args):
    result=subprocess.run([sys.executable,'-c',code,*map(str,args)],cwd=ROOT,text=True,capture_output=True,timeout=45)
    return result


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_two_concurrent_ordinary_requests_cannot_double_reserve(paper,side):
    workers=[reopen(paper),reopen(paper)]
    c=supply(paper,side)
    proposals=[paper.prepare(c.candidate_id,'worker-'+str(i)) for i in range(2)]
    assert all(p['body']['result']!='REJECT' for p in proposals)
    assert proposals[0]['body']['account_revision']==proposals[1]['body']['account_revision']
    def submit(pair): return pair[0].submit(pair[1]['approval_id'])
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(submit,zip(workers,proposals)))
    assert sum(r['result']!='REJECT' for r in results)==1
    assert sum('ACCOUNT_REVISION_CHANGED_REISSUE' in r['reason_codes'] for r in results)==1
    with paper.store.transaction() as db:
        assert len(rows(db,'reservations'))==len(rows(db,'paper_consumptions'))==len(rows(db,'outbox'))==1
    paper.pump();assert len(paper.summary()['orders'])==1


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('fault',['before_intent_commit','after_intent_commit','after_broker_execution'])
def test_actual_child_crash_at_intent_boundaries(paper,side,fault):
    # Fresh independent run; demo uses normal provider and approvals throughout.
    result=subprocess.run([sys.executable,'-m','app.admitted_paper.cli','demo','--workspace',str(paper.store.paths.root),
        '--run-id','crash-run','--side',side,'--fault',fault],cwd=ROOT,capture_output=True,text=True,timeout=45)
    assert result.returncode==91,(result.stdout,result.stderr)
    resumed=AdmittedPaper(AdmittedStore(paper.store.paths.root,'crash-run',INSTANCE));resumed.recover()
    once=resumed.summary()
    again=AdmittedPaper(AdmittedStore(paper.store.paths.root,'crash-run',INSTANCE));again.recover();twice=again.summary()
    assert once['counts']==twice['counts'] and once['fees_usdt']==twice['fees_usdt'] and once['account']['cash']==twice['account']['cash']
    assert len([o for o in once['orders'].values() if o['action']['kind']=='ENTRY'])==(0 if fault=='before_intent_commit' else 1)
    assert once['consumption_count']==(0 if fault=='before_intent_commit' else 1)
    if fault=='after_intent_commit':
        # Recovery changes revision: an unaccepted stale grant is REJECTED at
        # the original ID, settled zero; not silently reissued on new state.
        order=next(iter(once['orders'].values()))
        assert order['status']=='REJECTED' and order['reason_code']=='POST_RESERVATION_ACCOUNT_CHANGED'
        assert once['risk_snapshot']['reserved_risk_usdt']=='0'
    if fault=='after_broker_execution':
        assert D(once['risk_snapshot']['reserved_risk_usdt'])>0
        assert next(iter(once['orders'].values()))['status']=='ACCEPTED'


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('prior',['0','.1'])
def test_actual_process_unnotified_entry_fact_is_recovered(paper,side,prior):
    entry,a=accepted(paper,side)
    if D(prior): paper.broker.fill(entry,prior,'prior');paper.pump()
    quantity=D(a['body']['quantity'])-D(prior)
    code='''
import os,sys
from app.admitted_paper.storage import AdmittedStore
from app.admitted_paper.engine import AdmittedBroker
store=AdmittedStore(sys.argv[1],'normal-test','admitted-paper-demo')
AdmittedBroker(store).fill(sys.argv[2],sys.argv[3],'process-fill',defer_details=True,defer_receipt=True)
os._exit(91)
'''
    result=child(code,paper.store.paths.root,entry,quantity);assert result.returncode==91,result.stderr
    r=reopen(paper);one=r.summary()
    assert D(latest_state(r)['remaining_quantity'])==D(a['body']['quantity'])
    assert D(latest_state(r)['protection_covered_quantity'])==D(a['body']['quantity'])
    assert r.ready and one['account']['reconciliation_clear']
    two=reopen(r).summary()
    assert one['fills']==two['fills'] and one['fees_usdt']==two['fees_usdt'] and one['account']['cash']==two['account']['cash']
    complete_exit(r);assert latest_state(r)['phase']=='CLOSED';assert_conservation(r)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('which',['entry','exit'])
def test_child_dies_after_confirmed_fill_commit_before_response(paper,side,which):
    entry,a=accepted(paper,side);q=a['body']['quantity']
    if which=='entry': paper.broker.fill(entry,q,'committed-entry')
    else:
        paper.broker.fill(entry,q,'opening');paper.pump()
        tick(paper,'80' if side=='LONG' else '120','stop')
        paper.broker.fill(action(paper,'CLOSE_ALL'),q,'committed-exit')
    with paper.store.transaction() as db:
        delivery=next(k for k,e in rows(db,'broker_events') if not e['consumed'] and e['payload']['kind']==('ENTRY_FILL' if which=='entry' else 'EXIT_FILL'))
    code='''
import sys
from app.admitted_paper.engine import AdmittedPaper
from app.admitted_paper.storage import AdmittedStore
p=AdmittedPaper(AdmittedStore(sys.argv[1],'normal-test','admitted-paper-demo'))
p.deliver(sys.argv[2],fault='after_receipt_commit')
'''
    result=child(code,paper.store.paths.root,delivery);assert result.returncode==91,result.stderr
    once=reopen(paper);first=once.summary();twice=reopen(once).summary()
    assert first['counts']==twice['counts'] and first['fills']==twice['fills']
    assert first['fees_usdt']==twice['fees_usdt'] and first['account']['cash']==twice['account']['cash']
    if which=='exit': assert latest_state(once)['phase']=='CLOSED'
    else: complete_exit(once)
    assert_conservation(once)


@pytest.mark.parametrize('failure',['intent','receipt'])
def test_database_write_failure_rolls_back_and_stops_new_admission(paper,failure):
    if failure=='intent':
        c=supply(paper,'LONG');approval=paper.prepare(c.candidate_id,'db-failure')
    else:
        entry,approval=accepted(paper,'LONG');paper.broker.fill(entry,approval['body']['quantity'],'dbfill')
        with paper.store.transaction() as db:
            delivery=next(k for k,e in rows(db,'broker_events') if not e['consumed'] and e['payload']['kind']=='ENTRY_FILL')
    with paper.store.transaction() as db:
        table='outbox' if failure=='intent' else 'inbox'
        db.execute("CREATE TRIGGER failure BEFORE INSERT ON "+table+" BEGIN SELECT RAISE(ABORT,'injected test fault'); END")
    with pytest.raises(sqlite3.Error):
        if failure=='intent': paper.submit(approval['approval_id'])
        else: paper.deliver(delivery)
    assert paper.store.failed
    with pytest.raises(OfflineError): paper.prepare('anything','blocked-after-store-error')
    db=sqlite3.connect(paper.store.path.as_uri()+'?mode=ro',uri=True)
    assert db.execute('SELECT count(*) FROM positions').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM fills').fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM paper_consumptions').fetchone()[0]==(0 if failure=='intent' else 1)
    assert db.execute('SELECT count(*) FROM reservations').fetchone()[0]==(0 if failure=='intent' else 1)
    db.close()


@pytest.mark.parametrize('change',['candidate','approval','consumption','reservation','checkpoint','input-chain'])
def test_recovery_corruption_preserves_evidence_and_quarantines(paper,change):
    result,a=normal_open(paper,'LONG')
    with paper.store.transaction() as db:
        if change=='candidate':
            k,v=rows(db,'paper_candidates')[0];v['setup']['confidence']['value']=.7;put(db,'paper_candidates',k,v)
        elif change=='approval':
            k,v=rows(db,'paper_approvals')[0];v['body']['risk']='1';put(db,'paper_approvals',k,v)
        elif change=='consumption':
            k,v=rows(db,'paper_consumptions')[0];v['action_id']='changed';put(db,'paper_consumptions',k,v)
        elif change=='reservation':
            k,v=rows(db,'reservations')[0];v['risk']='0';put(db,'reservations',k,v)
        elif change=='checkpoint':
            k,v=rows(db,'positions')[0];v['checkpoint']['state']['remaining_quantity']='0';put(db,'positions',k,v)
        elif change=='input-chain':
            k,v=rows(db,'paper_inputs')[0];v['input']['sol']='1';put(db,'paper_inputs',k,v)
        counts={t:len(rows(db,t)) for t in ('broker_orders','broker_fills','fills','positions','paper_inputs','paper_approvals')}
    r=AdmittedPaper(AdmittedStore(paper.store.paths.root,'normal-test',INSTANCE))
    with pytest.raises(ValueError): r.recover()
    with r.store.transaction() as db:
        assert counts=={t:len(rows(db,t)) for t in counts}
        assert get(db,'account','account')['quarantined'] and rows(db,'quarantine')
    assert not r.ready


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_failed_current_provider_cannot_stop_bound_position_protection(paper,side):
    normal_open(paper,side)
    with paper.store.transaction() as db:
        k,v=rows(db,'paper_providers')[0];v['funding_model']='not-supported';put(db,'paper_providers',k,v)
    r=reopen(paper)
    assert r.summary()['account']['paused'] and r.summary()['account']['new_entry_block_reason']
    assert D(latest_state(r)['remaining_quantity'])>0
    complete_exit(r,path='gap')
    assert latest_state(r)['phase']=='CLOSED'
    assert_conservation(r)
