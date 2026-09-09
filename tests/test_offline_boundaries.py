"""No normal-entry bypass, fixed configuration binding and offline capability fence."""
import ast
import builtins
from decimal import Decimal as D
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from app.admission.models import AdmissionRequest
from app.configuration.examples import example_plan
from app.offline_paper.models import OfflineError, Quote, RunSettings
from app.offline_paper.fixtures import opening, quote, action, run_scenario, example_settings, example_bundle
from app.offline_paper.storage import Store, get, put, rows
from app.offline_paper.engine import OfflinePaper
from app.offline_paper.broker import synthetic_rules
from app.offline_paper.review import review, markdown
from test_offline_paper import paper, entered, state, conservation, ROOT


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_ordinary_rejection_binding_repeats_and_no_external_safety_claims(paper,side):
    setup=example_plan('runner-unmodeled',side).setup
    request=AdmissionRequest(request_id='request')
    with paper.store.transaction() as db:
        account=get(db,'account','account');bundle=paper.store.bundle(db)
    args=dict(expected_revision=account['version'],bundle_digest=bundle.bundle_digest,at=account['now'])
    result=paper.request_entry('ordinary',setup,request,**args)
    assert result==paper.request_entry('ordinary',setup,request,**args)
    assert result['result']=='REJECT'
    assert result['validation']['runtime_trust']=='INCOMPLETE'
    assert result['validation']['plan_consistency']!='PASS'
    assert result['validation']['execution_authority']=='none'
    with pytest.raises(OfflineError): paper.request_entry('external',setup.model_dump(),request,**args)
    with pytest.raises(TypeError): paper.request_entry('external',setup,request,account={'verified':True,'equity_usdt':'9999'},**args)
    changed=setup.model_copy(update={'plan_version':'same-signal-altered'})
    with pytest.raises(OfflineError,match='Signal ID'): paper.request_entry('ordinary',changed,request,**args)
    assert not paper.summary()['orders'] and not paper.summary()['reservations']


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('changed',['expiry','config','revision'])
def test_normal_locked_binding_failure_is_explicit(paper,side,changed):
    setup=example_plan('allocation-mismatch',side).setup;request=AdmissionRequest(request_id='request')
    with paper.store.transaction() as db:
        a=get(db,'account','account');b=paper.store.bundle(db)
    args=dict(at=a['now'],expected_revision=a['version'],bundle_digest=b.bundle_digest)
    if changed=='expiry':args['at']=int(setup.valid_until)+1
    if changed=='config':args['bundle_digest']='0'*64
    if changed=='revision':args['expected_revision']+=1
    result=paper.request_entry('binding',setup,request,**args)
    expected={'expiry':'PLAN_EXPIRED_OR_MISSING_VALIDITY','config':'CONFIGURATION_BINDING_CHANGED','revision':'ACCOUNT_VERSION_CHANGED'}[changed]
    assert expected in result['reason_codes'] and result['result']=='REJECT'
    assert not paper.summary()['orders']


@pytest.mark.parametrize('mutation,reason',[
    ({'expected_revision':999},'ACCOUNT_VERSION_CHANGED'),({'bundle_digest':'0'*64},'CONFIGURATION_BINDING_CHANGED'),
    ({'expires_at':1800000000},'REQUEST_EXPIRED'),({'risk_budget':'6'},'RISK_BUDGET_EXHAUSTED'),
    ({'risk_budget':'.01'},'FIXTURE_RISK_REQUEST_UNDERFUNDED'),({'quantity':'100'},'FIXTURE_RISK_REQUEST_UNDERFUNDED')])
def test_fixture_recheck_never_reserves_failed_request(paper,mutation,reason):
    original=opening(paper)
    req=type(original).model_validate({**original.model_dump(),**mutation})
    with pytest.raises(OfflineError,match=reason):paper.fixture_entry(req)
    assert not paper.summary()['reservations'] and not paper.summary()['orders']


@pytest.mark.parametrize('setting,value',[
    ('live_allowed',True),('live_allowed',0),('mode','live'),('application','old-paper'),
    ('schema_version',True),('schema_version',2),('initial_balance','NaN'),('initial_balance','Infinity'),
    ('initial_time',False),('allow_fixtures','true'),('leverage',6)])
