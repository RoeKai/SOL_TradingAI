"""Ledger-based offline review. Fixture PnL is never strategy validation."""
from decimal import Decimal as D


def review(summary):
    states=list(summary['positions'].values());fills=list(summary['fills'].values())
    closed=[s for s in states if s['phase']=='CLOSED']
    return dict(scope='8A_SYNTHETIC_REVIEW',strategy_efficacy='NOT_EVALUATED',
        ordinary_entries_executed=0,fixture_positions=len(states),closed_fixture_positions=len(closed),
        cash_usdt=summary['account']['cash'],fees_usdt=str(sum((D(f['fee_usdt']) for f in fills),D(0))),
        realized_net_usdt=str(sum((D(s['realized_net_pnl']) for s in states),D(0))),
        remaining_quantity=str(sum((D(s['remaining_quantity']) for s in states),D(0))),
        pending_actions=len(summary['pending_actions']),
        limitations=['Synthetic liquidity and costs only','Runner policy return remains unmodeled',
                     'No realtime, account connectivity or live authority'])


def markdown(summary):
    data=review(summary)
    return '# Stage 8A offline ledger review\n\n'+''.join(f'- {k}: {v}\n' for k,v in data.items())
