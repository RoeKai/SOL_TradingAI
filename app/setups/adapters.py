"""One-way, opt-in legacy description. No conversion to executable Signal.

The running strategies still create their original Signal. They do NOT call
this adapter. It does not read config, account state, a clock or any service.
Legacy score/target evidence must not acquire new authority through mapping.
"""

from dataclasses import asdict

from app.models import Signal
from .models import Evidence, EntryPlan, InitialStop, ScoreBreakdown, TargetLevel, TradeSetup


STRATEGY_TYPES = ('trend_breakout', 'pullback_entry', 'panic_rebound', 'fake_breakout_reverse')


def adapt_legacy_signal(signal: Signal) -> TradeSetup:
    """Copy a structurally valid legacy signal into an unassessed description.

    Invalid or richer legacy payloads raise explicitly rather than silently
    discarding fields. This cannot affect execution: no runtime imports it.
    """
    if type(signal) is not Signal:
        raise TypeError('Expected the unchanged legacy Signal type')
    raw = asdict(signal)  # Deep copy: no mutable target list is shared.
    targets = []
    for index, target in enumerate(raw['take_profits'], 1):
        if not isinstance(target, dict) or set(target) != {'price', 'fraction'}:
            raise ValueError('Legacy target must have exactly price and fraction; no silent field loss')
        targets.append(TargetLevel(target_id=f'legacy-tp-{index}', price=target['price'],
            fraction=target['fraction'], kind='legacy_unspecified',
            basis='Copied legacy target; structural origin and RR are unknown'))
    evidence = Evidence(evidence_id='legacy-reason', kind='legacy_unspecified',
        description=raw['reason'] or 'Legacy Signal supplies no structural explanation',
        status='unverified', source='legacy_signal')
    return TradeSetup(plan_version='legacy-signal-adapter/v1', setup_id=raw['id'], origin='legacy_signal',
        symbol=raw['symbol'], strategy_name=raw['strategy'],
        strategy_type=raw['strategy'] if raw['strategy'] in STRATEGY_TYPES else 'legacy_unknown',
        side=raw['side'], structure_evidence=(evidence,),
        entry=EntryPlan(order_type=raw['order_type'], reference_price=raw['entry_price'],
                        lower_price=raw['entry_price'], upper_price=raw['entry_price'], basis='legacy_point'),
        initial_stop=InitialStop(price=raw['stop_price'], basis='Copied legacy stop; structural basis unverified'),
        targets=tuple(targets), created_at=raw['created_at'],
        score=ScoreBreakdown(legacy_score=raw['score']),
        compatibility_notes=('Descriptive copy only; the original Signal and runtime remain unchanged',
                             'Legacy targets are not certified structure targets; RR and costs stay unknown',
                             'Legacy score is not the eight-dimension score; grade and confidence stay unknown',
                             'No execution conversion, admission, expiry inference or data access is performed'))
