"""Normal approval path, no fixture_entry and no mocked gate/Broker/ledger."""
from decimal import Decimal as D
import json
import subprocess
import sys
import pytest
from test_stage08d_contracts import system, supply, market
from test_stage08c_historical import ROOT, observation
from app.historical_replay.data import START
from app.execution_costs.storage import QuantifiedStore,hget,hput
from app.execution_costs.engine import QuantifiedPaper,grant
from app.execution_costs.replay import Replay,saved_cursor
from app.execution_costs.funding import settle,net_funding
from app.offline_paper.storage import get,put,rows
from app.offline_paper.broker import inventory


def enter(p,r,side,*,partial=False):
    result=supply(p,r,side)[0]
    assert result['result'] in ('APPROVE','REDUCE'),result
    market(p,r,100,START+1000)
    market(p,r,100,START+2000,qty='10' if partial else '10000')
    return result


def state(p,pid):
    with p.store.transaction() as db:return get(db,'positions',pid)['checkpoint']['state']


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_normal_partial_opening_cover_exit_funding_and_double_recovery(tmp_path,side):
    p,r=system(tmp_path);a=enter(p,r,side,partial=True);pid=a['position_id'];sgn=1 if side=='LONG' else -1
    first=state(p,pid);assert D(first['remaining_quantity'])==D('.1')
    market(p,r,100,START+3000)
    assert D(state(p,pid)['original_quantity'])>D('.1')
    market(p,r,100,START+4000)
    market(p,r,100,START+5000)
    with p.store.transaction() as db:
        q=D(get(db,'reservations',pid)['approved_quantity'])
        assert inventory(db,pid)==q
        stop=next(o for _,o in reversed(rows(db,'broker_orders')) if o['action']['kind'] in ('ARM_STOP','MOVE_STOP') and o['status']=='ACCEPTED')
        assert D(stop['action']['quantity'])==q
        fact=dict(at_ms=START+5500,rate='.001',mark_price='100',source_digest='f'*64)
        cur=hget(db,'history_cursor','cursor');r._set_clock(db,cur,fact['at_ms'])
        hput(db,'history_meta','active_funding',fact);settle(p,db,fact);settle(p,db,fact);saved_cursor(db,cur)
        assert net_funding(db)==-sgn*q*D('.1')
    for second,mid in ((6,115),(7,115),(8,115),(9,115),(10,126),(11,126),(12,126),(13,126),(14,155),(15,155),(16,130),(17,130),(18,130)):
        market(p,r,mid if sgn==1 else 200-mid,START+second*1000)
    s=state(p,pid)
    assert s['phase']=='CLOSED' and D(s['remaining_quantity'])==0,s
    with p.store.transaction() as db:
        facts=[f for _,f in rows(db,'fills')]
        cashflow=sum((D(f['quantity'])*D(f['price'])*(-sgn if f['kind']=='ENTRY_FILL' else sgn) for f in facts),D(0))
        fees=sum((D(f['fee_usdt']) for f in facts),D(0))
        assert D(s['realized_gross_pnl'])==cashflow
        assert D(get(db,'account','account')['cash'])==D(500)+cashflow-fees+net_funding(db)
        before=(rows(db,'fills'),rows(db,'broker_orders'),get(db,'account','account')['cash'])
        b=hget(db,'history_approvals',a['approval_id'])['body']
        assert get(db,'reservations',pid)['plan']['admission_result'] in ('APPROVE','REDUCE')
        assert D(b['quantity'])==q and D(s['frozen_initial_r'])==abs(D(first['frozen_r_anchor_entry'])-D(90 if sgn==1 else 110))
    for _ in range(2):
        restored=QuantifiedPaper(QuantifiedStore(tmp_path,p.store.run_id,p.store.instance_id));restored.recover()
        with restored.store.transaction() as db:
            assert before==(rows(db,'fills'),rows(db,'broker_orders'),get(db,'account','account')['cash'])


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('first_notified',[False,True])
def test_unnotified_entry_fills_recovered_by_original_id(tmp_path,side,first_notified):
    p,r=system(tmp_path);a=supply(p,r,side)[0];market(p,r,100,START+1000)
    if first_notified: market(p,r,100,START+2000,qty='10')
    with p.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');at=START+3000
        e=observation('SOLUSDT',100,at,c['sequences']['SOLUSDT'][0]+1)
        r.trade(db,c,e,active=False)
        r._set_clock(db,c,at)
        # Explicit same-event fault injection boundary normally installed by
        # _fills; only notification is deferred, not approval or fill checks.
        hput(db,'history_meta','active_market',e)
        hput(db,'history_meta','liquidity',dict(event_id=e['event_id'],used='0'))
        order=get(db,'broker_orders',a['entry_action_id']);q=D(order['action']['quantity'])-D(order['cumulative'])
        p.broker.fill(a['entry_action_id'],q,e['event_id'],defer_details=True,defer_receipt=True);saved_cursor(db,c)
        filled=len(rows(db,'fills'));brokerq=inventory(db,a['position_id'])
    for _ in range(2):
        p=QuantifiedPaper(QuantifiedStore(tmp_path,p.store.run_id,p.store.instance_id));p.recover()
        with p.store.transaction() as db:
            if len(rows(db,'fills'))==filled:
                assert not p.ready and not get(db,'account','account')['reconciliation_clear']
                assert any(o.get('entry_reconciliation') and not o['target_confirmed'] for _,o in rows(db,'outbox'))
            at=hget(db,'history_cursor','cursor')['at_ms']
        # Historical controls retain the declared acceptance delay. Recovery
        # schedules original-ID queries; it does not invent an instant response.
        r=Replay(p)
        for offset in (1000,2000,3000):market(p,r,100,at+offset)
        assert D(state(p,a['position_id'])['remaining_quantity'])==brokerq
        with p.store.transaction() as db:
            assert len(rows(db,'fills'))==filled+1
            assert len([o for _,o in rows(db,'broker_orders') if o['action']['kind']=='ENTRY'])==1


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_quote_drift_before_acceptance_never_chases(tmp_path,side):
    p,r=system(tmp_path);a=supply(p,r,side)[0]
    market(p,r,101 if side=='LONG' else 99,START+1000)
    with p.store.transaction() as db:
        order=get(db,'broker_orders',a['entry_action_id'])
        assert order['status']=='REJECTED' and order['reason_code']=='ADVERSE_EXECUTION_QUOTE_DRIFT'
        assert not rows(db,'positions') and not rows(db,'broker_fills')


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_confirmed_off_model_fill_kept_remaining_canceled_and_protected(tmp_path,side):
    p,r=system(tmp_path);a=supply(p,r,side)[0];market(p,r,100,START+1000)
    market(p,r,101 if side=='LONG' else 99,START+2000,qty='10')
    market(p,r,101 if side=='LONG' else 99,START+3000)
    with p.store.transaction() as db:
        assert get(db,'account','account')['paused']
        assert len(rows(db,'fills'))==1
        rr=get(db,'reservations',a['position_id']);assert rr['fill_deviation_reason']=='ACTUAL_FILL_OUTSIDE_APPROVED_GEOMETRY'
        assert get(db,'broker_orders',a['entry_action_id'])['status']=='CANCELED'
        assert any(o['status']=='ACCEPTED' and o['action']['kind']=='ARM_STOP' for _,o in rows(db,'broker_orders'))


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_actual_funding_budget_breach_latches_without_rewriting_approval(tmp_path,side):
    p,r=system(tmp_path);a=enter(p,r,side);market(p,r,100,START+3000)
    with p.store.transaction() as db:
        before=hget(db,'history_approvals',a['approval_id']);cur=hget(db,'history_cursor','cursor');r._set_clock(db,cur,START+3500)
        f=dict(at_ms=START+3500,rate='.1' if side=='LONG' else '-.1',mark_price='100',source_digest='f'*64)
        hput(db,'history_meta','active_funding',f);settle(p,db,f);saved_cursor(db,cur)
        assert hget(db,'history_meta','funding-limit:'+a['position_id']) is not None
        assert get(db,'account','account')['paused']
        assert hget(db,'history_approvals',a['approval_id'])==before
    for n in (4,5,6):market(p,r,85 if side=='LONG' else 115,START+n*1000)
    assert state(p,a['position_id'])['phase']=='CLOSED'


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('fault',['before_intent_commit','after_intent_commit','after_broker_execution','after_receipt_commit'])
def test_real_subprocess_crash_recovery_normal_approval(tmp_path,side,fault):
    p,r=system(tmp_path)
    # Supply through the real provider, without automatically consuming the
    # approval: temporarily not-ready is a legitimate new-entry state, not mock.
    p._recovered=False;p.ready=False;supply(p,r,side);p.recover()
    with p.store.transaction() as db:
        cid=db.execute('SELECT id FROM history_candidates').fetchone()[0]
    rec=p.prepare(cid,'crash-normal');assert rec['body']['result'] in ('APPROVE','REDUCE')
    code='''
import sys,json
from app.execution_costs.storage import QuantifiedStore,hget
from app.execution_costs.engine import QuantifiedPaper
from app.execution_costs.replay import Replay,saved_cursor
from app.offline_paper.storage import get
store=QuantifiedStore(sys.argv[1],sys.argv[2],sys.argv[3]);p=QuantifiedPaper(store);p.recover()
with store.transaction() as db: old=hget(db,'history_approvals',sys.argv[4])
fresh=p.prepare(old['body']['candidate_id'],'crash-normal-child')
assert fresh['body']['result'] in ('APPROVE','REDUCE'),fresh
print(json.dumps({'approval_id':fresh['approval_id']}),flush=True)
a=p.submit(fresh['approval_id'],fault=sys.argv[5]);assert a['entry_action_id'],a
r=Replay(p)
with store.transaction() as db:
 c=hget(db,'history_cursor','cursor');r._set_clock(db,c,c['at_ms']+1000);saved_cursor(db,c)
p.pump(fault=sys.argv[5])
'''
    child=subprocess.run([sys.executable,'-c',code,str(tmp_path),p.store.run_id,p.store.instance_id,rec['approval_id'],fault],cwd=ROOT,capture_output=True,text=True)
    assert child.returncode==91,(child.stdout,child.stderr)
    p=QuantifiedPaper(QuantifiedStore(tmp_path,p.store.run_id,p.store.instance_id));p.recover()
    if fault=='before_intent_commit':
        # Recovery changes account revision: obtain a NEW grant, never rewrite.
        fresh=p.prepare(cid,'retry-after-crash');p.submit(fresh['approval_id'])
    r=Replay(p)
    for sec in (2,3,4):market(p,r,100,START+sec*1000)
    with p.store.transaction() as db:
        assert len([o for _,o in rows(db,'broker_orders') if o['action']['kind']=='ENTRY'])==1
        assert len([f for _,f in rows(db,'fills') if f['kind']=='ENTRY_FILL'])==1
        before=rows(db,'fills'),get(db,'account','account')['cash']
    p.recover()
    with p.store.transaction() as db:assert before==(rows(db,'fills'),get(db,'account','account')['cash'])
