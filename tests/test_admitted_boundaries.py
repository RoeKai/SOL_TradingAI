"""Isolation remains project-wide. No network/testnet/private adapter is used."""
import ast
import builtins
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from decimal import Decimal as D
import pytest
from app.admitted_paper.models import AdmittedSettings, MarketInput
from app.admitted_paper.demo import (demo_settings, demo_bundle, supply, observations, NOW,
    normal_open, complete_exit, latest_state, INSTANCE)
from app.admitted_paper.storage import AdmittedStore
from app.admitted_paper.engine import AdmittedPaper
from app.admitted_paper.review import review, markdown
from app.offline_paper.models import OfflineError
from app.offline_paper.storage import Store, get, put, rows
from app.offline_paper.engine import OfflinePaper
from app.offline_paper.fixtures import example_bundle, example_settings
from test_admitted_paper import paper, ROOT, reopen, assert_conservation


@pytest.mark.parametrize('value',[True,1,'true','false',0])
def test_live_cannot_be_enabled_or_coerced(paper,value):
    raw=demo_settings().model_dump();raw['live_allowed']=value
    with pytest.raises(ValueError): AdmittedSettings.model_validate(raw)


@pytest.mark.parametrize('field',['sol','btc','eth','volume'])
@pytest.mark.parametrize('value',['NaN','Infinity','-Infinity','0','-1'])
def test_invalid_market_amounts_rejected(field,value):
    raw=observations('LONG')[0].model_dump();raw[field]=value
    with pytest.raises(ValueError): MarketInput.model_validate(raw)


def test_btc_three_minute_field_uses_actual_180_second_baseline(paper):
    obs=observations('LONG')
    for o in obs: paper.ingest(o)
    c=paper.candidate('LONG')
    baseline=next(o for o in obs if o.observed_at==NOW-180)
    assert c.setup.market_state.btc_return_3m_pct==float((obs[-1].btc/baseline.btc-1)*100)
    assert c.setup.market_state.eth_return_3m_pct==float((obs[-1].eth/baseline.eth-1)*100)


def test_missing_real_three_minute_baseline_not_relabelled_shorter_window(paper):
    for i,o in enumerate(observations('LONG')):
        paper.ingest(o.model_copy(update={'observed_at':NOW-(13-i)*10,'available_at':NOW-(13-i)*10}))
    with pytest.raises(OfflineError,match='THREE_MINUTE_REFERENCE_MISSING'): paper.candidate('LONG')


def test_no_8a_database_adoption_or_default_path_change(paper):
    for name in ('examples/offline-paper/main.yaml','examples/offline-paper/manifest.yaml'):
        dest=paper.store.paths.root/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,dest)
    old=Store.create(paper.store.paths.root,'old-run',example_settings(),example_bundle(paper.store.paths.root))
    runner=OfflinePaper(old);runner.recover()
    other=AdmittedStore(paper.store.paths.root,'foreign-run',INSTANCE)
    paper.store.paths.directory('offline-admitted-runs/foreign-run')
    shutil.copyfile(old.path,other.path)
    with pytest.raises(OfflineError,match='Unknown database/version'): other.connect()
    assert old.path.exists() and other.path.exists()
    assert runner.summary()['scope']=='8A_SYNTHETIC_OFFLINE'
    assert not runner.summary()['normal_entry_complete']
    with pytest.raises(OfflineError,match='already exists'):
        AdmittedStore.create(paper.store.paths.root,'normal-test',demo_settings(),demo_bundle(paper.store.paths.root))


