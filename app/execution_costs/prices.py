"""Three price layers and a conservatively rounded mapping to original RR."""
from decimal import Decimal as D, ROUND_CEILING
from app.admission.models import fingerprint
from app.offline_paper.pricing import execution_price
from app.offline_paper.storage import digest
from app.historical_replay.data import HistoricalError
from app.setups.models import TradeSetup
from app.admitted_paper.provider import number
from .models import PriceContract, CostFunction


def fill_from_mid(mid,side,model,tick,*,entry=True):
    sign=(1 if side=='LONG' else -1)*(1 if entry else -1)
    quote=mid*(1+sign*model.spread_bps/20000)
    fill=execution_price(quote,'BUY' if sign==1 else 'SELL',model.slippage_bps,tick)
    if fill<=0: raise HistoricalError('NONPOSITIVE_MODELED_FILL')
    return quote,fill


def describe_prices(candidate,model,quote,tick,*,now):
    """Non-authorizing historical description, including stale past quotes.

    Visibility and same-snapshot identity are mandatory. A stale descriptor
    remains useful for A/B/C/D attribution but never passes the new gate.
    """
    s=candidate.setup;market=s.market_state
    if not quote or any(k not in quote for k in ('event_id','at','bid','ask')):
        raise HistoricalError('EXECUTION_QUOTE_MISSING')
    raw=candidate.evidence['window'][-1]['last']['SOLUSDT']
    mid=D(raw['price']);q,m=fill_from_mid(mid,s.side,model,tick)
    if (D(str(market.reference_price))!=mid or D(str(s.entry.reference_price))!=mid or
        quote['event_id']!=raw['event_id'] or D(quote['ask'])!=mid*(1+model.spread_bps/20000) or
        D(quote['bid'])!=mid*(1-model.spread_bps/20000) or quote['at']!=raw['event_time_ms']/1000):
        raise HistoricalError('SIGNAL_EXECUTION_SNAPSHOT_MISMATCH')
    if raw['available_at_ms']/1000>now or now<quote['at']:
        raise HistoricalError('EXECUTION_QUOTE_STALE_OR_FUTURE')
    value=PriceContract(contract_id='0'*64,original_setup_digest=fingerprint(s),side=s.side,
        signal_reference_price=D(str(s.entry.reference_price)),market_trade_price=mid,
        executable_quote=q,modeled_fill_price=m,signal_at=s.created_at,quote_at=quote['at'],
        source_event_id=raw['event_id'],available_at=raw['available_at_ms']/1000,
        spread_bps=model.spread_bps,slippage_bps=model.slippage_bps,price_tick=tick,fee_rate=model.fee_rate,
        source_digest=digest(raw),model_digest=digest(model))
    return value.model_copy(update={'contract_id':digest(value)})


def prices(candidate,model,quote,tick,*,now):
    value=describe_prices(candidate,model,quote,tick,now=now)
    if now-value.quote_at>=5:
        raise HistoricalError('EXECUTION_QUOTE_STALE_OR_FUTURE')
    return value


def check_quote(contract,quote,*,now,expires_at):
    reasons=[]
    if not quote or any(k not in quote for k in ('at','bid','ask','event_id')):
        return ['EXECUTION_QUOTE_MISSING']
    if now>=expires_at: reasons.append('SIGNAL_EXPIRED')
    if quote['at']>now or now-quote['at']>=5: reasons.append('EXECUTION_QUOTE_STALE_OR_FUTURE')
    current=D(quote['ask'] if contract.side=='LONG' else quote['bid'])
    if (current-contract.executable_quote)*(1 if contract.side=='LONG' else -1)>0:
        reasons.append('ADVERSE_EXECUTION_QUOTE_DRIFT')
    return reasons


