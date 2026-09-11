"""Six fixed counterfactuals, never a changed ExecutionModel or permission.

Reuse original RR calculator and 8D adverse tick/combined-bps projection.
Funding is a frozen TOTAL at the unchanged 8E diagnostic quantity.
"""
from collections import Counter
from decimal import Decimal as D, Context, localcontext, ROUND_CEILING
from app.setups.models import TradeSetup
from app.setups.rr import calculate_rr
from app.admitted_paper.provider import number
from app.offline_paper.pricing import execution_price
from app.offline_paper.storage import digest

GRID = (0,2,5,10,20)


def projected_bps(setup, *, spread_bps, slippage_bps, tick):
    if spread_bps<0 or slippage_bps<0 or tick<=0:
        raise ValueError('INVALID_COST_UNITS')
    sign=1 if setup.side=='LONG' else -1
    def budget(p, entry):
        d=sign if entry else -sign
        quote=p*(1+d*spread_bps/20000)
        fill=execution_price(quote,'BUY' if d==1 else 'SELL',slippage_bps,tick)
        if fill<=0: raise ValueError('NONPOSITIVE_COUNTERFACTUAL_FILL')
        bps=abs(fill-p)/p*10000
        return bps.to_integral_value(rounding=ROUND_CEILING)
    return budget(D(str(setup.entry.reference_price)),True),max(
        budget(D(str(x)),False) for x in [setup.initial_stop.price,*[t.price for t in setup.targets]])


def evaluate(setup, quantity, funding, floor, *, spread_bps, tick, slippage_bps=None, fees_only=False):
    with localcontext(Context(prec=50)):
        if quantity<=0 or funding<0 or floor<=0 or setup.cost_assumptions.entry_fee_rate is None or setup.cost_assumptions.exit_fee_rate is None:
            raise ValueError('MISSING_OR_INVALID_FROZEN_COST')
        eb, xb = (D(0),D(0)) if fees_only else projected_bps(setup,spread_bps=spread_bps,slippage_bps=D(slippage_bps),tick=tick)
        money=D(0) if fees_only else funding
        costs=setup.cost_assumptions.model_copy(update=dict(entry_slippage_bps=number(eb),exit_slippage_bps=number(xb),
            funding_cost_usdt=number(money),source='counterfactual-static-cost/v1:NOT_ADMISSION'))
        hypothetical=setup.model_copy(update={'cost_assumptions':costs})
        calc=calculate_rr(hypothetical,quantity=number(quantity));r=calc.reference
        if calc.status!='complete' or r is None or r.net_rr is None:
            raise ValueError('COUNTERFACTUAL_RR_UNAVAILABLE')
        def leg_cost(c):
            return dict(entry_fee=quantity*c.entry_fee_per_unit,exit_fee=quantity*c.exit_fee_per_unit,
                entry_friction=quantity*c.entry_slippage_per_unit,exit_friction=quantity*c.exit_slippage_per_unit,
                funding=quantity*c.funding_per_unit)
        target={k:sum((D(str(t.fraction))*leg_cost(t.costs)[k] for t in r.targets),D(0)) for k in leg_cost(r.stop_costs)}
        stop=leg_cost(r.stop_costs)
        if abs(r.net_pnl_usdt-(r.gross_pnl_usdt-sum(target.values(),D(0))))>D('1e-43'):
            raise AssertionError('TARGET_CASH_CONSERVATION')
        if abs(r.net_stop_loss_usdt-(r.gross_risk_usdt+sum(stop.values(),D(0))))>D('1e-43'):
            raise AssertionError('STOP_CASH_CONSERVATION')
        return dict(version='fixed-quantity-cost-counterfactual/v1',scope='COUNTERFACTUAL_DIAGNOSTIC / NOT_ADMISSION',
            quantity=quantity,scenario='FEES_ONLY' if fees_only else 'SLIPPAGE_'+str(slippage_bps)+'_BPS',
            per_leg_slippage_bps=None if fees_only else slippage_bps,spread_bps=D(0) if fees_only else spread_bps,
            funding_budget=money,entry_combined_bps=eb,exit_combined_bps=xb,fee_rates=(costs.entry_fee_rate,costs.exit_fee_rate),
            original_setup_digest=digest(setup),hypothetical_input_digest=digest(hypothetical),rr_version=calc.calculation_version,
            quantity_basis='UNCHANGED_8E_DIAGNOSTIC_NOT_APPROVED',target_costs=target,stop_costs=stop,
            gross_target=r.gross_pnl_usdt,gross_stop_risk=r.gross_risk_usdt,
            net_target=r.net_pnl_usdt,net_stop_risk=r.net_stop_loss_usdt,net_rr=r.net_rr,required_net_rr=floor,
            necessary_static_condition=r.net_pnl_usdt>=floor*r.net_stop_loss_usdt,
            execution_authority='NONE',actual_execution_cost_identified=False)


