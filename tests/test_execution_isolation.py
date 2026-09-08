"""Section 9.7 isolation acceptance: synthetic ledger + FakeBridge only."""
import asyncio
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from app.config import Config
from app.execution.engine import ExecutionEngine
from app.execution.bridge_client import BridgeClient, BridgeError
from app.models import MarketState, Position, Signal
from app.portfolio.manager import PortfolioManager


NOW = 1_800_000.0


class Audit:
    def __init__(self):
        self.rows = []

    def emit(self, kind, **payload):
        self.rows.append((kind, payload))


class FakeBridge:
    """No transport, no sockets, no environment, no old database/API dependency."""
    def __init__(self):
        self.calls = []
        self.amount = 0.0
        self.foreign_orders = []
        self.foreign_stops = []
        self.stops = {}
        self.responses = {}
        self.fee_error = False
        self.stop_error = False
        self.stop_adds_external_position = False
        self.next_order_id = 700

    async def call(self, operation, **payload):
        self.calls.append((operation, payload))
        if operation == 'positions':
            return [{'symbol': 'SOLUSDT', 'positionAmt': str(self.amount), 'positionSide': 'BOTH'}]
        if operation == 'open_orders':
            return self.foreign_orders
        if operation == 'open_stops':
            return self.foreign_stops + list(self.stops.values())
        if operation == 'account':
            return {'totalWalletBalance': '500', 'totalMarginBalance': '500', 'availableBalance': '450'}
        if operation == 'position_mode':
            return {'dualSidePosition': False}
        if operation == 'rules':
            return {'stepSize': '.001', 'tickSize': '.01', 'minQty': '.001', 'minNotional': '5'}
        if operation == 'order':
            return self.responses.get(payload.get('client_id'))
        if operation == 'stop':
            if self.stop_error:
                if self.stop_adds_external_position:
                    self.amount += 1
                raise BridgeError('Synthetic stop refusal')
            stop = {'symbol': payload['symbol'], 'side': payload['side'], 'clientAlgoId': payload['client_id'],
                    'algoStatus': 'NEW', 'quantity': payload['quantity']}
            self.stops[payload['client_id']] = stop
            return stop
        if operation == 'query_stop':
            return self.stops.get(payload['client_id'])
        if operation == 'cancel_stop':
            self.stops.pop(payload['client_id'], None)
            return {'algoStatus': 'CANCELED'}
        if operation == 'cancel_order':
            return {'status': 'CANCELED', 'executedQty': '0'}
        if operation == 'place_order':
            assert payload['type'] == 'MARKET'
            assert payload['reduce_only'] is True
            quantity = float(payload['quantity'])
            assert 0 < quantity <= abs(self.amount)
            self.amount += quantity if payload['side'] == 'BUY' else -quantity
            self.next_order_id += 1
            result = response(payload['client_id'], quantity, order_id=str(self.next_order_id), side=payload['side'])
            self.responses[payload['client_id']] = result
            return result
        if operation == 'trades':
            if self.fee_error:
                raise BridgeError('Synthetic fee endpoint failure', ambiguous=True)
            assert 'order_id' not in payload
            match = self.responses.get(payload['client_id'])
            assert match, 'Tests only allow fees for a known synthetic order'
            return [{'orderId': str(match['orderId']), 'symbol': 'SOLUSDT', 'qty': match['executedQty'],
                     'price': match['avgPrice'], 'commissionAsset': 'USDT', 'commission': '0.05'}]
        raise AssertionError(f'Unscoped RPC: {operation}')


def response(oid, quantity=1, *, order_id='501', side='BUY', **overrides):
    return {'clientOrderId': oid, 'symbol': 'SOLUSDT', 'side': side, 'status': 'FILLED',
            'executedQty': str(quantity), 'avgPrice': '100', 'orderId': order_id, 'reduceOnly': side == 'SELL', **overrides}


def signal(position_id='new-signal-1'):
    return Signal(position_id, 'panic_rebound', 'SOLUSDT', 'LONG', 100, 98,
                  [{'price': 102, 'fraction': .5}, {'price': 104, 'fraction': .5}], NOW)


