class PaperBroker:
    """Bid/ask + adverse slippage, fees, limit eligibility. Never owns a network client."""
    def __init__(self, fee_rate=.0005, slippage_bps=10):
        self.fee_rate = fee_rate
        self.slippage = slippage_bps/10000

    def fill(self, market, side, quantity, order_type='MARKET', limit_price=None):
        # Ticker and depth arrive independently. Never use an old, favorable
        # quote to erase a gap through a stop or improve a simulated entry.
        reference = max(market.ask or market.price, market.price) if side == 'BUY' else min(market.bid or market.price, market.price)
        if order_type == 'LIMIT':
            if limit_price is None or (side == 'BUY' and reference > limit_price) or (side == 'SELL' and reference < limit_price):
                return None
            price = min(reference*(1+self.slippage), limit_price) if side == 'BUY' else max(reference*(1-self.slippage), limit_price)
        else:
            price = reference*(1+self.slippage) if side == 'BUY' else reference*(1-self.slippage)
        return {'status': 'FILLED', 'executedQty': quantity, 'avgPrice': price,
                'fee': price*quantity*self.fee_rate}
