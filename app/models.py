from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Candle:
    symbol: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True


@dataclass
class MarketState:
    symbol: str
    timestamp: float
    price: float
    returns: dict[str, float]
    momentum: float
    volume_ratio: float
    buy_pressure: float
    low_3m: float
    high_3m: float
    btc_return_3m: float | None
    eth_return_3m: float | None
    ready: bool
    stale: bool
    bid: float = 0
    ask: float = 0
    reason: str = ''


@dataclass
class Signal:
    id: str
    strategy: str
    symbol: str
    side: str
    entry_price: float
    stop_price: float
    take_profits: list[dict]
    created_at: float
    order_type: str = 'MARKET'
    reason: str = ''
    score: float = 0.0


@dataclass
class Position:
    id: str
    strategy: str
    symbol: str
    side: str
    entry_price: float
    quantity: float
    initial_quantity: float
    stop_price: float
    take_profits: list[dict]
    opened_at: float
    leverage: int
    realized_pnl: float = 0
    fees: float = 0
    stop_id: str = ''
    stop_status: str = 'PENDING'
    tp_completed: list[int] = field(default_factory=list)
    tp_deferred: list[int] = field(default_factory=list)

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.quantity * (1 if self.side == 'LONG' else -1)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketRules:
    step_size: str = '0.001'
    tick_size: str = '0.01'
    min_qty: float = 0.001
    min_notional: float = 5


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list[str]
    quantity: float = 0
    max_loss: float = 0
    margin: float = 0
