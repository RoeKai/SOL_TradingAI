"""Self-authored 8A R1 review reproductions, not the reviewer's attachment.

All cases use the real project configuration, Store.create and fixture_entry
through the existing ``paper`` fixture. No mocked configuration or Broker.
"""
from decimal import Decimal as D
import json
import subprocess
import sys

import pytest

from app.offline_paper.broker import inventory
from app.offline_paper.engine import OfflinePaper
from app.offline_paper.fixtures import cancel_entry, opening, quote
from app.offline_paper.storage import Store, get, put, rows
from test_offline_paper import paper, conservation, ROOT


def reopen(paper):
    return OfflinePaper(Store(paper.store.paths.root, 'test-run', 'offline-paper-demo'))


def accepted_entry(paper, side):
    quote(paper, '100', 'entry-price')
    entry = paper.fixture_entry(opening(paper, side))
    paper.pump()
    assert not paper.summary()['positions']
    return entry


def entry_status(paper, entry, key, status, quantity):
    with paper.store.transaction() as db:
        pid = get(db, 'broker_orders', entry)['action']['position_id']
    paper.broker.fixture_delivery(key, dict(kind='ENTRY_STATUS', position_id=pid,
        action_id=entry, status=status, cumulative_filled_quantity=quantity))
    paper.deliver('fixture-fault:' + key)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('already_delivered', ['0', '.2'])
@pytest.mark.parametrize('complete', [False, True])
def test_recover_entry_facts_without_first_or_subsequent_delivery(paper, side, already_delivered, complete):
    entry = accepted_entry(paper, side)
    previous = D(already_delivered)
    if previous:
        paper.broker.fill(entry, previous, 'first-delivered')
        paper.pump()
    missing = D('.5') - previous if complete else D('.1')
    paper.broker.fill(entry, missing, 'durable-but-unnotified',
        defer_details=True, defer_receipt=True)
    with paper.store.transaction() as db:
        assert not any(not e['consumed'] for _, e in rows(db, 'broker_events'))
        pid = get(db, 'broker_orders', entry)['action']['position_id']
        expected_inventory = inventory(db, pid)
        expected_fees = sum((D(f['fee_usdt']) for _, f in rows(db, 'broker_fills')), D(0))
        assert get(db, 'outbox', entry)['status'] == 'CONFIRMED'
    for _ in range(2):
        paper = reopen(paper)
        result = paper.recover()
        actual_inventory = sum((D(s['remaining_quantity']) for s in result['positions'].values()), D(0))
        assert actual_inventory == expected_inventory  # accepted != fully settled
        assert result['counts']['confirmed_ledger_fills'] == (2 if previous else 1)
        assert D(result['fees_usdt']) == expected_fees
        assert D(result['account']['cash']) == D('500') - expected_fees
        state = result['positions'][pid]
        assert D(state['protection_covered_quantity']) == expected_inventory
        assert state['protection_status'] == 'ACTIVE'
        assert len([o for o in result['orders'].values() if o['action']['kind'] == 'ENTRY']) == 1
        assert paper.ready and result['account']['reconciliation_clear']
        assert not result['reservations'][pid]['released']
        conservation(paper)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_terminal_zero_then_larger_nonterminal_ack_reholds_risk_and_quarantines(paper, side):
    entry = accepted_entry(paper, side)
    cancel_entry(paper, entry)
    paper.pump()
    initial = paper.summary()
    pid = next(iter(initial['reservations']))
    assert initial['reservations'][pid]['entry_sealed']
    assert initial['reservations'][pid]['released']
    entry_status(paper, entry, 'late-larger', 'ACCEPTED', '.2')
    for runner in (paper, reopen(paper)):
        if runner is not paper:
            runner.recover()
        result = runner.summary()
        r = result['reservations'][pid]
        assert D(r['entry_high_water']) == D('.2')
        assert r['entry_terminal'] == 'CANCELED'
        assert D(r['entry_terminal_quantity']) == 0
        assert result['account']['quarantined']
        assert not result['account']['reconciliation_clear']
        assert not runner.ready
        assert not r['released']
        assert D(result['risk_snapshot']['reserved_risk_usdt']) == 5
        assert not result['positions'] and not result['fills']  # ACK is not a fill
        assert result['pending_reconciliation']


