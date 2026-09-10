"""Author-written 8C R1 reproductions, NOT the unavailable reviewer original.

Initialization is an explicitly injected, already-filled COMPONENT position.
It is NOT a historical admission success: its PlanSnapshot says REJECT and it
has no historical grant. Store.initialize, HistoricalPaper, HistoricalBroker,
Replay.trade, receipt delivery, ExitPolicy and SQLite are the real classes.
No mock replaces fills, state consumers, risk gates or exception handling.
"""
from decimal import Decimal as D
import json
import sqlite3
import subprocess
import sys

import pytest

from app.configuration.compiler import policy_from
from app.exits.models import EntryFill, ExitSeed, PlanSnapshot
from app.exits.persistence import checkpoint, ExitCheckpoint
from app.historical_replay.data import START, HistoricalError, seal
from app.historical_replay.configuration import INSTANCE, configuration, run_manifest
from app.historical_replay.models import ExecutionModel
from app.historical_replay.engine import ORIGIN, HistoricalPaper
from app.historical_replay.report import result as report_result
from app.historical_replay.replay import Replay
from app.historical_replay.storage import HistoricalStore
from app.historical_replay.replay import saved_cursor, quote_from
from app.historical_replay.storage import hget, hput, hrows
from app.offline_paper.broker import synthetic_rules, inventory
from app.offline_paper.storage import digest, get, put, rows
from test_stage08c_historical import workspace, market, observation, ROOT
from test_offline_paper import conservation


def system(tmp_path):
    workspace(tmp_path)
    model = ExecutionModel()
    bundle, settings = configuration(tmp_path, model)
    dataset = seal(dict(version='historical-dataset/v1', status='COMPLETE',
        market='SYNTHETIC_TEST_NOT_HISTORICAL', files=[dict(file='funding.json',
            kind='fundingRate', sha256='f'*64)]))
    run = run_manifest(experiment_id='test-historical', code_commit='b'*40,
        code_digest='c'*64, dataset=dataset, bundle=bundle, settings=settings,
        model=model, kind='engineering', end_ms=START+3600000)
    p = HistoricalPaper(HistoricalStore.initialize(tmp_path, 'test-historical',
        settings, bundle, dataset, run))
    p.recover()
    return p, Replay(p)


def clock(paper, replay, at):
    with paper.store.transaction() as db:
        cursor = hget(db, 'history_cursor', 'cursor')
        replay._set_clock(db, cursor, at)
        saved_cursor(db, cursor)
    paper.pump()


