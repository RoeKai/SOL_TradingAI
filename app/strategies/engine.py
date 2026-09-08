"""Deterministic signal proposals. These classes have no order authority.

Returns and threshold percentages use percentage points (2 means 2%).
Panic rebound's retracement is a fraction of the observed absolute decline.
All entries must separately pass the runtime's risk and execution approvals.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import json
from typing import Any

from app.models import MarketState, Signal


@dataclass
class Observation:
    started: float
    anchor: float
    extreme: float
    side: str = "LONG"


class StrategyEngine:
    NAMES = ("panic_rebound", "trend_breakout", "pullback_entry", "fake_breakout_reverse")

    def __init__(self, config: dict | None = None, symbol: str = "SOLUSDT"):
        config = config or {}
        if set(config) - set(self.NAMES):
            raise ValueError('Unknown strategy name')
        self.symbol = symbol
        self.config = {
            name: {"enabled": name == "panic_rebound", **config.get(name, {})}
            for name in self.NAMES
        }
        common = {'enabled', 'order_type', 'cooldown_seconds', 'observation_seconds', 'min_score',
                  'max_stop_distance_pct', 'btc_crash_pct', 'btc_max_drop_pct'}
        parameters = {
            'panic_rebound': {'drop_threshold_pct', 'drop_pct', 'rebound_fraction', 'stop_buffer_pct'},
            'trend_breakout': {'min_trend_pct', 'breakout_buffer_pct', 'min_volume_ratio', 'min_buy_pressure', 'stop_distance_pct'},
            'pullback_entry': {'min_trend_pct', 'pullback_pct', 'bounce_pct'},
            'fake_breakout_reverse': {'breakout_buffer_pct'},
        }
        for name, settings in self.config.items():
            if set(settings) - common - parameters[name]:
                raise ValueError(f'{name} contains an unknown parameter')
            if not isinstance(settings["enabled"], bool):
                raise ValueError(f"{name}.enabled must be a boolean")
            for key, value in settings.items():
                if key in {"enabled", "order_type"}:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{name}.{key} must be a finite number")
                if key in {"drop_threshold_pct", "btc_crash_pct"}:
                    if not -100 < value < 0:
                        raise ValueError(f"{name}.{key} must be between -100 and 0")
                elif value <= 0:
                    raise ValueError(f"{name}.{key} must be positive")
            if settings.get("order_type", "MARKET") not in {"MARKET", "LIMIT"}:
                raise ValueError(f"{name}.order_type must be MARKET or LIMIT")
            if not 0 < settings.get("rebound_fraction", 0.25) <= 1:
                raise ValueError(f"{name}.rebound_fraction must be in (0, 1]")
            if settings.get('min_score', 60) > 100:
                raise ValueError(f'{name}.min_score must not exceed 100')
            if settings.get('min_buy_pressure', .55) > 1:
                raise ValueError(f'{name}.min_buy_pressure must be <= 1')
        self.observations: dict[str, Observation] = {}
        self._last_signal: dict[str, float] = {}
        self._last_evaluated = 0.0
        self._previous: MarketState | None = None
        self._reason = {name: "waiting_for_market" for name in self.NAMES}
        self._awaiting_rearm: set[str] = set()
        self._scores = {name: 0.0 for name in self.NAMES}
        self._evidence: dict[str, dict] = {name: {} for name in self.NAMES}
        self._eligible: set[str] = set()
        self._selected: str | None = None
        self._fingerprint = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

    def status(self) -> dict[str, Any]:
        return {
            name: {"enabled": bool(self.config[name]["enabled"]),
                   "state": "observing" if name in self.observations else "idle",
                   "reason": self._reason[name], "last_signal_at": self._last_signal.get(name),
                   "score": self._scores[name], "evidence": self._evidence[name],
                   "eligible": name in self._eligible, "selected": name == self._selected,
                   "observation": vars(self.observations[name]).copy() if name in self.observations else None}
            for name in self.NAMES
        }

    def checkpoint(self):
        # Observation/previous prices are intentionally NOT restored across outages.
        return {'version': 1, 'fingerprint': self._fingerprint,
                'last_signal': self._last_signal, 'awaiting_rearm': sorted(self._awaiting_rearm)}

    def restore(self, saved, now):
        if not isinstance(saved, dict) or saved.get('version') != 1:
            return
        for name, stamp in saved.get('last_signal', {}).items():
            if name in self.NAMES and isinstance(stamp, (int, float)) and math.isfinite(stamp) and 0 < stamp <= now:
                self._last_signal[name] = stamp
        if saved.get('fingerprint') == self._fingerprint:
            self._awaiting_rearm = set(saved.get('awaiting_rearm', [])) & set(self.NAMES)

    def invalidate(self, reason='market_not_ready'):
        self.observations.clear()
        self._previous = None
        self._eligible.clear()
        self._selected = None
        self._scores = {name: 0.0 for name in self.NAMES}
        self._evidence = {name: {} for name in self.NAMES}
        self._reason = {name: reason if self.config[name]['enabled'] else 'disabled' for name in self.NAMES}

    def _score(self, name, state, eligible):
        """Transparent heuristic, NOT a probability or an alternative risk gate.

        Confirmed setup=60, volume confirmation=up to 15, directional book
        pressure=up to 15, positive directional momentum=up to 10.
        Unconfirmed observations get at most 59 and are never executable.
        """
        observation = self.observations.get(name)
        side = observation.side if observation else 'LONG'
        pressure = state.buy_pressure if side == 'LONG' else 1 - state.buy_pressure
        momentum = state.momentum if side == 'LONG' else -state.momentum
        parts = {'setup': 60 if eligible else 25 if observation else 0,
                 'volume': round(min(15, max(0, state.volume_ratio - 1) * 15), 2),
                 'pressure': round(min(15, max(0, pressure - .5) * 50), 2),
                 'momentum': round(min(10, max(0, momentum) * 10), 2)}
        self._evidence[name] = parts
        return min(100 if eligible else 59, round(sum(parts.values()), 2))

    def evaluate(self, state: MarketState, has_position: bool = False) -> list[Signal]:
        if state.symbol != self.symbol:
            return []
        if (not state.ready or state.stale or state.price <= 0 or not math.isfinite(state.price)
                or state.btc_return_3m is None or state.eth_return_3m is None):
            self.invalidate()
            return []
        if has_position:
            self.invalidate('existing_position_no_pyramiding')
            self._previous = state
            self._reason = {name: "existing_position_no_pyramiding" for name in self.NAMES}
            return []
        if state.timestamp <= self._last_evaluated:
            return []
        self._last_evaluated = state.timestamp
        candidates = []
        self._eligible.clear()
        self._selected = None
        previous = self._previous
        for name in self.NAMES:
            settings = self.config[name]
            if not settings["enabled"]:
                self.observations.pop(name, None)
                self._reason[name] = "disabled"
                self._scores[name], self._evidence[name] = 0, {}
                continue
            if state.timestamp - self._last_signal.get(name, -float("inf")) < float(settings.get("cooldown_seconds", 300)):
                self._reason[name] = "cooldown"
                self._scores[name], self._evidence[name] = 0, {}
                continue
            observation = self.observations.get(name)
            if observation and state.timestamp - observation.started > float(settings.get("observation_seconds", 300)):
                self.observations.pop(name, None)
                self._awaiting_rearm.add(name)
                self._reason[name] = "observation_expired_waiting_for_reset"
            proposal = getattr(self, f"_{name}")(state, previous, settings)
            self._scores[name] = self._score(name, state, proposal is not None)
            if proposal is not None:
                proposal.score = self._scores[name]
                if proposal.score >= float(settings.get('min_score', 60)):
                    self._eligible.add(name)
                    candidates.append(proposal)
                    self._reason[name] = 'eligible_not_selected'
                else:
                    self._reason[name] = 'score_below_threshold'
        self._previous = state
        if not candidates:
            return []
        # All four evaluated; score wins, stable NAMES priority resolves exact ties.
        winner = sorted(candidates, key=lambda p: (-p.score, self.NAMES.index(p.strategy)))[0]
        self._selected = winner.strategy
        self._last_signal[winner.strategy] = state.timestamp
        self._awaiting_rearm.add(winner.strategy)
        self._reason[winner.strategy] = 'signal_proposed_risk_approval_required'
        self.observations.clear()
        return [winner]

    def _long_allowed(self, state: MarketState, settings: dict, name: str) -> bool:
        limit = -abs(float(settings.get("btc_crash_pct", settings.get("btc_max_drop_pct", 0.8))))
        if state.btc_return_3m is None or state.btc_return_3m <= limit:
            self._reason[name] = "btc_downside_filter"
            return False
        return True

    def _signal(self, name: str, state: MarketState, stop: float, settings: dict,
                reason: str, side: str = "LONG") -> Signal | None:
        risk = (state.price - stop) if side == "LONG" else (stop - state.price)
        if not math.isfinite(risk) or risk <= 0 or risk / state.price > float(settings.get("max_stop_distance_pct", 5.0)) / 100:
            self._reason[name] = "invalid_stop_distance"
            return None
        multiplier = 1 if side == "LONG" else -1
        tps = [{"price": state.price + multiplier * risk * rr, "fraction": fraction}
               for rr, fraction in ((1.0, 0.5), (2.0, 0.5))]
        identity = f"{name}:{state.symbol}:{side}:{state.timestamp:.3f}"
        signal_id = "sol-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        return Signal(id=signal_id, strategy=name, symbol=state.symbol, side=side,
                      entry_price=state.price, stop_price=stop, take_profits=tps,
                      created_at=state.timestamp, order_type=str(settings.get("order_type", "MARKET")), reason=reason)

    def _panic_rebound(self, state: MarketState, previous: MarketState | None, settings: dict) -> Signal | None:
        name = "panic_rebound"
        drop = state.returns.get("3m", 0)
        threshold = -abs(float(settings.get("drop_threshold_pct", settings.get("drop_pct", 2.0))))
        if drop > threshold * 0.5:
            self._awaiting_rearm.discard(name)
        if name in self._awaiting_rearm:
            self._reason[name] = "waiting_for_new_panic_cycle"
            return None
        observation = self.observations.get(name)
        if observation is None:
            if drop >= threshold:
                self._reason[name] = "waiting_for_3m_drop"
                return None
            anchor = state.price / (1 + drop / 100)
            self.observations[name] = Observation(state.timestamp, anchor, min(state.low_3m, state.price))
            self._reason[name] = "watching_low_and_rebound"
            return None  # Requires a subsequent market event, not replayed warmup.
        observation.extreme = min(observation.extreme, state.low_3m, state.price)
        decline = observation.anchor - observation.extreme
        if decline <= 0 or state.price - observation.extreme < decline * float(settings.get("rebound_fraction", 0.25)):
            self._reason[name] = "rebound_not_confirmed"
            return None
        if not self._long_allowed(state, settings, name):
            return None
        stop = observation.extreme * (1 - float(settings.get("stop_buffer_pct", 0.15)) / 100)
        return self._signal(name, state, stop, settings, "SOL 3m panic decline then 25% retracement; BTC filter passed")

    def _trend_breakout(self, state: MarketState, previous: MarketState | None, settings: dict) -> Signal | None:
        name = "trend_breakout"
        if previous is None or state.returns.get("15m", 0) < float(settings.get("min_trend_pct", 0.5)):
            self._reason[name] = "waiting_for_trend"
            return None
        if (state.price <= previous.high_3m * (1 + float(settings.get("breakout_buffer_pct", 0.05)) / 100)
                or state.volume_ratio < float(settings.get("min_volume_ratio", 1.2))
                or state.buy_pressure < float(settings.get("min_buy_pressure", 0.55))):
            self._reason[name] = "breakout_not_confirmed"
            return None
        if not self._long_allowed(state, settings, name):
            return None
        stop = max(state.low_3m, state.price * (1 - float(settings.get("stop_distance_pct", 0.8)) / 100))
        return self._signal(name, state, stop, settings, "3m range breakout with positive 15m trend and volume")

    def _pullback_entry(self, state: MarketState, previous: MarketState | None, settings: dict) -> Signal | None:
        name = "pullback_entry"
        if state.returns.get("15m", 0) < float(settings.get("min_trend_pct", 0.8)):
            self.observations.pop(name, None)
            self._awaiting_rearm.discard(name)
            self._reason[name] = "waiting_for_uptrend"
            return None
        if name in self._awaiting_rearm:
            self._reason[name] = 'waiting_for_new_pullback_cycle'
            return None
        observation = self.observations.get(name)
        if observation is None:
            if state.returns.get("1m", 0) <= -float(settings.get("pullback_pct", 0.3)):
                self.observations[name] = Observation(state.timestamp, state.high_3m, state.price)
                self._reason[name] = "pullback_observed"
            else:
                self._reason[name] = "waiting_for_pullback"
            return None
        observation.extreme = min(observation.extreme, state.price)
        if state.price < observation.extreme * (1 + float(settings.get("bounce_pct", 0.2)) / 100):
            self._reason[name] = "waiting_for_support_bounce"
            return None
        if not self._long_allowed(state, settings, name):
            return None
        return self._signal(name, state, observation.extreme * 0.9985, settings, "Uptrend pullback support rebound")

    def _fake_breakout_reverse(self, state: MarketState, previous: MarketState | None, settings: dict) -> Signal | None:
        name = "fake_breakout_reverse"
        if previous is None:
            return None
        observation = self.observations.get(name)
        buffer = float(settings.get("breakout_buffer_pct", 0.05)) / 100
        if name in self._awaiting_rearm:
            if previous.low_3m <= state.price <= previous.high_3m:
                self._awaiting_rearm.discard(name)
            self._reason[name] = 'waiting_for_new_breakout_cycle'
            return None
        if observation is None:
            if state.price > previous.high_3m * (1 + buffer):
                self.observations[name] = Observation(state.timestamp, previous.high_3m, state.price, "SHORT")
            elif state.price < previous.low_3m * (1 - buffer):
                self.observations[name] = Observation(state.timestamp, previous.low_3m, state.price, "LONG")
            self._reason[name] = "watching_range_breakout"
            return None
        if observation.side == "SHORT":
            observation.extreme = max(observation.extreme, state.price)
            if state.price >= observation.anchor * (1 - buffer) or state.buy_pressure > 0.5:
                self._reason[name] = "waiting_for_failed_upward_breakout"
                return None
            return self._signal(name, state, observation.extreme * 1.0015, settings,
                                "Failed upward range breakout and selling pressure", "SHORT")
        observation.extreme = min(observation.extreme, state.price)
        if state.price <= observation.anchor * (1 + buffer) or state.buy_pressure < 0.5:
            self._reason[name] = "waiting_for_failed_downward_breakout"
            return None
        if not self._long_allowed(state, settings, name):
            return None
        return self._signal(name, state, observation.extreme * 0.9985, settings,
                            "Failed downward range breakout and buying pressure")
