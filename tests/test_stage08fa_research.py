"""Author-written synthetic research tests; never historical validation claims."""
import ast
import copy
from decimal import Decimal as D, Context, localcontext
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from app.signal_research.features import extract, price_units, SCALE
from app.signal_research.labels import ObservationIndex, HORIZONS
from app.signal_research.statistics import compare, compact, block_interval, summarize
from app.signal_research.costs import evaluate, research_row, boundary, GRID, CostSummary
from app.signal_research.cli import export_features, cost_pass, sol_events, PROTOCOL_SHA256, run
from app.scenario_diagnostics.source import frozen_source
from app.scenario_diagnostics.costs import cost_row
from app.offline_paper.storage import digest
from app.execution_costs.gate import quantify
from test_stage08d_quantification import inputs

ROOT=Path(__file__).resolve().parents[1]
START=300_000
END=4_800_000


def stream(*, change=None, end=END, remove=None):
    out=[]
    for seq,at in enumerate(range(0,end,1000),1):
        if remove and remove(at):continue
        price=D(100) if change is None else D(str(change(at)))
        out.append((at,at+250,price_units(price),seq))
    return out


def feature(events,side='LONG',at=START,stop=None):
    last=next(e for e in reversed(events) if e[1]<=at)
    return dict(kind='SIGNAL',candidate_id='synthetic-'+side,side=side,signal_at_ms=at,
        origin_price_units=last[2],origin_event_time_ms=last[0],origin_available_at_ms=last[1],
        origin_event_id='SOLUSDT:agg:'+str(last[3]),candidate_digest='a'*64,setup_digest='b'*64,
        stop_units=price_units(stop if stop is not None else 99 if side=='LONG' else 101))


def indexed(events,f):
    return ObservationIndex(events,[f],start=START,end=END,warmup=0)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('horizon',HORIZONS)
def test_signed_labels_mirror_all_fixed_windows(side,horizon):
    sign=1 if side=='LONG' else -1
    events=stream(change=lambda t:100+sign*2 if t>=START else 100)
    f=feature(events,side);label=indexed(events,f).label(f,horizon)
    assert label['eligible']
    assert label['observed_return']==dict(usdt_per_sol=D(2),bps=D(200))
    assert label['mfe']['usdt_per_sol']==2 and label['mae']['usdt_per_sol']==0
    assert label['simulated_profit'] is None and label['scope']=='PRICE_LABEL_NOT_TRADE_PNL'


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_first_invalidation_precedes_later_favorable_and_keeps_cash_unknown(side):
    sign=1 if side=='LONG' else -1
    def values(t):
        if t<START:return 100
        if t<START+5000:return 100+sign*D('.5')
        if t<START+30000:return 100-sign*2
        return 100+sign*10
    events=stream(change=values);f=feature(events,side)
    label=indexed(events,f).label(f,900)
    assert label['first_observed_invalidation']['event_time_ms']==START+5000
    assert label['mfe_before_invalidation']['usdt_per_sol']==D('.5')
    assert label['mfe']['usdt_per_sol']==10
    assert label['later_favorable_after_invalidation']
    assert label['simulated_profit'] is None


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_future_mutation_does_not_mutate_features_or_prior_visibility(side):
    base=stream();f=feature(base,side);before=copy.deepcopy(f)
    changed=stream(change=lambda t:105 if t>START+60_000 else 100)
    a=indexed(base,f);b=indexed(changed,f)
    assert f==before and a.bar(START)==b.bar(START)
    assert a.label(f,60)==b.label(f,60)
    assert a.label(f,900)!=b.label(f,900)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_endpoint_inclusive_availability_and_after_endpoint_excluded(side):
    events=stream();deadline=START+60_000
    # Replace one raw event so it is visible exactly at the endpoint.
    pos=next(i for i,e in enumerate(events) if e[0]==deadline-1000)
    events[pos]=(deadline-250,deadline,price_units(103),events[pos][3])
    f=feature(events,side);label=indexed(events,f).label(f,60)
    assert label['endpoint_event_time_ms']==deadline-250
    assert label['observed_return']['usdt_per_sol']==(3 if side=='LONG' else -3)
    later=events.copy();later[pos]=(deadline-249,deadline+1,price_units(103),events[pos][3])
    assert indexed(later,f).label(f,60)['observed_return']['usdt_per_sol']==0


@pytest.mark.parametrize('horizon',HORIZONS)
def test_month_end_exact_or_beyond_is_censored_not_shortened(horizon):
    events=stream();at=END-horizon*1000;f=feature(events,at=at)
    idx=indexed(events,f);label=idx.label(f,horizon)
    assert label['deadline_ms']==END and label['status']=='CENSORED'
    assert not label['eligible'] and label['observed_through_ms']==END
    assert idx.bars[-1].event_at==END-1000  # last 15s is retained for partial descriptions