@pytest.mark.parametrize('dotted',['risk.max_loss_per_trade: 1000','risk.max_loss_per_trade: nonsense'])
def test_cli_rejects_original_semantic_key_collision_before_creating_db(paper,dotted):
    path=paper.store.paths.root/'examples/admitted-paper/main.yaml'
    path.write_text(path.read_text()+'\n'+dotted+'\n')
    result=subprocess.run([sys.executable,'-m','app.admitted_paper.cli','demo','--workspace',str(paper.store.paths.root),
        '--run-id','invalid-config'],cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode!=0 and 'LITERAL_DOTTED_KEY_FORBIDDEN' in result.stderr+result.stdout
    assert not (paper.store.paths.root/'offline-admitted-runs/invalid-config').exists()


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_environment_network_and_original_runtime_not_consumed(paper,side,monkeypatch):
    def denied(*args,**kwargs): raise AssertionError('No network or secret access')
    monkeypatch.setattr(socket,'socket',denied)
    real_open=builtins.open
    def guarded(path,*args,**kwargs):
        assert '.env' not in str(path)
        return real_open(path,*args,**kwargs)
    monkeypatch.setattr(builtins,'open',guarded)
    monkeypatch.setenv('BINANCE_API_KEY','SYNTHETIC_SECRET_MUST_NOT_BE_USED')
    monkeypatch.setenv('DRY_RUN','false');monkeypatch.setenv('LIVE_ENABLED','true')
    normal_open(paper,side);complete_exit(paper)
    r=reopen(paper);data=r.summary()
    assert data['normal_entry_complete'] and not data['live_allowed'] and not data['allow_fixtures']
    report=review(data)
    assert report['groups']['NORMAL_ADMITTED_8B']['closed']==1
    assert report['groups']['FIXTURE_EXISTING_POSITION']['confirmed_positions']==0
    assert report['win_probability'] is None and report['strategy_efficacy']=='NOT_EVALUATED'
    assert 'Stage 8B' in markdown(data)
    assert_conservation(r)


def test_precise_new_modules_do_not_import_live_or_old_loop():
    forbidden=('app.runtime','app.execution','app.paper','app.alerts','app.dashboard','app.data','httpx','websockets','socket','dotenv')
    for path in (ROOT/'app/admitted_paper').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            modules=[n.name for n in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
            assert not any(n.startswith(forbidden) for n in modules),(path,modules)
    for name in ('main.py','app/runtime.py','app/paper/broker.py'):
        assert 'admitted_paper' not in (ROOT/name).read_text()
    result=subprocess.run([sys.executable,'-c',
        'import sys;import app.admitted_paper.cli;print([x for x in sys.modules if x in '
        '("app.runtime","app.execution.engine","app.paper.broker","app.config","app.alerts.telegram")])'],
        cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode==0 and result.stdout.strip()=='[]'


def test_original_review_artifact_untouched():
    path=ROOT/'tests/test_stage07_semantic_key_collisions.py'
    assert path.stat().st_size==4770
    assert hashlib.sha256(path.read_bytes()).hexdigest()=='757ec0b46741fd22f51c5e181a431bf9196943ce6040ed311c3ce11b9f27c8e3'


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_full_cli_normal_from_zero_has_no_fixture_authority(paper,side):
    result=subprocess.run([sys.executable,'-m','app.admitted_paper.cli','demo','--workspace',str(paper.store.paths.root),
        '--run-id','cli-normal','--side',side],cwd=ROOT,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    assert value['entry_kind']=='ORDINARY_FROM_ZERO_NOT_FIXTURE'
    before,after=value['before_restart'],value['after_restart']
    assert before==after and after['normal_entry_complete']
    assert not after['allow_fixtures'] and after['fixture_positions']==0 and after['ordinary_positions']==1
    assert after['counts']['confirmed_ledger_fills']==5


@pytest.mark.parametrize('reason',['evidence','risk','scenario'])
def test_refusal_cli_has_reasons_and_zero_inventory(paper,reason):
    result=subprocess.run([sys.executable,'-m','app.admitted_paper.cli','reject','--workspace',str(paper.store.paths.root),
        '--run-id','cli-refusal','--reason',reason],cwd=ROOT,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    assert value['result']=='REJECT' and value['reason_codes']
    assert value['ledger']['counts']['orders']==0 and not value['ledger']['positions']
    if reason=='scenario': assert value['original_admission']!='REJECT'
