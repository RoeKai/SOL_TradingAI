"""Deterministic proposal interface. No indicators, AI, execution, or stop mutation."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Protocol

from app.setups.models import Record, Text
from .models import ExitState, MarketEvent, Positive
from .policy import ExitPolicy

D=Decimal


class RunnerProposal(Record):
    stop_price: Positive | None = None
    reason_code: Text


class RunnerRule(Protocol):
    def __call__(self, state: ExitState, market: MarketEvent,
                 policy: ExitPolicy) -> RunnerProposal: ...


def inward_tick(price, tick, side):
    return (price/tick).to_integral_value(rounding=ROUND_CEILING if side=='LONG' else ROUND_FLOOR)*tick


def break_even_price(state: ExitState, policy: ExitPolicy) -> Decimal:
    if policy.break_even_mode=='entry_price':
        return state.actual_average_entry
    cost=(state.entry_fees+state.exit_fees)/state.remaining_quantity
    fee=policy.expected_exit_fee_rate
    slip=policy.expected_exit_slippage_bps/10000
    if state.side=='LONG':
        return (state.actual_average_entry+cost)/((1-slip)*(1-fee))
    return (state.actual_average_entry-cost)/((1+slip)*(1+fee))


def _fresh_evidence(state,market,policy,kind):
    matches=[e for e in market.evidence if e.kind==kind and e.confirmed and
             state.opened_at<=e.observed_at<=market.received_at and
             D(str(market.received_at))-D(str(e.observed_at))<=policy.evidence_max_age_seconds]
    return max(matches,key=lambda e:e.observed_at) if matches else None


def fixed_r(state,market,policy):
    sign=1 if state.side=='LONG' else -1
    price=state.favorable_extreme-sign*policy.runner_trail_r*state.frozen_initial_r
    return RunnerProposal(stop_price=price if price>0 else None,reason_code='RUNNER_FIXED_R')


def swing(state,market,policy):
    evidence=_fresh_evidence(state,market,policy,'swing_low' if state.side=='LONG' else 'swing_high')
    if evidence is None or evidence.value is None:
        return RunnerProposal(reason_code='RUNNER_SWING_DATA_UNCONFIRMED')
    sign=1 if state.side=='LONG' else -1
    price=evidence.value-sign*policy.swing_buffer_r*state.frozen_initial_r
    return RunnerProposal(stop_price=price if price>0 else None,reason_code='RUNNER_SWING')


def atr(state,market,policy):
    evidence=_fresh_evidence(state,market,policy,'atr')
    if evidence is None or evidence.value is None:
        return RunnerProposal(reason_code='RUNNER_ATR_DATA_UNCONFIRMED')
    sign=1 if state.side=='LONG' else -1
    price=state.favorable_extreme-sign*policy.atr_multiple*evidence.value
    return RunnerProposal(stop_price=price if price>0 else None,reason_code='RUNNER_ATR')


def propose_runner(state: ExitState, market: MarketEvent, policy: ExitPolicy) -> RunnerProposal:
    sign=1 if state.side=='LONG' else -1
    if sign*(state.favorable_extreme-state.frozen_r_anchor_entry)<policy.runner_activation_r*state.frozen_initial_r:
        return RunnerProposal(reason_code='RUNNER_NOT_ACTIVATED')
    rule: RunnerRule={'fixed_r':fixed_r,'swing':swing,'atr':atr}[policy.runner_strategy]
    return rule(state,market,policy)


def trend_invalid(state,market,policy):
    evidence=_fresh_evidence(state,market,policy,'trend_invalid')
    return policy.trend_exit_enabled and evidence is not None and evidence.invalid is True