@pytest.fixture
def make_engine(tmp_path):
    portfolios = []

    def make(mode='live', instance_id='sol-isolation-test'):
        root = (tmp_path / f'{mode}-{len(portfolios)}').resolve()
        root.mkdir(mode=0o700)
        marker = root / 'isolation-policy.json'
        marker.write_text(json.dumps({'application': 'sol-ai-trading-system', 'policy_version': 1,
                                      'live_runtime_allowed': False}))
        marker.chmod(0o600)
        portfolio = PortfolioManager(root / 'trades' / mode / 'ledger.sqlite3', mode=mode,
                                     state_root=root, instance_id=instance_id)
        portfolios.append(portfolio)
        # Mock-internal construction only. Production loaders/BridgeClient remain disabled.
        cfg = Config()
        if mode == 'live':
            cfg = cfg.model_copy(update={'dry_run': False, 'live': cfg.live.model_copy(update={
                'enabled': True, 'dedicated_account_confirmed': True, 'confirmation': 'ENABLE_SMALL_CAPITAL_LIVE'})})
        bridge = FakeBridge() if mode == 'live' else None
        engine = ExecutionEngine(cfg, portfolio, Audit(), bridge=bridge, clock=lambda: NOW)
        return engine, portfolio, bridge

    yield make
    for portfolio in portfolios:
        portfolio.close()


def prepare_entry(engine, *, status='SUBMITTING', position_id='new-signal-1', quantity=1):
    sig = signal(position_id)
    oid = engine._client_id(sig.id, 'entry')
    engine._prepare(oid, 'ENTRY', sig.id, {'signal': asdict(sig), 'quantity': quantity}, NOW,
                    identity_parts=(sig.id, 'entry'))
    engine.portfolio.update_order(oid, status)
    return oid


def seed_confirmed_position(engine, bridge, *, quantity=1):
    oid = prepare_entry(engine, quantity=quantity)
    result = response(oid, quantity)
    engine._apply_entry(oid, {**result, 'fee': 0}, NOW)
    bridge.amount = quantity
    bridge.responses[oid] = result
    return oid, engine.portfolio.position('new-signal-1')


def writes(bridge):
    return [(op, data) for op, data in bridge.calls if op in {'place_order', 'stop', 'cancel_order', 'cancel_stop', 'leverage'}]


def test_dry_run_cannot_construct_with_live_client(make_engine):
    engine, portfolio, _ = make_engine('paper')
    spy = FakeBridge()
    with pytest.raises(ValueError, match='Dry run'):
        ExecutionEngine(engine.config, portfolio, Audit(), bridge=spy)
    assert spy.calls == []


def test_dry_run_post_construction_injection_cannot_read_private_accounts(make_engine):
    engine, _, _ = make_engine('paper')
    spy = FakeBridge()
    engine.bridge = spy
    with pytest.raises(BridgeError) as caught:
        asyncio.run(engine.refresh_account())
    assert caught.value.code == 'EXECUTION_ISOLATION_VIOLATION'
    assert spy.calls == []


def test_dry_run_cannot_become_live_by_mutating_config(make_engine):
    engine, _, _ = make_engine('paper')
    engine.config.dry_run = False
    engine.bridge = FakeBridge()
    with pytest.raises(BridgeError):
        asyncio.run(engine._bridge_call('place_order', symbol='SOLUSDT'))
    assert engine.bridge.calls == []


@pytest.mark.parametrize('private_operation', ['account', 'positions', 'place_order', 'cancel_order', 'leverage', 'stop'])
def test_production_bridge_cannot_be_enabled_even_with_config_and_fake_credentials(private_operation):
    spy = SimpleNamespace(post=lambda *a, **k: pytest.fail('No HTTP requests are allowed'))
    with pytest.raises(BridgeError) as caught:
        BridgeClient('http://127.0.0.1:8766', 'fake-isolation-token-000000000', client=spy,
                     config_provider=lambda: {'dry_run': False})
    assert caught.value.code == 'LIVE_ISOLATION_NOT_ACCEPTED'
    bypass_constructor = object.__new__(BridgeClient)
    with pytest.raises(BridgeError) as caught:
        asyncio.run(bypass_constructor.call(private_operation))
    assert caught.value.code == 'LIVE_ISOLATION_NOT_ACCEPTED'


