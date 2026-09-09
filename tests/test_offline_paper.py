"""8A tests: ordinary rejection and labelled existing-position fixtures are distinct."""
from decimal import Decimal as D
from pathlib import Path
import json
import shutil

import pytest

from app.offline_paper.models import OfflineError, Quote
from app.offline_paper.storage import Store, get, put, rows
from app.offline_paper.engine import OfflinePaper
from app.offline_paper.fixtures import example_bundle, example_settings, opening, quote, action, cancel_entry, run_scenario

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def paper(tmp_path):
    for name in ('isolation-policy.json','admission.yaml','exit-policy.yaml','examples/offline-paper/main.yaml','examples/offline-paper/manifest.yaml'):
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    store=Store.create(tmp_path,'test-run',example_settings(fixtures=True),example_bundle(tmp_path))
    runner=OfflinePaper(store);runner.recover()
    return runner


def state(paper):
    return next(iter(paper.summary()['positions'].values()))


def entered(paper,side='LONG',partial=False):
    quote(paper,'100','start')
    req=opening(paper,side);entry=paper.fixture_entry(req);paper.pump()
    paper.broker.fill(entry,'.2' if partial else '.5','entry');paper.pump()
    return entry


def conservation(paper):
    with paper.store.transaction() as db:
        for pid,p in rows(db,'positions'):
            s=p['checkpoint']['state'];fs=[f for _,f in rows(db,'fills') if f['position_id']==pid]
            entries=sum((D(f['quantity'])*D(f['price']) for f in fs if f['kind']=='ENTRY_FILL'),D(0))
            exits=sum((D(f['quantity'])*D(f['price']) for f in fs if f['kind']=='EXIT_FILL'),D(0))
            fees=sum((D(f['fee_usdt']) for f in fs),D(0))
            sign=1 if s['side']=='LONG' else -1
            assert D(s['realized_gross_pnl'])==sign*(exits-entries+D(s['remaining_entry_cost']))
            assert D(s['realized_net_pnl'])==D(s['realized_gross_pnl'])-fees
            assert D(s['remaining_quantity'])==sum((D(f['quantity'])*(1 if f['kind']=='ENTRY_FILL' else -1) for f in fs),D(0))
        expected=paper.store.settings(db).initial_balance+sum((D(p['checkpoint']['state']['realized_net_pnl']) for _,p in rows(db,'positions')),D(0))
        assert D(get(db,'account','account')['cash'])==expected


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('name',['admission-rejection','partial-cover','partial-cancel','tp-runner','stop-gap','unknown-reconcile'])
def test_scenarios(paper,side,name):
    result=run_scenario(paper,name,side)
    assert result['summary']['live_allowed'] is False
    if name=='admission-rejection':
        assert result['origin']=='A_NORMAL_ADMISSION'
        assert result['result']['result']=='REJECT'
        assert 'NORMAL_ENTRY_FULL_EXIT_CONTRACT_UNSUPPORTED_8A' in result['result']['reason_codes']
        assert not result['summary']['orders'] and not result['summary']['reservations']
    else:
        assert result['origin']=='B_FIXTURE_EXISTING_POSITION'
        conservation(paper)
        if name in ('tp-runner','stop-gap'):
            assert D(state(paper)['remaining_quantity'])==0
            assert state(paper)['phase']=='CLOSED'
        if name=='partial-cancel': assert state(paper)['entry_sealed']


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_acceptance_no_fill_partial_growth_reconfirmation_unknown(paper,side):
    quote(paper,'100','first-price');req=opening(paper,side)
    entry=paper.fixture_entry(req);paper.pump()
    assert not paper.summary()['positions'] and not paper.summary()['fills']
    paper.broker.fill(entry,'.2','first');paper.pump()
    before=state(paper);old=before['protection_action_id']
    assert D(before['protection_covered_quantity'])==D('.2')
    paper.broker.fill(entry,'.3','second')
    with paper.store.transaction() as db:
        fill_event=[k for k,e in rows(db,'broker_events') if not e['consumed'] and e['payload']['kind']=='ENTRY_FILL'][-1]
    paper.deliver(fill_event)
    s=state(paper)
    assert D(s['remaining_quantity'])==D('.5')
    assert s['protection_status']!='ACTIVE'
    with paper.store.transaction() as db:
        assert get(db,'broker_orders',old)['status']=='ACCEPTED'  # no protection gap while new intent unconfirmed
    move=next(a for a in reversed(s['actions']) if a['kind']=='MOVE_STOP')
    paper.broker.fixture_delivery('unknown-replacement',dict(kind='PROTECTION_LOST',position_id=s['position_id'],action_id=move['action_id'],status='UNKNOWN',cumulative_filled_quantity=None))
    paper.deliver('fixture-fault:unknown-replacement')
    assert state(paper)['protection_status']!='ACTIVE'
    # Do not claim UNKNOWN coverage. Durable original intent is reconciled.
    paper.pump();conservation(paper)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_fill_dedup_new_delivery_and_restart(paper,side):
    entry=entered(paper,side)
    initial=paper.summary();fid=next(iter(initial['fills']))
    assert paper.broker.fill(entry,'.5','entry')==fid
    paper.broker.reconcile(entry,'redelivery');paper.pump()
    assert paper.summary()['fills']==initial['fills']
    before=state(paper)
    restarted=OfflinePaper(Store(paper.store.paths.root,'test-run','offline-paper-demo'))
    restarted.recover()
    assert restarted.ready
    assert state(restarted)['remaining_quantity']==before['remaining_quantity']
    assert state(restarted)['frozen_initial_r']==before['frozen_initial_r']
    assert restarted.summary()['account']['cash']==initial['account']['cash']
    assert len(restarted.summary()['orders'])==len(initial['orders'])
    conservation(restarted)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_tp_partial_terminal_before_details_and_breakeven(paper,side):
    entered(paper,side);s=state(paper);sign=1 if side=='LONG' else -1
    quote(paper,D(s['frozen_r_anchor_entry'])+sign*D(s['frozen_initial_r'])*D('1.1'),'tp1');paper.pump()
    tp=action(paper,'TP1');initial_stop=state(paper)['current_stop']
    assert not state(paper)['tp1_complete'] and D(state(paper)['tp1_filled'])==0
    paper.broker.fill(tp,'.05','tp-part');paper.pump()
    assert D(state(paper)['tp1_filled'])==D('.05') and not state(paper)['tp1_complete']
    assert state(paper)['current_stop']==initial_stop
    paper.broker.fill(tp,'.1','tp-last',defer_details=True)
    with paper.store.transaction() as db:
        receipt=next(k for k,e in rows(db,'broker_events') if not e['consumed'] and e['payload']['kind']=='RECEIPT')
    paper.deliver(receipt)  # Inspect before the queued original-ID query supplies details.
    assert not state(paper)['tp1_complete'] and state(paper)['current_stop']==initial_stop
    paper.broker.reconcile(tp,'tp-final-details');paper.pump()
    s=state(paper);assert s['tp1_complete']
    assert (D(s['current_stop'])-D(s['frozen_r_anchor_entry']))*sign>0
    assert not s['faults'];conservation(paper)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_tp_stop_race_no_overexit_and_stop_gap(paper,side):
    entered(paper,side);s=state(paper);sign=1 if side=='LONG' else -1
    quote(paper,D('111') if sign==1 else D('89'),'tp-price');paper.pump()
    tp=action(paper,'TP1')
    paper.broker.fill(tp,'.05','early-tp',defer_details=True,defer_receipt=True)
    quote(paper,D('80') if sign==1 else D('120'),'gap')
    # The old stop is still accepted at the synthetic venue until cancellation.
    stop=s['protection_action_id']
    paper.broker.fill(stop,'.5','stop-race',defer_details=True)
    paper.pump()
    s=state(paper)
    assert D(s['remaining_quantity'])==0 and s['phase']=='CLOSED'
    fs=list(paper.summary()['fills'].values());exits=[f for f in fs if f['kind']=='EXIT_FILL']
    assert sum(D(f['quantity']) for f in exits)==D('.5')
    stop_fill=next(f for f in exits if f['action_id']==stop)
    assert (D(stop_fill['price'])-D('95' if sign==1 else '105'))*sign<0
    assert not s['faults'];conservation(paper)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_late_entry_after_exit_uses_remaining_cost_not_historical_average(paper,side):
    entry=entered(paper,side,partial=True);s=state(paper);stop=s['protection_action_id'];frozen=s['frozen_initial_r']
    quote(paper,'90' if side=='LONG' else '110','gap')
    paper.broker.fill(stop,'.2','first-exit',defer_details=True,defer_receipt=True)
    # Fill was executed before opening-leg cancel; its delivery is late.
    paper.broker.fill(entry,'.3','late-opening',defer_details=True,defer_receipt=True)
    paper.broker.reconcile(stop,'exit-details');paper.pump()
    assert D(state(paper)['remaining_quantity'])==D('.3')
    assert state(paper)['frozen_initial_r']==frozen
    paper.pump()
    close=action(paper,'CLOSE_ALL');paper.broker.fill(close,'.3','last-exit');paper.pump()
    assert state(paper)['phase']=='CLOSED';conservation(paper)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_loss_halt_and_missing_score_do_not_stop_existing_protection(paper,side):
    entered(paper,side)
    with paper.store.transaction() as db:
        a=get(db,'account','account');a['paused']=True;put(db,'account','account',a)
    quote(paper,'90' if side=='LONG' else '110','halt-gap');paper.pump()
    close=action(paper,'CLOSE_ALL');paper.broker.fill(close,'.5','halt-close');paper.pump()
    assert state(paper)['phase']=='CLOSED'
    assert paper.summary()['risk_snapshot']['reserved_risk_usdt']=='0'
    assert D(paper.summary()['risk_snapshot']['day_realized_loss_usdt'])>0
    assert paper.summary()['risk_snapshot']['consecutive_losses']==1
    conservation(paper)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('quantity',[None,'0','.2'])