def test_strict_settings_never_enable_live_or_implicit_fixtures(setting,value):
    data=example_settings().model_dump();data[setting]=value
    with pytest.raises(ValueError):RunSettings.model_validate(data)


@pytest.mark.parametrize('setting,value',[
    ('max_loss_per_trade_usdt','6'),('daily_loss_limit_usdt','21'),('max_positions',2),
    ('max_leverage',6),('max_margin_ratio','.21'),('max_trades_per_day',4),('max_consecutive_losses',3)])
def test_account_caps_cannot_weaken_frozen_configuration(paper,setting,value):
    data=example_settings().model_dump();data['limits'][setting]=value
    with pytest.raises(OfflineError,match='exceeds frozen'):
        Store.create(paper.store.paths.root,'bad-limits',RunSettings.model_validate(data),example_bundle(paper.store.paths.root))


def test_fixture_opt_in_required_and_init_balance_conflict(paper):
    store=Store.create(paper.store.paths.root,'no-fixtures',example_settings(),example_bundle(paper.store.paths.root))
    runner=OfflinePaper(store);runner.recover()
    with pytest.raises(OfflineError,match='fixture instance'):runner.fixture_entry(opening(runner))
    assert not runner.summary()['reservations']
    bad=example_settings().model_copy(update={'initial_balance':D('999')})
    with pytest.raises(OfflineError,match='balance'):Store.create(paper.store.paths.root,'bad-balance',bad,example_bundle(paper.store.paths.root))


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_missing_quote_staleness_clock_regression_and_candle_never_fills(paper,side):
    entered(paper,side);before=paper.summary();stop=state(paper)['protection_action_id']
    with paper.store.transaction() as db:now=get(db,'account','account')['now']
    with pytest.raises(OfflineError):paper.tick({'open':80,'high':120,'low':60,'close':110})
    with pytest.raises(OfflineError):paper.tick(Quote(event_id='past',at=now-1,bid=100,ask=100))
    paper.tick(Quote(event_id='missing',at=now+1))
    with pytest.raises(OfflineError,match='NO_VALID_QUOTE'):paper.broker.fill(stop,'.5','no-market')
    assert paper.summary()['risk_snapshot']['status']=='incomplete'
    assert paper.summary()['fills']==before['fills']
    with pytest.raises(ValueError):paper.tick(Quote(event_id='crossed',at=now+2,bid=101,ask=100))


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_pending_expiry_rechecked_at_broker_without_fill_and_released_safely(paper,side):
    req=opening(paper,side);entry=paper.fixture_entry(req)
    paper.tick(Quote(event_id='expired',at=req.expires_at,bid=100,ask=100));paper.pump()
    assert paper.summary()['orders'][entry]['status']=='REJECTED'
    assert D(paper.summary()['risk_snapshot']['reserved_risk_usdt'])==0
    assert not paper.summary()['fills']


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_precision_rounding_residual_complete_close(paper,side):
    quote(paper,'100','first');entry=paper.fixture_entry(opening(paper,side,quantity='.501'));paper.pump()
    paper.broker.fill(entry,'.501','entry');paper.pump();s=state(paper)
    assert D(s['tp1_planned'])+D(s['tp2_planned'])+D(s['runner_planned'])==D('.501')
    quote(paper,'90' if side=='LONG' else '110','stop');paper.pump()
    close=action(paper,'CLOSE_ALL');paper.broker.fill(close,'.501','exit');paper.pump()
    assert D(state(paper)['remaining_quantity'])==0;conservation(paper)
    assert synthetic_rules().dynamic_full_position_stop is False


