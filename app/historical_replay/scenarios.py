"""Conditional cash paths with assumed spread and pre-known funding BUDGET.

Trade fills and PnL use the accepted Exit reducer via PathExperiment. No future
funding dataset is an input; budget is not a settlement or a probability.
"""
from decimal import Decimal as D, Context, localcontext, ROUND_CEILING, ROUND_FLOOR
from app.admitted_paper.scenarios import PathExperiment
from app.admitted_paper.models import Scenario
from app.exits.models import MarketEvent
from app.offline_paper.storage import digest
from app.setups.rr import calculate_rr
from app.admitted_paper.provider import number
from .models import HistoricalScenarios
from .data import HistoricalError


class SpreadPath(PathExperiment):
    def __init__(self,plan,snapshot,quantity,entry_quote):
        self.half=plan.spread_bps/20000
        sign=1 if plan.side=='LONG' else -1
        super().__init__(plan,snapshot,quantity,entry_quote*(1+sign*self.half))

    def tick(self,price):
        if price<=0: raise HistoricalError('SCENARIO_NONPOSITIVE_PRICE')
        self.at+=1
        bid=price*(1-self.half);ask=price*(1+self.half)
        self.quote=bid if self.sign==1 else ask
        self.event(MarketEvent,observed_at=self.at,bid=bid,ask=ask,confirmed=True)
        self.controls()

    def tp(self,name,multiple):
        raw=self.state.frozen_r_anchor_entry+self.sign*self.state.frozen_initial_r*multiple
        # Choose a market trade price whose modeled exit-side quote reaches TP.
        mid=raw/(1-self.sign*self.half)
        tick=self.rules.price_tick
        mid=(mid/tick).to_integral_value(rounding=ROUND_CEILING if self.sign==1 else ROUND_FLOOR)*tick
        self.tick(mid);self.fill(name)


def path(plan,snapshot,q,entry,name):
    assumptions=('Conditional ordered path, not probability or expected return',
        'Trade price plus ASSUMED spread; adverse tick rounding and explicit taker fee',
        'Partial TP fills explicitly confirmed before stop replacement; first fill freezes R',
        'Atomic fixed-quantity replacement confirmed before retrace',
        'Worst pre-run funding allowance subtracted independently; NOT future actual settlement',
        'Bounded path within '+str(plan.holding_seconds)+' seconds; no gap-loss guarantee')
    if name=='S3' and plan.runner_reference_price is None:
        return Scenario(scenario_id=name,status='UNAVAILABLE',assumptions=assumptions,quantity=q,entry_quote=entry,
            reason_codes=('NO_SUPPORTED_RUNNER_STRUCTURE',))
    exp=SpreadPath(plan,snapshot,q,entry)
    if name!='S0':
        exp.tp('TP1',plan.policy.tp1_r)
        if name in ('S2','S3'): exp.tp('TP2',plan.policy.tp2_r)
        if name=='S3':
            ref=plan.runner_reference_price
            liquidation_quote=ref*(1-exp.sign*exp.half)
            if (liquidation_quote-exp.state.frozen_r_anchor_entry)*exp.sign<plan.policy.runner_activation_r*exp.state.frozen_initial_r:
                return Scenario(scenario_id=name,status='UNAVAILABLE',assumptions=assumptions,quantity=q,entry_quote=entry,
                    reason_codes=('ACTUAL_REFERENCE_GEOMETRY_CANNOT_ACTIVATE_RUNNER',))
            exp.tick(ref)
    # Market retrace to stop-side quote; the fill remains adverse, not at peak.
    exp.tick(exp.state.current_stop/(1-exp.sign*exp.half));exp.fill('CLOSE_ALL')
    if exp.state.phase!='CLOSED' or exp.state.remaining_quantity!=0: raise HistoricalError('SCENARIO_NOT_CLOSED')
    gross=sum((x.gross_cash_flow for x in exp.legs),D(0));fee=sum((x.fee_usdt for x in exp.legs),D(0))
    if gross!=exp.state.realized_gross_pnl or gross-fee!=exp.state.realized_net_pnl:
        raise HistoricalError('SCENARIO_TRADE_CASHFLOW_CONSERVATION_FAILED')
    return Scenario(scenario_id=name,status='SUPPORTED',assumptions=assumptions,quantity=q,entry_quote=entry,
        legs=tuple(exp.legs),gross_pnl=gross,fees_usdt=fee,net_pnl=gross-fee-plan.pre_entry_funding_budget_usdt,
        frozen_initial_r=exp.state.frozen_initial_r,final_stop=exp.state.current_stop,
        reference_structure_price=plan.runner_reference_price if name=='S3' else None)


def evaluate_scenarios(plan,setup,snapshot,quantity):
    with localcontext(Context(prec=50)):
        q=D(quantity)
        if q<=0 or q%plan.rules.quantity_step: raise HistoricalError('SCENARIO_QUANTITY_INVALID')
        static=calculate_rr(setup,quantity=number(q))
        s0=min((path(plan,snapshot,q,e,'S0') for e in sorted({plan.entry_lower,plan.entry_upper,plan.reference_entry})),key=lambda s:s.net_pnl)
        risk=-s0.net_pnl
        if risk<=0: raise HistoricalError('INVALID_INITIAL_RISK')
        results=[s0,*[path(plan,snapshot,q,plan.reference_entry,n) for n in ('S1','S2','S3')]]
        results=tuple(s.model_copy(update={'initial_stop_risk_usdt':risk,'scenario_net_rr':None if s.net_pnl is None else s.net_pnl/risk}) for s in results)
        out=HistoricalScenarios(evaluation_id='0'*64,exit_plan_digest=digest(plan),setup_digest=plan.setup_digest,
            quantity=q,conservative_initial_risk_usdt=risk,scenarios=results,static_rr_digest=digest(static),
            pre_entry_funding_budget_usdt=plan.pre_entry_funding_budget_usdt,
            limitations=('Conditional paths only; whole-policy valuation and probabilities UNKNOWN',
                'Assumed spread/slippage/liquidity/rules are not historical order-book reconstruction',
                'Fixed ex-ante funding allowance; actual funding is separate later cash flow',
                'No liquidation, maintenance margin or ADL engine'))
        return out.model_copy(update={'evaluation_id':digest(out)})
