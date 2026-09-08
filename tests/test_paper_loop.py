"""Full raw-market -> strategy -> risk -> paper -> ledger -> report acceptance.

Synthetic messages and fake transports only; no validation orders or live API.
"""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import socket

import pytest
import yaml
from fastapi.testclient import TestClient

from app.runtime import PaperRuntime
from app.config import ROOT
from app.models import MarketRules
from app.strategies.engine import StrategyEngine
from app.dashboard.server import create_app
from app.execution.bridge_client import BridgeClient, BridgeError
from app.data.service import DataService
from app.utils.paths import IsolationError
from test_market_strategies import SYMBOLS, NOW, candle, tick, state
from test_execution_isolation import signal


class Clock:
    def __init__(self):
        self.now = NOW
    def __call__(self):
        return self.now


class OfflineFeed:
    def __init__(self, symbols, on_event, **kwargs):
        self.on_event = on_event
        self.receipts = {}
        self.stopped = False
    def status(self):
        return {name: {'connected': True, 'error': None} for name in ('market', 'public')}
    async def run(self):
        await asyncio.Event().wait()
    async def stop(self):
        self.stopped = True


@pytest.fixture
def module_root(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('Offline paper acceptance attempted networking')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    root = tmp_path / 'standalone-sol'
    root.mkdir(mode=0o700)
    (root / 'isolation-policy.json').write_text((ROOT / 'isolation-policy.json').read_text())
    cfg = yaml.safe_load((ROOT / 'config.yaml').read_text())
    cfg['dashboard']['require_auth'] = False
    (root / 'config.yaml').write_text(yaml.safe_dump(cfg))
    env = root / '.env'
    env.write_text('BINANCE_API_KEY=FAKE_DO_NOT_USE\nBINANCE_API_SECRET=FAKE_SECRET_NEVER_USE\n'
                   'SOL_DASHBOARD_TOKEN=offline-dashboard-token-123456\n')
    env.chmod(0o600)
    return root


def runtime(root, strategy=None):
    if strategy:
        cfg = yaml.safe_load((root / 'config.yaml').read_text())
        cfg['strategies'] = {name: {'enabled': name == strategy} for name in StrategyEngine.NAMES}
        (root / 'config.yaml').write_text(yaml.safe_dump(cfg))
    return PaperRuntime(root, clock=Clock(), data_factory=OfflineFeed)


async def rules(r):
    await r.on_event({'e': 'exchange_rules', 's': 'SOLUSDT', 'rules': {'filters': [
        {'filterType': 'LOT_SIZE', 'stepSize': '.001', 'minQty': '.001'},
        {'filterType': 'MARKET_LOT_SIZE', 'stepSize': '.001', 'minQty': '.001'},
        {'filterType': 'PRICE_FILTER', 'tickSize': '.01'},
        {'filterType': 'MIN_NOTIONAL', 'notional': '5'}]}})


async def quote(r, price, *, symbol='SOLUSDT', pressure=.65, advance=1):
    r.clock.now += advance
    now = r.clock()
    await r.on_event({'e': 'depthUpdate', 's': symbol, 'E': int(now*1000), 'u': int(now*1000),
                     'b': [[str(price-.01), str(pressure*100)]],
                     'a': [[str(price+.01), str((1-pressure)*100)]]})
    await r.on_event(tick(symbol, now, price))


async def seed(r, strategy='panic_rebound'):
    await rules(r)
    for symbol in SYMBOLS:
        for offset in range(21, 0, -1):
            price = 98 if symbol == 'SOLUSDT' and strategy in ('trend_breakout', 'pullback_entry') and offset >= 4 else 100
            await r.on_event(candle(symbol, int(NOW/60)-offset, price,
                volume=20 if offset == 1 else 10))
    # Warmup cannot trade. All three reference feeds + own book must be fresh.
    assert not r.portfolio.positions()
    for symbol in ('BTCUSDT', 'ETHUSDT', 'SOLUSDT'):
        await quote(r, 100, symbol=symbol)
    assert r.market().ready


async def trigger(r, strategy):
    if strategy == 'panic_rebound':
        await quote(r, 97)
        assert r.strategies.status()[strategy]['state'] == 'observing'
        await quote(r, 98)
    elif strategy == 'trend_breakout':
        await quote(r, 100.2)
    elif strategy == 'pullback_entry':
        await quote(r, 98.8)
        await quote(r, 99.1)
    else:
        await quote(r, 101, pressure=.4)
        await quote(r, 99.8, pressure=.4)


@pytest.mark.asyncio
@pytest.mark.parametrize('strategy', StrategyEngine.NAMES)
async def test_raw_market_to_all_four_paper_strategies_report(module_root, strategy):
    r = runtime(module_root, strategy)
    await r.start(connect=False, background=False)
    try:
        assert 'BINANCE_API_KEY' not in r.controls
        assert r.execution.bridge is None
        await seed(r, strategy)
        await trigger(r, strategy)
        positions = r.portfolio.positions()
        assert len(positions) == 1, r.strategies.status()
        p = positions[0]
        assert p.strategy == strategy and p.stop_status == 'ACTIVE'
        assert p.side == ('SHORT' if strategy == 'fake_breakout_reverse' else 'LONG')
        entries = [o for o in r.portfolio.recent_orders() if o['kind'] == 'ENTRY']
        assert len(entries) == 1 and entries[0]['status'] == 'FILLED'
        intent = r.portfolio.order(entries[0]['client_id'])['payload']['signal']
        assert intent['score'] >= 60
        log = r.audit.recent(500)
        assert sum(e['event'] == 'risk_approved' for e in log) == 2  # under-lock second risk check
        assert r.portfolio.metrics(r.clock())['margin_used'] <= 100
        original = p.quantity
        await quote(r, p.take_profits[0]['price'] + (.02 if p.side == 'LONG' else -.02))
        remaining = r.portfolio.positions()[0]
        assert 0 < remaining.quantity < original
        assert remaining.tp_completed == [0]
        await quote(r, p.take_profits[1]['price'] + (.02 if p.side == 'LONG' else -.02))
        assert r.portfolio.positions() == []
        trade = r.portfolio.all_trades()[0]
        assert trade['strategy'] == strategy and trade['mode'] == 'paper'
        assert trade['fees'] > 0 and trade['holding_seconds'] > 0
        assert r.portfolio.balance == pytest.approx(500 + trade['net_pnl'])
        assert sum(o['applied_qty'] for o in r.portfolio.recent_orders() if o['kind'] == 'EXIT') == pytest.approx(original)
        text = (await r.generate_report()).read_text()
        assert all(name in text for name in StrategyEngine.NAMES)
        assert '完成交易：1 笔' in text and '含浮盈采样最大回撤' in text
        safety = r.snapshot()['safety']
        assert safety['production_live_sealed'] and not safety['private_client_constructed']
        assert safety['private_requests'] == safety['real_orders'] == 0
        assert safety['network_receipts'] == {}
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_fake_downward_breakout_long(module_root):
    r = runtime(module_root, 'fake_breakout_reverse')
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        await quote(r, 99)
        await quote(r, 100.1)
        assert r.portfolio.positions()[0].side == 'LONG'
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_pause_and_reference_disconnect_still_stop_owned_sol(module_root):
    r = runtime(module_root, 'panic_rebound')
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        await trigger(r, 'panic_rebound')
        p = r.portfolio.positions()[0]
        await r.pause()
        r.clock.now += 20  # BTC/ETH and SOL now stale.
        await r.heartbeat()
        assert len(r.portfolio.positions()) == 1  # No imaginary stale stop fill.
        await quote(r, p.stop_price-2)
        assert not r.market().ready  # reference still stale; exit must not wait for BTC.
        assert not r.portfolio.positions()
        trade = r.portfolio.all_trades()[0]
        assert trade['reason'] == 'stop_loss' and trade['exit_price'] < p.stop_price-2
        assert trade['net_pnl'] < 0
        assert r.execution.paused
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_missing_public_rules_blocks_new_entry(module_root):
    r = runtime(module_root, 'panic_rebound')
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        r.rules_ready = r.execution.ready = False
        await trigger(r, 'panic_rebound')
        assert r.portfolio.positions() == []
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_limits_btc_pause_and_daily_loss_are_not_waived(module_root):
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        s = signal()
        s.created_at = r.clock()
        market = r.market()
        for name, mutate in [
            ('BTC_CRASH_OR_MISSING_FILTER', lambda: replace(market, btc_return_3m=-.9)),
            ('MARKET_STALE_OR_WARMUP', lambda: replace(market, stale=True)),
        ]:
            result = await r.execution.submit(s, mutate())
            assert not result.allowed and name in result.reasons
        await r.pause()
        denied = await r.execution.submit(s, market)
        assert 'OPERATOR_PAUSED' in denied.reasons
        await r.resume()
        r.portfolio.add_cash('simulated-day-loss', -21, r.clock())
        denied = await r.execution.submit(s, market)
        assert 'DAILY_LOSS_HALT' in denied.reasons
        assert await r.resume() is False
        assert r.portfolio.recent_orders() == []
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_restart_persists_state_and_cooldown_without_credentials(module_root):
    r = runtime(module_root, 'panic_rebound')
    await r.start(connect=False, background=False)
    await seed(r)
    await trigger(r, 'panic_rebound')
    p = r.portfolio.positions()[0]
    balance = r.portfolio.balance
    checkpoint = r.strategies.checkpoint()
    await r.pause()
    await r.stop()
    again = runtime(module_root)
    again.clock.now = r.clock() + 1
    await again.start(connect=False, background=False)
    try:
        assert again.execution.paused
        assert again.portfolio.positions()[0].id == p.id
        assert again.portfolio.balance == balance
        assert again.strategies._last_signal == checkpoint['last_signal']
        assert again.strategies.observations == {}
        assert not again.rules_ready  # Never inherit fresh exchange-data authority.
        assert not again.market().ready
        assert again.execution.bridge is None
    finally:
        await again.stop()


@pytest.mark.asyncio
async def test_daily_review_scheduler_once_and_report_symlink_refused(module_root):
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        r.clock.now = datetime.fromisoformat('2026-09-10T00:05:00+08:00').timestamp()
        await r.heartbeat()
        first = sum(x['event'] == 'review_generated' for x in r.audit.recent())
        await r.heartbeat()
        assert sum(x['event'] == 'review_generated' for x in r.audit.recent()) == first
        assert r.portfolio.get_meta('last_scheduled_review') == '2026-09-09'
        target = module_root / 'reports/paper/review-2026-09-10.md'
        sentinel = module_root / 'protected.txt'
        sentinel.write_text('unchanged')
        target.symlink_to(sentinel)
        with pytest.raises(IsolationError):
            await r.generate_report('2026-09-10')
        assert sentinel.read_text() == 'unchanged'
        target.unlink()  # test-created symlink only; allow runtime shutdown report.
    finally:
        await r.stop()


def test_all_four_scored_before_candidate_selection(monkeypatch):
    engine = StrategyEngine({name: {'enabled': True} for name in StrategyEngine.NAMES})
    visited = []
    def proposal(name):
        def run(s, previous, settings):
            visited.append(name)
            return engine._signal(name, s, 99, settings, 'synthetic ranking fixture')
        return run
    for name in StrategyEngine.NAMES:
        monkeypatch.setattr(engine, '_' + name, proposal(name))
    proposals = engine.evaluate(state(volume_ratio=2))
    assert visited == list(StrategyEngine.NAMES)
    assert len(proposals) == 1 and proposals[0].strategy == StrategyEngine.NAMES[0]
    assert all(s['score'] >= 60 and s['eligible'] for s in engine.status().values())
    assert sum(s['selected'] for s in engine.status().values()) == 1


@pytest.mark.parametrize('settings', [
    {'typo_strategy': {'enabled': True}},
    {'trend_breakout': {'min_score': 101}},
    {'trend_breakout': {'min_buy_pressure': 2}},
    {'trend_breakout': {'min_volum_ratio': 2}},
])
def test_bad_strategy_settings_refused(settings):
    with pytest.raises(ValueError):
        StrategyEngine(settings)


@pytest.mark.asyncio
async def test_public_request_allowlist_no_private_calls(module_root, monkeypatch):
    seen = []
    class Client:
        async def get(self, url, **kwargs):
            seen.append((url, kwargs))
            class Response:
                def raise_for_status(self):
                    pass
                def json(self):
                    return []
            return Response()
    service = DataService(SYMBOLS, lambda e: None)
    params = {'symbol': 'SOLUSDT', 'interval': '1m', 'limit': 20}
    await service._public_get(Client(), '/fapi/v1/klines', params)
    assert seen[0][1] == {'params': params}
    for path in ('/fapi/v1/order', '/fapi/v1/order/test', '/fapi/v2/account', '/fapi/v1/userTrades'):
        with pytest.raises(ValueError, match='REFUSED'):
            await service._public_get(Client(), path)
    with pytest.raises(ValueError, match='REFUSED'):
        await service._consume('private', 'wss://fstream.binance.com/private')
    assert len(seen) == 1


def test_runtime_refuses_manual_live_switch_before_writes(module_root):
    cfg = yaml.safe_load((module_root / 'config.yaml').read_text())
    cfg['dry_run'] = False
    cfg['live'].update(enabled=True, dedicated_account_confirmed=True, confirmation='ENABLE_SMALL_CAPITAL_LIVE')
    (module_root / 'config.yaml').write_text(yaml.safe_dump(cfg))
    with pytest.raises(IsolationError, match='LIVE_ISOLATION_NOT_ACCEPTED'):
        runtime(module_root)
    assert not (module_root / 'trades').exists()
    with pytest.raises(BridgeError):
        BridgeClient('http://127.0.0.1:8766', 'fake-token')


def test_dashboard_uses_real_paper_runtime_lifespan(module_root):
    r = runtime(module_root)
    app = create_app(r, r.config)
    with TestClient(app) as client:
        data = client.get('/api/snapshot').json()
        assert set(data['strategies']) == set(StrategyEngine.NAMES)
        assert data['safety']['production_live_sealed']
        assert not data['ready']
        assert client.post('/api/pause').json()['ok']
        assert client.post('/api/resume').json()['ok']
        assert '闭环运行证据' in client.get('/api/report').text
        assert client.post('/api/live').status_code == 404
    assert not r.started and r.data.stopped


@pytest.mark.asyncio
async def test_feed_task_failure_halts_entries(module_root):
    r = runtime(module_root)
    class BrokenFeed(OfflineFeed):
        async def run(self):
            raise RuntimeError('synthetic feed failure')
    r.data_factory = BrokenFeed
    await r.start(background=False)
    try:
        await asyncio.sleep(0)
        assert r.execution.halt_reason == 'PUBLIC_FEED_TASK_FAILED'
        assert await r.resume() is False
    finally:
        await r.stop()


def disable_all(root):
    cfg = yaml.safe_load((root / 'config.yaml').read_text())
    cfg['strategies'] = {name: {'enabled': False} for name in StrategyEngine.NAMES}
    (root / 'config.yaml').write_text(yaml.safe_dump(cfg))


def fresh_signal(r, identity='paper-risk-test', **changes):
    return replace(signal(identity), created_at=r.clock(), **changes)


@pytest.mark.asyncio
async def test_second_risk_check_observes_changed_market(module_root):
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        market = r.market()
        r.execution.state_provider = lambda: replace(market, btc_return_3m=-2)
        decision = await r.execution.submit(fresh_signal(r), market)
        assert not decision.allowed and 'BTC_CRASH_OR_MISSING_FILTER' in decision.reasons
        assert r.portfolio.recent_orders() == []
        assert any(row['event'] == 'risk_approved' for row in r.audit.recent())
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_concurrent_candidates_never_pyramid(module_root):
    disable_all(module_root)
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        results = await asyncio.gather(*(r.execution.submit(fresh_signal(r, str(i)), r.market()) for i in range(10)))
        assert sum(bool(x and x.allowed) for x in results) == 1
        assert len(r.portfolio.positions()) == 1
        assert len([o for o in r.portfolio.recent_orders() if o['kind'] == 'ENTRY']) == 1
        p = r.portfolio.positions()[0]
        await quote(r, p.entry_price-.2)  # A losing position cannot receive an add.
        decision = await r.execution.submit(fresh_signal(r, 'loss-add'), r.market())
        assert not decision.allowed and 'POSITION_EXISTS_NO_ADDING' in decision.reasons
    finally:
        await r.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_price,cycles,reason', [(97.9, 2, 'CONSECUTIVE_LOSS_HALT'), (104.2, 3, 'DAILY_TRADE_LIMIT')])
async def test_trade_count_and_two_losses_persist_through_resume(module_root, exit_price, cycles, reason):
    disable_all(module_root)
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        for i in range(cycles):
            await quote(r, 100)
            decision = await r.execution.submit(fresh_signal(r, f'cycle-{i}'), r.market())
            assert decision.allowed and decision.max_loss <= 5
            await quote(r, exit_price)
            assert not r.portfolio.positions()
        await quote(r, 100)
        denied = await r.execution.submit(fresh_signal(r, 'cycle-denied'), r.market())
        assert not denied.allowed and reason in denied.reasons
        await r.heartbeat()
        assert reason in r.snapshot()['risk']['active_limits']
        assert await r.resume() is False
        assert len(r.portfolio.all_trades()) == cycles
    finally:
        await r.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['fill', 'expiry', 'pause', 'stale_reference', 'rules_unready'])
