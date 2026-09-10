"""Real CLI subprocesses on tiny synthetic archive FORMAT fixtures, not downloads.

The required market/schema/checksum fields are parser inputs here, not proof of
official provenance. No result from these artificial files is a historical
performance result. No parsing, Broker, receipt or authority function is mocked.
"""
import json
import shutil
import subprocess
import sys
import zipfile

from app.historical_replay.data import START, END, WARMUP, seal, inspect_archive, sha_file
from app.historical_replay.configuration import INSTANCE
from app.historical_replay.engine import HistoricalPaper
from app.historical_replay.replay import Replay
from app.historical_replay.storage import HistoricalStore, hput
from app.offline_paper.storage import get, rows
from test_stage08c_historical import ROOT, workspace
from test_stage08c_active_integration import existing_position


def cli(root, command, *extra, expected=0):
    p = subprocess.run([sys.executable, '-m', 'app.historical_replay.cli', command,
        '--workspace', str(root), '--dataset', str(root / 'format-test-data'),
        '--run-id', 'test-historical', *extra], cwd=ROOT, text=True,
        capture_output=True, timeout=30)
    assert p.returncode == expected, (p.stdout, p.stderr)
    # Progress JSON lines and the final pretty JSON are separate CLI records.
    remaining = p.stdout.lstrip(); outputs = []
    while remaining:
        value, end = json.JSONDecoder().raw_decode(remaining)
        outputs.append(value); remaining = remaining[end:].lstrip()
    assert all('progress_events' in value for value in outputs[:-1])
    return outputs[-1]


def cli_instance(root):
    workspace(root)
    shutil.copytree(ROOT / 'app', root / 'app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    data = root / 'format-test-data'; data.mkdir()
    files = []
    for symbol in ('SOLUSDT', 'BTCUSDT', 'ETHUSDT'):
        for seq, period, at in ((1, '2026-07-31', WARMUP), (2, '2026-08', START + 1000)):
            name = symbol + '-aggTrades-' + period
            path = data / (name + '.zip')
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr(name + '.csv', f'{seq},100,100,{seq},{seq},{at},true\n')
            sha = sha_file(path)
            files.append(dict(file=path.name, symbol=symbol, kind='aggTrades',
                sha256=sha, official_sha256=sha, downloaded_at='SYNTHETIC_FORMAT_TEST_NO_DOWNLOAD',
                **inspect_archive(path, symbol, period)))
    f = data / 'SOLUSDT-funding.json'
    f.write_text(json.dumps([dict(symbol='SOLUSDT', fundingTime=START,
        fundingRate='.001', markPrice='100')]))
    files.append(dict(file=f.name, kind='fundingRate', sha256=sha_file(f)))
    (data / 'dataset.json').write_text(json.dumps(seal(dict(version='historical-dataset/v1',
        status='COMPLETE', market='BINANCE_USD_M_LINEAR_PERPETUAL',
        test_provenance='SYNTHETIC_FORMAT_ONLY_NOT_OFFICIAL_DATA',
        evaluation_range_ms=[START, END], warmup_range_ms=[WARMUP, START], files=files))))
    cli(root, 'index')
    manifest = str(root / 'format-test-run.json')
    cli(root, 'freeze', '--manifest', manifest, '--code-commit', 'b'*40, '--kind', 'engineering')
    cli(root, 'init', '--manifest', manifest)
    return HistoricalPaper(HistoricalStore(root, 'test-historical', INSTANCE))


def test_real_cli_initialize_partial_stream_and_recover_empty_ledger(tmp_path):
    cli_instance(tmp_path)
    out = cli(tmp_path, 'run', '--max-events', '6')
    assert out['segment']['new_events'] == 6
    assert out['result']['counts']['simulated_orders'] == 0
    assert out['result']['metrics']['cash_usdt'] == '500'
    for _ in range(2):
        restored = cli(tmp_path, 'recover')['result']
        assert restored['counts'] == out['result']['counts']
        assert restored['market_prefix_digest'] == out['result']['market_prefix_digest']
        assert restored['metrics']['cash_usdt'] == '500'


def test_real_cli_latched_model_failure_before_first_timing_record(tmp_path):
    p = cli_instance(tmp_path)
    # Explicit component initialization of an already-invalid run, no position
    # or approval injection. Full HistoricalPaper.recover still runs in the CLI.
    with p.store.transaction() as db:
        hput(db, 'history_meta', 'model_limit', dict(reason_code='MODEL_LIMIT_EXCEEDED',
            at_ms=WARMUP, position_id='SYNTHETIC_PRIOR_EXPOSURE', valid_after=False))
    for _ in range(2):
        out = cli(tmp_path, 'run')
        assert out['segment']['model_invalid'] and out['segment']['new_events'] == 0
        assert out['result']['metrics'] is None
        assert out['result']['diagnostic_metrics']['cash_usdt'] == '500'
        assert out['result']['performance'] is None  # no invented timing record
    restored = cli(tmp_path, 'recover')['result']
    assert restored['invalid_after_ms'] == WARMUP
    assert not restored['model_supported_metrics_available']
    assert restored['counts']['simulated_orders'] == 0


def test_real_cli_recovery_cannot_launder_component_position_into_authority(tmp_path):
    p = cli_instance(tmp_path)
    existing_position(p, Replay(p), 'LONG')
    with p.store.transaction() as db:
        before = rows(db, 'fills'), get(db, 'account', 'account')['cash']
    out = cli(tmp_path, 'recover', expected=2)
    assert 'NO_INSTANCE_ISSUED_HISTORICAL_APPROVAL' in out['error']
    assert out['live_allowed'] is False
    with p.store.transaction() as db:
        assert get(db, 'account', 'account')['quarantined']
        assert (rows(db, 'fills'), get(db, 'account', 'account')['cash']) == before
