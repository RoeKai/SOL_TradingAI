import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys

import pytest
import yaml

from app.bootstrap import run_isolation_smoke
from app.config import ROOT, Config, load_config, load_secrets
from app.portfolio import PortfolioManager
from app.utils.audit import AuditLog
from app.utils.paths import POLICY, ModulePaths, IsolationError, assert_paper_runtime


@pytest.fixture
def module(tmp_path):
    root = (tmp_path / 'isolated').resolve()
    root.mkdir(mode=0o700)
    (root / 'isolation-policy.json').write_text(json.dumps(POLICY))
    (root / 'config.yaml').write_text((ROOT / 'config.yaml').read_text())
    return root


def ledger(root, mode='paper', instance='sol-ai-local'):
    return PortfolioManager(root / 'trades' / mode / 'ledger.sqlite3', mode=mode,
                            state_root=root, instance_id=instance)


def hashes(root):
    import hashlib
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def test_default_config_and_no_env_inheritance(module, monkeypatch):
    monkeypatch.setenv('BINANCE_API_KEY', 'OLD_ACCOUNT_KEY_SENTINEL')
    monkeypatch.setenv('BINANCE_API_SECRET', 'OLD_ACCOUNT_SECRET_SENTINEL')
    monkeypatch.setenv('DATABASE_URL', 'mysql://old-db.invalid/production')
    monkeypatch.setenv('DRY_RUN', 'false')
    monkeypatch.setenv('LIVE_ENABLED', 'true')
    (module.parent / '.env').write_text('BINANCE_API_KEY=PARENT_SENTINEL\n')
    config = load_config(module / 'config.yaml', module_root=module)
    assert config.dry_run is True and config.live.enabled is False
    assert_paper_runtime(config)
    assert set(load_secrets(module / '.env', module_root=module).values()) == {''}


@pytest.mark.parametrize('body', ['BINANCE_API_KEY=${OLD_KEY}', 'DATABASE_URL=mysql://old-db.invalid/production'])
def test_env_interpolation_and_foreign_fields_refused(module, monkeypatch, body):
    monkeypatch.setenv('OLD_KEY', 'inherited-secret')
    env = module / '.env'
    env.write_text(body)
    env.chmod(0o600)
    with pytest.raises(IsolationError):
        load_secrets(env, module_root=module)


def test_credentials_explicit_module_file_only_and_permissions(module):
    env = module / '.env'
    env.write_text('BINANCE_API_KEY=FAKE_NEW_KEY\nBINANCE_API_SECRET=FAKE_NEW_SECRET\n')
    env.chmod(0o600)
    assert load_secrets(env, module_root=module)['BINANCE_API_KEY'] == 'FAKE_NEW_KEY'
    env.chmod(0o644)
    with pytest.raises(IsolationError, match='PERMISSIONS'):
        load_secrets(env, module_root=module)
    with pytest.raises(IsolationError):
        load_config(module.parent / 'config.yaml', module_root=module)


@pytest.mark.parametrize('field', ['dry_run', 'live.enabled', 'live.dedicated_account_confirmed'])
@pytest.mark.parametrize('value', ['false', 'true', 0, 1, None])
def test_gate_bool_types_fail_closed(module, field, value):
    data = yaml.safe_load((module / 'config.yaml').read_text())
    container = data
    parts = field.split('.')
    for key in parts[:-1]:
        container = container[key]
    container[parts[-1]] = value
    (module / 'config.yaml').write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_config(module / 'config.yaml', module_root=module)
    assert not (module / 'trades').exists()


@pytest.mark.parametrize('text', ['', '[]', 'live: {}', 'dry_run: [broken', 'dry_run: true\ndatabase_url: old'])
def test_missing_corrupt_and_extra_config_no_state_created(module, text):
    (module / 'config.yaml').write_text(text)
    with pytest.raises((ValueError, yaml.YAMLError)):
        run_isolation_smoke(module)
    assert not (module / 'trades').exists()
    assert not (module / 'logs').exists()