async def test_limit_entry_lifecycle_no_network(module_root, action):
    disable_all(module_root)
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        s = fresh_signal(r, order_type='LIMIT', entry_price=99.9)
        assert (await r.execution.submit(s, r.market())).allowed
        assert not r.portfolio.positions() and len(r.portfolio.pending()) == 1
        oid = r.portfolio.pending()[0]['client_id']
        if action == 'fill':
            await quote(r, 99.89)
            assert r.portfolio.order(oid)['status'] == 'FILLED'
            assert r.portfolio.positions()[0].entry_price <= 99.9
        else:
            if action == 'expiry':
                r.clock.now += 31
                await r.heartbeat()  # Works without fresh market events.
            elif action == 'pause':
                await r.pause()
            elif action == 'rules_unready':
                r.execution.ready = False
                await quote(r, 99.89)
            else:
                r.clock.now += 16
                await quote(r, 99.89)
            assert r.portfolio.order(oid)['status'] == 'CANCELED'
            assert r.portfolio.positions() == []
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_limit_restart_cancels_instead_of_replaying(module_root):
    disable_all(module_root)
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    await seed(r)
    assert (await r.execution.submit(fresh_signal(r, order_type='LIMIT', entry_price=99.9), r.market())).allowed
    oid = r.portfolio.pending()[0]['client_id']
    await r.stop()
    again = runtime(module_root)
    await again.start(connect=False, background=False)
    try:
        assert again.portfolio.order(oid)['status'] == 'CANCELED'
        assert not again.portfolio.positions()
        assert again.portfolio.metrics(again.clock())['today_trades'] == 1
    finally:
        await again.stop()


