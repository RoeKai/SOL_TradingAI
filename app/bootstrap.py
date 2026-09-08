"""Offline isolation acceptance entrypoint, NOT the future strategy runtime.

No live exchange, HTTP client, market service, credential loader or bridge imports.
Synthetic prices prove ledger/risk/paper execution can run without the old system.
"""
from dataclasses import replace
import hashlib
from pathlib import Path
import time
from app.config import load_config
from app.models import MarketState, MarketRules, Signal, Position
from app.paper import PaperBroker
from app.portfolio import PortfolioManager
from app.risk import RiskEngine
from app.utils.audit import AuditLog
from app.utils.paths import ModulePaths, assert_paper_runtime


def run_isolation_smoke(root: Path, cycles=1):
    if type(cycles) is not int or not 1 <= cycles <= 3:
        raise ValueError('isolation cycles must be 1..3')
    config = load_config(root / 'config.yaml', module_root=root)
    assert_paper_runtime(config)  # Must happen BEFORE any writable state/log is opened.
    paths = ModulePaths(root, config.instance_id, 'paper')
    paths.validate_sqlite_files()
    paths.file('logs/paper/events.jsonl')
    portfolio = PortfolioManager(paths.ledger, config.paper.initial_balance, config.timezone,
                                 state_root=root, instance_id=config.instance_id)
    audit = None
    try:
        audit = AuditLog(paths.file('logs/paper'), paths=paths)
        risk, paper = RiskEngine(config, audit), PaperBroker(config.risk.taker_fee_rate, config.risk.slippage_bps)
        start_trades = len(portfolio.all_trades())
        audit.emit('isolation_start', synthetic=True, network='none', live_capability=False)
        for _ in range(cycles):
            now = time.time()
            # Deliberately synthetic; not claimed to be an actual strategy or exchange event.
            market = MarketState('SOLUSDT', now, 100, {'1m': 0, '3m': 0, '5m': 0, '15m': 0},
                                 0, 1, .5, 99, 101, 0, 0, True, False, bid=99.99, ask=100.01)
            intent = 'sol' + hashlib.sha256(f'{config.instance_id}:{time.time_ns()}'.encode()).hexdigest()[:29]
            signal = Signal(intent, 'isolation_smoke', 'SOLUSDT', 'LONG', 100, 99,
                            [{'price': 101, 'fraction': 1}], now, reason='SYNTHETIC_ACCEPTANCE_ONLY')
            audit.emit('market_data', synthetic=True, symbol='SOLUSDT', price=100)
            audit.emit('signal', synthetic=True, signal_id=intent)
            decision = risk.assess(signal, market, portfolio, MarketRules(), now,
                                   paused=portfolio.get_meta('paused', False),
                                   halt_reason=portfolio.get_meta('halt_reason', ''))
            if not decision.allowed:
                continue
            # This acceptance harness has no real transport path even if config or env contains keys.
            assert_paper_runtime(config)
            fill = paper.fill(market, 'BUY', decision.quantity)
            portfolio.prepare(intent, 'ENTRY', intent, {'synthetic': True, 'instance_id': config.instance_id}, now)
            position = Position(intent, signal.strategy, signal.symbol, 'LONG', fill['avgPrice'],
                                decision.quantity, decision.quantity, 99, signal.take_profits, now,
                                config.execution.leverage, fees=fill['fee'], stop_status='PAPER_ONLY')
            portfolio.save_position(position)
            portfolio.add_cash(intent + ':entry', -fill['fee'], now)
            portfolio.update_order(intent, 'FILLED', decision.quantity, fill['fee'])
            audit.emit('paper_entry', synthetic=True, order_id=intent, quantity=decision.quantity)
            exit_fill = paper.fill(replace(market, price=101, bid=100.99, ask=101.01), 'SELL', decision.quantity)
            position.realized_pnl = (exit_fill['avgPrice']-position.entry_price)*decision.quantity
            position.fees += exit_fill['fee']
            portfolio.add_cash(intent + ':exit', position.realized_pnl-exit_fill['fee'], now+1)
            portfolio.closed(position, exit_fill['avgPrice'], now+1, 'synthetic_acceptance_close')
            audit.emit('paper_exit', synthetic=True, order_id=intent, pnl=position.realized_pnl-position.fees)
        result = {'application': 'sol-ai-trading-system', 'acceptance_only': True,
                  'dry_run': True, 'mode': 'paper', 'synthetic_market': True,
                  'network_clients_created': 0, 'private_requests': 0, 'live_orders': 0,
                  'trades_created': len(portfolio.all_trades())-start_trades,
                  'instance_id': config.instance_id, 'ledger': str(paths.ledger),
                  'risk_state_preserved': True}
        audit.emit('isolation_completed', **result)
        return result
    finally:
        if audit:
            audit.close()
        portfolio.close()
