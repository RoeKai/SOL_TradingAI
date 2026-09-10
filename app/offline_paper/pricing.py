"""Shared adverse synthetic fill-price model. No orders or external venue claim."""
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING


def execution_price(quoted, side, slippage_bps, tick):
    price=quoted*(1+slippage_bps/D(10000) if side=='BUY' else 1-slippage_bps/D(10000))
    return (price/tick).to_integral_value(rounding=ROUND_CEILING if side=='BUY' else ROUND_FLOOR)*tick