@pytest.mark.asyncio
async def test_substep_partial_tp_defers_and_final_closes_everything(module_root):
    disable_all(module_root)
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        r.execution.rules = MarketRules('1', '.01', 1, 5)
        s = fresh_signal(r, take_profits=[{'price': 102, 'fraction': .1}, {'price': 104, 'fraction': .9}])
        assert (await r.execution.submit(s, r.market())).allowed
        p = r.portfolio.positions()[0]
        await quote(r, 102.1)
        current = r.portfolio.positions()[0]
        assert current.quantity == p.quantity
        assert current.tp_completed == [] and current.tp_deferred == [0]
        await quote(r, 104.1)
        assert not r.portfolio.positions()
        assert r.portfolio.all_trades()[0]['reason'] == 'take_profit_2'
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_failed_tp_not_marked_done_and_stop_has_priority(module_root, monkeypatch):
    disable_all(module_root)
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        assert (await r.execution.submit(fresh_signal(r), r.market())).allowed
        original_close = r.execution._close_unlocked
        async def failed(*a, **kw):
            return None
        monkeypatch.setattr(r.execution, '_close_unlocked', failed)
        await quote(r, 102.1)
        assert r.portfolio.positions()[0].tp_completed == []
        monkeypatch.setattr(r.execution, '_close_unlocked', original_close)
        await quote(r, 97)
        assert r.portfolio.all_trades()[0]['reason'] == 'stop_loss'
        assert not any(o['kind'] == 'EXIT' and 'take_profit' in r.portfolio.order(o['client_id'])['payload']['reason']
                       for o in r.portfolio.recent_orders())
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_tp_completion_atomic_with_fill_across_restart(module_root):
    disable_all(module_root)
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    await seed(r)
    assert (await r.execution.submit(fresh_signal(r), r.market())).allowed
    p = r.portfolio.positions()[0]
    # Call the fill transaction without the outer on_market TP-marker code.
    await r.execution._close_unlocked(p, 'take_profit_1', .5, replace(r.market(), price=102.1, bid=102.09, ask=102.11))
    remaining = r.portfolio.positions()[0]
    assert remaining.tp_completed == [0]
    await r.stop()
    again = runtime(module_root)
    again.clock.now = r.clock()
    await again.start(connect=False, background=False)
    try:
        assert again.portfolio.positions()[0].tp_completed == [0]
        assert again.portfolio.positions()[0].quantity == remaining.quantity
    finally:
        await again.stop()


