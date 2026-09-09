"""Explicit synthetic scenarios. Never promoted to an admitted real plan."""
from decimal import Decimal as D

from app.configuration.compiler import compile_bundle
from app.configuration.examples import example_plan
from app.admission.models import AdmissionRequest
from app.utils.paths import ModulePaths, read_text_nofollow
from .models import RunSettings, FixtureEntry, Quote, OfflineError
from .storage import get, rows

NOW=1800000000


def example_bundle(workspace):
    paths=ModulePaths(workspace)
    files=dict(main_text='examples/offline-paper/main.yaml',admission_text='admission.yaml',
               exit_text='exit-policy.yaml',manifest_text='examples/offline-paper/manifest.yaml')
    result=compile_bundle(**{k:read_text_nofollow(paths.file(v)) for k,v in files.items()})
    if result.bundle is None: raise OfflineError(result.model_dump_json())
    return result.bundle


def example_settings(*,fixtures=False):
    return RunSettings(instance_id='offline-paper-demo',initial_balance='500',initial_time=NOW,
        allow_fixtures=fixtures,leverage=5,limits=dict(max_loss_per_trade_usdt='5',daily_loss_limit_usdt='20',
            max_trades_per_day=3,max_consecutive_losses=2,max_positions=1,max_leverage=5,
            max_margin_ratio='.2',max_margin_usdt='100',max_position_notional_usdt='500',max_position_quantity='100'))


def opening(runner,side='LONG',fixture_id='demo',quantity='.5'):
    with runner.store.transaction() as db:
        a=get(db,'account','account');bundle=runner.store.bundle(db)
        return FixtureEntry(fixture_id=fixture_id,side=side,quantity=quantity,reference_price='100',
            initial_stop='95' if side=='LONG' else '105',risk_budget='5',expires_at=a['now']+60,
            expected_revision=a['version'],bundle_digest=bundle.bundle_digest)


def quote(runner,price,event_id):
    with runner.store.transaction() as db: at=get(db,'account','account')['now']+1
    runner.tick(Quote(event_id=event_id,at=at,bid=str(price),ask=str(price)))


def action(runner,kind,*,active=True):
    with runner.store.transaction() as db:
        candidates=[(key,o) for key,o in rows(db,'broker_orders') if o['action']['kind']==kind and
                    (not active or o['status']=='ACCEPTED')]
    if not candidates: raise OfflineError('No '+kind+' order')
    return candidates[-1][0]


def cancel_entry(runner,entry_id):
    """Explicit fixture operator event, durable intent before cancellation."""
    from .storage import put, digest
    with runner.store.transaction() as db:
        if not runner.store.settings(db).allow_fixtures: raise OfflineError('Fixture instance required')
        order=get(db,'broker_orders',entry_id);pid=order['action']['position_id']
        key=digest({'fixture-cancel-entry':entry_id})
        if get(db,'outbox',key) is None:
            put(db,'outbox',key,dict(origin='FIXTURE_EXISTING_POSITION',status='PENDING',attempts=0,
                action=dict(action_id=key,kind='CANCEL',position_id=pid,target_action_id=entry_id)))
    return key


def run_scenario(runner,name,side='LONG'):
    if name=='admission-rejection':
        with runner.store.transaction() as db:
            a=get(db,'account','account');bundle=runner.store.bundle(db)
        setup=example_plan('allocation-mismatch',side).setup
        result=runner.request_entry('synthetic-plan',setup,AdmissionRequest(request_id='synthetic-plan'),expected_revision=a['version'],
                                    bundle_digest=bundle.bundle_digest,at=a['now'])
        return {'scenario':name,'origin':'A_NORMAL_ADMISSION','result':result,'summary':runner.summary()}
    # Scenario runs are one-shot; resume uses the durable state, not this script.
    quote(runner,'100','open-price')
    request=opening(runner,side);entry=runner.fixture_entry(request);runner.pump()
    runner.broker.fill(entry,'.2','entry-first');runner.pump()
    first=runner.summary()
    if name=='partial-cancel':
        cancel_entry(runner,entry);runner.pump()
    else:
        runner.broker.fill(entry,'.3','entry-rest');runner.pump()
    if name in ('tp-runner','stop-gap'):
        if name=='stop-gap':
            quote(runner,'90' if side=='LONG' else '110','gap');runner.pump()
            runner.broker.fill(action(runner,'CLOSE_ALL'),'.5','gap-exit');runner.pump()
        else:
            with runner.store.transaction() as db:
                s=rows(db,'positions')[0][1]['checkpoint']['state']
            entry_price=D(s['frozen_r_anchor_entry']);r=D(s['frozen_initial_r']);sign=1 if side=='LONG' else -1
            for multiple,kind,q in [(D('1.1'),'TP1','.15'),(D('2.1'),'TP2','.2')]:
                quote(runner,entry_price+sign*r*multiple,'price-'+kind);runner.pump()
                runner.broker.fill(action(runner,kind),q,'fill-'+kind);runner.pump()
            quote(runner,entry_price+sign*r*4,'runner-high');runner.pump()
            quote(runner,entry_price+sign*r*D('2.5'),'runner-back');runner.pump()
            runner.broker.fill(action(runner,'CLOSE_ALL'),'.15','runner-exit');runner.pump()
    elif name=='unknown-reconcile':
        with runner.store.transaction() as db:
            s=rows(db,'positions')[0][1]['checkpoint']['state'];stop=s['protection_action_id'];pid=s['position_id']
        runner.broker.fixture_delivery('lost',dict(kind='PROTECTION_LOST',position_id=pid,action_id=stop,
            status='UNKNOWN',cumulative_filled_quantity=None))
        runner.pump()
    elif name not in ('partial-cover','partial-cancel'):
        raise OfflineError('Unsupported scenario')
    return {'scenario':name,'origin':'B_FIXTURE_EXISTING_POSITION','first_partial':first,'summary':runner.summary()}
