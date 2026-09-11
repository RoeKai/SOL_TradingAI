"""Mutually exclusive attribution, never a proposal to remove actual risk costs."""
from decimal import Decimal as D, Context, localcontext, ROUND_CEILING
from app.historical_replay.models import HistoricalCandidate
from app.execution_costs.models import PriceContract, CostFunction, QuantificationPolicy
from app.execution_costs.prices import derive,describe_prices,cost_function
from app.configuration.compiler import policy_from
from app.execution_costs.funding import exchange_snapshot
from .proofs import coefficients, dec


def primary_class(exfund,net,rr,floor,*,incomplete):
    # Ordered, mutually exclusive; unresolved state has precedence over costs.
    return 'E' if incomplete else 'A' if exfund<=0 else 'B' if net<=0 else 'C' if rr<floor else 'D'


def cost_row(body,bundle,model,*,unquantified_candidate=None,quantification=None):
    with localcontext(Context(prec=50)):
        unquantified=not body.get('price_contract')
        if unquantified and unquantified_candidate is not None:
            # Explicit past descriptor only. A stale quote does not become fresh,
            # nor does a missing original quantification become a completed solve.
            candidate=HistoricalCandidate.model_validate(unquantified_candidate)
            raw=candidate.evidence['window'][-1]['last']['SOLUSDT'];mid=dec(raw['price'])
            quote=dict(event_id=raw['event_id'],at=raw['event_time_ms']/1000,
                       bid=mid*(1-model.spread_bps/20000),ask=mid*(1+model.spread_bps/20000))
            rules=exchange_snapshot(candidate.setup.created_at)
            pc=describe_prices(candidate,model,quote,rules.price_tick,now=candidate.setup.created_at)
            f=cost_function(candidate,pc,model,QuantificationPolicy.model_validate(quantification))
            body=dict(body,candidate=unquantified_candidate,price_contract=pc.model_dump(mode='json'),
                      cost_function=f.model_dump(mode='json'),search_status='NOT_QUANTIFIED_STALE_INPUT',
                      exchange=rules.model_dump(mode='json'))
        # Stale attempts rejected before quantification have no bound inputs.
        if not body.get('candidate') or not body.get('price_contract'):
            return dict(candidate_id=body.get('candidate_id'),side=body.get('side','UNKNOWN'),primary='E',
                        reason_codes=body['reason_codes'],metrics=None,quantity_basis='NOT_AVAILABLE')
        candidate=HistoricalCandidate.model_validate(body['candidate']);s=candidate.setup
        pc=PriceContract.model_validate(body['price_contract']);f=CostFunction.model_validate(body['cost_function'])
        v=body['exchange'];step=dec(v['quantity_step'])
        q=dec(body['domain']['high']) if body.get('domain') and dec(body['domain']['high'])>0 else (
            max(dec(v['min_quantity']),dec(v['min_notional_usdt'])/pc.signal_reference_price)/step).to_integral_value(rounding=ROUND_CEILING)*step
        derived,_=derive(candidate,pc,f,q,model)
        c=coefficients(derived.model_dump(mode='json'),f.model_dump(mode='json'))
        p=pc.signal_reference_price;sign=1 if s.side=='LONG' else -1
        cc=derived.cost_assumptions;eb=dec(cc.entry_slippage_bps)/10000;xb=dec(cc.exit_slippage_bps)/10000
        target=sum((dec(t.fraction)*dec(t.price) for t in s.targets),D(0))
        fee=(p+sign*p*eb)*dec(cc.entry_fee_rate)+(target-sign*target*xb)*dec(cc.exit_fee_rate)
        spread=(p+target)*model.spread_bps/20000
        slip=(p+target)*model.slippage_bps/10000
        rounding=p*eb+target*xb-spread-slip
        funding=f.fixed_usdt+q*f.per_unit_usdt
        exfund=q*(c['weighted_reward']-c['target_cost_ex_funding'])
        net=q*c['g']-c['f0'];loss=q*c['l']+c['f0'];rr=net/loss
        policy=policy_from(bundle,'admission');market=next(x for x in policy.markets if x.regime==s.market_state.regime)
        floor=dec(body['required_net_rr']) if body.get('required_net_rr') is not None else max(policy.minimum_net_rr,market.minimum_net_rr)
        incomplete=unquantified or body.get('search_status')=='EVALUATION_BUDGET_EXHAUSTED' or body['result'] in ('UNSUPPORTED','UNRESOLVED')
        category=primary_class(exfund,net,rr,floor,incomplete=incomplete)
        return dict(candidate_id=body['candidate_id'],side=s.side,at=s.created_at,primary=category,
            reason_codes=body['reason_codes'],quantity_basis='HARD_DOMAIN_UPPER_NOT_ORDER_SIZE' if body.get('domain') else 'MINIMUM_DIAGNOSTIC_NOT_APPROVED',
            original_status=body['search_status'],metrics=dict(quantity=q,structural_stop_distance=c['structural_risk'],
            weighted_target_reward_distance=c['weighted_reward'],fee_per_unit=fee,spread_per_unit=spread,
            slippage_per_unit=slip,rounding_budget_residual_per_unit=rounding,funding_per_unit=f.per_unit_usdt,
            total_fee_budget=q*fee,total_spread_budget=q*spread,total_slippage_budget=q*slip,
            total_rounding_budget=q*rounding,funding_budget=funding,net_target_usdt=net,net_rr=rr,
            stop_risk_usdt=loss,required_net_rr=floor,net_target_without_funding_counterfactual=exfund),
            accounting='BUDGETS_NOT_SETTLED_COSTS; additive spread/slip split is first-order with compound/tick/bps residual',
            counterfactual='Removing funding for attribution ONLY; no admission or order',execution_authority='NONE')


def distribution(rows):
    result={}
    for side in ('LONG','SHORT','UNKNOWN'):
        selected=[r for r in rows if r['side']==side]
        if not selected:continue
        counts={c:sum(r['primary']==c for r in selected) for c in 'ABCDE'}
        metrics={};examples={}
        usable=[r for r in selected if r['metrics'] is not None]
        for key in (usable[0]['metrics'] if usable else ()):
            ordered=sorted((r['metrics'][key],r['candidate_id']) for r in usable)
            quantiles={}
            for label,n,d in (('min',0,1),('p10',1,10),('p25',1,4),('p50',1,2),('p75',3,4),('p90',9,10),('max',1,1)):
                quantiles[label]=ordered[((len(ordered)-1)*n)//d][0]
            metrics[key]=quantiles
        for cat in 'ABCDE':
            matches=sorted((r for r in selected if r['primary']==cat),key=lambda r:r['candidate_id'])
            if matches:examples[cat]=matches[0]
        result[side]=dict(candidates=len(selected),primary=counts,metrics_count=len(usable),quantiles=metrics,
            representatives=examples,representative_rule='lexicographically smallest candidate ID per side and primary class',
            quantile_rule='sorted observed values, floor((N-1)*p), no interpolation')
    return result
