"""Deterministic TEST DATA, not a market strategy or profitability backtest."""
from decimal import Decimal as D
from app.configuration.compiler import compile_bundle
from app.utils.paths import ModulePaths, read_text_nofollow
from app.offline_paper.storage import get, rows
from app.offline_paper.models import Quote, OfflineError
from .models import AdmittedSettings, MarketInput
from .storage import AdmittedStore
from .engine import AdmittedPaper

NOW=1800000000
INSTANCE='admitted-paper-demo'


def demo_bundle(workspace):
    paths=ModulePaths(workspace)
    files=dict(main_text='examples/admitted-paper/main.yaml',admission_text='admission.yaml',
        exit_text='exit-policy.yaml',manifest_text='examples/admitted-paper/manifest.yaml')
    compiled=compile_bundle(**{k:read_text_nofollow(paths.file(path)) for k,path in files.items()})
    if compiled.bundle is None: raise OfflineError(compiled.model_dump_json())
    return compiled.bundle


def demo_settings():
    return AdmittedSettings(instance_id=INSTANCE,initial_balance='500',initial_time=NOW,
        allow_fixtures=False,limits=dict(max_loss_per_trade_usdt='5',daily_loss_limit_usdt='20',max_trades_per_day=3,
        max_consecutive_losses=2,max_positions=1,max_leverage=5,max_margin_ratio='.2',max_margin_usdt='100',
        max_position_notional_usdt='500',max_position_quantity='100'))


def observations(side,now=NOW,*,reference='155'):
    # Past observations only. Subsequent demo price paths are not input here.
    values=[D('150'),D(reference),D('149'),D('158'),D('160'),D('150'),D('100'),D('90'),D('95'),D('96'),D('97'),D('98'),D('99'),D('100')]
    if side=='SHORT': values=[200-p for p in values]
    sign=1 if side=='LONG' else -1
    return tuple(MarketInput(sequence=i+1,observed_at=now-(len(values)-1-i)*15,
        available_at=now-(len(values)-1-i)*15,sol=str(p),btc=str(10000+sign*i),eth=str(2000+sign*i),volume=str(100+i))
        for i,p in enumerate(values))


def initialize(workspace,run_id):
    store=AdmittedStore.create(workspace,run_id,demo_settings(),demo_bundle(workspace))
    paper=AdmittedPaper(store);paper.recover()
    return paper


def supply(paper,side):
    with paper.store.transaction() as db: now=get(db,'account','account')['now']
    for obs in observations(side,now): paper.ingest(obs)
    paper.tick(Quote(event_id='decision-price',at=now,bid='100',ask='100'))
    return paper.candidate(side)


def latest_state(paper):
    states=paper.summary()['positions']
    if len(states)!=1: raise OfflineError('Demo expects one position')
    return next(iter(states.values()))


def action(paper,kind):
    with paper.store.transaction() as db:
        candidates=[(k,o) for k,o in rows(db,'broker_orders') if o['action']['kind']==kind and o['status']=='ACCEPTED']
    if not candidates: raise OfflineError('No accepted '+kind)
    return candidates[-1][0]


def tick(paper,price,event_id):
    with paper.store.transaction() as db: now=get(db,'account','account')['now']+1
    paper.tick(Quote(event_id=event_id,at=now,bid=str(price),ask=str(price)));paper.pump()


def normal_open(paper,side,*,fault=None):
    candidate=supply(paper,side)
    approval=paper.prepare(candidate.candidate_id,'normal-demo')
    if approval['body']['result']=='REJECT': raise OfflineError('Normal demo rejected: '+str(approval['body']['reason_codes']))
    result=paper.submit(approval['approval_id'],fault=fault)
    if result['entry_action_id'] is None: raise OfflineError(str(result))
    paper.pump(fault=fault)
    entry=result['entry_action_id'];q=D(approval['body']['quantity'])
    half=(q/2/D('.001')).to_integral_value(rounding='ROUND_FLOOR')*D('.001')
    paper.broker.fill(entry,str(half),'first-entry',defer_details=fault=='unnotified-entry',defer_receipt=fault=='unnotified-entry')
    if fault=='unnotified-entry': paper._crash(fault,fault)
    paper.pump(fault=fault)
    paper.broker.fill(entry,str(q-half),'second-entry');paper.pump()
    return result,approval