def test_unknown_retries_only_original_current_instance_client_id(make_engine):
    engine, portfolio, bridge = make_engine()
    oid = prepare_entry(engine, status='UNKNOWN')
    asyncio.run(engine.reconcile())
    asyncio.run(engine.reconcile())
    queries = [(op, p) for op, p in bridge.calls if op == 'order']
    assert queries == [('order', {'symbol': 'SOLUSDT', 'client_id': oid})] * 2
    assert portfolio.order(oid)['status'] == 'UNKNOWN'
    assert portfolio.positions() == []
    assert writes(bridge) == []


def test_unknown_cancel_request_only_queries_original_id(make_engine):
    engine, portfolio, bridge = make_engine()
    oid = prepare_entry(engine, status='UNKNOWN')
    asyncio.run(engine.cancel_order(oid))
    assert bridge.calls == [('order', {'symbol': 'SOLUSDT', 'client_id': oid})]
    assert portfolio.order(oid)['status'] == 'UNKNOWN'


def test_restart_recovers_only_current_instance_original_ledger_order(make_engine):
    engine, portfolio, _ = make_engine()
    oid = prepare_entry(engine, status='UNKNOWN')
    root, instance_id = portfolio.paths.root, portfolio.instance_id
    portfolio.close()
    reopened = PortfolioManager(root / 'trades/live/ledger.sqlite3', mode='live', state_root=root, instance_id=instance_id)
    try:
        bridge = FakeBridge()
        resumed = ExecutionEngine(engine.config, reopened, Audit(), bridge=bridge, clock=lambda: NOW)
        asyncio.run(resumed.reconcile())
        assert ('order', {'symbol': 'SOLUSDT', 'client_id': oid}) in bridge.calls
        assert writes(bridge) == []
        assert reopened.positions() == []
    finally:
        reopened.close()


def test_arbitrary_ids_and_old_ledger_rows_are_not_queried_or_imported(make_engine):
    engine, portfolio, bridge = make_engine()
    portfolio.prepare('old-copy-client-id', 'ENTRY', 'old-position', {'quantity': 50, 'signal': asdict(signal('old-position'))}, NOW)
    portfolio.update_order('old-copy-client-id', 'UNKNOWN')
    asyncio.run(engine._query_order('unknown-external-id'))
    asyncio.run(engine.reconcile())
    assert not any(op in ('order', 'trades') for op, _ in bridge.calls)
    assert portfolio.order('old-copy-client-id')['status'] == 'UNKNOWN'
    assert portfolio.positions() == []
    assert writes(bridge) == []


def test_client_order_ids_are_instance_specific(make_engine):
    first, _, _ = make_engine(instance_id='sol-alpha')
    second, _, _ = make_engine(instance_id='sol-beta')
    assert first._client_id('same-signal', 'entry') != second._client_id('same-signal', 'entry')
    assert len(first._client_id('same-signal', 'entry')) <= 36


@pytest.mark.parametrize('mismatch', [{'clientOrderId': 'old-copy-client-id'}, {'symbol': 'ETHUSDT'}, {'side': 'SELL'}])
def test_mismatched_response_never_becomes_owned_exposure(make_engine, mismatch):
    engine, portfolio, bridge = make_engine()
    oid = prepare_entry(engine)
    result = response(oid)
    result.update(mismatch)
    with pytest.raises(BridgeError):
        asyncio.run(engine._consume_order(oid, result))
    assert portfolio.positions() == []
    assert bridge.calls == []


def test_foreign_open_order_and_position_are_detected_not_adopted(make_engine):
    engine, portfolio, bridge = make_engine()
    bridge.amount = 8
    bridge.foreign_orders = [{'symbol': 'SOLUSDT', 'clientOrderId': 'copy-system-order'}]
    bridge.foreign_stops = [{'symbol': 'SOLUSDT', 'clientAlgoId': 'copy-system-stop'}]
    asyncio.run(engine.audit_external_state())
    assert engine.halt_reason
    assert portfolio.positions() == []
    assert portfolio.order('copy-system-order') is None
    assert writes(bridge) == []


