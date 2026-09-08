import math
from decimal import Decimal, ROUND_DOWN
from app.models import MarketRules, RiskDecision, Signal, MarketState


def floor_step(value, step):
    return float((Decimal(str(value))/Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)*Decimal(str(step)))


class RiskEngine:
    """Approval is necessary at signal time AND under the execution lock immediately before write."""
    def __init__(self, config, audit):
        self.config, self.audit = config, audit

    def assess(self, signal: Signal, market: MarketState, portfolio, rules: MarketRules,
               now: float, *, paused=False, halt_reason='', prices=None, own_pending=False):
        c = self.config.risk
        metrics = portfolio.metrics(now, prices or {market.symbol: market.price})
        reasons = []
        if paused:
            reasons.append('OPERATOR_PAUSED')
        if halt_reason:
            reasons.append(f'RECONCILIATION_HALT:{halt_reason}')
        if not market.ready or market.stale or now-market.timestamp > self.config.data.stale_after_seconds:
            reasons.append('MARKET_STALE_OR_WARMUP')
        if now-signal.created_at > 30 or signal.created_at > now+2:
            reasons.append('SIGNAL_EXPIRED_OR_FUTURE')
        if signal.symbol != self.config.trade_symbol or signal.side not in ('LONG', 'SHORT'):
            reasons.append('SYMBOL_OR_SIDE_NOT_ALLOWED')
        if signal.order_type not in ('MARKET', 'LIMIT'):
            reasons.append('ORDER_TYPE_NOT_ALLOWED')
        values = [signal.entry_price, signal.stop_price, market.price, metrics['equity']]
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in values):
            reasons.append('INVALID_PRICE_STOP_OR_EQUITY')
        if (signal.side == 'LONG' and signal.stop_price >= signal.entry_price) or (signal.side == 'SHORT' and signal.stop_price <= signal.entry_price):
            reasons.append('STOP_REQUIRED_ON_CORRECT_SIDE')
        if signal.side == 'LONG' and (market.btc_return_3m is None or not math.isfinite(market.btc_return_3m) or market.btc_return_3m <= c.btc_crash_pct):
            reasons.append('BTC_CRASH_OR_MISSING_FILTER')
        if metrics['positions_count'] >= c.max_positions:
            reasons.append('POSITION_EXISTS_NO_ADDING')
        if metrics['pending_count'] and not own_pending:
            reasons.append('UNRESOLVED_ORDER_EXISTS')
        if metrics['today_trades'] >= c.max_trades_per_day and not own_pending:
            reasons.append('DAILY_TRADE_LIMIT')
        if metrics['consecutive_losses'] >= c.max_consecutive_losses:
            reasons.append('CONSECUTIVE_LOSS_HALT')
        if metrics['today_pnl'] + min(0, metrics['unrealized_pnl']) <= -c.daily_loss_limit:
            reasons.append('DAILY_LOSS_HALT')
        if self.config.execution.leverage > c.max_leverage:
            reasons.append('LEVERAGE_LIMIT')
        if not signal.take_profits:
            reasons.append('TAKE_PROFIT_PLAN_REQUIRED')
        else:
            try:
                fractions = [float(t['fraction']) for t in signal.take_profits]
                if any(not math.isfinite(f) or f <= 0 for f in fractions) or abs(sum(fractions)-1) > 1e-8:
                    reasons.append('INVALID_TAKE_PROFIT_FRACTIONS')
                for target in signal.take_profits:
                    price = float(target['price'])
                    if not math.isfinite(price) or price <= 0 or (price-signal.entry_price)*(1 if signal.side == 'LONG' else -1) <= 0:
                        reasons.append('INVALID_TAKE_PROFIT_PRICE')
            except (KeyError, TypeError, ValueError):
                reasons.append('INVALID_TAKE_PROFIT_PLAN')
        if reasons:
            self.audit.emit('risk_rejected', signal_id=signal.id, strategy=signal.strategy, reasons=reasons)
            return RiskDecision(False, reasons)
        slip = c.slippage_bps / 10000
        quote = (market.ask or market.price) if signal.side == 'LONG' else (market.bid or market.price)
        worst_entry = max(signal.entry_price, quote)*(1+slip) if signal.side == 'LONG' else min(signal.entry_price, quote)*(1-slip)
        if abs(worst_entry-signal.entry_price)/signal.entry_price > .015:
            return self._reject(signal, 'ENTRY_PRICE_MOVED')
        loss_per_unit = abs(worst_entry-signal.stop_price) + signal.stop_price*slip + (worst_entry+signal.stop_price)*c.taker_fee_rate
        remaining = c.daily_loss_limit + min(0, metrics['today_pnl'] + min(0, metrics['unrealized_pnl']))
        budget = min(c.max_loss_per_trade, remaining)
        free_margin = max(0, min(metrics['equity']*c.max_margin_ratio-metrics['margin_used'], metrics.get('available_balance', metrics['equity'])))
        quantity = floor_step(min(budget/loss_per_unit, free_margin*self.config.execution.leverage/worst_entry), rules.step_size)
        if quantity < rules.min_qty or quantity*min(worst_entry, market.price) < rules.min_notional:
            return self._reject(signal, 'EXCHANGE_MINIMUM_EXCEEDS_RISK_BUDGET')
        decision = RiskDecision(True, [], quantity, quantity*loss_per_unit, quantity*worst_entry/self.config.execution.leverage)
        self.audit.emit('risk_approved', signal_id=signal.id, quantity=quantity, max_loss=decision.max_loss, margin=decision.margin)
        return decision

    def _reject(self, signal, reason):
        self.audit.emit('risk_rejected', signal_id=signal.id, reasons=[reason])
        return RiskDecision(False, [reason])