def test_protection_lost_highwater_none_zero_and_contradiction(paper,side,quantity):
    entered(paper,side);s=state(paper);stop=s['protection_action_id'];pid=s['position_id']
    paper.broker.fixture_delivery('lost',dict(kind='PROTECTION_LOST',position_id=pid,action_id=stop,
        status='UNKNOWN',cumulative_filled_quantity=quantity))
    paper.deliver('fixture-fault:lost')
    paper.broker.fixture_delivery('old',dict(kind='RECEIPT',position_id=pid,action_id=stop,status='ACCEPTED',
        cumulative_filled_quantity='0',reduce_only_verified=True,stop_price=s['current_stop'],covers_remaining=True))
    paper.deliver('fixture-fault:old')
    if quantity=='.2':
        stop_state=next(a for a in state(paper)['actions'] if a['action_id']==stop)
        assert max(D(stop_state['acknowledged_quantity']),D(stop_state['filled_quantity']),D(stop_state['terminal_quantity'] or 0))==D('.2')
        assert not any(a['target_confirmed'] for a in state(paper)['actions'] if a['kind']=='RECONCILE')
        paper.broker.fixture_delivery('contradiction',dict(kind='RECEIPT',position_id=pid,action_id=stop,status='CANCELED',
            cumulative_filled_quantity='0'))
        paper.deliver('fixture-fault:contradiction')
        assert state(paper)['faults'] and not any(a['kind']=='CLOSE_ALL' for a in state(paper)['actions'])
    assert D(state(paper)['remaining_quantity'])==D('.5')
    assert len(paper.summary()['fills'])==1


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_protection_lost_known_quantity_then_original_fill_details_and_restart(paper,side):
    entered(paper,side);s=state(paper);pid=s['position_id'];stop=s['protection_action_id']
    quote(paper,'90' if side=='LONG' else '110','stop-price')
    paper.broker.fill(stop,'.2','delayed',defer_details=True,defer_receipt=True)
    paper.broker.fixture_delivery('loss-evidence',dict(kind='PROTECTION_LOST',position_id=pid,action_id=stop,
        status='UNKNOWN',cumulative_filled_quantity='.2'))
    paper.deliver('fixture-fault:loss-evidence')
    assert D(state(paper)['remaining_quantity'])==D('.5')
    resumed=OfflinePaper(Store(paper.store.paths.root,'test-run','offline-paper-demo'));resumed.recover()
    assert D(state(resumed)['remaining_quantity'])==D('.3')
    close=action(resumed,'CLOSE_ALL');resumed.broker.fill(close,'.3','remaining-exit');resumed.pump()
    assert state(resumed)['phase']=='CLOSED';conservation(resumed)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_actual_adverse_entry_risk_cancels_unfilled_leg_without_erasing_fill(paper,side):
    quote(paper,'100','planned');entry=paper.fixture_entry(opening(paper,side));paper.pump()
    quote(paper,'120' if side=='LONG' else '80','adverse-entry')
    paper.broker.fill(entry,'.2','adverse-part');paper.pump()
    summary=paper.summary();s=state(paper)
    assert len(summary['fills'])==1 and D(s['remaining_quantity'])==D('.2')
    assert summary['account']['paused'] and summary['orders'][entry]['status']=='CANCELED'
    assert s['entry_sealed'] and D(s['protection_covered_quantity'])==D('.2')
    assert next(iter(summary['reservations'].values()))['risk_breach']=='ACTUAL_INITIAL_STOP_RISK_EXCEEDS_RESERVATION'
    conservation(paper)
