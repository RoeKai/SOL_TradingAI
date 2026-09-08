import asyncio
from dataclasses import replace
import json

import pytest

from app.data import DataService
from app.indicators import IndicatorEngine
from app.models import MarketState
from app.strategies import StrategyEngine


SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT"]
NOW = 1_800_000.0  # Aligned to a minute, deterministic and no real network.


def candle(symbol, minute, close=100, *, volume=10, closed=True, warmup=True):
    start = int(minute * 60_000)
    return {"e": "kline", "s": symbol, "E": start + 59_999, "_warmup": warmup,
            "k": {"t": start, "T": start + 59_999, "i": "1m", "o": str(close),
                  "h": str(close), "l": str(close), "c": str(close), "v": str(volume), "x": closed}}


def tick(symbol="SOLUSDT", now=NOW, price=100):
    return {"e": "24hrTicker", "s": symbol, "E": int(now * 1000), "c": str(price)}


def book(symbol="SOLUSDT", now=NOW, update=1):
    return {"e": "depthUpdate", "s": symbol, "E": int(now * 1000), "u": update,
            "b": [["99.9", "10"]], "a": [["100.1", "5"]]}


def warm(engine, now=NOW):
    for symbol in SYMBOLS:
        for offset in range(21, 0, -1):
            engine.ingest(candle(symbol, int(now / 60) - offset), received_at=now)
        engine.ingest(tick(symbol, now), received_at=now)
        engine.ingest(book(symbol, now), received_at=now)


def state(now=NOW, price=100, r3=0, **kwargs):
    data = dict(symbol="SOLUSDT", timestamp=now, price=price,
                returns={"1m": 0, "3m": r3, "5m": 0, "15m": 0}, momentum=0,
                volume_ratio=1, buy_pressure=0.6, low_3m=price, high_3m=price,
                btc_return_3m=0, eth_return_3m=0, ready=True, stale=False,
                bid=price - .01, ask=price + .01)
    data.update(kwargs)
    return MarketState(**data)


def test_indicators_bootstrap_and_rolling_returns():
    engine = IndicatorEngine(SYMBOLS)
    warm(engine)
    engine.ingest(tick(price=97), received_at=NOW)
    result = engine.snapshot("SOLUSDT", NOW)
    assert result.ready
    assert result.returns == pytest.approx({"1m": -3, "3m": -3, "5m": -3, "15m": -3})
    assert result.momentum == pytest.approx(-2)
    assert .65 < result.buy_pressure < .67
    assert result.volume_ratio == 1


def test_warmup_alone_cannot_become_live_ready():
    engine = IndicatorEngine(SYMBOLS)
    for symbol in SYMBOLS:
        for offset in range(21, 0, -1):
            engine.ingest(candle(symbol, int(NOW / 60) - offset), received_at=NOW)
    assert not engine.snapshot("SOLUSDT", NOW).ready


def test_missing_bar_or_reference_or_depth_prevents_signal():
    engine = IndicatorEngine(SYMBOLS)
    warm(engine)
    del engine.candles["BTCUSDT"][int(NOW / 60 - 3) * 60_000]
    assert not engine.snapshot("SOLUSDT", NOW).ready
    assert engine.snapshot("SOLUSDT", NOW).reason == "reference_market_not_ready"
    engine.ingest(candle("BTCUSDT", int(NOW / 60 - 3)), received_at=NOW)
    assert engine.snapshot("SOLUSDT", NOW).ready
    engine.ingest({"e": "feed_disconnected", "route": "public"}, received_at=NOW)
    assert engine.snapshot("SOLUSDT", NOW).stale


def test_stale_event_does_not_refresh_freshness():
    engine = IndicatorEngine(SYMBOLS)
    warm(engine)
    engine.ingest(tick(now=NOW - 100, price=10), received_at=NOW + 20)
    result = engine.snapshot("SOLUSDT", NOW + 20)
    assert result.stale
    assert result.price == 100


def test_duplicate_trade_and_depth_are_not_counted_twice():
    engine = IndicatorEngine(SYMBOLS)
    warm(engine)
    trade = {"e": "aggTrade", "s": "SOLUSDT", "E": int(NOW * 1000),
             "a": 7, "p": "100", "q": "2", "m": False}
    engine.ingest(trade, received_at=NOW)
    engine.ingest(trade, received_at=NOW)
    assert len(engine._trades["SOLUSDT"]) == 1
    bad_duplicate = book()
    bad_duplicate["a"] = [["90", "10"]]
    engine.ingest(bad_duplicate, received_at=NOW)
    assert engine.snapshot("SOLUSDT", NOW).ask == 100.1