def test_even_full_manual_live_gate_cannot_run_this_release(module):
    data = yaml.safe_load((module / 'config.yaml').read_text())
    data.update(dry_run=False)
    data['live'].update(enabled=True, dedicated_account_confirmed=True, confirmation='ENABLE_SMALL_CAPITAL_LIVE')
    (module / 'config.yaml').write_text(yaml.safe_dump(data))
    with pytest.raises(IsolationError, match='LIVE_ISOLATION_NOT_ACCEPTED'):
        run_isolation_smoke(module)
    assert not (module / 'trades').exists()


@pytest.mark.parametrize('relative', ['.env', 'config.yaml', 'trades', 'logs', 'isolation-policy.json'])
def test_symlink_sentinels_untouched(module, relative):
    sentinel = module.parent / 'old-system'
    sentinel.mkdir(exist_ok=True)
    (sentinel / 'DO_NOT_TOUCH').write_text('OLD_SYSTEM_SENTINEL')
    target = module / relative
    if target.exists():
        target.unlink()
    target.symlink_to(sentinel, target_is_directory=True)
    before = hashes(sentinel)
    with pytest.raises(IsolationError, match='SYMLINK'):
        if relative == '.env':
            load_secrets(target, module_root=module)
        else:
            run_isolation_smoke(module)
    assert hashes(sentinel) == before


def test_hardlink_database_and_env_refused_without_writes(module):
    outside = module.parent / 'old.data'
    outside.write_text('old sentinel')
    os.link(outside, module / '.env')
    with pytest.raises(IsolationError, match='HARDLINK'):
        load_secrets(module / '.env', module_root=module)
    paths = ModulePaths(module)
    paths.directory('trades/paper')
    os.link(outside, paths.ledger)
    with pytest.raises(IsolationError, match='HARDLINK'):
        ledger(module)
    assert outside.read_text() == 'old sentinel'


def test_foreign_db_refused_before_schema_or_sidecar_write(module):
    paths = ModulePaths(module)
    paths.directory('trades/paper')
    with sqlite3.connect(paths.ledger) as foreign:
        foreign.execute('CREATE TABLE old_orders(id TEXT)')
        foreign.execute("INSERT INTO old_orders VALUES ('old-order')")
    before = hashes(module)
    with pytest.raises(IsolationError, match='FOREIGN_LEDGER'):
        ledger(module)
    assert hashes(module) == before


def test_mode_instance_and_path_mismatch_refused_before_writes(module):
    paper = ledger(module)
    paper.set_meta('paused', True)
    paper.close()
    before = hashes(module)
    with pytest.raises(IsolationError, match='MODE_OR_INSTANCE'):
        ledger(module, instance='different-instance')
    with pytest.raises(IsolationError, match='OUTSIDE_EXPECTED_STATE_PATH'):
        PortfolioManager(module / 'trades/paper/ledger.sqlite3', mode='live',
                         state_root=module, instance_id='sol-ai-local')
    assert hashes(module) == before
    paths = ModulePaths(module, mode='live')
    paths.directory('trades/live')
    shutil.copyfile(module / 'trades/paper/ledger.sqlite3', paths.ledger)
    before = hashes(module)
    with pytest.raises(IsolationError, match='MODE_OR_INSTANCE'):
        ledger(module, mode='live')
    assert hashes(module) == before


def test_distinct_ledgers_restart_only_own_risk_state(module):
    paper, live = ledger(module), ledger(module, mode='live')
    paper.set_meta('paused', True)
    paper.set_meta('halt_reason', 'PAPER_SENTINEL')
    assert live.get_meta('paused') is None
    with pytest.raises(IsolationError, match='IMMUTABLE'):
        paper.set_meta('mode', 'live')
    with pytest.raises((RuntimeError, IsolationError)):
        ledger(module)
    paper.close()
    live.close()
    restarted = ledger(module)
    assert restarted.get_meta('paused') is True
    assert restarted.get_meta('halt_reason') == 'PAPER_SENTINEL'
    restarted.close()