def existing_position(paper, replay, side, pid='component-existing'):
    """Only test setup; never issue/forge an ordinary approval or ENTRY command.

    The reservation origin is the schema's compositor tag required by _save,
    NOT authority: the REJECT plan / absent grant remain unexecutable and full
    HistoricalPaper.recover must refuse this unadmitted initialization.
    """
    entry_id = 'component-opening:' + pid
    fid = 'component-first-fill:' + pid
    with paper.store.transaction() as db:
        bundle = paper.store.bundle(db)
        policy = policy_from(bundle, 'exit')
        cursor = hget(db, 'history_cursor', 'cursor')
        replay._set_clock(db, cursor, START)
        plan = PlanSnapshot(setup_id=pid, plan_version='COMPONENT_NOT_ADMITTED',
            symbol='SOLUSDT', side=side, original_stop='95' if side == 'LONG' else '105',
            original_targets=(), setup_digest=digest(pid), rr_digest=digest('NO_RR'),
            scorecard_digest=digest('NO_SCORE'), admission_digest=digest('NO_APPROVAL'),
            admission_result='REJECT')
        entry = EntryFill(event_id=fid, position_id=pid, received_at=START / 1000,
            occurred_at=START / 1000, entry_action_id=entry_id, fill_id=fid,
            price='100', quantity='.5', fee_usdt='.025')
        cp = checkpoint(ExitSeed(position_id=pid, first_fill=entry, plan=plan,
            rules=synthetic_rules()), policy, ())
        put(db, 'reservations', pid, dict(origin=ORIGIN, side=side, quantity='.5',
            approved_quantity='.5', risk='5', margin='10', fee_reserve='.025',
            reference_price='100', expires_at=START / 1000 + 60,
            bundle_digest=bundle.bundle_digest, account_revision=1,
            released=False, entry_sealed=False, entry_high_water='.5',
            entry_terminal='FILLED', entry_terminal_quantity='.5',
            entry_status_unknown=False, entry_faults=[], entry_pending_reasons=[],
            entry_reconciliation_required=False, entry_action_id=entry_id,
            plan=plan.model_dump(mode='json'), rules=synthetic_rules().model_dump(mode='json'),
            exit_policy=policy.model_dump(mode='json'),
            entry_costs=dict(fee_rate='.0005', slippage_bps='10'),
            approval_id='COMPONENT_NO_GRANT', exit_plan_id='COMPONENT_NO_EXIT_PLAN'))
        opening = dict(action_id=entry_id, kind='ENTRY', position_id=pid,
            side='BUY' if side == 'LONG' else 'SELL', quantity='.5')
        put(db, 'outbox', entry_id, dict(origin=ORIGIN, action=opening,
            status='CONFIRMED', attempts=1))
        put(db, 'broker_orders', entry_id, dict(action=opening, status='FILLED',
            cumulative='.5', created_at=START / 1000))
        put(db, 'broker_commands', entry_id, dict(action_digest=digest(opening), at=START / 1000))
        fact = dict(kind='ENTRY_FILL', action_id=entry_id, position_id=pid,
            fill_id=fid, quantity='.5', requested_quantity='.5', price='100',
            fee_usdt='.025', occurred_at=START / 1000)
        put(db, 'broker_fills', fid, fact)
        put(db, 'fills', fid, dict(fact, net_outcome='0'))
        paper._save(db, dict(origin=ORIGIN, entry_fee_remaining='.025', closed_at=None), cp)
        paper._seal(db, pid)
        paper._cash(db)
        hput(db, 'history_meta', 'component_initialization', dict(
            provenance='SYNTHETIC_EXISTING_POSITION_NOT_ADMITTED', admission_result='REJECT'))
        saved_cursor(db, cursor)
    return pid


def position_state(paper, pid='component-existing'):
    with paper.store.transaction() as db:
        return get(db, 'positions', pid)['checkpoint']['state']


