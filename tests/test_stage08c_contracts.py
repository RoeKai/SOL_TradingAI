from decimal import Decimal as D
import copy
import json
import pytest
from test_stage08c_historical import system, supply, observation, component_inventory
from app.historical_replay.data import START, HistoricalError, seal
from app.historical_replay.storage import hget,hput
from app.historical_replay.models import HistoricalCandidate
from app.historical_replay.plans import build_plan,validate_plan
from app.historical_replay.scenarios import evaluate_scenarios
from app.historical_replay.provider import describe
from app.historical_replay.evidence import review
from app.historical_replay.replay import saved_cursor
from app.historical_replay.funding import settle, snapshot, net_funding
from app.offline_paper.storage import get,put,rows
from app.configuration.inputs import PlanInputs
from app.exits.bindings import capture_plan
from app.historical_replay.engine import HistoricalPaper


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_historical_conditional_paths_are_not_approval(tmp_path,side):
    p,r=system(tmp_path);supply(p,r,side)
    with p.store.transaction() as db:
        c=HistoricalCandidate.model_validate(json.loads(db.execute('SELECT payload FROM history_candidates').fetchone()[0]))
        a=json.loads(db.execute('SELECT payload FROM history_approvals').fetchone()[0])['body']
        inputs=PlanInputs.model_validate(a['input_binding']);assert inputs.admission.result=='REJECT'
        plan=build_plan(c,r.bundle,r.model);validate_plan(plan,c,r.bundle,r.model)
        snapshot=capture_plan(inputs.setup,inputs.rr,inputs.scorecard,inputs.admission)
        ev=evaluate_scenarios(plan,c.setup,snapshot,D('.4'))
        assert ev.full_policy_expected_return is None and ev.win_probability is None
        assert ev.pre_entry_funding_budget_usdt==D('1')
        for s in ev.scenarios:
            if s.status!='SUPPORTED': continue
            assert s.net_pnl==s.gross_pnl-s.fees_usdt-D('1')
            assert sum((leg.gross_cash_flow for leg in s.legs),D(0))==s.gross_pnl
            assert s.scenario_net_rr==s.net_pnl/ev.conservative_initial_risk_usdt
        s0,s1,s2,s3=ev.scenarios
        assert s0.net_pnl<0
        assert s3.status=='SUPPORTED'
        final=s3.legs[-1].fill_price
        assert final!=s3.reference_structure_price
        assert (s3.reference_structure_price-final)*(1 if side=='LONG' else -1)>0
        assert not rows(db,'broker_orders') and not rows(db,'reservations')


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_changed_policy_direction_and_structure_never_rebind(tmp_path,side):
    p,r=system(tmp_path);supply(p,r,side)
    with p.store.transaction() as db:
        c=HistoricalCandidate.model_validate(json.loads(db.execute('SELECT payload FROM history_candidates').fetchone()[0]))
        plan=build_plan(c,r.bundle,r.model)
        for bad in (plan.model_copy(update={'initial_stop':plan.initial_stop+D(1)}),
                    plan.model_copy(update={'runner_fraction':D('.4')}),
                    plan.model_copy(update={'side':'SHORT' if side=='LONG' else 'LONG'})):
            with pytest.raises(HistoricalError): validate_plan(bad,c,r.bundle,r.model)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_nearest_runner_reference_not_farthest(tmp_path,side):
    p,r=system(tmp_path);supply(p,r,side)
    with p.store.transaction() as db:
        c=HistoricalCandidate.model_validate(json.loads(db.execute('SELECT payload FROM history_candidates').fetchone()[0]))
        plan=build_plan(c,r.bundle,r.model)
        assert plan.runner_reference_price==D(155 if side=='LONG' else 45)