def cancelled_with_deferred_fill(paper, side):
    entry = accepted_entry(paper, side)
    fill_id = paper.broker.fill(entry, '.2', 'before-cancel', defer_details=True, defer_receipt=True)
    cancel = cancel_entry(paper, entry)
    paper.broker.execute(cancel)
    with paper.store.transaction() as db:
        terminal = next(k for k, e in rows(db, 'broker_events') if not e['consumed']
            and e['payload']['kind'] == 'ENTRY_STATUS')
        detail = next(k for k, e in rows(db, 'broker_events') if e['payload']['kind'] == 'ENTRY_FILL')
    return entry, fill_id, terminal, detail


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('order', [
    ('terminal', 'smaller', 'detail'),
    ('detail', 'terminal', 'smaller'),
    ('known', 'smaller', 'terminal', 'detail'),
    ('terminal', 'known', 'smaller', 'detail'),
])
def test_terminal_ack_detail_permutations_and_duplicates(paper, side, order):
    entry, fill_id, terminal, detail = cancelled_with_deferred_fill(paper, side)
    for operation in order:
        for _ in range(2):  # Same delivery ID is idempotent as well as same fill ID.
            if operation in ('known', 'smaller'):
                entry_status(paper, entry, operation, 'ACCEPTED', '.2' if operation == 'known' else '0')
            else:
                paper.deliver(terminal if operation == 'terminal' else detail)
        result = paper.summary(); r = next(iter(result['reservations'].values()))
        assert D(r['entry_high_water']) == D('.2')
        assert not r['released']
        assert not result['account']['quarantined']
        if fill_id not in result['fills']:
            assert not r['entry_sealed']
            assert r['entry_reconciliation_required']
            assert not result['account']['reconciliation_clear']
            assert result['pending_reconciliation']
            assert not result['positions']
    paper.pump()
    before = paper.summary()
    assert len(before['fills']) == 1
    r = next(iter(before['reservations'].values()))
    assert r['entry_sealed'] and not r['entry_reconciliation_required']
    assert not r['entry_faults']
    assert r['entry_terminal'] == 'CANCELED' and D(r['entry_terminal_quantity']) == D('.2')
    # Fresh delivery IDs carry the same durable fill; no second fee or position.
    paper.broker.reconcile(entry, 'same-facts-new-deliveries'); paper.pump()
    paper = reopen(paper); after = paper.recover()
    assert paper.ready
    assert after['fills'] == before['fills']
    assert after['account']['cash'] == before['account']['cash']
    assert D(next(iter(after['positions'].values()))['protection_covered_quantity']) == D('.2')
    conservation(paper)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('restart_before_details', [False, True])