def competing_orders(tmp_path, side):
    paper, replay = system(tmp_path)
    pid = existing_position(paper, replay, side)
    clock(paper, replay, START + 1000)  # actual ARM_STOP acceptance / receipt
    assert position_state(paper)['protection_status'] == 'ACTIVE'
    market(paper, replay, '106' if side == 'LONG' else '94', START + 2000)
    clock(paper, replay, START + 3000)  # actual TP1 acceptance / receipt
    with paper.store.transaction() as db:
        stop = next(k for k, o in rows(db, 'broker_orders') if o['action']['kind'] == 'ARM_STOP')
        tp = next(k for k, o in rows(db, 'broker_orders') if o['action']['kind'] == 'TP1')
        assert get(db, 'broker_orders', stop)['status'] == 'ACCEPTED'
        assert get(db, 'broker_orders', tp)['status'] == 'ACCEPTED'
    return paper, replay, pid, stop, tp


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_r1_stop_then_competing_tp_does_not_rollback(tmp_path, side):
    p, r, pid, stop, tp = competing_orders(tmp_path, side)
    event = market(p, r, '90' if side == 'LONG' else '110', START + 4000, qty='100')
    with p.store.transaction() as db:
        assert inventory(db, pid) == 0
        assert D(get(db, 'broker_orders', stop)['cumulative']) == D('.5')
        assert D(get(db, 'broker_orders', tp)['cumulative']) == 0
        assert hget(db, 'history_cursor', 'cursor')['last']['SOLUSDT'] == event
    assert D(position_state(p)['remaining_quantity']) == 0
    conservation(p)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_r1_post_fill_pump_cancels_later_candidate(tmp_path, side):
    p, r, pid, stop, tp = competing_orders(tmp_path, side)
    # A real reducer-generated CANCEL matures during this independently called
    # fill/pump boundary. Nothing substitutes for the actual receipt consumer.
    with p.store.transaction() as db:
        c = hget(db, 'history_cursor', 'cursor')
        r._set_clock(db, c, START + 3500)
        gap = observation('SOLUSDT', '90' if side == 'LONG' else '110', START + 3500, 101)
        r._protect(db, gap)
        assert any(o['action']['kind'] == 'CANCEL' and o['action']['target_action_id'] == tp
            for _, o in rows(db, 'outbox'))
        saved_cursor(db, c)
    with p.store.transaction() as db:
        c = hget(db, 'history_cursor', 'cursor')
        event = observation('SOLUSDT', gap['price'], START + 5000, 102, qty='100')
        r._set_clock(db, c, event['available_at_ms'])
        a = get(db, 'account', 'account'); a['quote'] = quote_from(event, r.model)
        put(db, 'account', 'account', a)
        hput(db, 'history_meta', 'active_market', event)
        hput(db, 'history_meta', 'liquidity', dict(event_id=event['event_id'], used='0'))
        r._fills(db, event)
        assert get(db, 'broker_orders', tp)['status'] == 'CANCELED'
        assert inventory(db, pid) == 0
        saved_cursor(db, c)
    conservation(p)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_r1_model_limit_is_recorded_before_same_event_stop_closes(tmp_path, side):
    p, r = system(tmp_path)
    pid = existing_position(p, r, side)
    clock(p, r, START + 1000)
    market(p, r, '70' if side == 'LONG' else '130', START + 2000, qty='100')
    with p.store.transaction() as db:
        limit = hget(db, 'history_meta', 'model_limit')
        assert limit is not None, 'pre-fill isolated margin breach disappeared after stop filled flat'
        assert limit['reason_code'] == 'MODEL_LIMIT_EXCEEDED'
        assert limit['at_ms'] == START + 2000 and limit['position_id'] == pid
        assert get(db, 'account', 'account')['paused']
    assert D(position_state(p)['remaining_quantity']) == 0
    conservation(p)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('first_quantity', ['.2', '.499'])
def test_partial_stop_shared_budget_tail_and_repeated_event(tmp_path, side, first_quantity):
    p, r, pid, stop, tp = competing_orders(tmp_path, side)
    q = D(first_quantity)
    event = market(p, r, '90' if side == 'LONG' else '110', START + 4000, qty=str(q / r.model.participation))
    with p.store.transaction() as db:
        assert inventory(db, pid) == D('.5') - q
        assert D(get(db, 'broker_orders', stop)['cumulative']) == q
        assert D(get(db, 'broker_orders', tp)['cumulative']) == 0
        assert D(hget(db, 'history_meta', 'liquidity')['used']) == q
        before = rows(db, 'fills')
        cursor = hget(db, 'history_cursor', 'cursor')
        r.trade(db, cursor, event, active=True)  # duplicate source record
        r._fills(db, event)  # idempotent re-entry of the same liquidity consumer
        assert rows(db, 'fills') == before
        saved_cursor(db, cursor)
    # Cancellation is not due until +5000; the native stop still covers the tail.
    tail = D('.5') - q
    market(p, r, event['price'], START + 4200, qty=str(tail / r.model.participation))
    assert D(position_state(p)['remaining_quantity']) == 0
    assert position_state(p)['phase'] == 'CLOSED'
    with p.store.transaction() as db:
        assert D(get(db, 'broker_orders', stop)['cumulative']) == D('.5')
        assert len([f for _, f in rows(db, 'fills') if f['kind'] == 'EXIT_FILL']) == 2
    conservation(p)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('raw_volume', ['75', '125'])
