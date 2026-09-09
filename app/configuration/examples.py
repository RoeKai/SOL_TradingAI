"""Minimal, deliberately incomplete synthetic descriptions; not runtime fixtures."""

from app.setups.models import TradeSetup
from app.setups.rr import calculate_rr
from .inputs import PlanInputs


def example_plan(name, side='LONG'):
    if name not in ('allocation-mismatch','runner-unmodeled'): raise ValueError('Unknown offline example')
    sign=1 if side=='LONG' else -1
    fractions=(.5,.5) if name=='allocation-mismatch' else (.3,.4,.3)
    setup=TradeSetup(setup_id='synthetic-config-example',plan_version='synthetic/v1',symbol='SOLUSDT',
        strategy_name='trend_breakout',strategy_type='trend_breakout',side=side,
        entry=dict(order_type='MARKET',reference_price=100,lower_price=100,upper_price=100),
        initial_stop=dict(price=100-sign*5,basis='Synthetic example only; no verified structure'),
        targets=tuple(dict(target_id='synthetic-target-'+str(i+1),price=100+sign*5*(i+1),fraction=f,
            basis='Synthetic price, not a market structure claim',kind='unverified') for i,f in enumerate(fractions)),
        cost_assumptions=dict(entry_fee_rate=.0005,exit_fee_rate=.0005,entry_slippage_bps=10,
            exit_slippage_bps=10,funding_cost_usdt=None,assumed_holding_seconds=3600,
            source='synthetic-cost-assumption-not-verified',observed_at=1800000000),
        created_at=1800000000,data_as_of=1800000000,valid_until=1800000060)
    return PlanInputs(setup=setup,rr=calculate_rr(setup,quantity=1))