def test_independent_imports_no_old_runtime_and_no_credentials(paper,monkeypatch):
    def denied(*args,**kwargs):raise AssertionError('No socket/credential/clock access')
    monkeypatch.setattr(socket,'socket',denied)
    def guarded_env(key,*args):
        # Pydantic checks its plugin switch during JSON-schema generation.
        # It gets a fixed disabled value, not the host environment or credentials.
        if key=='PYDANTIC_DISABLE_PLUGINS':return '__all__'
        return denied(key,*args)
    monkeypatch.setattr(os,'getenv',guarded_env)
    original=builtins.open
    def guarded(file,*args,**kwargs):
        assert '.env' not in str(file)
        return original(file,*args,**kwargs)
    monkeypatch.setattr(builtins,'open',guarded)
    run_scenario(paper,'tp-runner','LONG')
    assert review(paper.summary())['strategy_efficacy']=='NOT_EVALUATED'
    assert 'Stage 8A' in markdown(paper.summary())
    for path in (ROOT/'app/offline_paper').glob('*.py'):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            names=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
            assert not any(n.startswith(('app.runtime','app.execution','app.paper','app.config.','httpx','websocket','socket','dotenv','app.alerts')) for n in names)
    for name in ('main.py','app/runtime.py','app/paper/broker.py'):
        assert 'offline_paper' not in (ROOT/name).read_text()


def test_original_collision_test_exact_hash_preserved():
    path=ROOT/'tests/test_stage07_semantic_key_collisions.py'
    assert path.stat().st_size==4770
    assert hashlib.sha256(path.read_bytes()).hexdigest()=='757ec0b46741fd22f51c5e181a431bf9196943ce6040ed311c3ce11b9f27c8e3'


def test_new_cli_refuses_semantic_key_collision_before_database_creation(paper):
    template=paper.store.paths.root/'examples/offline-paper/main.yaml'
    template.write_text(template.read_text()+'\nrisk.max_loss_per_trade: 4\n')
    result=subprocess.run([sys.executable,'-m','app.offline_paper.cli','init','--run','collision-test',
        '--workspace',str(paper.store.paths.root)],cwd=ROOT,text=True,capture_output=True,timeout=20)
    assert result.returncode==2
    assert 'LITERAL_DOTTED_KEY_FORBIDDEN' in result.stdout
    assert not (paper.store.paths.root/'offline-runs/collision-test').exists()


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_synthetic_prices_quantities_and_costs_use_declared_rules(paper,side):
    run_scenario(paper,'tp-runner',side)
    rules=synthetic_rules()
    assert rules.exact_close_remainder is False
    assert rules.reduce_only_min_notional_exempt is True
    for f in paper.summary()['fills'].values():
        assert D(f['price'])%rules.price_tick==0
        assert D(f['quantity'])%rules.quantity_step==0
        assert D(f['fee_usdt'])==D(f['quantity'])*D(f['price'])*D('.0005')
    conservation(paper)


def test_unfunded_off_grid_and_minimum_entry_never_get_broker_order(paper):
    for quantity in ('.0001','.001','.5001'):
        req=opening(paper,quantity=quantity)
        with pytest.raises(OfflineError,match='QUANTITY_OR_NOTIONAL'):paper.fixture_entry(req)
    assert not paper.summary()['reservations'] and not paper.summary()['orders']


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_same_synthetic_events_and_frozen_config_replay_deterministically(paper,side):
    first=run_scenario(paper,'tp-runner',side)
    other=OfflinePaper(Store.create(paper.store.paths.root,'replay-run',example_settings(fixtures=True),example_bundle(paper.store.paths.root)))
    other.recover();second=run_scenario(other,'tp-runner',side)
    assert first==second


def test_broker_cannot_use_fixture_label_in_nonfixture_instance(paper):
    store=Store.create(paper.store.paths.root,'default-only',example_settings(),example_bundle(paper.store.paths.root))
    runner=OfflinePaper(store);runner.recover()
    # Even a mislabeled durable command is not sufficient to unlock this path.
    with store.transaction() as db:
        put(db,'outbox','forged-fixture-label',dict(origin='FIXTURE_EXISTING_POSITION',status='PENDING',attempts=0,
            action=dict(action_id='forged-fixture-label',kind='ENTRY',position_id='unadmitted',side='BUY',quantity='.5')))
    with pytest.raises(OfflineError,match='NORMAL_ENTRY_CONTRACT_NOT_SUPPORTED'):runner.broker.execute('forged-fixture-label')
    assert not runner.summary()['orders'] and not runner.summary()['fills']
