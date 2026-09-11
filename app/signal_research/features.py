"""Past-only extraction: this module cannot receive a future price stream."""
from decimal import Decimal as D
from app.admission.models import fingerprint
from app.historical_replay.models import HistoricalCandidate
from app.offline_paper.storage import digest

SCALE = 100_000_000


def price_units(value):
    x = D(str(value)) * SCALE
    if not x.is_finite() or x <= 0 or x != x.to_integral_value() or x >= 2**63:
        raise ValueError('NONPOSITIVE_OR_LOSSY_PRICE')
    return int(x)


def milliseconds(value):
    x = D(str(value)) * 1000
    if not x.is_finite() or x != x.to_integral_value():
        raise ValueError('INTEGER_MILLISECOND_REQUIRED')
    return int(x)


def extract(candidate):
    c = HistoricalCandidate.model_validate(candidate)
    if c.candidate_id != digest(c.model_copy(update={'candidate_id': '0' * 64})):
        raise ValueError('CANDIDATE_CONTENT_CHANGED')
    s = c.setup
    at = milliseconds(s.created_at)
    if s.symbol != 'SOLUSDT' or s.side not in ('LONG', 'SHORT'):
        raise ValueError('FROZEN_SYMBOL_OR_DIRECTION_CHANGED')
    for point in c.evidence['window']:
        if point['at_ms'] > at:
            raise ValueError('FUTURE_FEATURE_POINT')
        for event in point['last'].values():
            if event['available_at_ms'] > point['at_ms'] or event['event_time_ms'] > event['available_at_ms']:
                raise ValueError('FUTURE_FEATURE_OBSERVATION')
    for evidence in s.structure_evidence:
        if evidence.observed_at is None or milliseconds(evidence.observed_at) > at:
            raise ValueError('FUTURE_OR_MISSING_STRUCTURE_CONFIRMATION')
    conditions = s.invalidation_conditions
    if (len(conditions) != 1 or conditions[0].kind != 'price' or
        conditions[0].operator != ('lte' if s.side == 'LONG' else 'gte') or
        conditions[0].price != s.initial_stop.price):
        raise ValueError('UNSUPPORTED_OR_CHANGED_ORIGINAL_INVALIDATION')
    raw = c.evidence['window'][-1]['last']['SOLUSDT']
    if price_units(s.entry.reference_price) != price_units(raw['price']):
        raise ValueError('ORIGINAL_PRICE_IDENTITY_MISMATCH')
    return dict(version='frozen-signal-features/v1', kind='SIGNAL',
        candidate_id=c.candidate_id, candidate_digest=digest(c), setup_digest=fingerprint(s),
        side=s.side, signal_at_ms=at, origin_price_units=price_units(raw['price']),
        origin_event_id=raw['event_id'], origin_event_time_ms=raw['event_time_ms'],
        origin_available_at_ms=raw['available_at_ms'], stop_units=price_units(s.initial_stop.price),
        invalidation=conditions[0].model_dump(mode='json'),
        direction_checks=c.evidence['direction_checks'],
        confidence_is_win_probability=False, dataset_digest=c.dataset_digest,
        data_use='DEVELOPMENT / ALREADY_EXAMINED', execution_authority='NONE')