def test_partial_candle_updates_replace_instead_of_accumulate():
    engine = IndicatorEngine(SYMBOLS)
    warm(engine)
    event = candle("SOLUSDT", int(NOW / 60), closed=False, warmup=False)
    event["E"] = int((NOW + 1) * 1000)
    engine.ingest(event, received_at=NOW + 1)
    event["k"].update(c="99", l="99", v="15")
    event["E"] += 1000
    engine.ingest(event, received_at=NOW + 2)
    assert len(engine.candles["SOLUSDT"]) == 22
    assert list(engine.candles["SOLUSDT"].values())[-1].close == 99
    assert engine.snapshot("SOLUSDT", NOW + 2).low_3m == 99
    event["E"] -= 1000
    event["k"].update(c="100", l="100", v="1")
    engine.ingest(event, received_at=NOW + 3)
    assert list(engine.candles["SOLUSDT"].values())[-1].close == 99


def test_buffers_stay_bounded():
    engine = IndicatorEngine(SYMBOLS, max_candles=25)
    for offset in range(200):
        engine.ingest(candle("SOLUSDT", int(NOW / 60) - 200 + offset), received_at=NOW)
    for offset in range(2000):
        engine.ingest(tick(now=NOW + offset), received_at=NOW + offset)
    assert len(engine.candles["SOLUSDT"]) == 25
    assert len(engine._prices["SOLUSDT"]) <= 1020


def test_future_or_nonfinite_quotes_rejected():
    engine = IndicatorEngine(SYMBOLS)
    with pytest.raises(ValueError):
        engine.ingest(tick(price="nan"), received_at=NOW)
    with pytest.raises(ValueError):
        engine.ingest(tick(now=NOW + 30), received_at=NOW)


def test_panic_observation_and_quarter_rebound_proposes_protected_long():
    engine = StrategyEngine()
    assert engine.evaluate(state(price=97, r3=-3, low_3m=97)) == []
    assert engine.status()["panic_rebound"]["state"] == "observing"
    assert engine.evaluate(state(now=NOW + 1, price=97.74, r3=-2.26, low_3m=97)) == []
    signals = engine.evaluate(state(now=NOW + 2, price=97.75, r3=-2.25, low_3m=97))
    assert len(signals) == 1
    proposal = signals[0]
    assert proposal.side == "LONG"
    assert proposal.stop_price < 97 < proposal.entry_price
    assert sum(tp["fraction"] for tp in proposal.take_profits) == 1
    assert proposal.take_profits[0]["price"] > proposal.entry_price


def test_panic_uses_new_low_and_original_decline_not_small_bounce():
    engine = StrategyEngine()
    engine.evaluate(state(price=97, r3=-3))
    engine.evaluate(state(now=NOW + 1, price=96, r3=-4, low_3m=96))
    assert engine.evaluate(state(now=NOW + 2, price=96.8, r3=-3.2, low_3m=96)) == []
    assert engine.evaluate(state(now=NOW + 3, price=97, r3=-3, low_3m=96))


@pytest.mark.parametrize("btc", [-0.8, -1.2, None])
def test_panic_btc_downside_filter(btc):
    engine = StrategyEngine()
    engine.evaluate(state(price=97, r3=-3))
    assert engine.evaluate(state(now=NOW + 1, price=98, r3=-2, low_3m=97, btc_return_3m=btc)) == []


def test_no_repeated_signal_on_replay_cooldown_or_same_panic_cycle():
    engine = StrategyEngine()
    engine.evaluate(state(price=97, r3=-3))
    rebound = state(now=NOW + 1, price=98, r3=-2, low_3m=97)
    assert engine.evaluate(rebound)
    assert engine.evaluate(rebound) == []
    assert engine.evaluate(replace(rebound, timestamp=NOW + 2)) == []
    assert engine.evaluate(state(now=NOW + 400, price=97, r3=-3)) == []
    assert engine.status()["panic_rebound"]["reason"] == "waiting_for_new_panic_cycle"


def test_stale_data_clears_observation_even_with_same_price_timestamp():
    engine = StrategyEngine()
    first = state(price=97, r3=-3)
    engine.evaluate(first)
    engine.evaluate(replace(first, stale=True, ready=False))
    assert not engine.observations
    assert engine.evaluate(state(now=NOW + 1, price=98, r3=-2, low_3m=97)) == []


def test_position_prevents_every_strategy_entry_and_expired_observation_resets():
    engine = StrategyEngine()
    engine.evaluate(state(price=97, r3=-3))
    assert engine.evaluate(state(now=NOW + 1, price=98, r3=-2, low_3m=97), has_position=True) == []
    assert not engine.observations
    engine = StrategyEngine()
    engine.evaluate(state(price=97, r3=-3))
    assert engine.evaluate(state(now=NOW + 301, price=98, r3=-2, low_3m=97)) == []


