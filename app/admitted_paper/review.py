"""Read-only ledger recap; simulated returns are not strategy validation."""
from decimal import Decimal as D


def review(summary):
    groups={}
    for kind in ('NORMAL_ADMITTED_8B','FIXTURE_EXISTING_POSITION'):
        ids={pid for pid,r in summary['reservations'].items() if r['origin']==kind}
        states=[s for pid,s in summary['positions'].items() if pid in ids]
        fills=[f for f in summary['fills'].values() if f['position_id'] in ids]
        closed=[s for s in states if s['phase']=='CLOSED']
        groups[kind]=dict(reservations=len(ids),confirmed_positions=len(states),closed=len(closed),
            realized_net_usdt=str(sum((D(s['realized_net_pnl']) for s in states),D(0))),
            fees_usdt=str(sum((D(f['fee_usdt']) for f in fills),D(0))),
            remaining_quantity=str(sum((D(s['remaining_quantity']) for s in states),D(0))))
    rejected=[r for k,r in summary['requests'].items() if k.startswith('admitted:') and r['result']=='REJECT']
    return dict(scope='8B_SYNTHETIC_LEDGER_REVIEW',strategy_efficacy='NOT_EVALUATED',full_policy_expected_return=None,
        win_probability=None,groups=groups,rejected_requests=len(rejected),
        rejection_reasons=[r['reason_codes'] for r in rejected],cash_usdt=summary['account']['cash'],
        pending_reconciliation=len(summary['pending_reconciliation']),live_allowed=False,
        limitations=['Conditional paths have no occurrence probabilities','No real market, liquidity or funding validation'])


def markdown(summary):
    return '# Stage 8B synthetic ledger review\n\n'+''.join(f'- {k}: {v}\n' for k,v in review(summary).items())