@pytest.mark.parametrize('gap',[15_000,16_000,60_000])
def test_internal_gaps_not_interpolated(gap):
    events=stream(remove=lambda t:START+10_000<=t<START+10_000+gap)
    f=feature(events);out=indexed(events,f).label(f,900)
    assert 'INSUFFICIENT_COVERAGE' in out['flags'] and not out['eligible']


def test_signal_background_use_identical_label_math():
    events=stream(change=lambda t:100+D(t)/1_000_000)
    f=feature(events,at=900_000);idx=indexed(events,f)
    bg=next(b for b in idx.background() if b['signal_at_ms']==f['signal_at_ms'] and b['side']==f['side'])
    a,b=idx.label(f,900),idx.label(bg,900)
    for key in ('eligible','flags','observed_return','mfe','mae','coverage','past_volatility_bps','volatility_band'):
        assert a[key]==b[key]
    assert b['invalidation_status']=='NOT_APPLICABLE'


def test_labels_use_all_ticks_not_only_last_sample():
    events=stream(change=lambda t:110 if t==START+7000 else 100)
    f=feature(events);out=indexed(events,f).label(f,900)
    assert out['mfe']['usdt_per_sol']==10 and out['observed_return']['usdt_per_sol']==0


def test_unknown_horizon_and_lossy_prices_refused():
    events=stream();f=feature(events)
    with pytest.raises(ValueError,match='UNREGISTERED_HORIZON'):indexed(events,f).label(f,120)
    for bad in ('1.000000001','NaN','Infinity','0','-1'):
        with pytest.raises(ValueError):price_units(bad)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_real_frozen_candidate_extractor_rejects_future_and_content_change(tmp_path,side):
    _,_,args,_=inputs(tmp_path,side);c=args[0]
    f=extract(c);assert f['candidate_id']==c.candidate_id
    before=digest(c);future=[dict(price=999,time=c.setup.created_at+900)]
    future[0]['price']=1
    assert extract(c)==f and digest(c)==before
    bad=c.model_copy(update={'candidate_id':'a'*64})
    with pytest.raises(ValueError,match='CONTENT_CHANGED'):extract(bad)
    raw=c.model_dump();raw['evidence']['window'][-1]['last']['SOLUSDT']['available_at_ms']+=1000
    bad=type(c).model_validate(raw);bad=bad.model_copy(update={'candidate_id':digest(bad.model_copy(update={'candidate_id':'0'*64}))})
    with pytest.raises(ValueError,match='FUTURE_FEATURE_OBSERVATION'):extract(bad)


def stat_row(kind,side,at,value,*,band=1,cid='x',eligible=True):
    return dict(kind=kind,side=side,at=at,r=D(value),band=band,id=cid,eligible=eligible,horizon=900,
        flags=[],distance=D(value)/100,mfe=D(1),mae=D(1),before=D(1),invalid=False,later=False)


def test_stratified_difference_controls_same_direction_and_missing_cells():
    rows=[stat_row('SIGNAL','LONG',0,'8'),stat_row('BACKGROUND','LONG',0,'7'),
          stat_row('SIGNAL','SHORT',0,'-6'),stat_row('BACKGROUND','SHORT',0,'-7'),
          stat_row('SIGNAL','LONG',3_600_000,'999',band=4)]
    out=compare(rows,0,31*86_400_000)
    assert out['stratified_difference_bps']==1 and out['matched_signals']==2
    assert out['unmatched_signals']==1 and out['conclusion']=='INSUFFICIENT_DIRECTION_EVIDENCE'


def test_block_interval_deterministic_not_iid_and_duplicates_do_not_inflate_blocks():
    sums=[D(x) for x in range(31)];counts=[1]*31
    a=block_interval(sums,counts);b=block_interval([s*100 for s in sums],[100]*31)
    assert a==b and not a['iid_samples'] and a['available_blocks']==31
    assert a['repetitions']==2000 and a['seed']==808061