def test_all_four_strategies_can_be_disabled():
    engine = StrategyEngine({name: {"enabled": False} for name in StrategyEngine.NAMES})
    engine.evaluate(state(price=97, r3=-3))
    assert engine.evaluate(state(now=NOW + 1, price=98, r3=-2, low_3m=97)) == []
    assert all(item["reason"] == "disabled" for item in engine.status().values())


def test_canonical_threshold_configuration_is_not_silently_ignored():
    engine = StrategyEngine({"panic_rebound": {"drop_threshold_pct": -4, "btc_crash_pct": -0.3}})
    engine.evaluate(state(price=97, r3=-3))
    assert not engine.observations
    engine.evaluate(state(now=NOW + 1, price=95, r3=-5))
    assert engine.observations
    assert engine.evaluate(state(now=NOW + 2, price=97, r3=-3, low_3m=95, btc_return_3m=-0.4)) == []


@pytest.mark.parametrize("settings", [{"enabled": "false"}, {"drop_threshold_pct": 2},
                                      {"rebound_fraction": 2}, {"cooldown_seconds": -1}])
def test_invalid_strategy_configuration_rejected(settings):
    with pytest.raises(ValueError):
        StrategyEngine({"panic_rebound": settings})


def test_trend_breakout_enabled_proposes_long():
    engine = StrategyEngine({"panic_rebound": {"enabled": False}, "trend_breakout": {"enabled": True}})
    engine.evaluate(state(high_3m=100.1, low_3m=99))
    signals = engine.evaluate(state(now=NOW + 1, price=100.3, low_3m=99, volume_ratio=1.5,
                                    returns={"1m": .3, "3m": .5, "5m": .6, "15m": 1}))
    assert signals[0].strategy == "trend_breakout"


def test_pullback_and_fake_breakout_have_independent_state_machines():
    engine = StrategyEngine({"panic_rebound": {"enabled": False}, "pullback_entry": {"enabled": True}})
    returns = {"1m": -.5, "3m": 0, "5m": .5, "15m": 1}
    engine.evaluate(state(price=99, high_3m=100, returns=returns))
    signals = engine.evaluate(state(now=NOW + 1, price=99.3, low_3m=99, high_3m=100, returns=returns))
    assert signals[0].strategy == "pullback_entry"
    engine = StrategyEngine({"panic_rebound": {"enabled": False}, "fake_breakout_reverse": {"enabled": True}})
    engine.evaluate(state(price=100, low_3m=99, high_3m=100.1))
    engine.evaluate(state(now=NOW + 1, price=100.3, high_3m=100.3, low_3m=99))
    signals = engine.evaluate(state(now=NOW + 2, price=100, high_3m=100.3, low_3m=99, buy_pressure=.4))
    assert signals[0].strategy == "fake_breakout_reverse"
    assert signals[0].side == "SHORT"


def test_market_and_public_streams_are_routed_independently():
    service = DataService(SYMBOLS, lambda event: None)
    urls = service.stream_urls()
    assert "/market/stream?streams=" in urls["market"]
    assert "/public/stream?streams=" in urls["public"]
    assert "solusdt@aggTrade" in urls["market"]
    assert "btcusdt@kline_1m" in urls["market"]
    assert "ethusdt@depth20@500ms" in urls["public"]
    assert "@depth" not in urls["market"]
    assert all(.5 <= service.reconnect_delay(i) <= 60 for i in range(100))


def test_market_payload_logging_and_unknown_symbols_filtered():
    events, audit = [], []
    service = DataService(SYMBOLS, events.append, audit=lambda kind, payload: audit.append((kind, payload)))
    asyncio.run(service.handle_message(json.dumps({"stream": "solusdt@ticker", "data": tick()}), "market"))
    asyncio.run(service.handle_message(json.dumps(tick("DOGEUSDT")), "market"))
    asyncio.run(service.handle_message("not json", "market"))
    assert events == [tick()]
    assert audit[0][0] == "market_data"
    assert audit[-1][0] == "market_payload_error"


def test_consumer_reconnects_then_can_stop_without_network(monkeypatch):
    alerts, events = [], []
    service = DataService(SYMBOLS, events.append, alert=lambda kind, message: alerts.append(kind))
    attempts = []

    class BrokenConnection:
        async def __aenter__(self):
            attempts.append(1)
            if len(attempts) >= 2:
                service._stop.set()
            raise ConnectionError("offline test disconnect")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("app.data.service.websockets.connect", lambda *a, **kw: BrokenConnection())
    monkeypatch.setattr(service, "reconnect_delay", lambda *a: .001)
    asyncio.run(service._consume("public", service.stream_urls()['public']))
    assert len(attempts) == 2
    assert service.status()["public"]["connected"] is False
    assert "websocket_disconnect" in alerts
    assert events[0]["e"] == "feed_disconnected"