def test_conflicting_terminal_does_not_erase_or_block_real_late_details(paper, side, restart_before_details):
    entry, fill_id, terminal, detail = cancelled_with_deferred_fill(paper, side)
    # A false/old terminal 0 arrives before the genuine terminal .2 and details.
    # Broker fact storage is unchanged; no fabricated fill is supplied here.
    entry_status(paper, entry, 'bad-terminal', 'CANCELED', '0')
    initial = paper.summary(); pid = next(iter(initial['reservations']))
    assert not initial['reservations'][pid]['entry_sealed']
    assert not initial['reservations'][pid]['released']
    assert initial['account']['quarantined'] and not initial['fills']
    entry_status(paper, entry, 'late-known', 'ACCEPTED', '.2')
    if restart_before_details:
        paper = reopen(paper); paper.recover()
    else:
        paper.deliver(detail); paper.deliver(terminal); paper.pump()
    for _ in range(2):
        result = paper.summary(); r = result['reservations'][pid]
        assert r['entry_terminal'] == 'CANCELED' and D(r['entry_terminal_quantity']) == 0
        assert D(r['entry_high_water']) == D('.2')
        assert len(result['fills']) == 1 and fill_id in result['fills']
        assert D(result['positions'][pid]['remaining_quantity']) == D('.2')
        assert D(result['positions'][pid]['protection_covered_quantity']) == D('.2')
        assert result['positions'][pid]['protection_status'] == 'ACTIVE'
        assert not r['released'] and not paper.ready
        assert result['account']['quarantined'] and result['pending_reconciliation']
        assert D(result['risk_snapshot']['reserved_risk_usdt']) == 5
        conservation(paper)
        paper = reopen(paper); paper.recover()


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('quantity', [None, '0', '.2'])
def test_unknown_entry_evidence_waits_for_original_order_query(paper, side, quantity):
    entry = accepted_entry(paper, side)
    fid = paper.broker.fill(entry, '.2', 'unknown-detail', defer_details=True, defer_receipt=True)
    entry_status(paper, entry, 'unknown', 'UNKNOWN', quantity)
    first = paper.summary(); r = next(iter(first['reservations'].values()))
    assert r['entry_status_unknown'] and r['entry_reconciliation_required']
    assert not first['fills'] and not r['released']
    assert D(r['entry_high_water']) == (D('.2') if quantity == '.2' else D(0))
    entry_status(paper, entry, 'old-zero', 'ACCEPTED', '0')
    # Even if UNKNOWN/0 is followed by ACK/0, the local .2 Broker fact still
    # prevents readiness; neither receipt can stand in for its missing detail.
    assert not paper.summary()['account']['reconciliation_clear']
    paper.pump()
    assert list(paper.summary()['fills']) == [fid]
    assert paper.ready
    conservation(paper)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_unavailable_entry_details_persist_not_ready_then_recover(paper, side, monkeypatch):
    from app.offline_paper.broker import Broker
    entry = accepted_entry(paper, side)
    paper.broker.fill(entry, '.5', 'missing-detail', defer_details=True, defer_receipt=True)
    original = Broker._details
    monkeypatch.setattr(Broker, '_details', lambda *args: None)  # delivery outage, NOT a config mock
    for _ in range(2):
        paper = reopen(paper); result = paper.recover()
        assert not paper.ready and not result['account']['reconciliation_clear']
        assert not result['fills'] and result['pending_reconciliation']
        r = next(iter(result['reservations'].values()))
        assert D(r['entry_high_water']) == D('.5')
        assert not r['entry_sealed'] and not r['released']
        assert D(result['risk_snapshot']['reserved_risk_usdt']) == 5
    monkeypatch.setattr(Broker, '_details', original)
    paper = reopen(paper); result = paper.recover()
    assert paper.ready and len(result['fills']) == 1
    assert not result['pending_reconciliation']
    assert len([o for o in result['orders'].values() if o['action']['kind'] == 'ENTRY']) == 1
    conservation(paper)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