def boundary(rows):
    flags=[r['necessary_static_condition'] for r in rows]
    if len(rows)!=5 or [r['per_leg_slippage_bps'] for r in rows]!=list(GRID):
        raise ValueError('FIXED_GRID_REQUIRED')
    if any(not a and b for a,b in zip(flags,flags[1:])):
        return dict(status='NON_MONOTONE_DISCRETE_OUTCOMES',passing=[GRID[i] for i,f in enumerate(flags) if f])
    if not any(flags):return dict(status='NO_TESTED_SLIPPAGE_PASSES',tested_min=0,tested_max=20)
    if all(flags):return dict(status='ALL_TESTED_SLIPPAGES_PASS_NOT_A_MAXIMUM',tested_max=20)
    last=max(i for i,f in enumerate(flags) if f)
    return dict(status='BRACKET_ONLY_WITH_TICK_DISCONTINUITIES',passing_bps=GRID[last],failing_bps=GRID[last+1],
        exact_threshold=None,interpretation='observed pass at lower / fail at upper; no interpolated equality')


def research_row(candidate, old, *, spread_bps, tick):
    s=TradeSetup.model_validate(candidate['setup']);m=old['metrics']
    if not m:raise ValueError('8E_SAME_QUANTITY_COST_RECORD_REQUIRED')
    q,f,k=map(D,(str(m['quantity']),str(m['funding_budget']),str(m['required_net_rr'])))
    fee=evaluate(s,q,f,k,spread_bps=spread_bps,tick=tick,fees_only=True)
    rows=[evaluate(s,q,f,k,spread_bps=spread_bps,tick=tick,slippage_bps=x) for x in GRID]
    baseline=rows[3]
    for key,old_key in (('net_target','net_target_usdt'),('net_stop_risk','stop_risk_usdt'),('net_rr','net_rr')):
        if abs(baseline[key]-D(str(m[old_key])))>D('1e-42'):
            raise AssertionError('8E_BASELINE_COST_MISMATCH:'+key)
    category=('FEES_EXHAUST_REWARD' if fee['net_target']<=0 else
        'POSITIVE_FEES_ONLY_BELOW_RR' if not fee['necessary_static_condition'] else
        'FROZEN_SPREAD_FUNDING_BLOCK_EVEN_ZERO_SLIP' if not rows[0]['necessary_static_condition'] else
        'LOWER_SLIPPAGE_NECESSARY_ONLY' if not baseline['necessary_static_condition'] else
        'CURRENT_STATIC_NECESSARY_CONDITION_ONLY')
    return dict(candidate_id=old['candidate_id'],side=s.side,at=s.created_at,quantity=q,
        quantity_basis=old['quantity_basis'],frozen_8e_record_digest=digest(old),
        primary=category,fee_only=fee,sensitivities=rows,boundary=boundary(rows),
        original_rejection_reasons=old['reason_codes'],real_executability='UNIDENTIFIED_COST_EVIDENCE',
        execution_authority='NONE')


class CostSummary:
    """Bounded counters and one fixed representative per class, not all rows."""
    def __init__(self):
        self.sides={side:dict(candidates=0,primary=Counter(),boundaries=Counter(),representatives={},
            slippage={str(x):Counter() for x in GRID},fees_only=Counter(),real_executability_unknown=0)
            for side in ('LONG','SHORT')}

    def add(self,row):
        out=self.sides[row['side']];out['candidates']+=1;out['real_executability_unknown']+=1
        out['primary'][row['primary']]+=1;out['boundaries'][row['boundary']['status']]+=1
        old=out['representatives'].get(row['primary'])
        if old is None or row['candidate_id']<old['candidate_id']:out['representatives'][row['primary']]=row
        for result,counter in [(row['fee_only'],out['fees_only']),*[(r,out['slippage'][str(r['per_leg_slippage_bps'])]) for r in row['sensitivities']]]:
            counter['total']+=1
            counter['nonpositive_net']+=result['net_target']<=0
            counter['positive_below_floor']+=result['net_target']>0 and not result['necessary_static_condition']
            counter['static_necessary_pass']+=result['necessary_static_condition']

    def result(self):
        return dict(version='cost-boundary-research/v1',scope='COUNTERFACTUAL_DIAGNOSTIC / NOT_ADMISSION',sides=self.sides,
            fee_spread_impact_rules='DECLARED_NOT_ACCOUNT_OR_BOOK_VERIFIED',funding='FROZEN_EX_ANTE_BUDGET_NOT_SETTLEMENT',
            expected_return=None,production_costs_changed=False)


def summarize(rows):
    result=CostSummary()
    for row in rows:result.add(row)
    return result.result()
