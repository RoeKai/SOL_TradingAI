"""Independent, paper-only trading agent. No private transport is constructed.

Public feeds -> indicators -> all-strategy scores -> two risk gates -> paper
ledger -> daily review. Market callbacks are serialized (bounded WS queues),
and protection runs before scoring. Telegram never blocks this path.
"""
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, date as Date
from decimal import Decimal
import math
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from app.config import ROOT, load_config, load_secrets
from app.data.service import DataService
from app.indicators.engine import IndicatorEngine
from app.strategies.engine import StrategyEngine
from app.execution.engine import ExecutionEngine
from app.models import MarketRules
from app.portfolio.manager import PortfolioManager
from app.review.daily import DailyReview
from app.alerts.telegram import AlertManager
from app.utils.audit import AuditLog
from app.utils.paths import ModulePaths, assert_paper_runtime


class PaperRuntime:
    def __init__(self, root: Path = ROOT, *, clock=time.time, data_factory=DataService):
        self.root = Path(root)
        self.config = load_config(self.root / 'config.yaml', module_root=self.root)
        assert_paper_runtime(self.config)  # Before credentials, writable state or clients.
        self.paths = ModulePaths(self.root, self.config.instance_id, 'paper')
        self.controls = load_secrets(self.root / '.env', module_root=self.root, paper_controls_only=True)
        if self.config.alerts.enabled and not all(self.controls[k] for k in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')):
            raise ValueError('Enabled alerts require this module own Telegram configuration')
        self.clock, self.data_factory = clock, data_factory
        self.audit = self.portfolio = self.execution = self.data = self.alerts = None
        self._tasks = []
        self._event_lock = asyncio.Lock()
        self._report_lock = asyncio.Lock()
        self.started = False
        self.stopping = False
        self.rules_ready = False
        self.rules_received_at = None
        self.event_counts = Counter()
        self._last_score = 0
        self._last_health = None
        self.last_ready_at = None
        self.report_error = None
        self._last_equity_second = -1
        self._last_snapshot = {}

    async def start(self, *, connect=True, background=True):
        if self.started:
            raise RuntimeError('Paper runtime already started')
        assert_paper_runtime(self.config)
        cfg = self.config
        # Validate every sink before opening the independent database.
        self.paths.validate_sqlite_files()
        self.paths.file('logs/paper/events.jsonl')
        self.paths.file('reports/paper')
        self.portfolio = PortfolioManager(self.paths.ledger, cfg.paper.initial_balance, cfg.timezone,
            state_root=self.root, instance_id=cfg.instance_id)
        try:
            self.audit = AuditLog(self.paths.file('logs/paper'), secrets=self.controls.values(), paths=self.paths)
            self.alerts = AlertManager(enabled=cfg.alerts.enabled, bot_token=self.controls['TELEGRAM_BOT_TOKEN'],
                chat_id=self.controls['TELEGRAM_CHAT_ID'], audit=self.audit)
            self.indicators = IndicatorEngine(cfg.symbols, cfg.data.stale_after_seconds)
            settings = {name: {'order_type': cfg.execution.entry_order_type, **cfg.strategies.get(name, {})}
                        for name in StrategyEngine.NAMES}
            # Reject typo'd strategy names even though the runtime supplies defaults.
            if set(cfg.strategies) - set(StrategyEngine.NAMES):
                raise ValueError('Unknown strategy name')
            self.strategies = StrategyEngine(settings)
            self.strategies.restore(self.portfolio.get_meta('strategy_checkpoint'), self.clock())
            self.execution = ExecutionEngine(cfg, self.portfolio, self.audit, alerts=self.alerts,
                bridge=None, state_provider=self.market, clock=self.clock)
            self.execution.ready = False  # Public contract filters must be fetched before entries.
            saved_rules = self.portfolio.get_meta('paper_contract_rules')
            if saved_rules:
                self.execution.rules = MarketRules(**saved_rules)  # Own ledger, protection only.
            await self.execution.initialize()
            self.review = DailyReview(self.paths.file('reports/paper'), cfg.timezone, paths=self.paths,
                                      strategies=StrategyEngine.NAMES)
            self.data = self.data_factory(cfg.symbols, self.on_event, audit=self._data_audit,
                alert=self._data_alert, **cfg.data.model_dump(exclude={'stale_after_seconds'}))
            await self.alerts.start()
            self.started = True
            self.audit.emit('paper_runtime_started', dry_run=True, live_capability=False,
                            strategies=list(StrategyEngine.NAMES), persisted_positions=len(self.portfolio.positions()))
            if connect:
                self._tasks.append(asyncio.create_task(self._run_feed(), name='sol-public-feed'))
            if background:
                self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name='sol-paper-heartbeat'))
        except BaseException:
            if self.alerts:
                await self.alerts.stop()
            if self.audit:
                self.audit.close()
            self.portfolio.close()
            raise

    def _data_audit(self, event, payload):
        # on_event logs every raw accepted market message, including replay tests.
        if event != 'market_data':
            self.audit.emit(event, **payload)

    def _data_alert(self, event, message):
        self.alerts.notify(event, {'mode': 'PAPER', 'message': message})

    def market(self):
        return self.indicators.snapshot(self.config.trade_symbol, self.clock())

    def _set_rules(self, raw):
        filters = {row['filterType']: row for row in raw['filters']}
        lot = filters['LOT_SIZE']
        market_lot = filters.get('MARKET_LOT_SIZE', lot)
        # Common valid lattice for market entry/exit and limits; Binance step
        # grids are nested powers of ten. Refuse incompatible grids explicitly.
        steps = [Decimal(lot['stepSize']), Decimal(market_lot.get('stepSize', '0'))]
        step = max(steps)
        if step <= 0 or any(s > 0 and step % s for s in steps):
            raise ValueError('INCOMPATIBLE_CONTRACT_QUANTITY_GRIDS')
        minimum = filters.get('MIN_NOTIONAL', {}).get('notional')
        rules = MarketRules(str(step), filters['PRICE_FILTER']['tickSize'],
            max(float(lot['minQty']), float(market_lot.get('minQty', 0))), float(minimum))
        if not all(math.isfinite(float(x)) and float(x) > 0 for x in
                   (rules.step_size, rules.tick_size, rules.min_qty, rules.min_notional)):
            raise ValueError('INVALID_PUBLIC_CONTRACT_RULES')
        self.execution.rules = rules
        self.portfolio.set_meta('paper_contract_rules', asdict(rules))
        self.rules_ready = self.execution.ready = True
        self.rules_received_at = self.clock()
        self.audit.emit('paper_rules_ready', **asdict(rules))

    async def on_event(self, event):
        if not self.started or self.stopping:
            return
        async with self._event_lock:
            assert_paper_runtime(self.config)
            kind = event.get('e', 'unknown')
            self.event_counts[kind] += 1
            if kind == 'exchange_rules':
                try:
                    self._set_rules(event['rules'])
                except (ValueError, KeyError, TypeError, ArithmeticError):
                    self.rules_ready = self.execution.ready = False
                    self.execution.halt('INVALID_PUBLIC_CONTRACT_RULES')
                return
            self.audit.emit('market_data', payload=event)
            try:
                self.indicators.ingest(event, received_at=self.clock())
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                self.audit.emit('market_payload_rejected', error_type=type(exc).__name__, event_type=kind)
                return
            if kind == 'feed_disconnected':
                self.strategies.invalidate('feed_disconnected')
                await self._health_check()
                return
            if event.get('_warmup'):
                return  # Historical bootstrap cannot open or close paper positions.
            market = self.market()
            before = len(self.portfolio.all_trades())
            # Protection before scoring, even when paused or entries halted.
            await self.execution.on_market(market, self.indicators.protection_snapshot('SOLUSDT', self.clock()))
            if len(self.portfolio.all_trades()) > before:
                self._persist_checkpoint()
                await self._safe_report()
            await self._health_check()
            if market.stale or not market.ready:
                if market.reason != 'candle_close_pending':
                    self.strategies.invalidate(market.reason or 'market_not_ready')
                return
            if event.get('s') != 'SOLUSDT' or kind not in ('24hrTicker', 'aggTrade', 'kline'):
                return
            if market.timestamp <= self.strategies._last_evaluated:
                return  # Depth/reference events do not consume the price scoring interval.
            if self.clock() - self._last_score < self.config.runtime.scoring_interval_seconds:
                return
            self._last_score = self.clock()
            # A pending entry has the same exclusive slot as an open position.
            occupied = bool(self.portfolio.positions() or self.portfolio.pending())
            signals = self.strategies.evaluate(market, has_position=occupied)
            stats = self.portfolio.get_meta('decisions:' + self.portfolio.day(self.clock()), {})
            for name, status in self.strategies.status().items():
                counts = stats.setdefault(name, {'evaluations': 0, 'signals': 0, 'approved': 0, 'rejected': 0})
                if status['enabled']:
                    counts['evaluations'] += 1
            self.audit.emit('strategy_scores', market=asdict(market), strategies=self.strategies.status())
            for signal in signals:
                stats[signal.strategy]['signals'] += 1
                self.audit.emit('signal', signal=asdict(signal))
                self._persist_checkpoint()  # A crash/restart must not replay this candidate.
                decision = await self.execution.submit(signal, market)
                field = 'approved' if decision and decision.allowed else 'rejected'
                stats[signal.strategy][field] += 1
                self.audit.emit('signal_decision', signal_id=signal.id, strategy=signal.strategy,
                                decision=asdict(decision) if decision else {'allowed': False, 'reasons': ['DUPLICATE_SIGNAL']})
            self.portfolio.set_meta('decisions:' + self.portfolio.day(self.clock()), stats)
            self._persist_checkpoint()

    def _persist_checkpoint(self):
        self.portfolio.set_meta('strategy_checkpoint', self.strategies.checkpoint())

    async def _health_check(self):
        market = self.market()
        healthy = market.ready and not market.stale and self.rules_ready
        if healthy:
            self.last_ready_at = self.clock()
        pending_close = market.reason == 'candle_close_pending'
        if not pending_close and healthy != self._last_health:
            self._last_health = healthy
            self.audit.emit('paper_readiness_changed', ready=healthy,
                            reason='' if healthy else market.reason or 'PUBLIC_RULES_PENDING')
            self.alerts.notify('websocket_reconnected' if healthy else 'websocket_disconnect',
                               {'mode': 'PAPER', 'reason': market.reason or 'PUBLIC_RULES_PENDING'})
        if not healthy and not pending_close:
            self.strategies.invalidate(market.reason or 'PUBLIC_RULES_PENDING')
        # Mark-to-market drawdown samples only valid SOL quotes, no fabricated prices.
        own = self.indicators.protection_snapshot('SOLUSDT', self.clock())
        if not own.stale and own.price > 0 and int(self.clock()) != self._last_equity_second:
            self.portfolio.record_equity(self.clock(), {'SOLUSDT': own.price})
            self._last_equity_second = int(self.clock())
        metrics = self.portfolio.metrics(self.clock(), {'SOLUSDT': market.price} if market.price else {})
        limits = self._limits(metrics)
        key = 'risk_notifications:' + metrics['day']
        sent = self.portfolio.get_meta(key, [])
        for reason in limits:
            if reason not in sent:
                self.execution.notify('circuit_breaker', reason=reason, mode='paper')
                sent.append(reason)
        if limits:
            self.portfolio.set_meta(key, sent)

    def _limits(self, metrics):
        risk = self.config.risk
        reasons = []
        if metrics['today_trades'] >= risk.max_trades_per_day:
            reasons.append('DAILY_TRADE_LIMIT')
        if metrics['today_pnl'] + min(0, metrics['unrealized_pnl']) <= -risk.daily_loss_limit:
            reasons.append('DAILY_LOSS_HALT')
        if metrics['consecutive_losses'] >= risk.max_consecutive_losses:
            reasons.append('CONSECUTIVE_LOSS_HALT')
        return reasons

    async def heartbeat(self):
        async with self._event_lock:
            assert_paper_runtime(self.config)
            await self.execution.expire_paper_entries()
            await self._health_check()
            local = datetime.fromtimestamp(self.clock(), ZoneInfo(self.config.timezone))
            cfg = self.config.review
            if (local.hour, local.minute) >= (cfg.daily_hour, cfg.daily_minute):
                yesterday = (local.date() - timedelta(days=1)).isoformat()
                if self.portfolio.get_meta('last_scheduled_review') != yesterday:
                    if await self._safe_report(yesterday):
                        self.portfolio.set_meta('last_scheduled_review', yesterday)

    async def _run_feed(self):
        try:
            await self.data.run()
            if not self.stopping:
                self.execution.halt('PUBLIC_FEED_TASK_STOPPED')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.execution.halt('PUBLIC_FEED_TASK_FAILED')
            self.audit.emit('runtime_error', task='public_feed', error_type=type(exc).__name__)

    async def _heartbeat_loop(self):
        interval = self.config.runtime.heartbeat_seconds
        expected = time.monotonic() + interval
        while not self.stopping:
            await asyncio.sleep(interval)
            if time.monotonic() - expected > self.config.runtime.max_event_loop_lag_seconds:
                self.execution.halt('EVENT_LOOP_LAG_NEW_ENTRIES_BLOCKED')
            try:
                await self.heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.execution.halt('HEARTBEAT_OR_REPORT_FAILED')
                self.audit.emit('runtime_error', task='heartbeat', error_type=type(exc).__name__)
            expected = time.monotonic() + interval

    async def generate_report(self, day=None):
        day = Date.fromisoformat(day).isoformat() if day else self.portfolio.day(self.clock())
        async with self._report_lock:
            # Bounded daily SQL read; no unrelated history/database loads.
            context = {'decisions': self.portfolio.get_meta('decisions:' + day, {}),
                       'equity': self.portfolio.get_meta('equity:' + day, {}),
                       'open_positions': len(self.portfolio.positions())}
            path = self.review.write(self.portfolio.trades_for_day(day), day, context=context)
            self.audit.emit('review_generated', day=day, path=str(path.relative_to(self.root)))
            return path

    async def _safe_report(self, day=None):
        try:
            result = await self.generate_report(day)
            self.report_error = None
            return result
        except (ValueError, OSError, RuntimeError) as exc:
            self.report_error = type(exc).__name__
            self.audit.emit('review_error', error_type=self.report_error)
            # A reporting sink failure must never kill the market/protection task.
            self.execution.halt('REVIEW_FAILED_NEW_ENTRIES_BLOCKED')
            return None

    async def pause(self):
        self.execution.paused = True
        self.portfolio.set_meta('paused', True)
        await self.execution.expire_paper_entries()
        self.audit.emit('operator_paused', stops_continue=True)
        return True

    async def resume(self):
        if self.execution.halt_reason or self._limits(self.portfolio.metrics(self.clock())):
            self.audit.emit('operator_resume_refused', reason=self.execution.halt_reason or 'RISK_LIMIT_ACTIVE')
            return False
        assert_paper_runtime(self.config)
        self.execution.paused = False
        self.portfolio.set_meta('paused', False)
        self.audit.emit('operator_resumed', mode='paper')
        return True

    def snapshot(self):
        if not self.started:
            return self._last_snapshot or {'mode': 'paper', 'dry_run': True, 'status': 'stopped'}
        markets = {s: asdict(self.indicators.snapshot(s, self.clock())) for s in self.config.symbols}
        metrics = self.portfolio.metrics(self.clock(), {s: m['price'] for s, m in markets.items() if m['price'] > 0})
        routes = self.data.status()
        ready = bool(self.rules_ready and markets['SOLUSDT']['ready'] and not markets['SOLUSDT']['stale'])
        return {'application': 'sol-ai-trading-system', 'instance_id': self.config.instance_id,
                'mode': 'paper', 'dry_run': True, 'ready': ready, 'status': 'running' if ready else 'warming_or_disconnected',
                'markets': markets, 'portfolio': metrics, 'positions': [dict(p.to_dict(),
                    unrealized_pnl=p.unrealized(markets[p.symbol]['price']) if markets[p.symbol]['price'] else None)
                    for p in self.portfolio.positions()],
                'strategies': self.strategies.status(), 'trades': self.portfolio.all_trades(100),
                'orders': self.portfolio.recent_orders(), 'logs': self.audit.recent(100),
                'risk': {'paused': self.execution.paused, 'halted': bool(self.execution.halt_reason),
                         'halt_reason': self.execution.halt_reason, 'active_limits': self._limits(metrics),
                         'limits': self.config.risk.model_dump()},
                'data': {'connected': all(r['connected'] for r in routes.values()), 'routes': routes,
                         'event_counts': dict(self.event_counts), 'rules_ready': self.rules_ready,
                         'last_ready_at': self.last_ready_at, 'report_error': self.report_error},
                'safety': {'production_live_sealed': True, 'private_client_constructed': self.execution.bridge is not None,
                           'private_requests': 0, 'real_orders': 0, 'ledger_mode': self.portfolio.get_meta('mode'),
                           'network_receipts': dict(self.data.receipts),
                           'verification': 'Structural paper capability; confirm with isolation tests, not counters alone'}}

    async def stop(self):
        if not self.started or self.stopping:
            return
        self.stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.data.stop()
        try:
            # No shutdown flattening or live side effects. Persisted paper stops
            # resume at fresh quotes; downtime fills are never invented.
            self.execution.ready = False
            await self.execution.expire_paper_entries()
            self._persist_checkpoint()
            await self._safe_report()
            self.audit.emit('paper_runtime_stopped', open_positions=len(self.portfolio.positions()),
                            offline_protection=False)
            self._last_snapshot = self.snapshot()
            self._last_snapshot.update(status='stopped', ready=False)
        finally:
            await self.alerts.stop()
            self.audit.close()
            self.portfolio.close()
            self.started = False