@pytest.mark.parametrize('already_delivered', ['0', '.2'])
def test_real_process_dies_after_unnotified_fill_and_cli_recovers_twice(paper, side, already_delivered):
    entry = accepted_entry(paper, side)
    if D(already_delivered):
        paper.broker.fill(entry, already_delivered, 'delivered'); paper.pump()
    code = '''
import os, sys
from app.offline_paper.storage import Store
from app.offline_paper.broker import Broker
store = Store(sys.argv[1], 'test-run', 'offline-paper-demo')
Broker(store).fill(sys.argv[2], sys.argv[3], 'process-death', defer_details=True, defer_receipt=True)
os._exit(91)
'''
    result = subprocess.run([sys.executable, '-c', code, str(paper.store.paths.root), entry,
        str(D('.5') - D(already_delivered))], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 91, (result.stdout, result.stderr)
    previous = None
    for _ in range(2):
        result = subprocess.run([sys.executable, '-m', 'app.offline_paper.cli', 'resume',
            '--workspace', str(paper.store.paths.root), '--run', 'test-run'],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (result.stdout, result.stderr)
        data = json.loads(result.stdout)
        s = next(iter(data['positions'].values()))
        assert D(s['remaining_quantity']) == D('.5')
        assert D(s['protection_covered_quantity']) == D('.5')
        assert data['account']['reconciliation_clear']
        assert len([o for o in data['orders'].values() if o['action']['kind'] == 'ENTRY']) == 1
        if previous is not None:
            assert data['fills'] == previous['fills'] and data['account']['cash'] == previous['account']['cash']
        previous = data


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_missing_original_order_quarantines_and_never_reissues_entry(paper, side):
    entry = accepted_entry(paper, side)
    # Deliberate corruption of ONLY the test's synthetic DB. Runtime must not
    # "repair" it by executing an accepted entry intent a second time.
    with paper.store.transaction() as db:
        db.execute('DELETE FROM broker_orders WHERE id=?', (entry,))
        before = len(rows(db, 'broker_commands'))
    paper = reopen(paper); result = paper.recover()
    assert not paper.ready and result['account']['quarantined']
    assert result['pending_reconciliation'] and not result['orders'] and not result['fills']
    r = next(iter(result['reservations'].values()))
    assert not r['released'] and 'ENTRY_ORIGINAL_ORDER_UNRESOLVED' in r['entry_faults']
    with paper.store.transaction() as db:
        assert len([x for _, x in rows(db, 'broker_commands') if x == get(db, 'broker_commands', entry)]) == 1
        assert len(rows(db, 'broker_commands')) >= before  # queries, never another ENTRY


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_recovery_does_not_claim_ready_before_new_protection_is_confirmed(paper, side, monkeypatch):
    from app.offline_paper.broker import Broker
    entry = accepted_entry(paper, side)
    paper.broker.fill(entry, '.5', 'unnotified', defer_details=True, defer_receipt=True)
    original = Broker._snapshot
    def withhold_stop_receipt(self, db, order, cause):
        if order['action']['kind'] == 'ARM_STOP': return
        return original(self, db, order, cause)
    monkeypatch.setattr(Broker, '_snapshot', withhold_stop_receipt)
    paper = reopen(paper); result = paper.recover()
    assert len(result['fills']) == 1
    s = next(iter(result['positions'].values()))
    assert D(s['remaining_quantity']) == D('.5')
    assert s['protection_status'] != 'ACTIVE'
    assert not paper.ready and not result['account']['reconciliation_clear']
    assert result['pending_actions']
    monkeypatch.setattr(Broker, '_snapshot', original)
    paper = reopen(paper); result = paper.recover()
    assert paper.ready
    assert D(next(iter(result['positions'].values()))['protection_covered_quantity']) == D('.5')
    assert len(result['fills']) == 1
    conservation(paper)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_baseline_reservations_without_new_diagnostics_are_derived_not_reset(paper, side):
    entry = accepted_entry(paper, side)
    paper.broker.fill(entry, '.5', 'baseline-unnotified', defer_details=True, defer_receipt=True)
    with paper.store.transaction() as db:
        pid, r = rows(db, 'reservations')[0]
        for field in ('entry_status_unknown', 'entry_faults', 'entry_reconciliation_required', 'entry_pending_reasons'):
            r.pop(field, None)
        put(db, 'reservations', pid, r)
        identity = get(db, 'identity', 'identity')
    paper = reopen(paper); result = paper.recover()
    assert paper.ready
    assert len(result['fills']) == 1
    assert D(result['positions'][pid]['remaining_quantity']) == D('.5')
    assert result['reservations'][pid]['entry_sealed']
    assert D(result['account']['cash']) < D('500')  # no balance reinitialization
    with paper.store.transaction() as db:
        assert get(db, 'identity', 'identity') == identity
    conservation(paper)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_inflight_entry_with_broker_order_but_missing_command_is_not_resubmitted(paper, side):
    entry = accepted_entry(paper, side)
    paper.broker.fill(entry, '.2', 'fact-before-corruption', defer_details=True, defer_receipt=True)
    with paper.store.transaction() as db:
        before = get(db, 'broker_orders', entry)
        db.execute('DELETE FROM broker_commands WHERE id=?', (entry,))
        intent = get(db, 'outbox', entry); intent['status'] = 'INFLIGHT'; put(db, 'outbox', entry, intent)
    paper = reopen(paper); result = paper.recover()
    assert not paper.ready and result['account']['quarantined']
    assert result['orders'][entry] == before  # never overwrite original cumulative .2 with 0
    assert len(result['fills']) == 1
    assert not next(iter(result['reservations'].values()))['released']
    with paper.store.transaction() as db:
        assert get(db, 'broker_commands', entry) is None  # evidence is retained, not "repaired"
    conservation(paper)