def test_foreign_order_and_stop_cannot_be_cancelled(make_engine):
    engine, _, bridge = make_engine()
    asyncio.run(engine.cancel_order('copy-order'))
    asyncio.run(engine._cancel_stop('copy-stop', 'SOLUSDT'))
    assert bridge.calls == []


def test_position_row_without_owned_confirmed_entry_cannot_be_closed_or_protected(make_engine):
    engine, portfolio, bridge = make_engine()
    p = Position('old-position', 'copy-strategy', 'SOLUSDT', 'LONG', 100, 1, 1, 98, [], NOW, 5)
    portfolio.save_position(p)
    bridge.amount = 1
    asyncio.run(engine._protect(p.id))
    asyncio.run(engine._close_unlocked(p, 'stop_failed'))
    assert bridge.calls == []
    assert portfolio.position(p.id).quantity == 1


@pytest.mark.parametrize('external', ['extra_position', 'foreign_order', 'foreign_stop'])
def test_matching_symbol_foreign_exposure_prevents_protection_and_close(make_engine, external):
    engine, _, bridge = make_engine()
    _, p = seed_confirmed_position(engine, bridge)
    if external == 'extra_position':
        bridge.amount = 2
    elif external == 'foreign_order':
        bridge.foreign_orders = [{'symbol': 'SOLUSDT', 'clientOrderId': 'other-system'}]
    else:
        bridge.foreign_stops = [{'symbol': 'SOLUSDT', 'clientAlgoId': 'other-system'}]
    asyncio.run(engine._protect(p.id))
    asyncio.run(engine._close_unlocked(p, 'stop_failed'))
    assert writes(bridge) == []
    assert engine.halt_reason


def test_stop_failure_immediately_exits_confirmed_owned_fill_before_fee_query(make_engine):
    engine, portfolio, bridge = make_engine()
    oid = prepare_entry(engine)
    bridge.amount = 1
    bridge.responses[oid] = response(oid)
    bridge.stop_error = True
    asyncio.run(engine._consume_order(oid, response(oid)))
    operations = [op for op, _ in bridge.calls]
    assert operations.index('stop') < operations.index('place_order') < operations.index('trades')
    exits = [payload for op, payload in bridge.calls if op == 'place_order']
    assert len(exits) == 1
    assert exits[0]['reduce_only'] is True
    assert exits[0]['type'] == 'MARKET'
    assert exits[0]['quantity'] == '1.0'
    assert exits[0]['side'] == 'SELL'
    assert portfolio.positions() == []
    assert bridge.amount == 0
    assert portfolio.all_trades()[0]['fees'] == pytest.approx(.1)


def test_fee_request_failure_does_not_delay_confirmed_stop_protection(make_engine):
    engine, portfolio, bridge = make_engine()
    oid = prepare_entry(engine)
    bridge.amount = 1
    bridge.responses[oid] = response(oid)
    bridge.fee_error = True
    asyncio.run(engine._consume_order(oid, response(oid)))
    operations = [op for op, _ in bridge.calls]
    assert operations.index('stop') < operations.index('query_stop') < operations.index('trades')
    p = portfolio.position('new-signal-1')
    assert p.stop_status == 'ACTIVE'
    assert portfolio.order(oid)['status'] == 'FILLED'
    assert oid in portfolio.get_meta('fee_pending_orders')
    assert not any(op == 'place_order' for op, _ in bridge.calls)
    bridge.fee_error = False
    asyncio.run(engine.reconcile())
    assert portfolio.get_meta('fee_pending_orders') == {}
    assert portfolio.position('new-signal-1').fees == pytest.approx(.05)


def test_stop_failure_cannot_close_when_ownership_becomes_uncertain(make_engine):
    engine, portfolio, bridge = make_engine()
    oid = prepare_entry(engine)
    bridge.amount = 1
    bridge.responses[oid] = response(oid)
    bridge.stop_error = True
    bridge.stop_adds_external_position = True
    asyncio.run(engine._consume_order(oid, response(oid)))
    assert not any(op == 'place_order' for op, _ in bridge.calls)
    assert portfolio.position('new-signal-1').quantity == 1
    assert bridge.amount == 2
    assert 'EXTERNAL_POSITION' in engine.halt_reason


