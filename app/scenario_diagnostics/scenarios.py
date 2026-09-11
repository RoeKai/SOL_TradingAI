"""Protective-path descriptions/v1. The accepted Exit reducer remains authority.

These are conditional confirmation experiments, not broker orders. In particular,
settling a protective CLOSE_ALL is NOT proof that an S3 premise occurred.
"""
from decimal import Decimal as D, Context, localcontext
from typing import Literal
from app.setups.models import Record
from app.admitted_paper.models import ScenarioLeg
from app.historical_replay.scenarios import SpreadPath
from app.offline_paper.models import OfflineError
from app.offline_paper.storage import digest
from app.exits.engine import apply_event


class Description(Record):
    version: Literal['protective-path-description/v1'] = 'protective-path-description/v1'
    scope: Literal['READ_ONLY_CONDITIONAL_NOT_EXECUTION'] = 'READ_ONLY_CONDITIONAL_NOT_EXECUTION'
    scenario_id: Literal['S0','S1','S2','S3']
    plan_digest: str
    quantity: D
    capability: Literal['SUPPORTED','UNSUPPORTED'] = 'SUPPORTED'
    premise: Literal['FULFILLED','EARLY_PROTECTIVE_TERMINATION','UNAVAILABLE']
    reason_codes: tuple[str,...] = ()
    legs: tuple[ScenarioLeg,...] = ()
    events_digest: str | None = None
    confirmed_events: int = 0
    confirmation_trace: tuple[dict,...] = ()
    final_state_digest: str | None = None
    reached: tuple[str,...] = ()
    remaining_quantity: D | None = None
    gross_pnl: D | None = None
    fees_usdt: D | None = None
    funding_budget_usdt: D
    conditional_net_cash: D | None = None
    frozen_initial_r: D | None = None
    final_stop: D | None = None
    initial_stop_risk_usdt: D | None = None
    conditional_cash_rr: D | None = None
    # Only a fulfilled S3 gets an S3 RR; early cash is diagnostic, not S3 success.
    scenario_net_rr: D | None = None
    expected_return: None = None
    execution_authority: Literal['NONE'] = 'NONE'


class ProtectivePath(SpreadPath):
    def __init__(self,*args,duplicate_confirmations=False,**kwargs):
        self.events=[]
        self.duplicate_confirmations=duplicate_confirmations
        super().__init__(*args,**kwargs)

    def event(self,cls,**kwargs):
        self.index+=1
        ev=cls(event_id='path-'+str(self.index),position_id=self.state.position_id,
               received_at=self.at,**kwargs)
        self.state=apply_event(self.seed,self.policy,self.state,ev).state
        self.events.append(ev.model_dump(mode='json'))
        if self.state.faults:
            raise OfflineError('SCENARIO_REDUCER_FAULT:'+','.join(self.state.faults))
        if self.duplicate_confirmations:
            after=apply_event(self.seed,self.policy,self.state,ev).state
            if after!=self.state:
                raise AssertionError('DUPLICATE_CONFIRMATION_CHANGED_STATE')

    def protective_exit(self):
        if self.state.phase=='CLOSED':
            return True
        if self.state.phase=='STOP_PENDING':
            # controls has confirmed cancellations and accepted the actual intent.
            # Use the latest visible quote, not a future target or a made-up stop fill.
            self.fill('CLOSE_ALL')
            if self.state.phase!='CLOSED':
                raise AssertionError('PROTECTION_DID_NOT_CLOSE')
            return True
        return False


def describe_path(plan,snapshot,q,entry,name,*,duplicate_confirmations=False):
    with localcontext(Context(prec=50)):
        return _describe(plan,snapshot,D(q),D(entry),name,duplicate_confirmations)