@pytest.mark.parametrize('strategy', StrategyEngine.NAMES)
def test_all_four_respect_disabled_and_high_score_threshold(strategy):
    cfg = {name: {'enabled': False} for name in StrategyEngine.NAMES}
    cfg[strategy] = {'enabled': True, 'min_score': 100}
    e = StrategyEngine(cfg)
    if strategy == 'panic_rebound':
        e.evaluate(state(price=97, r3=-3))
        out = e.evaluate(state(now=NOW+1, price=98, r3=-2, low_3m=97))
    elif strategy == 'trend_breakout':
        e.evaluate(state(high_3m=100, low_3m=99))
        out = e.evaluate(state(now=NOW+1, price=101, volume_ratio=1.3, low_3m=99,
                              returns={'1m': .1, '3m': .2, '5m': .3, '15m': 1}))
    elif strategy == 'pullback_entry':
        e.evaluate(state(price=99, returns={'1m': -.5, '15m': 1}))
        out = e.evaluate(state(now=NOW+1, price=99.4, returns={'1m': 0, '15m': 1}))
    else:
        e.evaluate(state(price=100))
        e.evaluate(state(now=NOW+1, price=101))
        out = e.evaluate(state(now=NOW+2, price=99.8, buy_pressure=.4))
    assert not out
    assert e.status()[strategy]['reason'] == 'score_below_threshold'
    assert all(s['score'] == 0 for name, s in e.status().items() if name != strategy)


