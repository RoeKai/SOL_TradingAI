"""Pure, bounded market-state projection; missing evidence fails closed."""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from typing import Any

from app.models import Candle, MarketState


def _number(value: Any, *, positive: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError("Invalid market numeric value")
    return value


class IndicatorEngine:
    def __init__(self, symbols: list[str], stale_after_seconds: float = 15, max_candles: int = 240):
        self.symbols = tuple(symbols)
        self.stale_after_seconds = float(stale_after_seconds)
        self.max_candles = max(20, min(1440, int(max_candles)))
        self.candles: dict[str, OrderedDict[int, Candle]] = {s: OrderedDict() for s in symbols}
        # One observation per second plus minute-close warmup points, bounded ~17 minutes.
        self._prices: dict[str, OrderedDict[int, tuple[float, float]]] = {s: OrderedDict() for s in symbols}
        self._trades = {s: deque(maxlen=3000) for s in symbols}
        self._last_trade_id: dict[str, int] = {}
        self._price: dict[str, tuple[float, float]] = {}
        self._book: dict[str, tuple[float, float, float, float]] = {}
        self._book_id: dict[str, int] = {}
        self._live_received: dict[str, float] = {}
        self._candle_times: dict[str, dict[int, float]] = {s: {} for s in symbols}

    def ingest(self, event: dict, received_at: float | None = None) -> None:
        received_at = time.time() if received_at is None else received_at
        kind = event.get("e")
        if kind == "feed_disconnected":
            if event.get("route") == "public":
                self._book.clear()
                self._book_id.clear()
            else:
                self._live_received.clear()
            return
        symbol = event.get("s")
        if symbol not in self.candles:
            return
        stamp = float(event.get("E", 0)) / 1000
        if not math.isfinite(stamp) or stamp <= 0 or stamp > received_at + 5:
            raise ValueError("Invalid or future market timestamp")
        warmup = event.get("_warmup", False)
        if kind == "kline":
            k = event["k"]
            if k.get("i", "1m") != "1m":
                return
            candle = Candle(symbol=symbol, open_time=int(k["t"]), close_time=int(k["T"]),
                            open=_number(k["o"], positive=True), high=_number(k["h"], positive=True),
                            low=_number(k["l"], positive=True), close=_number(k["c"], positive=True),
                            volume=_number(k["v"]), closed=bool(k["x"]))
            if not candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high:
                raise ValueError("Invalid candle OHLC")
            if candle.close_time - candle.open_time != 59_999 or candle.open_time % 60_000:
                raise ValueError("Expected aligned one-minute candle")
            current = self.candles[symbol].get(candle.open_time)
            if current and current.closed and not candle.closed:
                return
            if current and stamp < self._candle_times[symbol].get(candle.open_time, 0):
                return
            self.candles[symbol][candle.open_time] = candle
            self.candles[symbol] = OrderedDict(sorted(self.candles[symbol].items())[-self.max_candles:])
            self._candle_times[symbol][candle.open_time] = stamp
            self._candle_times[symbol] = {key: value for key, value in self._candle_times[symbol].items()
                                          if key in self.candles[symbol]}
            if candle.closed:
                self._sample(symbol, candle.close_time / 1000, candle.close)
            if not warmup:
                self._set_price(symbol, stamp, candle.close, received_at)
        elif kind in {"aggTrade", "24hrTicker"}:
            if kind == "aggTrade":
                trade_id = int(event["a"])
                if trade_id <= self._last_trade_id.get(symbol, -1):
                    return
                self._last_trade_id[symbol] = trade_id
                price, qty = _number(event["p"], positive=True), _number(event["q"])
                self._trades[symbol].append((stamp, qty, not bool(event["m"])))
            else:
                price = _number(event["c"], positive=True)
            self._set_price(symbol, stamp, price, received_at)
        elif kind == "depthUpdate":
            update_id = int(event["u"])
            if update_id <= self._book_id.get(symbol, -1):
                return
            bids = [(_number(p, positive=True), _number(q)) for p, q in event["b"][:20] if float(q) > 0]
            asks = [(_number(p, positive=True), _number(q)) for p, q in event["a"][:20] if float(q) > 0]
            if not bids or not asks:
                self._book.pop(symbol, None)
                return
            bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
            if bid >= ask:
                raise ValueError("Crossed partial-depth snapshot")
            bid_value, ask_value = sum(p * q for p, q in bids), sum(p * q for p, q in asks)
            self._book[symbol] = (stamp, bid, ask, bid_value / (bid_value + ask_value))
            self._book_id[symbol] = update_id

    def _sample(self, symbol: str, stamp: float, price: float) -> None:
        samples = self._prices[symbol]
        second = int(stamp)
        if second not in samples or stamp >= samples[second][0]:
            samples[second] = (stamp, price)
        self._prices[symbol] = OrderedDict(sorted(samples.items())[-1020:])

    def _set_price(self, symbol: str, stamp: float, price: float, received_at: float) -> None:
        if stamp < self._price.get(symbol, (0, 0))[0]:
            return
        self._sample(symbol, stamp, price)
        self._price[symbol] = (stamp, price)
        # An old replay event must not refresh the feed's freshness.
        self._live_received[symbol] = min(stamp, received_at)

    def _returns(self, symbol: str, now: float, price: float) -> dict[str, float]:
        result: dict[str, float] = {}
        for minutes in (1, 3, 5, 15):
            target = now - minutes * 60
            baseline = next((point for point in reversed(self._prices[symbol].values()) if point[0] <= target), None)
            # Initial bootstrap precision is one minute; streaming becomes second-level.
            if baseline and target - baseline[0] <= 61 and baseline[1] > 0:
                result[f"{minutes}m"] = (price / baseline[1] - 1) * 100
        return result

    def _own_state(self, symbol: str, now: float) -> dict[str, Any]:
        stamp, price = self._price.get(symbol, (0, 0))
        book_stamp, bid, ask, pressure = self._book.get(symbol, (0, 0, 0, 0.5))
        stale = (now - stamp > self.stale_after_seconds or now - book_stamp > self.stale_after_seconds
                 or now - self._live_received.get(symbol, 0) > self.stale_after_seconds)
        candles = list(self.candles[symbol].values())
        latest_closed = int(now // 60) * 60_000 - 60_000
        required = [latest_closed - step * 60_000 for step in range(15)]
        missing = [key for key in required if key not in self.candles[symbol] or not self.candles[symbol][key].closed]
        gap = bool(missing)
        close_pending = missing == [latest_closed] and now % 60 < 2
        returns = self._returns(symbol, stamp or now, price) if price else {}
        recent = [c for c in candles if c.close_time / 1000 >= now - 180 and c.open_time / 1000 <= now]
        observed = [p for stamp, p in self._prices[symbol].values() if now - 180 <= stamp <= now]
        closed = [c for c in candles if c.closed and c.close_time / 1000 <= now]
        volumes = [c.volume for c in closed[-21:-1]]
        avg_volume = sum(volumes) / len(volumes) if volumes else 0
        volume_ratio = closed[-1].volume / avg_volume if closed and avg_volume > 0 else 0.0
        recent_trades = [t for t in self._trades[symbol] if now - 60 <= t[0] <= now]
        trade_volume = sum(t[1] for t in recent_trades)
        # Top-of-book pressure and aggressor-side volume are both bounded in [0, 1].
        if trade_volume:
            pressure = 0.7 * pressure + 0.3 * sum(t[1] for t in recent_trades if t[2]) / trade_volume
        reason = ("stale_market_data" if stale else "candle_close_pending" if close_pending
                  else "candle_gap_or_warmup" if gap else "insufficient_history" if len(returns) < 4 else "")
        return dict(timestamp=stamp, price=price, returns=returns,
                    momentum=returns.get("1m", 0) - returns.get("3m", 0) / 3,
                    volume_ratio=volume_ratio, buy_pressure=pressure,
                    low_3m=min([c.low for c in recent] + observed + [price]) if price else 0,
                    high_3m=max([c.high for c in recent] + observed + [price]) if price else 0,
                    ready=not reason, stale=stale, bid=bid, ask=ask, reason=reason)

    def snapshot(self, symbol: str, now: float | None = None) -> MarketState:
        now = time.time() if now is None else now
        own = self._own_state(symbol, now)
        btc = self._own_state("BTCUSDT", now) if "BTCUSDT" in self.candles else None
        eth = self._own_state("ETHUSDT", now) if "ETHUSDT" in self.candles else None
        if symbol == "SOLUSDT" and (not btc or not btc["ready"] or not eth or not eth["ready"]):
            pending_only = (own['reason'] in ('', 'candle_close_pending') and btc and eth
                            and all(ref['ready'] or ref['reason'] == 'candle_close_pending' for ref in (btc, eth)))
            own.update(ready=False, reason="candle_close_pending" if pending_only else "reference_market_not_ready")
            own["stale"] = own["stale"] or not btc or btc["stale"] or not eth or eth["stale"]
        return MarketState(symbol=symbol, **own,
                           btc_return_3m=btc["returns"].get("3m") if btc and btc["ready"] else None,
                           eth_return_3m=eth["returns"].get("3m") if eth and eth["ready"] else None)

    def protection_snapshot(self, symbol: str, now: float | None = None) -> MarketState:
        """Fresh SOL price/book can close existing paper exposure even if BTC is stale.

        Not entry authority: reference returns intentionally absent. Missing SOL
        quotes never manufacture a stop fill at the configured stop price.
        """
        own = self._own_state(symbol, time.time() if now is None else now)
        return MarketState(symbol=symbol, **own, btc_return_3m=None, eth_return_3m=None)
