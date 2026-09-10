"""Conditional path experiments using the accepted Stage 6 reducer/accounting.

Not a forecast, probability or replacement for Stage 3 static RR. The local
event driver confirms synthetic fills explicitly; price touch is never a fill.
No database, clock, network or account lookup occurs in this module.
"""
from decimal import Decimal as D, Context, localcontext, ROUND_FLOOR, ROUND_CEILING
from app.exits.engine import initialize_exit, apply_event
from app.exits.models import ExitSeed, EntryFill, EntrySealed, ExitFill, ActionReceipt, MarketEvent
from app.offline_paper.models import OfflineError
from app.offline_paper.pricing import execution_price
from app.offline_paper.storage import digest
from app.setups.rr import calculate_rr
from .models import Scenario, ScenarioLeg, ScenarioEvaluation
from .provider import number

LIMITATIONS=('Conditional ordered tick paths, not probabilities or a policy expected return',
    'S0 bounded quote/slippage model is not a gap-loss guarantee',
    'Fixed-R runner only; finite configured time horizon and explicit zero-funding synthetic contract',
    'No liquidity forecast, liquidation engine, intrabar ordering inference or real exchange authentication')


class PathExperiment:
    """Synthetic receipts to the SAME exit reducer, not a second exit/PnL engine."""
    def __init__(self,plan,snapshot,quantity,entry_quote):
        self.plan=plan;self.policy=plan.policy;self.rules=plan.rules;self.costs=dict(plan.costs)
        self.at=plan.created_at;self.index=0;self.legs=[];self.quote=entry_quote
        self.sign=1 if plan.side=='LONG' else -1
        side='BUY' if self.sign==1 else 'SELL'
        fill=execution_price(entry_quote,side,self.costs['entry_slippage_bps'],self.rules.price_tick)
        fee=quantity*fill*self.costs['entry_fee_rate']
        self.seed=ExitSeed(position_id='conditional-position',plan=snapshot,rules=self.rules,
            first_fill=EntryFill(event_id='first',position_id='conditional-position',received_at=self.at,
                fill_id='opening-fill',entry_action_id='conditional-entry',quantity=quantity,price=fill,fee_usdt=fee,occurred_at=self.at))
        self.state=initialize_exit(self.seed,self.policy).state
        self._leg('ENTRY',quantity,entry_quote,fill,fee)
        self.controls()
        self.event(EntrySealed,entry_action_id='conditional-entry',total_filled_quantity=quantity)
        self.controls()

    def event(self,cls,**kwargs):
        self.index+=1
        self.state=apply_event(self.seed,self.policy,self.state,cls(event_id='path-'+str(self.index),
            position_id=self.state.position_id,received_at=self.at,**kwargs)).state
        if self.state.faults: raise OfflineError('SCENARIO_REDUCER_FAULT:'+','.join(self.state.faults))

    def _leg(self,kind,q,quote,price,fee):
        self.legs.append(ScenarioLeg(action=kind,quantity=q,quote=quote,fill_price=price,
            gross_cash_flow=q*price*(-self.sign if kind=='ENTRY' else self.sign),fee_usdt=fee,
            occurred_at=self.at,event_order=len(self.legs)+1))

    def controls(self):
        for _ in range(100):
            pending=next((a for a in self.state.actions if a.status=='INTENT'),None)
            if pending is None: return
            a=pending
            if a.kind in ('ARM_STOP','MOVE_STOP'):
                self.event(ActionReceipt,action_id=a.action_id,status='ACCEPTED',reduce_only_verified=True,
                    stop_price=a.stop_price,old_stop_retired=a.kind=='MOVE_STOP',
                    retired_stop_cumulative_filled=D(0) if a.kind=='MOVE_STOP' else None,
                    coverage=dict(mode='fixed_quantity',quantity=a.quantity,quantity_version=a.position_quantity_version,
                        evidence_id='conditional-confirmation-'+a.action_id))
            elif a.kind=='CANCEL':
                target=next(x for x in self.state.actions if x.action_id==a.target_action_id)
                self.event(ActionReceipt,action_id=a.action_id,status='ACCEPTED')
                self.event(ActionReceipt,action_id=target.action_id,status='CANCELED',cumulative_filled_quantity=target.filled_quantity)
            elif a.kind=='RECONCILE':
                raise OfflineError('SCENARIO_REQUIRES_UNMODELED_RECONCILIATION')
            else:
                self.event(ActionReceipt,action_id=a.action_id,status='ACCEPTED',reduce_only_verified=True)
        raise OfflineError('SCENARIO_CONTROL_BOUND_EXCEEDED')

    def tick(self,price):
        if price<=0: raise OfflineError('SCENARIO_NONPOSITIVE_PRICE')
        self.at+=1;self.quote=price
        self.event(MarketEvent,observed_at=self.at,bid=price,ask=price,confirmed=True)
        self.controls()

    def fill(self,kind,*,partial=True):
        a=next((a for a in reversed(self.state.actions) if a.kind==kind and a.status=='ACCEPTED'),None)
        if a is None: raise OfflineError('SCENARIO_EXPECTED_ACTION_MISSING:'+kind)
        quantity=a.quantity-a.filled_quantity
        first=(quantity/2/self.rules.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*self.rules.quantity_step
        pieces=(first,quantity-first) if partial and first>=self.rules.min_quantity and quantity-first>=self.rules.min_quantity else (quantity,)
        for index,q in enumerate(pieces):
            price=execution_price(self.quote,'SELL' if self.sign==1 else 'BUY',self.costs['exit_slippage_bps'],self.rules.price_tick)
            fee=q*price*self.costs['exit_fee_rate']
            self.event(ExitFill,action_id=a.action_id,fill_id=a.action_id+'-'+str(index),quantity=q,price=price,
                fee_usdt=fee,occurred_at=self.at)
            self._leg(kind,q,self.quote,price,fee)
            if index<len(pieces)-1 and kind=='TP1' and self.state.tp1_complete:
                raise OfflineError('PARTIAL_TP_MUST_NOT_COMPLETE')
        self.event(ActionReceipt,action_id=a.action_id,status='FILLED',cumulative_filled_quantity=a.quantity,reduce_only_verified=True)
        self.controls()

    def tp(self,name,multiple):
        raw=self.state.frozen_r_anchor_entry+self.sign*self.state.frozen_initial_r*multiple
        rounded=(raw/self.rules.price_tick).to_integral_value(rounding=ROUND_CEILING if self.sign==1 else ROUND_FLOOR)*self.rules.price_tick
        self.tick(rounded);self.fill(name)


def path(plan,snapshot,quantity,entry_quote,name):
    assumptions=('Ordered explicit synthetic ticks; acceptance and confirmed fill details are separate',
        'First actual adverse entry fill freezes R; floor quantities from ORIGINAL filled size',
        'Each TP may fill in two parts at the stated quote; full TP confirmed before stop replacement',
        'Atomic fixed-quantity stop replacement confirmed before retrace; adverse tick-rounded fills',
        'Zero periodic funding only under synthetic-no-funding/v1; holding path '+name)
    if name=='S3' and plan.runner_reference_price is None:
        return Scenario(scenario_id=name,status='UNAVAILABLE',assumptions=assumptions,quantity=quantity,
            entry_quote=entry_quote,reason_codes=('NO_SUPPORTED_RUNNER_STRUCTURE',))
    exp=PathExperiment(plan,snapshot,quantity,entry_quote)
    if name!='S0':
        exp.tp('TP1',plan.policy.tp1_r)
        if name in ('S2','S3'): exp.tp('TP2',plan.policy.tp2_r)
        if name=='S3':
            reference=plan.runner_reference_price
            if (reference-exp.state.frozen_r_anchor_entry)*exp.sign<plan.policy.runner_activation_r*exp.state.frozen_initial_r:
                return Scenario(scenario_id=name,status='UNAVAILABLE',assumptions=assumptions,quantity=quantity,
                    entry_quote=entry_quote,reason_codes=('ACTUAL_REFERENCE_GEOMETRY_CANNOT_ACTIVATE_RUNNER',))
            exp.tick(reference)
    # Stops execute at the supplied quote after cancellation/ack, not magically
    # at the prior favourable extreme or the runner activation price.
    exp.tick(exp.state.current_stop);exp.fill('CLOSE_ALL')
    if exp.state.phase!='CLOSED' or exp.state.remaining_quantity!=0:
        raise OfflineError('SCENARIO_DID_NOT_SETTLE_ALL_QUANTITY')
    # Independent conservation assertion against reducer; no alternative ledger.
    gross=sum((leg.gross_cash_flow for leg in exp.legs),D(0));fees=sum((leg.fee_usdt for leg in exp.legs),D(0))
    if gross!=exp.state.realized_gross_pnl or gross-fees!=exp.state.realized_net_pnl:
        raise OfflineError('SCENARIO_CASHFLOW_CONSERVATION_FAILED')
    return Scenario(scenario_id=name,status='SUPPORTED',assumptions=assumptions,quantity=quantity,entry_quote=entry_quote,
        legs=tuple(exp.legs),gross_pnl=gross,fees_usdt=fees,net_pnl=gross-fees,frozen_initial_r=exp.state.frozen_initial_r,
        final_stop=exp.state.current_stop,reference_structure_price=plan.runner_reference_price if name=='S3' else None)


def evaluate_scenarios(plan,setup,snapshot,quantity):
    with localcontext(Context(prec=50)):
        q=D(quantity)
        if q<=0 or q%plan.rules.quantity_step: raise OfflineError('SCENARIO_QUANTITY_INVALID')
        # Same Stage 3 function/result, retained independently of path results.
        static=calculate_rr(setup,quantity=number(q))
        s0s=[path(plan,snapshot,q,entry,'S0') for entry in sorted({plan.entry_lower,plan.entry_upper,plan.reference_entry})]
        s0=min(s0s,key=lambda result:result.net_pnl)
        risk=-s0.net_pnl
        if risk<=0: raise OfflineError('SCENARIO_INITIAL_RISK_INVALID')
        results=[s0,*[path(plan,snapshot,q,plan.reference_entry,n) for n in ('S1','S2','S3')]]
        results=tuple(r.model_copy(update={'initial_stop_risk_usdt':risk,
            'scenario_net_rr':None if r.net_pnl is None else r.net_pnl/risk}) for r in results)
        value=ScenarioEvaluation(evaluation_id='0'*64,exit_plan_digest=digest(plan),setup_digest=plan.setup_digest,
            quantity=q,conservative_initial_risk_usdt=risk,scenarios=results,static_rr_digest=digest(static),limitations=LIMITATIONS)
        return value.model_copy(update={'evaluation_id':digest(value)})