def _describe(plan,snapshot,q,entry,name,duplicates):
    if name not in ('S0','S1','S2','S3') or q<=0 or q%plan.rules.quantity_step:
        raise ValueError('INVALID_DESCRIPTION_INPUT')
    if plan.materialized_quantity!=q:
        raise ValueError('SCENARIO_COST_QUANTITY_MISMATCH')
    base=dict(scenario_id=name,plan_digest=digest(plan),quantity=q,
              funding_budget_usdt=plan.pre_entry_funding_budget_usdt)
    if plan.policy.runner_strategy!='fixed_r':
        return Description(**base,capability='UNSUPPORTED',premise='UNAVAILABLE',
                           reason_codes=('RUNNER_ALGORITHM_NOT_MODELLED',))
    if name=='S3' and plan.runner_reference_price is None:
        return Description(**base,premise='UNAVAILABLE',reason_codes=('NO_SUPPORTED_RUNNER_STRUCTURE',))
    exp=ProtectivePath(plan,snapshot,q,entry,duplicate_confirmations=duplicates)
    reached=['ENTRY'];early=False
    if name!='S0':
        for stage,multiple in [('TP1',plan.policy.tp1_r)]+([] if name=='S1' else [('TP2',plan.policy.tp2_r)]):
            # Check BEFORE the next requested price. Never invent a TP after a stop.
            if exp.protective_exit(): early=True;break
            exp.tp(stage,multiple)
            complete=exp.state.tp1_complete if stage=='TP1' else exp.state.tp2_complete
            if not complete: raise AssertionError('CONFIRMED_TP_INCOMPLETE')
            reached.append(stage)
            if exp.protective_exit(): early=True;break
        if name=='S3' and not early:
            ref=plan.runner_reference_price
            executable=ref*(1-exp.sign*exp.half)
            if (executable-exp.state.frozen_r_anchor_entry)*exp.sign<plan.policy.runner_activation_r*exp.state.frozen_initial_r:
                # Keep actual prefix cash separate; no claim about subsequent exits.
                return Description(**base,premise='UNAVAILABLE',legs=tuple(exp.legs),
                    reached=tuple(reached),remaining_quantity=exp.state.remaining_quantity,
                    reason_codes=('ACTUAL_REFERENCE_GEOMETRY_CANNOT_ACTIVATE_RUNNER',))
            exp.tick(ref);reached.append('RUNNER_REFERENCE')
            if exp.protective_exit(): early=True
    if not early:
        exp.tick(exp.state.current_stop/(1-exp.sign*exp.half))
        exp.fill('CLOSE_ALL')
    if exp.state.phase!='CLOSED' or exp.state.remaining_quantity!=0:
        raise AssertionError('DESCRIPTION_NOT_SETTLED')
    gross=sum((leg.gross_cash_flow for leg in exp.legs),D(0))
    fees=sum((leg.fee_usdt for leg in exp.legs),D(0))
    exited=sum((leg.quantity for leg in exp.legs if leg.action!='ENTRY'),D(0))
    if (exited!=q or gross!=exp.state.realized_gross_pnl or
            gross-fees!=exp.state.realized_net_pnl):
        raise AssertionError('DESCRIPTION_CASH_OR_QUANTITY_NOT_CONSERVED')
    return Description(**base,premise='EARLY_PROTECTIVE_TERMINATION' if early else 'FULFILLED',
        reason_codes=((exp.state.emergency_reason or 'EARLY_PROTECTION'),) if early else (),
        legs=tuple(exp.legs),events_digest=digest([exp.seed.first_fill.model_dump(mode='json'),*exp.events]),
        confirmed_events=len(exp.events)+1,
        confirmation_trace=(exp.seed.first_fill.model_dump(mode='json'),*exp.events),
        final_state_digest=digest(exp.state),
        reached=tuple(reached),remaining_quantity=exp.state.remaining_quantity,
        gross_pnl=gross,fees_usdt=fees,conditional_net_cash=gross-fees-plan.pre_entry_funding_budget_usdt,
        frozen_initial_r=exp.state.frozen_initial_r,final_stop=exp.state.current_stop)


def describe_scenarios(plan,snapshot,q,*,duplicate_confirmations=False):
    with localcontext(Context(prec=50)):
        s0=min((describe_path(plan,snapshot,q,e,'S0',duplicate_confirmations=duplicate_confirmations)
                for e in sorted({plan.reference_entry,plan.entry_lower,plan.entry_upper})),
               key=lambda x:x.conditional_net_cash)
        risk=-s0.conditional_net_cash
        if risk<=0: raise ValueError('NONPOSITIVE_INITIAL_RISK')
        results=[s0]+[describe_path(plan,snapshot,q,plan.reference_entry,n,
                     duplicate_confirmations=duplicate_confirmations) for n in ('S1','S2','S3')]
        return tuple(x.model_copy(update=dict(initial_stop_risk_usdt=risk,
            conditional_cash_rr=None if x.conditional_net_cash is None else x.conditional_net_cash/risk,
            scenario_net_rr=x.conditional_net_cash/risk if x.premise=='FULFILLED' else None)) for x in results)