def test_uncheckpointed_wal_identity_cannot_trigger_any_state_write(module):
    first = ledger(module)
    # Simulate corrupt/foreign low-level metadata mutation that bypasses the public API.
    first.db.execute('PRAGMA wal_autocheckpoint=0')
    first.db.execute("UPDATE meta SET value='\"live\"' WHERE key='mode'")
    before = hashes(module)
    with pytest.raises(IsolationError, match='WAL_RECOVERY_REQUIRES_OFFLINE_VALIDATION'):
        ledger(module)
    assert hashes(module) == before
    first.close()


def test_logs_reject_foreign_path_and_linked_rotation(module):
    paths = ModulePaths(module)
    with pytest.raises(IsolationError):
        AuditLog(module.parent, paths=paths)
    paths.directory('logs/paper')
    sentinel = module.parent / 'old.log'
    sentinel.write_text('old log')
    (module / 'logs/paper/events.jsonl.1').symlink_to(sentinel)
    with pytest.raises(IsolationError):
        AuditLog(module / 'logs/paper', paths=paths)
    assert sentinel.read_text() == 'old log'


def test_default_run_with_keys_and_poison_proxy_has_zero_network(module, monkeypatch):
    calls = []
    def reject(*args, **kwargs):
        calls.append(str(args))
        raise AssertionError('Any network attempt violates offline acceptance')
    monkeypatch.setattr(socket.socket, 'connect', reject)
    monkeypatch.setattr(socket.socket, 'connect_ex', reject)
    monkeypatch.setattr(socket, 'getaddrinfo', reject)
    monkeypatch.setenv('HTTP_PROXY', 'http://old-executor.invalid:8080')
    monkeypatch.setenv('HTTPS_PROXY', 'http://old-executor.invalid:8080')
    (module / '.env').write_text('BINANCE_API_KEY=FAKE_NEW\nBINANCE_API_SECRET=FAKE_SECRET\n')
    (module / '.env').chmod(0o600)
    result = run_isolation_smoke(module)
    assert result['trades_created'] == 1
    assert result['private_requests'] == result['live_orders'] == 0
    assert calls == []
    assert not (module / 'trades/live').exists()
    assert not (module / 'logs/live').exists()
    contents = (module / 'logs/paper/events.jsonl').read_text()
    assert 'FAKE_NEW' not in contents and 'FAKE_SECRET' not in contents
    assert 'paper_entry' in contents and 'paper_exit' in contents


def test_source_package_runs_without_repo_or_parent_env(module):
    # Copy only new source, NOT old server or data. Real OS sandbox proof is an additional command.
    target = module.parent / 'standalone'
    shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns(
        'node_modules', 'dist', '.env', '.venv', '__pycache__', '.pytest_cache', 'logs', 'trades', 'reports'))
    (target.parent / '.env').write_text('BINANCE_API_KEY=FAKE_OLD_PARENT')
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
           'BINANCE_API_KEY': 'FAKE_OLD_ENV', 'BINANCE_API_SECRET': 'FAKE_OLD_ENV_SECRET',
           'DATABASE_URL': 'mysql://old-db.invalid/live', 'HTTP_PROXY': 'http://old-proxy.invalid:9999',
           'DRY_RUN': 'false', 'LIVE_ENABLED': 'true'}
    outcome = subprocess.run([sys.executable, str(target / 'main.py'), '--isolation-smoke'], cwd=target.parent,
                             env=env, capture_output=True, text=True, timeout=20)
    assert outcome.returncode == 0, outcome.stderr
    result = json.loads(outcome.stdout)
    assert result['dry_run'] and result['trades_created'] == 1
    assert result['network_clients_created'] == result['private_requests'] == result['live_orders'] == 0
    assert not (target / 'server').exists()
    assert (target / 'trades/paper/ledger.sqlite3').exists()