def cost_function(candidate,pc,model,policy,*,old_fixed=False):
    s=candidate.setup
    if policy.fixed_fee_usdt!=0:
        raise HistoricalError('FIXED_COST_REQUIRES_ITS_OWN_EXPLICIT_FEE_SCHEDULE')
    values=[('signal',pc.signal_reference_price),('counterquote',pc.executable_quote),('entry_fill',pc.modeled_fill_price)]
    for name,price in [('stop',D(str(s.initial_stop.price))),*[(e.evidence_id,D(str(e.price))) for e in s.structure_evidence if e.price is not None]]:
        values.append((name,price))
        for entry in (True,False):
            quote,fill=fill_from_mid(price,s.side,model,pc.price_tick,entry=entry)
            values.extend(((name+':quote:'+str(entry),quote),(name+':fill:'+str(entry),fill)))
    budget=max(p for _,p in values)
    old=D(str(s.cost_assumptions.funding_cost_usdt))
    rate=policy.funding_rate_allowance;events=policy.funding_event_allowance
    value=CostFunction(version='fixed-ex-ante-funding-budget/v1' if old_fixed else 'quantity-funding-budget/v1',
        function_id='0'*64,fixed_usdt=old if old_fixed else policy.fixed_fee_usdt,
        per_unit_usdt=D(0) if old_fixed else budget*rate*events,budget_price=budget,
        rate_allowance=rate,event_allowance=events,price_basis=tuple(values),
        fixed_cost_basis='ORIGINAL_ENTIRE_FIXED_BUDGET_UNCHANGED' if old_fixed else policy.fixed_fee_basis,
        known_at=s.created_at,original_setup_digest=fingerprint(s),price_contract_digest=digest(pc),
        holding_seconds=int(s.cost_assumptions.assumed_holding_seconds))
    return value.model_copy(update={'function_id':digest(value)})


def materialize(function,q):
    q=D(q)
    if q<=0 or not q.is_finite(): raise HistoricalError('POSITIVE_FINITE_EVALUATION_QUANTITY_REQUIRED')
    return dict(version='quantity-materialization/v1',function_digest=digest(function),quantity=str(q),
        fixed_usdt=str(function.fixed_usdt),proportional_usdt=str(q*function.per_unit_usdt),
        funding_budget_usdt=str(function.fixed_usdt+q*function.per_unit_usdt))


def derive(candidate,pc,function,q,model,*,old_prices=False):
    """Preserve structure. The combined bps is used ONCE by calculate_rr."""
    s=candidate.setup;mat=materialize(function,q);sign=1 if s.side=='LONG' else -1
    entry=D(str(s.entry.reference_price))
    entry_bps=(abs(pc.modeled_fill_price-entry)/entry*10000).to_integral_value(rounding=ROUND_CEILING)
    exact=[]
    for price in [D(str(s.initial_stop.price)),*[D(str(t.price)) for t in s.targets]]:
        quote,fill=fill_from_mid(price,s.side,model,pc.price_tick,entry=False)
        bps=abs(fill-price)/price*10000
        exact.append((price,quote,fill,bps))
    exit_bps=max(x[3] for x in exact).to_integral_value(rounding=ROUND_CEILING)
    costs=s.cost_assumptions.model_dump()
    costs.update(funding_cost_usdt=number(D(mat['funding_budget_usdt'])),source=function.version,
        entry_slippage_bps=number(entry_bps),exit_slippage_bps=number(exit_bps))
    if old_prices:
        costs.update(entry_slippage_bps=s.cost_assumptions.entry_slippage_bps,exit_slippage_bps=s.cost_assumptions.exit_slippage_bps)
    lineage=dict(version='execution-priced-static-input/v1',purpose='STATIC_STRUCTURE_TARGETS_NOT_FULL_EXIT_POLICY',
        original_setup_digest=fingerprint(s),price_contract_digest=digest(pc),cost_function_digest=digest(function),
        materialization=mat,entry_combined_bps=str(entry_bps),exit_combined_bps=str(exit_bps),
        approximation='CEIL_WORST_ADVERSE_BPS_TO_INTEGER; fees on RR modeled fills; budget spread/tick only once',
        per_unit_entry_price_excess=str(abs(entry*(1+sign*entry_bps/10000)-pc.modeled_fill_price)),
        exit_mapping=[dict(structure_price=str(p),counterquote=str(qx),fill=str(f),exact_bps=str(b),
            conservative_fill=str(p*(1-sign*exit_bps/10000)),
            price_excess=str(abs(p*(1-sign*exit_bps/10000)-f))) for p,qx,f,b in exact])
    obj=s.model_dump();obj.update(setup_id='8d:'+digest(lineage),plan_version='execution-priced-static-input/v1',cost_assumptions=costs)
    derived=TradeSetup.model_validate(obj)
    lineage['derived_setup_digest']=fingerprint(derived)
    return derived,lineage


def linear_feasibility(g,l,f0,k,q):
    """Independent arithmetic oracle ONLY; not a production admission shortcut."""
    g,l,f0,k,q=map(D,(g,l,f0,k,q))
    if q<=0 or l*q+f0<=0 or f0<0: raise ValueError('Unsupported linear domain')
    return (g*q-f0)/(l*q+f0)>=k, (g-k*l)*q>=(1+k)*f0
