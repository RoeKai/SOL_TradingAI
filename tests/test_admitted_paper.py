"""8B ordinary entry tests; no approval mocks, no fixture entry or seeded positions."""
from decimal import Decimal as D
from pathlib import Path
import shutil
import pytest
from app.admitted_paper.demo import initialize, normal_open, complete_exit, latest_state, supply
from app.admitted_paper.engine import AdmittedPaper
from app.admitted_paper.storage import AdmittedStore
from app.admitted_paper.demo import action, tick, observations, NOW
from app.offline_paper.models import OfflineError, Quote
from app.offline_paper.storage import get, put, rows, digest
from app.admitted_paper.models import Candidate, ExitPlan, MarketInput
from app.admitted_paper.plans import validate_exit_plan, build_exit_plan
from app.admitted_paper.scenarios import evaluate_scenarios, PathExperiment
from app.configuration.inputs import PlanInputs
from app.exits.bindings import capture_plan

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def paper(tmp_path):
    for name in ('isolation-policy.json','admission.yaml','exit-policy.yaml','examples/admitted-paper/main.yaml','examples/admitted-paper/manifest.yaml'):
        dest=tmp_path/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,dest)
    return initialize(tmp_path,'normal-test')


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_normal_preparation(paper,side):
    candidate=supply(paper,side)
    result=paper.prepare(candidate.candidate_id,'normal')
    assert result['body']['result'] in ('APPROVE','REDUCE'), result['body']
    assert not paper.summary()['positions']


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('path',['runner','gap'])
def test_normal_zero_to_exit_recovery(paper,side,path):
    with paper.store.transaction() as db: assert paper.store.settings(db).allow_fixtures is False
    assert not paper.summary()['orders'] and not paper.summary()['positions']
    result,approval=normal_open(paper,side)
    assert D(latest_state(paper)['original_quantity'])==D(approval['body']['quantity'])
    state=complete_exit(paper,path=path)
    assert state['phase']=='CLOSED' and D(state['remaining_quantity'])==0
    data=paper.summary()
    assert data['normal_position_count']==1 and data['fixture_position_count']==0
    fs=list(data['fills'].values());entries=sum((D(f['quantity'])*D(f['price']) for f in fs if f['kind']=='ENTRY_FILL'),D(0))
    exits=sum((D(f['quantity'])*D(f['price']) for f in fs if f['kind']=='EXIT_FILL'),D(0))
    assert D(state['realized_gross_pnl'])==(1 if side=='LONG' else -1)*(exits-entries)
    assert D(state['realized_net_pnl'])==D(state['realized_gross_pnl'])-D(data['fees_usdt'])
    assert D(data['account']['cash'])==500+D(state['realized_net_pnl'])
    resumed=AdmittedPaper(AdmittedStore(paper.store.paths.root,'normal-test','admitted-paper-demo'));resumed.recover()
    after=resumed.summary()
    assert after['counts']==data['counts'] and after['fees_usdt']==data['fees_usdt']
    assert after['account']['cash']==data['account']['cash']


def accepted(paper,side):
    candidate=supply(paper,side);approval=paper.prepare(candidate.candidate_id,'entry')
    assert approval['body']['result']!='REJECT',approval['body']
    result=paper.submit(approval['approval_id']);assert result['entry_action_id']
    paper.pump()
    return result['entry_action_id'],approval


def reopen(paper):
    result=AdmittedPaper(AdmittedStore(paper.store.paths.root,'normal-test','admitted-paper-demo'))
    result.recover();return result