def test_multiple_competing_exits_share_one_budget_across_positions(tmp_path, side, raw_volume):
    p, r = system(tmp_path)
    for pid in ('component-a', 'component-b'):
        existing_position(p, r, side, pid)
    # This injected two-position portfolio is not new-entry risk authorization.
    clock(p, r, START + 1000)
    market(p, r, '106' if side == 'LONG' else '94', START + 2000)
    clock(p, r, START + 3000)
    market(p, r, '90' if side == 'LONG' else '110', START + 4000, qty=raw_volume)
    expected = min(D('1'), D(raw_volume) * r.model.participation)
    with p.store.transaction() as db:
        exits = [f for _, f in rows(db, 'fills') if f['kind'] == 'EXIT_FILL']
        assert sum(D(f['quantity']) for f in exits) == expected
        assert D(hget(db, 'history_meta', 'liquidity')['used']) == expected
        assert all(D(o['cumulative']) == 0 for _, o in rows(db, 'broker_orders') if o['action']['kind'] == 'TP1')
        assert sum(inventory(db, pid) for pid in ('component-a', 'component-b')) == 1 - expected
    conservation(p)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_prior_partial_tp_reduces_native_stop_capacity(tmp_path, side):
    p, r, pid, stop, tp = competing_orders(tmp_path, side)
    market(p, r, '106' if side == 'LONG' else '94', START + 4000, qty='5')
    assert D(position_state(p)['tp1_filled']) == D('.05')
    market(p, r, '90' if side == 'LONG' else '110', START + 4500, qty='100')
    with p.store.transaction() as db:
        assert get(db, 'broker_orders', stop)['status'] == 'CANCELED'  # quantity exhausted before full order amount
        assert D(get(db, 'broker_orders', stop)['cumulative']) == D('.45')
        assert D(get(db, 'broker_orders', tp)['cumulative']) == D('.05')
        assert inventory(db, pid) == 0
    conservation(p)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_real_sql_write_failure_keeps_original_atomic_boundary(tmp_path, side):
    p, r, pid, stop, tp = competing_orders(tmp_path, side)
    with p.store.transaction() as db:
        before = dict(fills=rows(db, 'fills'), orders=rows(db, 'broker_orders'),
            cursor=hget(db, 'history_cursor', 'cursor'), cash=get(db, 'account', 'account')['cash'])
        # Fail the cursor write AFTER the valid stop, receipts, ledger and cancel
        # intents. Genuine failures must still escape, rolling everything back.
        db.execute("""CREATE TRIGGER component_cursor_fault BEFORE INSERT ON history_cursor
            WHEN json_extract(NEW.payload, '$.content_digest') IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'component cursor write fault'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='component cursor write fault'):
        market(p, r, '90' if side == 'LONG' else '110', START + 4000, qty='100')
    fresh = HistoricalPaper(HistoricalStore(tmp_path, 'test-historical', INSTANCE))
    with fresh.store.transaction() as db:
        assert dict(fills=rows(db, 'fills'), orders=rows(db, 'broker_orders'),
            cursor=hget(db, 'history_cursor', 'cursor'), cash=get(db, 'account', 'account')['cash']) == before
        assert inventory(db, pid) == D('.5')


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('raw_volume', ['20', '100'])
def test_first_model_failure_latched_across_later_prices_and_restart(tmp_path, side, raw_volume):
    p, r = system(tmp_path)
    existing_position(p, r, side)
    clock(p, r, START + 1000)
    market(p, r, '70' if side == 'LONG' else '130', START + 2000, qty=raw_volume)
    with p.store.transaction() as db:
        first = hget(db, 'history_meta', 'model_limit')
        assert first['detection_phase'] == 'PRE_MARKET_EXECUTION'
        assert D(first['remaining_quantity']) == D('.5')
        assert D(first['remaining_entry_cost']) == D('50')
        assert D(first['allocated_margin_usdt']) == D('10')
        assert D(first['floating_pnl_usdt']) < D('-15')
        assert D(first['model_margin_balance_usdt']) < 0
    # Direct diagnostic protection remains possible; ordinary run_stream stops.
    market(p, r, '110' if side == 'LONG' else '90', START + 2500)
    fresh = HistoricalPaper(HistoricalStore(tmp_path, 'test-historical', INSTANCE))
    replay = Replay(fresh)
    with fresh.store.transaction() as db:
        replay._model_limit(db, START + 3000)
        assert hget(db, 'history_meta', 'model_limit') == first
    # Run-level refusal happens even with no active position and before reading
    # any future source data. No deletion/reinitialization or resetting capital.
    out = replay.run_stream(tmp_path / 'deliberately-not-read', {})
    assert out['model_invalid'] and out['new_events'] == 0
    assert out['invalid_after_ms'] == START + 2000
    result = report_result(fresh)
    assert result['version'] == 'historical-report/v2'
    assert result['metrics'] is None and not result['model_supported_metrics_available']
    assert result['diagnostic_metrics']['cash_usdt'] == fresh.summary()['account']['cash']
    assert result['result_usage'] == fresh.summary()['result_usage'] == 'DIAGNOSTIC_ONLY_MODEL_LIMIT_EXCEEDED'
    conservation(fresh)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('balance', ['0', '.000001'])
def test_model_boundary_equality_and_positive_balance(tmp_path, side, balance):
    p, r = system(tmp_path)
    existing_position(p, r, side)
    target = D('80') + D(balance) * 2 if side == 'LONG' else D('120') - D(balance) * 2
    with p.store.transaction() as db:
        a = get(db, 'account', 'account')
        a['quote'] = dict(event_id='component-exact-boundary', at=START / 1000, bid=str(target), ask=str(target))
        put(db, 'account', 'account', a)
        r._model_limit(db, START + 1, phase='EXPLICIT_COMPONENT_QUOTE')
        assert (hget(db, 'history_meta', 'model_limit') is not None) == (D(balance) == 0)


def active_stream(paper, side, *,breach=False):
    """Small synthetic file stream for run_stream, not an official data claim."""
    root = paper.store.paths.root / 'component-stream'
    stream = root / 'stream-v1'; stream.mkdir(parents=True)
    price = ('70' if side == 'LONG' else '130') if breach else ('90' if side == 'LONG' else '110')
    # The warmup/TP observation already consumed sequence 100 in this test.
    (stream / 'SOLUSDT.csv').write_text(''.join(
        f'{101+i},{price},100,{101+i},{101+i},{START+3750+i*1000},true\n' for i in range(2)))
    (root / 'funding.json').write_text(json.dumps([dict(symbol='SOLUSDT',
        fundingTime=START+25000, fundingRate='.001', markPrice='100')]))
    with paper.store.transaction() as db:
        dataset = hget(db, 'history_meta', 'dataset')
        cursor = hget(db, 'history_cursor', 'cursor')
        cursor['next_sample_ms'] = START + 15000
        cursor['by_file'] = {}
        saved_cursor(db, cursor)
    index = dict(dataset_digest=dataset['content_digest'], files=[dict(file='SOLUSDT.csv',
        symbol='SOLUSDT', source_file='SYNTHETIC_COMPONENT', source_digest='f'*64, downloaded_at='SYNTHETIC_COMPONENT')])
    (root / 'index.json').write_text(json.dumps(index))
    return root, index


def reconcile_exit_component(paper, original_stop, cause):
    """Explicit component recovery, NOT HistoricalPaper's admission authority.

    Verify actual event-sourced checkpoints and ledger conservation, then query
    the ORIGINAL exit order and consume its real receipts/details. An injected
    REJECT seed cannot truthfully pass full historical entry-grant recovery.
    """
    with paper.store.transaction() as db:
        for _, p in rows(db, 'positions'):
            cp = ExitCheckpoint.model_validate(p['checkpoint'])
            assert checkpoint(cp.seed, cp.policy, cp.journal) == cp
            assert {f.fill_id for f in cp.state.fill_facts} == {
                k for k, f in rows(db, 'fills') if f['position_id'] == cp.state.position_id}
    conservation(paper)
    paper.broker.reconcile(original_stop, cause)
    paper.pump()
    conservation(paper)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_stream_halts_after_invalid_event_even_when_flat(tmp_path, side):
    p, r, pid, stop, tp = competing_orders(tmp_path, side)
    data, index = active_stream(p, side, breach=True)
    out = r.run_stream(data, index, max_events=2)
    assert out['new_events'] == 1 and out['model_invalid'] and not out['finished']
    assert out['invalid_after_ms'] == START + 4000
    assert D(position_state(p)['remaining_quantity']) == 0
    with p.store.transaction() as db:
        assert not r._active(db)
        before = rows(db, 'fills')
    second = Replay(HistoricalPaper(HistoricalStore(tmp_path, 'test-historical', INSTANCE))).run_stream(data, index)
    assert second['new_events'] == 0 and second['model_invalid']
    with p.store.transaction() as db:
        assert rows(db, 'fills') == before
    conservation(p)


COMPONENT_CHILD = '''
import json,sys
from pathlib import Path
from app.historical_replay.storage import HistoricalStore
from app.historical_replay.engine import HistoricalPaper
from app.historical_replay.configuration import INSTANCE
from app.historical_replay.replay import Replay
p=HistoricalPaper(HistoricalStore(sys.argv[1],"test-historical",INSTANCE))
root=Path(sys.argv[1])/"component-stream"
Replay(p).run_stream(root,json.loads((root/"index.json").read_text()),max_events=1,
    fault=None if sys.argv[2]=="none" else sys.argv[2])
'''


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('fault', ['before_market_cursor_commit', 'after_market_cursor_commit'])
@pytest.mark.parametrize('breach', [False, True])
def test_active_process_death_atomic_cursor_stop_fee_and_model_latch(tmp_path, side, fault, breach):
    p, r, pid, stop, tp = competing_orders(tmp_path, side)
    data, index = active_stream(p, side, breach=breach)
    died = subprocess.run([sys.executable, '-c', COMPONENT_CHILD, str(tmp_path), fault],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert died.returncode == 91, (died.stdout, died.stderr)
    after = fault == 'after_market_cursor_commit'
    fresh = HistoricalPaper(HistoricalStore(tmp_path, 'test-historical', INSTANCE))
    with fresh.store.transaction() as db:
        assert inventory(db, pid) == (D(0) if after else D('.5'))
        assert D(get(db, 'broker_orders', stop)['cumulative']) == (D('.5') if after else D(0))
        assert hget(db, 'history_cursor', 'cursor')['event_count'] == (2 if after else 1)
        assert (hget(db, 'history_meta', 'model_limit') is not None) == (after and breach)
    reconcile_exit_component(fresh, stop, 'component-recovery-first')
    resumed = subprocess.run([sys.executable, '-c', COMPONENT_CHILD, str(tmp_path), 'none'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert resumed.returncode == 0, (resumed.stdout, resumed.stderr)
    again = HistoricalPaper(HistoricalStore(tmp_path, 'test-historical', INSTANCE))
    reconcile_exit_component(again, stop, 'component-recovery-second')
    with again.store.transaction() as db:
        assert inventory(db, pid) == 0
        assert len([f for _, f in rows(db, 'fills') if f['kind'] == 'EXIT_FILL']) == 1
        assert (hget(db, 'history_meta', 'model_limit') is not None) == breach
        before = rows(db, 'fills'), get(db, 'account', 'account')['cash']
    reconcile_exit_component(again, stop, 'component-recovery-third')
    with again.store.transaction() as db:
        assert (rows(db, 'fills'), get(db, 'account', 'account')['cash']) == before
    conservation(again)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_full_historical_recovery_refuses_component_inventory_as_admitted(tmp_path, side):
    p, r, *_ = competing_orders(tmp_path, side)
    fresh = HistoricalPaper(HistoricalStore(tmp_path, 'test-historical', INSTANCE))
    with pytest.raises(HistoricalError, match='NO_INSTANCE_ISSUED_HISTORICAL_APPROVAL'):
        fresh.recover()
    assert not fresh.ready
    with fresh.store.transaction() as db:
        assert get(db, 'account', 'account')['quarantined']
        assert not p.store.settings(db).allow_fixtures
        assert not hrows(db, 'history_approvals')
        assert get(db, 'positions', 'component-existing')['checkpoint']['seed']['plan']['admission_result'] == 'REJECT'
    conservation(p)