def test_future_prefix_change_does_not_change_prior_decision_geometry(tmp_path):
    p,r=system(tmp_path);supply(p,r)
    with p.store.transaction() as db:
        raw=json.loads(db.execute('SELECT payload FROM history_candidates').fetchone()[0]);w=raw['evidence']['window'];level=raw['evidence']['level']
        before=describe(r.bundle,r.model,r.run,'LONG',w,level,now=START//1000)
        # Later rows may exist on disk but are not part of the consumed prefix.
        future=dict(at_ms=START+15000,last={s:observation(s,99999,START+15000,999) for s in ('SOLUSDT','BTCUSDT','ETHUSDT')},volume_sol='99999')
        hput(db,'history_samples',str(START+15000),future)
        after,_=review(p.store,db,before.candidate_id,now=START//1000)
        assert before.model_dump(mode='json')==after.model_dump(mode='json')
        with pytest.raises(HistoricalError,match='FUTURE'):
            describe(r.bundle,r.model,r.run,'LONG',[*w,future],level,now=START//1000)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_right_swing_confirmation_required(tmp_path,side):
    p,r=system(tmp_path);supply(p,r,side)
    with p.store.transaction() as db:
        c=json.loads(db.execute('SELECT payload FROM history_candidates').fetchone()[0])
        for e in c['setup']['structure_evidence']:
            if e['kind'].startswith('swing'):
                at=int(e['evidence_id'].split(':')[-1])
                assert e['observed_at']*1000==at+15000
                assert e['observed_at']<=c['setup']['created_at']


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('rate',['.001','-.001'])
def test_component_partial_funding_cross_day_and_unrealized(tmp_path,side,rate):
    p,r=system(tmp_path);component_inventory(p,side)
    with p.store.transaction() as db:
        c=hget(db,'history_cursor','cursor');at=START+86400000
        r._set_clock(db,c,at)
        a=get(db,'account','account');a['quote']=dict(event_id='component-mid',at=at/1000,bid='95',ask='95');put(db,'account','account',a)
        f=dict(at_ms=at,rate=rate,mark_price='100',source_digest='f'*64)
        hput(db,'history_meta','active_funding',f);settle(p,db,f);saved_cursor(db,c)
        s=snapshot(p.store,db)
        assert s.trades_today==0 and s.equity_usdt==D(get(db,'account','account')['cash'])+(D('47.5')-50)*(1 if side=='LONG' else -1)
        previous=net_funding(db)
        pos=get(db,'positions','component-position');pos['checkpoint']['state']['remaining_quantity']='.2';pos['checkpoint']['state']['remaining_entry_cost']='20'
        put(db,'positions','component-position',pos)
        at+=28800000;r._set_clock(db,c,at);f=dict(f,at_ms=at);hput(db,'history_meta','active_funding',f);settle(p,db,f)
        assert net_funding(db)-previous==-D('.2')*100*D(rate)*(1 if side=='LONG' else -1)


def test_invalid_fake_grant_and_fixture_path_cannot_open(tmp_path):
    p,r=system(tmp_path)
    for value in ('0'*64,{'verified':True,'result':'APPROVE'},'Signal'):
        with pytest.raises(ValueError): p.submit(value)
    assert p.summary()['counts']['orders']==0


def test_atomic_cursor_and_funding_rollback(tmp_path):
    p,r=system(tmp_path);component_inventory(p,'LONG')
    with p.store.transaction() as db: before=hget(db,'history_cursor','cursor');cash=get(db,'account','account')['cash']
    with pytest.raises(OSError):
        with p.store.transaction() as db:
            c=hget(db,'history_cursor','cursor');r._set_clock(db,c,START+3000)
            f=dict(at_ms=START+3000,rate='.001',mark_price='100',source_digest='f'*64)
            hput(db,'history_meta','active_funding',f);settle(p,db,f);saved_cursor(db,c)
            raise OSError('injected before atomic commit')
    with p.store.transaction() as db:
        assert before==hget(db,'history_cursor','cursor') and cash==get(db,'account','account')['cash']
        assert net_funding(db)==0