def test_unknown_emergency_exit_never_resubmits_a_second_exit(make_engine):
    engine, portfolio, bridge = make_engine()
    _, p = seed_confirmed_position(engine, bridge)
    original = bridge.call

    async def ambiguous_exit(operation, **payload):
        if operation == 'place_order':
            bridge.calls.append((operation, payload))
            raise BridgeError('Synthetic lost POST response', ambiguous=True)
        return await original(operation, **payload)

    bridge.call = ambiguous_exit
    asyncio.run(engine._close_unlocked(p, 'stop_failed'))
    exit_order = next(o for o in portfolio.pending() if o['kind'] == 'EXIT')
    assert exit_order['status'] == 'UNKNOWN'
    asyncio.run(engine._close_unlocked(p, 'stop_failed'))
    asyncio.run(engine._query_order(exit_order['client_id']))
    assert len([op for op, _ in bridge.calls if op == 'place_order']) == 1
    assert ('order', {'symbol': 'SOLUSDT', 'client_id': exit_order['client_id']}) in bridge.calls


def test_native_stop_child_is_halted_not_imported_or_queried_by_bare_id(make_engine):
    engine, portfolio, bridge = make_engine()
    _, p = seed_confirmed_position(engine, bridge)
    asyncio.run(engine._protect(p.id))
    p = portfolio.position(p.id)
    bridge.stops[p.stop_id].update(algoStatus='FINISHED', actualOrderId='old-or-unclaimed-child-999')
    before_orders = portfolio.db.execute('SELECT count(*) FROM orders').fetchone()[0]
    bridge.calls.clear()
    asyncio.run(engine.reconcile())
    assert engine.halt_reason == 'NATIVE_STOP_CHILD_RECONCILIATION_NOT_ACCEPTED'
    assert portfolio.db.execute('SELECT count(*) FROM orders').fetchone()[0] == before_orders
    assert portfolio.position(p.id).quantity == 1
    assert portfolio.all_trades() == []
    assert writes(bridge) == []
    assert not any(op in ('order', 'trades') for op, _ in bridge.calls)
    assert all('order_id' not in payload for _, payload in bridge.calls)
    assert any(kind == 'api_error' and row.get('operation') == 'native_stop_child' for kind, row in engine.audit.rows)


def test_paper_execution_and_stop_exit_have_no_private_client(make_engine):
    engine, portfolio, bridge = make_engine('paper')
    market = MarketState('SOLUSDT', NOW, 100, {'1m': 0, '3m': 0, '5m': 0, '15m': 0},
                         0, 1, .5, 99, 101, 0, 0, True, False, 99.99, 100.01)
    decision = asyncio.run(engine.submit(signal(), market))
    assert decision.allowed
    assert bridge is None
    assert portfolio.positions()[0].stop_status == 'ACTIVE'
    market.price, market.bid, market.ask = 97, 96.99, 97.01
    asyncio.run(engine.on_market(market))
    assert portfolio.positions() == []
    assert portfolio.all_trades()[0]['reason'] == 'stop_loss'


def test_fee_failures_never_block_emergency_exit_and_later_reconcile_is_idempotent(make_engine):
    engine, portfolio, bridge = make_engine()
    oid = prepare_entry(engine)
    bridge.amount = 1
    bridge.responses[oid] = response(oid)
    bridge.stop_error = True
    bridge.fee_error = True
    asyncio.run(engine._consume_order(oid, response(oid)))
    assert portfolio.positions() == []
    assert len(portfolio.get_meta('fee_pending_orders')) == 2
    assert bridge.amount == 0
    bridge.fee_error = False
    asyncio.run(engine.reconcile())
    asyncio.run(engine.reconcile())
    assert portfolio.get_meta('fee_pending_orders') == {}
    assert portfolio.all_trades()[0]['fees'] == pytest.approx(.1)
    assert portfolio.balance == pytest.approx(499.9)
    assert len([op for op, _ in bridge.calls if op == 'place_order']) == 1