def assert_conservation(paper):
    data=paper.summary()
    for pid,s in data['positions'].items():
        fs=[f for f in data['fills'].values() if f['position_id']==pid]
        incoming=sum((D(f['quantity'])*D(f['price']) for f in fs if f['kind']=='ENTRY_FILL'),D(0))
        outgoing=sum((D(f['quantity'])*D(f['price']) for f in fs if f['kind']=='EXIT_FILL'),D(0))
        fees=sum((D(f['fee_usdt']) for f in fs),D(0))
        assert D(s['realized_gross_pnl'])==(1 if s['side']=='LONG' else -1)*(outgoing-incoming+D(s['remaining_entry_cost']))
        assert D(s['realized_net_pnl'])==D(s['realized_gross_pnl'])-fees
    assert D(data['account']['cash'])==500+sum((D(s['realized_net_pnl']) for s in data['positions'].values()),D(0))


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_original_static_half_half_is_not_actual_plan_and_runner_not_certain(paper,side):
    candidate=supply(paper,side);r=paper.prepare(candidate.candidate_id,'separate')['body']
    assert r['result']!='REJECT'
    assert [t.fraction for t in candidate.setup.targets]==[.5,.5]
    assert [D(t['original_fraction']) for t in r['exit_plan']['triggers']]==[D('.3'),D('.4')]
    assert r['exit_plan']['runner_fraction']=='0.3'
    assert any(i['reason_code']=='EXIT_ALLOCATION_MISMATCH' for i in r['legacy_validation']['issues'])
    assert any(i['reason_code']=='RUNNER_FULL_POLICY_VALUATION_UNSUPPORTED' for i in r['legacy_validation']['issues'])
    assert r['legacy_validation']['plan_consistency']!='PASS'
    assert r['scenarios']['full_policy_expected_return'] is r['scenarios']['win_probability'] is None
    ss={s['scenario_id']:s for s in r['scenarios']['scenarios']}
    assert D(ss['S0']['scenario_net_rr'])==-1
    assert D(ss['S3']['scenario_net_rr'])>=D(r['input_binding']['admission']['required_net_rr'])
    s3=ss['S3'];entry=s3['legs'][0];final=s3['legs'][-1]
    sign=1 if side=='LONG' else -1
    extreme=D(s3['reference_structure_price']);stop=D(s3['final_stop'])
    assert (extreme-stop)*sign>=D(s3['frozen_initial_r'])-D('.01')
    assert (extreme-D(final['fill_price']))*sign>D(s3['frozen_initial_r'])
    assert all(D(s['initial_stop_risk_usdt'])==D(r['scenarios']['conservative_initial_risk_usdt']) for s in ss.values())
    assert sum(D(l['quantity']) for l in s3['legs'] if l['action']=='TP1')==D(r['quantity'])*D('.3')//D('.001')*D('.001')


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_scenario_partial_confirmation_not_touch_moves_stop(paper,side):
    c=supply(paper,side);b=paper.prepare(c.candidate_id,'path')['body']
    inputs=PlanInputs.model_validate(b['input_binding']);plan=ExitPlan.model_validate(b['exit_plan'])
    experiment=PathExperiment(plan,capture_plan(inputs.setup,inputs.rr,inputs.scorecard,inputs.admission),D(b['quantity']),plan.reference_entry)
    sign=1 if side=='LONG' else -1
    experiment.tick(experiment.state.frozen_r_anchor_entry+sign*experiment.state.frozen_initial_r)
    assert not experiment.state.tp1_complete and experiment.state.current_stop==plan.initial_stop
    experiment.fill('TP1',partial=True)
    assert experiment.state.tp1_complete and (experiment.state.current_stop-plan.initial_stop)*sign>0


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['missing-reference','insufficient-s3'])
def test_static_eligible_but_new_scenario_rejects(paper,side,mode):
    values=observations(side)
    mapping=([120,125,119,126,128,120,100,90,95,96,97,98,99,100] if mode=='missing-reference'
             else [120,132,119,158,160,120,100,90,95,96,97,98,99,100])
    for obs,p in zip(values,mapping): paper.ingest(obs.model_copy(update={'sol':D(p if side=='LONG' else 200-p)}))
    paper.tick(Quote(event_id='price',at=NOW,bid='100',ask='100'));candidate=paper.candidate(side)
    body=paper.prepare(candidate.candidate_id,'reject')['body']
    assert body['input_binding']['admission']['result']!='REJECT',body
    assert body['result']=='REJECT'
    assert ('S3_UNAVAILABLE' if mode=='missing-reference' else 'S3_NET_RR_BELOW_ORIGINAL_CONTEXT_FLOOR') in body['reason_codes']
    result=paper.submit(digest({'issuer':'paper-scenario-admission/v1','body':body}))
    assert result['entry_action_id'] is None and not paper.summary()['orders']


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('field',['stop','target','fraction','cost','policy','side','rules','funding'])
def test_changed_plan_cannot_reuse_same_identity_or_approval(paper,side,field):
    c=supply(paper,side)
    with paper.store.transaction() as db: bundle=paper.store.bundle(db)
    plan=build_exit_plan(c,bundle);raw=plan.model_dump(mode='json')
    if field=='stop': raw['initial_stop']='89' if side=='LONG' else '111'
    elif field=='target': raw['runner_reference_price']='180' if side=='LONG' else '20'
    elif field=='fraction': raw['triggers'][0]['original_fraction']='0.5'
    elif field=='cost': raw['costs'][0][1]='0'
    elif field=='policy': raw['policy']['runner_trail_r']='0.1'
    elif field=='side': raw['side']='SHORT' if side=='LONG' else 'LONG'
    elif field=='rules': raw['rules']['quantity_step']='.1'
    else: raw['funding_model']='BINANCE_REAL_ZERO'
    with pytest.raises(ValueError): validate_exit_plan(ExitPlan.model_validate(raw),c,bundle)
    assert not paper.summary()['orders']