def test_time_blocks_and_auxiliary_windows_never_become_primary():
    rows=[]
    for h in HORIZONS:
        for day in range(31):
            for kind in ('SIGNAL','BACKGROUND'):
                r=stat_row(kind,'LONG',day*86_400_000,2 if kind=='SIGNAL' else 1,cid=str(day));r['horizon']=h;rows.append(r)
    result=summarize(rows,0,31*86_400_000)
    assert result['primary_horizon_seconds']==900
    assert [k for k,v in result['windows'].items() if v['primary']]==['900']
    assert result['independent_trade_count'] is None and result['simulated_profit'] is None


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('q',['.054','1','5.329'])
def test_fee_slippage_funding_same_quantity_and_both_cash_legs(tmp_path,side,q):
    _,_,args,_=inputs(tmp_path,side);s=args[0].setup;q=D(q);fund=q*D('.2')
    before=digest(s)
    with localcontext(Context(prec=50)):
        for slip in GRID:
            out=evaluate(s,q,fund,D(2),spread_bps=D(2),tick=D('.01'),slippage_bps=slip)
            assert out['quantity']==q and out['funding_budget']==fund
            assert out['target_costs']['funding']==out['stop_costs']['funding']==fund
            assert abs(out['net_target']-(out['gross_target']-sum(out['target_costs'].values())))<D('1e-43')
            assert abs(out['net_stop_risk']-(out['gross_stop_risk']+sum(out['stop_costs'].values())))<D('1e-43')
            assert out['necessary_static_condition']==(out['net_target']>=2*out['net_stop_risk'])
            assert out['actual_execution_cost_identified'] is False
        fee=evaluate(s,q,fund,D(2),spread_bps=D(2),tick=D('.01'),fees_only=True)
        assert fee['funding_budget']==0 and fee['entry_combined_bps']==fee['exit_combined_bps']==0
    assert digest(s)==before


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_8e_costs_reused_without_changing_approved_or_diagnostic_quantity(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side);body=quantify(*args,now=now)
    old=cost_row(body,args[1],args[2]);before=copy.deepcopy(body)
    row=research_row(body['candidate'],old,spread_bps=args[2].spread_bps,tick=D('.01'))
    assert row['quantity']==old['metrics']['quantity']
    assert len(row['sensitivities'])==5 and row['sensitivities'][3]['net_rr']==old['metrics']['net_rr']
    assert body==before and row['real_executability']=='UNIDENTIFIED_COST_EVIDENCE'


@pytest.mark.parametrize('flags,expected',[
    ([True,True,False,False,False],'BRACKET_ONLY_WITH_TICK_DISCONTINUITIES'),
    ([False]*5,'NO_TESTED_SLIPPAGE_PASSES'),([True]*5,'ALL_TESTED_SLIPPAGES_PASS_NOT_A_MAXIMUM'),
    ([False,True,False,False,False],'NON_MONOTONE_DISCRETE_OUTCOMES')])
def test_grid_bracket_not_precise_smoothed_threshold(flags,expected):
    out=boundary([dict(per_leg_slippage_bps=x,necessary_static_condition=f) for x,f in zip(GRID,flags)])
    assert out['status']==expected
    if flags==[True,True,False,False,False]:
        assert out['passing_bps']==2 and out['failing_bps']==5 and out['exact_threshold'] is None


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_readonly_store_feature_and_cost_integration(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side);body=quantify(*args,now=now)
    old=cost_row(body,args[1],args[2]);dest=tmp_path/'cost-rows.jsonl'
    from app.scenario_diagnostics.cli import serial
    dest.write_text(json.dumps(serial(old))+'\n')
    with frozen_source(p.store.path) as (db,run_meta,bundle,model):
        before=[db.execute('SELECT payload FROM '+t).fetchall() for t in ('account','broker_orders','reservations','history_candidates')]
        fs,chain=export_features(db,tmp_path/'candidate-features.jsonl')
        assert len(fs)==1 and fs[0]['candidate_id']==body['candidate_id']
        # Stored request body is the same real synthetic path, not mocked arithmetic.
        output=tmp_path/'research-runs'/'synthetic';output.mkdir(parents=True)
        import time
        result,count,_=cost_pass(db,dest,model,output,time.perf_counter())
        assert count==1 and result['sides'][side]['candidates']==1
        with pytest.raises(Exception):db.execute('UPDATE account SET payload=payload')
        assert before==[db.execute('SELECT payload FROM '+t).fetchall() for t in ('account','broker_orders','reservations','history_candidates')]


def test_cli_boundaries_and_no_runtime_import_or_network(tmp_path):
    with pytest.raises(ValueError,match='NEW_RESEARCH_DIRECTORY_REQUIRED'):
        run('missing','missing','missing',tmp_path/'offline-runs'/'bad',code_commit='a'*40)
    code='import sys; import app.signal_research.cli; print([x for x in sys.modules if x in '+repr([
        'app.runtime','app.config','app.paper.broker','app.execution.engine','app.offline_paper.engine',
        'app.offline_paper.broker','app.admitted_paper.engine','app.execution_costs.gate','app.historical_replay.replay',
        'app.alerts.telegram','app.execution.bridge_client'])+'])'
    result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,text=True,capture_output=True)
    assert result.returncode==0 and result.stdout.strip()=='[]'
    for path in (ROOT/'app/signal_research').glob('*.py'):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Call):
                name=node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ''
                assert name not in {'admit_trade','require_paper_admission','quantify','submit','fixture_entry','connect','create_order'}
    for path in (ROOT/'app').rglob('*.py'):
        if path.parent.name!='signal_research':assert 'signal_research' not in path.read_text()
    assert hashlib.sha256((ROOT/'docs/STAGE_08FA_RESEARCH_PROTOCOL.md').read_bytes()).hexdigest()==PROTOCOL_SHA256
    assert 'dry_run: true' in (ROOT/'config.yaml').read_text()