@pytest.mark.asyncio
async def test_minute_close_delivery_grace_blocks_entries_without_erasing_observation(module_root):
    r = runtime(module_root, 'panic_rebound')
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        r.clock.now = NOW + 58
        for symbol in ('BTCUSDT', 'ETHUSDT'):
            await quote(r, 100, symbol=symbol, advance=.1)
        await quote(r, 97, advance=.1)
        assert r.strategies.observations
        r.clock.now = NOW + 60.05
        await r.heartbeat()
        assert r.market().reason == 'candle_close_pending' and not r.market().ready
        assert r.strategies.observations
        for symbol in SYMBOLS:
            await r.on_event(candle(symbol, int(NOW / 60), 97 if symbol == 'SOLUSDT' else 100,
                                   closed=True, warmup=False))
        assert r.market().ready
        assert r.strategies.observations
        await quote(r, 98)
        assert r.portfolio.positions()
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_report_failure_cannot_stop_existing_position_protection(module_root, monkeypatch):
    disable_all(module_root)
    r = runtime(module_root)
    await r.start(connect=False, background=False)
    try:
        await seed(r)
        assert (await r.execution.submit(fresh_signal(r), r.market())).allowed
        def fail(*a, **kw):
            raise OSError('synthetic report failure')
        monkeypatch.setattr(r.review, 'write', fail)
        assert await r._safe_report() is None
        assert r.execution.halt_reason == 'REVIEW_FAILED_NEW_ENTRIES_BLOCKED'
        await quote(r, 97.5)
        assert not r.portfolio.positions()
        assert r.portfolio.all_trades()[0]['reason'] == 'stop_loss'
    finally:
        await r.stop()


@pytest.mark.asyncio
async def test_public_capability_refuses_retargeting_and_signature_parameters():
    service = DataService(SYMBOLS, lambda e: None)
    with pytest.raises(ValueError, match='PARAMETERS'):
        await service._public_get(None, '/fapi/v1/klines', {'signature': 'fake'})
    service.rest_base_url = 'https://unrelated.invalid'
    with pytest.raises(ValueError, match='REFUSED'):
        await service._public_get(None, '/fapi/v1/exchangeInfo')
    service.ws_base_url = 'wss://unrelated.invalid'
    with pytest.raises(ValueError, match='REFUSED'):
        await service._consume('public', service.stream_urls()['public'])
    assert service.receipts == {}
