"""Single-scan observation index and labels. No candidate generation or trading.

15s bins retain ALL observed tick extrema, not a 15s sampled approximation.
First invalidation is located at a real aggregate event using threshold heaps.
Only compact bins and <=40,000 frozen features are retained, not all ticks.
"""
from dataclasses import dataclass
from decimal import Decimal as D, Context, localcontext
import heapq
from .features import SCALE

STEP = 15_000
HORIZONS = (60, 300, 900, 3600)
MAX_AGE = 15_000


@dataclass(frozen=True, slots=True)
class Bar:
    at: int
    price: int
    event_at: int
    available_at: int
    sequence: int
    high: int
    low: int
    gap: int
    count: int


class ObservationIndex:
    def __init__(self, events, features, *, start, end, warmup, progress=None):
        if start % STEP or end % STEP or warmup % STEP or not warmup < start < end:
            raise ValueError('ALIGNED_BOUNDED_RANGE_REQUIRED')
        if (end-warmup)//STEP > 180_000 or len(features) > 40_000:
            raise ValueError('RESEARCH_MEMORY_BOUND_EXCEEDED')
        self.start, self.end, self.warmup = start, end, warmup
        self.bars = []
        self.invalidations = {}
        ordered = sorted(features, key=lambda f: (f['signal_at_ms'], f['candidate_id']))
        if len({f['candidate_id'] for f in ordered}) != len(ordered):
            raise ValueError('DUPLICATE_CANDIDATE_ID')
        if any(f['signal_at_ms'] % STEP or not start <= f['signal_at_ms'] < end for f in ordered):
            raise ValueError('CANDIDATE_OUTSIDE_FROZEN_GRID')
        pending = 0
        longs, shorts = [], []
        boundary = warmup
        price = high = low = seq = count = gap = 0
        last_at = last_available = -1
        previous = None
        self.records = self.outside_visibility = 0

        def close_bin(t):
            nonlocal high, low, count, gap, pending
            terminal_gap = MAX_AGE if last_available < 0 else t-last_available
            self.bars.append(Bar(t, price, last_at, last_available, seq, high, low,
                                 max(gap, terminal_gap), count))
            while pending < len(ordered) and ordered[pending]['signal_at_ms'] == t:
                f = ordered[pending]
                if (f['origin_price_units'] != price or f['origin_event_time_ms'] != last_at or
                    f['origin_available_at_ms'] != last_available or
                    f['origin_event_id'] != 'SOLUSDT:agg:'+str(seq)):
                    raise ValueError('FROZEN_CANDIDATE_OBSERVATION_MISMATCH:'+f['candidate_id'])
                key = -f['stop_units'] if f['side'] == 'LONG' else f['stop_units']
                heapq.heappush(longs if f['side'] == 'LONG' else shorts,
                    (key, f['candidate_id'], t+3_600_000))
                pending += 1
            high = low = count = gap = 0

        for e in events:
            at, available, p, sequence = e
            if (p <= 0 or at < warmup or at >= end or available < at or
                previous is not None and (at < previous[0] or available < previous[1] or sequence <= previous[3])):
                raise ValueError('OBSERVATION_TIME_ID_OR_PRICE_INVALID')
            previous = e
            self.records += 1
            if available >= end:
                self.outside_visibility += 1
                continue
            while boundary < available:
                close_bin(boundary)
                boundary += STEP
            # Prefix extrema do not include the invalidating event itself.
            for side, heap in (('LONG', longs), ('SHORT', shorts)):
                while heap:
                    key, cid, expires = heap[0]
                    if expires < available:
                        heapq.heappop(heap)
                        continue
                    crossed = p <= -key if side == 'LONG' else p >= key
                    if not crossed:
                        break
                    heapq.heappop(heap)
                    self.invalidations[cid] = dict(event_time_ms=at, available_at_ms=available,
                        event_id='SOLUSDT:agg:'+str(sequence), price_units=p,
                        bin_index=len(self.bars), prefix_high=high, prefix_low=low)
            gap = max(gap, available-last_available if last_available >= 0 else available-warmup)
            high = max(high, p)
            low = min(low, p) if low else p
            count += 1
            price, last_at, last_available, seq = p, at, available, sequence
            if progress and self.records % 1_000_000 == 0:
                progress(dict(parsed_sol_records=self.records, visible_at_ms=available))
        while boundary <= end:
            close_bin(boundary)
            boundary += STEP
        if pending != len(ordered):
            raise ValueError('UNCONSUMED_FROZEN_CANDIDATES')

    def number(self, at):
        if at % STEP or not self.warmup <= at <= self.end:
            raise ValueError('OUTSIDE_OBSERVATION_GRID')
        return (at-self.warmup)//STEP

    def bar(self, at):
        return self.bars[self.number(at)]

    def volatility(self, at):
        i = self.number(at)
        if i < 20 or self.bars[i].price <= 0:
            return None, None
        origin, path = self.bars[i-20], self.bars[i-19:i+1]
        if (origin.price <= 0 or origin.at-origin.event_at >= MAX_AGE or
            self.bars[i].at-self.bars[i].event_at >= MAX_AGE or any(b.gap >= MAX_AGE for b in path)):
            return None, None
        values_h = [origin.price, *(b.high for b in path if b.high)]
        values_l = [origin.price, *(b.low for b in path if b.low)]
        v = D(max(values_h)-min(values_l))*10000/D(self.bars[i].price)
        band = next((n for n, threshold in enumerate((10, 25, 50, 100)) if v < threshold), 4)
        return v, band

    def background(self):
        first=((self.start+899_999)//900_000)*900_000
        for at in range(first, self.end, 900_000):
            b = self.bar(at)
            for side in ('LONG', 'SHORT'):
                yield dict(kind='BACKGROUND', candidate_id=f'background:{at}:{side}', side=side,
                    signal_at_ms=at, origin_price_units=b.price, origin_event_time_ms=b.event_at,
                    origin_available_at_ms=b.available_at, origin_event_id='SOLUSDT:agg:'+str(b.sequence),
                    candidate_digest=None, setup_digest=None, stop_units=None)

    def label(self, feature, seconds):
        if seconds not in HORIZONS:
            raise ValueError('UNREGISTERED_HORIZON')
        with localcontext(Context(prec=40)):
            t = feature['signal_at_ms']; deadline = t+seconds*1000
            i = self.number(t); last = min(deadline, self.end)
            j = self.number(last)
            origin, endpoint = self.bars[i], self.bars[j]
            path = self.bars[i+1:j+1]
            p = feature['origin_price_units']; sign = 1 if feature['side'] == 'LONG' else -1
            flags = []
            if deadline >= self.end: flags.append('CENSORED')
            if p <= 0 or t-feature['origin_event_time_ms'] >= MAX_AGE: flags.append('MISSING_OR_STALE_ORIGIN')
            if endpoint.price <= 0 or last-endpoint.event_at >= MAX_AGE: flags.append('MISSING_OR_STALE_ENDPOINT')
            if any(b.gap >= MAX_AGE for b in path): flags.append('INSUFFICIENT_COVERAGE')
            highs = [p, *(b.high for b in path if b.high)]
            lows = [p, *(b.low for b in path if b.low)]
            mfe = max(highs)-p if sign == 1 else p-min(lows)
            mae = p-min(lows) if sign == 1 else max(highs)-p
            delta = sign*(endpoint.price-p)
            invalid = self.invalidations.get(feature['candidate_id'])
            invalid = invalid if invalid and invalid['available_at_ms'] <= min(deadline, self.end-1) else None
            before = None
            if feature['kind'] == 'SIGNAL':
                before = mfe
                if invalid:
                    prior = self.bars[i+1:invalid['bin_index']]
                    if sign == 1:
                        before = max([p, invalid['prefix_high'], *(b.high for b in prior)])-p
                    else:
                        before = p-min([p, *(x for x in [invalid['prefix_low'], *(b.low for b in prior)] if x)])
            def dist(x):
                return dict(usdt_per_sol=D(x)/SCALE, bps=D(x)*10000/p) if p > 0 else None
            vol, band = self.volatility(t)
            return dict(version='fixed-window-observation-label/v1', scope='PRICE_LABEL_NOT_TRADE_PNL',
                kind=feature['kind'], candidate_id=feature['candidate_id'], candidate_digest=feature['candidate_digest'],
                setup_digest=feature['setup_digest'], side=feature['side'], signal_at_ms=t,
                origin_event_id=feature['origin_event_id'], origin_event_time_ms=feature['origin_event_time_ms'],
                origin_available_at_ms=feature['origin_available_at_ms'], origin_price=D(p)/SCALE,
                horizon_seconds=seconds, deadline_ms=deadline, observed_through_ms=last,
                endpoint_event_time_ms=endpoint.event_at, endpoint_available_at_ms=endpoint.available_at,
                status=flags[0] if flags else 'COMPLETE', flags=flags, eligible=not flags,
                observed_return=dist(delta), mfe=dist(max(0, mfe)), mae=dist(max(0, mae)),
                first_observed_invalidation=invalid, mfe_before_invalidation=dist(before) if before is not None else None,
                later_favorable_after_invalidation=bool(invalid and mfe > before),
                invalidation_status='NOT_APPLICABLE' if feature['kind']=='BACKGROUND' else 'OBSERVED' if invalid else 'NOT_OBSERVED',
                past_volatility_bps=vol, volatility_band=band,
                coverage=dict(observed_events=sum(b.count for b in path), max_observation_gap_ms=max((b.gap for b in path),default=0)),
                simulated_profit=None, execution_authority='NONE', data_use='DEVELOPMENT / ALREADY_EXAMINED')