@pytest.mark.parametrize('cost',['entry_fee_rate','exit_fee_rate','entry_slippage_bps','exit_slippage_bps','funding_cost_usdt','assumed_holding_seconds','source'])
def test_missing_costs_never_filled_by_synthetic_default(paper,cost):
    c=supply(paper,'LONG');raw=c.model_dump(mode='json');raw['setup']['cost_assumptions'][cost]=None
    with paper.store.transaction() as db: bundle=paper.store.bundle(db)
    with pytest.raises(OfflineError): build_exit_plan(Candidate.model_validate(raw),bundle)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_provider_no_future_or_external_self_certification(paper,side):
    valid=observations(side)[0]
    with pytest.raises(ValueError): MarketInput.model_validate(dict(valid.model_dump(),verified=True,source='trusted'))
    with pytest.raises(OfflineError,match='FUTURE'): paper.ingest(valid.model_copy(update={'available_at':NOW+1}))
    with pytest.raises(OfflineError,match='INSUFFICIENT'): paper.candidate(side)
    missing=paper.prepare('0'*64,'unknown')['body']
    assert missing['result']=='REJECT' and not paper.summary()['orders']
    c=supply(paper,side)
    with paper.store.transaction() as db:
        row=get(db,'paper_candidates',c.candidate_id);row['setup']['confidence']['value']=.99
        put(db,'paper_candidates',c.candidate_id,row)
    result=paper.prepare(c.candidate_id,'tampered')
    assert result['body']['result']=='REJECT' and 'CANDIDATE_OR_EVIDENCE_CHANGED' in result['body']['reason_codes']


def test_direction_score_is_computed_not_probability_or_default_safe(paper):
    obs=observations('LONG')
    for p in obs: paper.ingest(p.model_copy(update={'btc':D(10000),'eth':D(2000)}))
    paper.tick(Quote(event_id='price',at=NOW,bid='100',ask='100'))
    c=paper.candidate('LONG')
    assert c.setup.confidence.value==.6
    assert sum(ok for _,ok in c.evidence.direction_checks)==3
    assert c.setup.confidence.interpretation=='evidence_confidence_not_win_probability'


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('change',['expired','quote','revision','pause','config'])
def test_lock_recheck_rejects_stale_approval(paper,side,change):
    c=supply(paper,side);a=paper.prepare(c.candidate_id,'stale')
    with paper.store.transaction() as db:
        account=get(db,'account','account')
        if change=='expired': account['now']+=5
        elif change=='quote': account['quote']['ask']='101'
        elif change=='revision': account['version']+=1
        elif change=='pause': account['paused']=True
        else:
            identity=get(db,'identity','identity');identity['bundle_digest']='1'*64;put(db,'identity','identity',identity)
        put(db,'account','account',account)
    result=paper.submit(a['approval_id'])
    assert result['result']=='REJECT' and result['reason_codes']
    with paper.store.transaction() as db:
        assert not rows(db,'reservations') and not rows(db,'outbox') and not rows(db,'paper_consumptions')