def refusal(paper,side,reason):
    """Separate normal-entry rejection demonstrations; never initialize inventory."""
    if reason=='evidence':
        for obs in observations(side)[:3]: paper.ingest(obs)
        try: paper.candidate(side)
        except OfflineError as error:
            return dict(origin='ORDINARY_FROM_ZERO_NOT_FIXTURE',result='REJECT',reason_codes=[str(error)],ledger=compact(paper))
        raise OfflineError('Expected insufficient-evidence refusal')
    if reason=='scenario':
        values=[120,132,119,158,160,120,100,90,95,96,97,98,99,100]
        for obs,p in zip(observations(side),values):
            paper.ingest(obs.model_copy(update={'sol':D(p if side=='LONG' else 200-p)}))
        paper.tick(Quote(event_id='refusal-price',at=NOW,bid='100',ask='100'))
        candidate=paper.candidate(side)
    else: candidate=supply(paper,side)
    approval=paper.prepare(candidate.candidate_id,'refusal',risk_budget='6' if reason=='risk' else '5')
    result=paper.submit(approval['approval_id'])
    if result['result']!='REJECT': raise OfflineError('Expected refusal, no execution permitted for this example')
    return dict(origin='ORDINARY_FROM_ZERO_NOT_FIXTURE',result='REJECT',approval_id=approval['approval_id'],
        reason_codes=result['reason_codes'],original_admission=approval['body'].get('input_binding',{}).get('admission',{}).get('result'),
        ledger=compact(paper))


def complete_exit(paper,*,path='runner'):
    s=latest_state(paper);sign=1 if s['side']=='LONG' else -1
    anchor=D(s['frozen_r_anchor_entry']);r=D(s['frozen_initial_r'])
    if path=='gap':
        tick(paper,D(s['original_stop'])-sign*D('5'),'gap')
    else:
        for name,multiple in (('TP1',D('1.1')),('TP2',D('2.1'))):
            tick(paper,anchor+sign*r*multiple,'touch-'+name)
            s=latest_state(paper);q=D(s['tp1_planned' if name=='TP1' else 'tp2_planned'])
            paper.broker.fill(action(paper,name),str(q),'fill-'+name);paper.pump()
        with paper.store.transaction() as db:
            rsv=rows(db,'reservations')[0][1];body=get(db,'paper_approvals',rsv['approval_id'])['body']
        tick(paper,D(body['exit_plan']['runner_reference_price']),'observed-conditional-extreme')
        stop=D(latest_state(paper)['current_stop'])
        tick(paper,stop-sign*D('.01'),'runner-retrace')
    s=latest_state(paper);paper.broker.fill(action(paper,'CLOSE_ALL'),s['remaining_quantity'],'final-exit');paper.pump()
    return latest_state(paper)


def compact(paper):
    data=paper.summary()
    approvals=[r for r in data['approvals'].values() if r['body']['result']!='REJECT']
    return dict(scope=data['scope'],ordinary_positions=data['normal_position_count'],fixture_positions=data['fixture_position_count'],
        allow_fixtures=data['allow_fixtures'],normal_entry_complete=data['normal_entry_complete'],
        live_allowed=False,real_runtime_connected=False,statistical_expectation=None,
        counts=data['counts'],cash=data['account']['cash'],fees_usdt=data['fees_usdt'],
        reserved_risk_usdt=data['risk_snapshot']['reserved_risk_usdt'],reconciliation_clear=data['account']['reconciliation_clear'],
        pending_reconciliation=len(data['pending_reconciliation']),
        positions={k:{n:s[n] for n in ('phase','side','remaining_quantity','remaining_entry_cost','frozen_initial_r',
            'realized_gross_pnl','realized_net_pnl','protection_covered_quantity')} for k,s in data['positions'].items()},
        bindings=[dict(approval_id=a['approval_id'],candidate_id=a['body']['candidate_id'],bundle_digest=a['body']['bundle_digest'],
            exit_plan_id=a['body']['exit_plan']['plan_id'],scenario_id=a['body']['scenarios']['evaluation_id'],
            admission_id=a['body']['input_binding']['admission']['decision_id'],reason_codes=a['body']['reason_codes'],
            scenario_net_rr={s['scenario_id']:s['scenario_net_rr'] for s in a['body']['scenarios']['scenarios']}) for a in approvals])