@pytest.mark.parametrize('risk',['0','.1','6','1000000'])
def test_risk_failure_no_orders_or_consumption(paper,risk):
    c=supply(paper,'LONG');approval=paper.prepare(c.candidate_id,'risk',risk_budget=risk)
    assert approval['body']['result']=='REJECT'
    assert paper.submit(approval['approval_id'])['entry_action_id'] is None
    assert not paper.summary()['orders'] and paper.summary()['consumption_count']==0


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_persisted_request_once_id_collision_and_bare_objects_rejected(paper,side):
    c=supply(paper,side);a=paper.prepare(c.candidate_id,'one')
    assert paper.prepare(c.candidate_id,'one')==a
    with pytest.raises(OfflineError,match='CONTENT_CONFLICT'): paper.prepare(c.candidate_id,'one',risk_budget='4')
    for obj in (c.setup,{},a):
        with pytest.raises(OfflineError): paper.submit(obj)
    result=paper.submit(a['approval_id']);assert paper.submit(a['approval_id'])==result
    paper.pump();paper.pump()
    assert len(paper.summary()['orders'])==1 and paper.summary()['consumption_count']==1
    assert not paper.summary()['positions']  # accepted != filled


@pytest.mark.parametrize('forgery',['label-only','no-consumption','wrong-reservation','wrong-quantity'])
def test_broker_no_origin_or_hash_based_bypass(paper,forgery):
    c=supply(paper,'LONG');a=paper.prepare(c.candidate_id,'forge')
    result=paper.submit(a['approval_id']);entry=result['entry_action_id']
    with paper.store.transaction() as db:
        if forgery=='no-consumption': db.execute('DELETE FROM paper_consumptions')
        elif forgery=='wrong-reservation':
            r=get(db,'reservations',result['position_id']);r['risk']='0';put(db,'reservations',result['position_id'],r)
        else:
            item=get(db,'outbox',entry)
            if forgery=='label-only': item['action']['position_id']='unreserved'
            else: item['action']['quantity']='.9'
            put(db,'outbox',entry,item)
    with pytest.raises(OfflineError): paper.broker.execute(entry)
    with paper.store.transaction() as db: assert not rows(db,'broker_orders')


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('prior',['0','.1'])
@pytest.mark.parametrize('rest',['partial','full'])
def test_missing_first_or_subsequent_entry_facts_recovered_once(paper,side,prior,rest):
    entry,a=accepted(paper,side);total=D(a['body']['quantity'])
    if D(prior): paper.broker.fill(entry,prior,'delivered');paper.pump()
    quantity=total-D(prior) if rest=='full' else D('.1')
    paper.broker.fill(entry,str(quantity),'not-notified',defer_details=True,defer_receipt=True)
    r=reopen(paper);one=r.summary();state=latest_state(r)
    assert D(state['remaining_quantity'])==D(prior)+quantity
    assert D(state['protection_covered_quantity'])==D(state['remaining_quantity'])
    assert r.ready
    two=reopen(r).summary()
    assert one['fills']==two['fills'] and one['fees_usdt']==two['fees_usdt'] and one['account']['cash']==two['account']['cash']
    assert len([o for o in two['orders'].values() if o['action']['kind']=='ENTRY'])==1
    assert_conservation(r)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_partial_entry_cancel_then_protection_and_actual_deviation(paper,side):
    entry,a=accepted(paper,side)
    paper.broker.fill(entry,'.1','partial');paper.pump()
    initial=latest_state(paper)
    tick(paper,'101' if side=='LONG' else '99','changed-after-acceptance')
    paper.broker.fill(entry,'.1','adverse-accepted-fill');paper.pump()
    s=latest_state(paper)
    assert s['frozen_initial_r']==initial['frozen_initial_r'] and D(s['remaining_quantity'])==D('.2')
    assert s['entry_sealed'] and paper.summary()['account']['paused']
    assert paper.summary()['orders'][entry]['status']=='CANCELED'
    assert D(s['protection_covered_quantity'])==D('.2')
    assert_conservation(paper)
    complete_exit(paper,path='gap');assert latest_state(paper)['phase']=='CLOSED';assert_conservation(paper)


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('known',[None,'0','.1'])
def test_protectionlost_highwater_keeps_unknown_zero_distinction(paper,side,known):
    normal_open(paper,side);s=latest_state(paper);stop=s['protection_action_id']
    paper.broker.fault_receipt('lost',stop,'UNKNOWN',known,protection_lost=True)
    paper.deliver('8b-transport-fault:lost')
    paper.broker.fault_receipt('old',stop,'ACCEPTED','0');paper.deliver('8b-transport-fault:old')
    current=next(a for a in latest_state(paper)['actions'] if a['action_id']==stop)
    if known=='.1':
        assert D(current['acknowledged_quantity'])==D('.1')
        assert not any(a['target_confirmed'] for a in latest_state(paper)['actions'] if a['kind']=='RECONCILE')
    assert D(latest_state(paper)['remaining_quantity'])==D(s['remaining_quantity'])
    assert_conservation(paper)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_entry_terminal_ack_conflict_and_late_details_never_fabricate_fill(paper,side):
    entry,a=accepted(paper,side)
    paper.cancel_opening(entry);paper.pump()
    r=next(iter(paper.summary()['reservations'].values()));assert r['released'] and r['entry_sealed']
    paper.broker.fault_receipt('late-entry',entry,'ACCEPTED','.2');paper.deliver('8b-transport-fault:late-entry')
    r=next(iter(paper.summary()['reservations'].values()))
    assert r['entry_high_water']=='0.2' and r['entry_terminal_quantity']=='0'
    assert r['entry_faults'] and not r['released'] and r['entry_reconciliation_required']
    assert paper.summary()['account']['quarantined'] and not paper.summary()['fills']
    r2=reopen(paper)
    assert not r2.ready and not r2.summary()['fills']


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_tp_stop_race_late_details_conservation(paper,side):
    normal_open(paper,side);s=latest_state(paper);stop=s['protection_action_id']
    tick(paper,'112' if side=='LONG' else '88','tp-touch');tp=action(paper,'TP1')
    paper.broker.fill(tp,'.02','late-tp',defer_details=True,defer_receipt=True)
    paper.tick(Quote(event_id='gap-race',at=NOW+2,bid='80' if side=='LONG' else '120',ask='80' if side=='LONG' else '120'))
    paper.broker.fill(stop,s['remaining_quantity'],'stop-at-venue',defer_details=True)
    paper.pump();r=reopen(paper)
    assert latest_state(r)['phase']=='CLOSED'
    assert D(latest_state(r)['remaining_quantity'])==0
    assert_conservation(r)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_s0_worst_entry_interval_uses_same_whole_quantity_and_is_recomputed(paper,side):
    c=supply(paper,side);body=paper.prepare(c.candidate_id,'range-component')['body']
    inputs=PlanInputs.model_validate(body['input_binding'])
    raw=c.model_dump(mode='json');raw['setup']['entry']['lower_price']=99.;raw['setup']['entry']['upper_price']=101.
    wider=Candidate.model_validate(raw)
    with paper.store.transaction() as db: bundle=paper.store.bundle(db)
    plan=build_exit_plan(wider,bundle)
    snap=capture_plan(inputs.setup,inputs.rr,inputs.scorecard,inputs.admission)
    evaluation=evaluate_scenarios(plan,wider.setup,snap,D(body['quantity']))
    s0=evaluation.scenarios[0]
    assert s0.entry_quote==D('101' if side=='LONG' else '99')
    assert evaluation.conservative_initial_risk_usdt>D(body['scenarios']['conservative_initial_risk_usdt'])
    assert all(s.quantity==D(body['quantity']) for s in evaluation.scenarios)
    assert evaluate_scenarios(plan,wider.setup,snap,D(body['quantity']))==evaluation
    # This is a PURE scenario test, not an alternative way to register a plan.
    assert paper.prepare(wider.candidate_id,'range-forged')['body']['candidate']['setup']['entry']['lower_price']==100.


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['crossed-stop','at-stop'])
def test_first_adverse_fill_uses_already_available_quote_for_emergency_exit(paper,side,mode):
    entry,a=accepted(paper,side)
    price=('80' if side=='LONG' else '120') if mode=='crossed-stop' else ('89.91' if side=='LONG' else '110.12')
    tick(paper,price,'before-first-fill')
    paper.broker.fill(entry,'.1','first-adverse-fill');paper.pump()
    # Do not wait for another market tick when a valid quote is already stored.
    close=action(paper,'CLOSE_ALL')
    assert latest_state(paper)['emergency_reason']=='FILL_ALREADY_BEYOND_INITIAL_STOP'
    if mode=='at-stop': assert D(latest_state(paper)['frozen_initial_r'])==0
    paper.broker.fill(close,'.1','emergency-exit');paper.pump()
    assert latest_state(paper)['phase']=='CLOSED'
    assert paper.summary()['orders'][entry]['status']=='CANCELED'
    assert_conservation(paper)
