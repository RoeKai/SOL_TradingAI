"""Versioned exact-quantity gate. Not Stage 5 permission, no IO in quantify.

All original hard checks/score thresholds are preserved. Only the explicitly
supplied entry-price/cost validators have 8D semantics. Every tested lattice
point gets its own materialized cost, original RR and original score.
"""
from decimal import Decimal as D, Context, localcontext, ROUND_FLOOR, ROUND_CEILING
from app.admission.engine import _check_plan_data, _check_account, _check_structure, _reason, _rr_min
from app.admission.models import fingerprint
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from app.configuration.compiler import verify_bundle, policy_from, main_values
from app.exits.models import PlanSnapshot
from app.offline_paper.storage import get, digest
from app.offline_paper.broker import synthetic_rules
from app.admitted_paper.provider import number
from app.historical_replay.data import HistoricalError
from .models import QuantificationPolicy
from .prices import prices, cost_function, derive, check_quote
from .plans import build_plan
from .scenarios import evaluate_scenarios
from .evidence import review, admission_request
from .funding import snapshot, exchange_snapshot

VERSION='quantity-consistent-admission/v1'
SCOPE='OFFLINE_QUANTIFIED_HISTORICAL_PAPER'


def plan_snapshot(setup,rr,card,decision_digest,decision_result):
    return PlanSnapshot(setup_id=setup.setup_id,plan_version=setup.plan_version,symbol=setup.symbol,side=setup.side,
        original_stop=str(setup.initial_stop.price),original_targets=tuple((t.target_id,str(t.price),str(t.fraction)) for t in setup.targets),
        setup_digest=fingerprint(setup),rr_digest=fingerprint(rr),scorecard_digest=fingerprint(card),
        admission_digest=decision_digest,admission_result=decision_result)


def soft_checks(card,policy):
    reasons=[];total=card.overall_trade_quality
    if total.score is None or total.status!='complete' or D(str(total.coverage))<policy.minimum_score_coverage:
        reasons.append('SCORECARD_INCOMPLETE')
    elif total.score<policy.minimum_total_score: reasons.append('TOTAL_SCORE_BELOW_MINIMUM')
    for floor in policy.dimension_floors:
        part=getattr(card,floor.dimension)
        if part.score is None or part.status!='complete' or part.score<floor.minimum:
            reasons.append('DIMENSION_SCORE_BELOW_MINIMUM:'+floor.dimension)
    tier=next((t for t in policy.tiers if total.score is not None and total.score>=t.minimum_total),None)
    if tier is None: reasons.append('NO_ELIGIBLE_SCORE_TIER')
    return tier,reasons


def caps_for(s,account,req,policy,limits,market,tier,max_price,entry_fee):
    """Stage 5 _size cap equations, not its fixed-funding quantity inversion."""
    remaining=max(D(0),limits['daily_loss_limit_usdt']-account.day_realized_loss_usdt-account.unrealized_loss_usdt-account.reserved_risk_usdt)
    caps={'REQUEST':req.risk_budget_usdt,'SINGLE_TRADE':limits['max_loss_per_trade_usdt'],
        'PLAN':D(str(s.risk_budget.max_loss_usdt)),'DAILY_REMAINING':remaining,
        'EQUITY_RISK':account.equity_usdt*policy.max_risk_fraction_of_equity}
    if s.risk_budget.remaining_daily_loss_usdt is not None: caps['PLAN_DAILY_REMAINING']=D(str(s.risk_budget.remaining_daily_loss_usdt))
    if s.risk_budget.max_risk_fraction_of_equity is not None: caps['PLAN_EQUITY_RISK']=account.equity_usdt*D(str(s.risk_budget.max_risk_fraction_of_equity))
    base=min(req.risk_budget_usdt,limits['max_loss_per_trade_usdt'])
    caps.update(SCORE=base*tier.risk_fraction,MARKET=base*market.risk_fraction)
    advice=s.position_limit_advice
    room=min(account.available_margin_usdt,account.equity_usdt*limits['max_margin_ratio']-account.margin_used_usdt,
             limits['max_margin_usdt']-account.margin_used_usdt)
    if advice.max_margin_usdt is not None: room=min(room,D(str(advice.max_margin_usdt)))
    if advice.max_margin_fraction_of_equity is not None: room=min(room,account.equity_usdt*D(str(advice.max_margin_fraction_of_equity)))
    qc={'AVAILABLE_MARGIN':max(D(0),room)/(max_price/D(req.leverage)+entry_fee),
        'GRADE_NOTIONAL':account.equity_usdt*tier.max_notional_equity_ratio/max_price,
        'POSITION_NOTIONAL':limits['max_position_notional_usdt']/max_price,'POSITION_QUANTITY':limits['max_position_quantity']}
    if advice.max_quantity is not None: qc['PLAN_QUANTITY']=D(str(advice.max_quantity))
    if advice.max_notional_usdt is not None: qc['PLAN_NOTIONAL']=D(str(advice.max_notional_usdt))/max_price
    return caps,qc


def quantify(candidate,bundle,model,settings,account,venue,request,pc,qp,*,now,old_prices=False,old_fixed=False,only_quantity=None):
    with localcontext(Context(prec=50)):
        return _quantify(candidate,bundle,model,settings,account,venue,request,pc,qp,now=now,
            old_prices=old_prices,old_fixed=old_fixed,only_quantity=only_quantity)


def _quantify(candidate,bundle,model,settings,account,venue,request,pc,qp,*,now,old_prices,old_fixed,only_quantity):
    s=candidate.setup;policy=policy_from(bundle,'admission');checked=verify_bundle(bundle)
    function=cost_function(candidate,pc,model,qp,old_fixed=old_fixed)
    out=dict(version=VERSION,scope=SCOPE,instance_id=account.instance_id,result='REJECT',reason_codes=[],
        candidate_id=candidate.candidate_id,candidate=candidate.model_dump(mode='json'),bundle_digest=bundle.bundle_digest,
        dataset_digest=candidate.dataset_digest,run_digest=candidate.run_digest,price_contract=pc.model_dump(mode='json'),
        cost_function=function.model_dump(mode='json'),quantification=qp.model_dump(mode='json'),
        original_setup_digest=fingerprint(s),quantity='0',risk='0',margin='0',fee_reserve='0',leverage=request.leverage,
        account=account.model_dump(mode='json'),exchange=venue.model_dump(mode='json'),request=request.model_dump(mode='json'),
        policy_digest=fingerprint(policy),exit_policy_digest=fingerprint(policy_from(bundle,'exit')),
        request_id=request.request_id,requested_risk=str(request.risk_budget_usdt),account_revision=account.snapshot_revision,
        issued_at=now,expires_at=min(s.valid_until,now+policy.decision_ttl_seconds,account.day_ends_at if account.day_ends_at is not None else now),
        evaluated_quantities=[],search_status='NOT_STARTED',proof=None,exit_plan=None,scenarios=None,
        legacy_full_policy_valuation='UNSUPPORTED',full_policy_expected_return=None,win_probability=None,
        real_runtime_connected=False,live_allowed=False,old_prices=old_prices,old_fixed=old_fixed)
    reasons=[]
    if checked.parsing!='PASS' or checked.consistency!='PASS': reasons.append('CONFIG_BUNDLE_INVALID')
    main=main_values(bundle)
    if account.instance_id!=bundle.manifest.instance_id or settings.instance_id!=account.instance_id: reasons.append('INSTANCE_BINDING_MISMATCH')
    if s.symbol!=bundle.manifest.trade_symbol or s.strategy_type not in main['strategies'] or not main['strategies'][s.strategy_type]['enabled']:
        reasons.append('PLAN_STRATEGY_SCOPE_UNSUPPORTED')
    if s.entry.order_type!=main['execution']['entry_order_type']: reasons.append('PLAN_ENTRY_METHOD_CONFLICT')
    if pc.original_setup_digest!=fingerprint(s) or pc.model_digest!=digest(model): reasons.append('PRICE_MODEL_BINDING_MISMATCH')
    rr_reasons=[]
    limits=_check_account(account,venue,request,s,policy,now,rr_reasons)
    _check_structure(s,request,policy,now,rr_reasons)
    reasons.extend(r.code for r in rr_reasons)
    market=next(m for m in policy.markets if m.regime==s.market_state.regime)
    if not market.allowed: reasons.append('MARKET_REGIME_NOT_ALLOWED')
    # Main caps remain a second authority, not silently overwritten by policy.
    for cap in bundle.comparable_limits:
        if cap.semantic in (limits or {}): limits[cap.semantic]=min(limits[cap.semantic],D(str(cap.effective_value)))
    if limits:
        # Recheck the strictly merged main/account/policy caps as well. The
        # original account validator ran on its own two authorities above.
        if request.risk_budget_usdt>limits['max_loss_per_trade_usdt']: reasons.append('REQUEST_EXCEEDS_SINGLE_TRADE_LIMIT')
        if request.leverage>limits['max_leverage']: reasons.append('LEVERAGE_LIMIT_OR_MISMATCH')
        if account.day_realized_loss_usdt+account.unrealized_loss_usdt>=limits['daily_loss_limit_usdt']: reasons.append('DAILY_LOSS_LIMIT')
        if account.consecutive_losses>=limits['max_consecutive_losses']: reasons.append('CONSECUTIVE_LOSS_HALT')
        if account.trades_today+len(account.pending_entries)>=limits['max_trades_per_day']: reasons.append('DAILY_TRADE_LIMIT')
        if len(account.positions)+len(account.pending_entries)>=limits['max_positions']: reasons.append('MAX_POSITIONS_LIMIT')
    btc=s.market_state.btc_return_3m_pct
    if s.side=='LONG' and btc is not None and D(str(btc))<=max(D(str(main['risk']['btc_crash_pct'])),D(str(policy.btc_crash_3m_pct))):
        reasons.append('BTC_CRASH_LONG_BLOCK')
    if reasons:
        out.update(reason_codes=list(dict.fromkeys(reasons)),search_status='QUANTITY_INDEPENDENT_REJECT');return out
    unit,_=derive(candidate,pc,function,D(1),model,old_prices=old_prices)
    unit_rr=calculate_rr(unit,quantity=1)
    if unit_rr.reference is None or unit_rr.reference.net_stop_loss_usdt is None:
        out.update(reason_codes=['UNIT_COST_MAPPING_UNSUPPORTED'],result='UNSUPPORTED');return out
    linear_loss=unit_rr.reference.net_stop_loss_usdt-function.fixed_usdt
    if linear_loss<=0:
        out.update(reason_codes=['POSITIVE_LINEAR_STOP_LOSS_REQUIRED'],result='UNSUPPORTED');return out
    max_price=max(unit_rr.reference.effective_entry_price,pc.modeled_fill_price)
    fee=max(unit_rr.reference.stop_costs.entry_fee_per_unit,pc.modeled_fill_price*model.fee_rate)
    caps,qcaps=caps_for(s,account,request,policy,limits,market,policy.tiers[0],max_price,fee)
    ceiling=min(caps.values())
    upper=min(*qcaps.values(),venue.max_quantity,max(D(0),(ceiling-function.fixed_usdt)/linear_loss))
    step=venue.quantity_step;high=int((upper/step).to_integral_value(rounding=ROUND_FLOOR))
    low=int((max(venue.min_quantity,venue.min_notional_usdt/min(pc.modeled_fill_price,pc.signal_reference_price))/step).to_integral_value(rounding=ROUND_CEILING))
    out['domain']=dict(low=str(low*step),high=str(high*step),step=str(step),size=max(0,high-low+1),linear_risk_per_unit=str(linear_loss))
    if ceiling<policy.minimum_risk_budget_usdt or high<low:
        out.update(reason_codes=['RISK_OR_EXCHANGE_MINIMUM_EXCEEDS_BUDGET'],search_status='PROVEN_EMPTY_DOMAIN');return out
    if only_quantity is not None:
        if only_quantity%step or not low*step<=only_quantity<=high*step:
            out.update(reason_codes=['REQUESTED_QUANTITY_OUTSIDE_HARD_DOMAIN'],search_status='SELECTED_QUANTITY_REJECT');return out
        ns=[int(only_quantity/step)]
    else:
        # Deterministic grade branches, then neighboring lattice residues.
        # This avoids iterating thousands of sizes above a lower grade's cap.
        # It is NOT a binary search, and incomplete coverage stays UNRESOLVED.
        tops=[]
        for branch in policy.tiers:
            rc,qc=caps_for(s,account,request,policy,limits,market,branch,max_price,fee)
            upper_branch=min(*qc.values(),venue.max_quantity,max(D(0),(min(rc.values())-function.fixed_usdt)/linear_loss))
            top=min(high,int((upper_branch/step).to_integral_value(rounding=ROUND_FLOOR)))
            if low<=top and top not in tops: tops.append(top)
        ns=[]
        for offset in range(qp.max_evaluations):
            for top in tops:
                n=top-offset
                if n>=low and n not in ns: ns.append(n)
                if len(ns)>=qp.max_evaluations: break
            if len(ns)>=qp.max_evaluations: break
    for n in ns:
        q=n*step;derived,lineage=derive(candidate,pc,function,q,model,old_prices=old_prices)
        rr=calculate_rr(derived,quantity=number(q))
        card=score_trade_setup(derived,rr,evaluated_at=now,max_data_age_seconds=float(bundle.manifest.score_context_max_age_seconds))
        req=request.model_copy(update={'invalidation_review':request.invalidation_review.model_copy(update={'setup_digest':fingerprint(derived)})})
        def price_check(setup,p,at,issues):
            for code in check_quote(pc,dict(at=pc.quote_at,bid=str(pc.market_trade_price*(1-model.spread_bps/20000)),
                ask=str(pc.market_trade_price*(1+model.spread_bps/20000)),event_id=pc.source_event_id),now=at,expires_at=s.valid_until):
                issues.append(_reason(code,'Execution price layer precondition failed','price_contract'))
        def cost_check(setup,p,at,issues):
            for field,actual,floor,ceiling in (('entry_fee_rate',model.fee_rate,p.minimum_entry_fee_rate,None),
                ('exit_fee_rate',model.fee_rate,p.minimum_exit_fee_rate,None),
                ('entry_slippage_bps',model.slippage_bps,p.minimum_entry_slippage_bps,p.maximum_entry_slippage_bps),
                ('exit_slippage_bps',model.slippage_bps,p.minimum_exit_slippage_bps,p.maximum_exit_slippage_bps)):
                if actual<floor or ceiling is not None and actual>ceiling:
                    issues.append(_reason('COST_ASSUMPTION_OUT_OF_BOUNDS','Declared component outside original bounds',field))
            if D(str(setup.cost_assumptions.funding_cost_usdt))!=function.fixed_usdt+q*function.per_unit_usdt:
                issues.append(_reason('COST_QUANTITY_MISMATCH','Funding must be materialized at final exact quantity'))
        errors=[]
        _check_plan_data(derived,rr,card,req,policy,now,errors,entry_quote_check=None if old_prices else price_check,cost_bounds_check=cost_check)
        why=[e.code for e in errors];tier,soft=soft_checks(card,policy);why.extend(soft)
        required=max(policy.minimum_net_rr,market.minimum_net_rr,tier.minimum_net_rr if tier else policy.minimum_net_rr)
        if _rr_min(rr) is None or _rr_min(rr)<required: why.append('NET_RR_BELOW_CONTEXT_FLOOR')
        rc,qc=caps_for(derived,account,req,policy,limits,market,tier or policy.tiers[-1],max_price,fee)
        budget=min(rc.values());risk=rr.reference.net_stop_loss_usdt
        if q>min(*qc.values(),venue.max_quantity): why.append('FINAL_QUANTITY_EXCEEDS_GRADE_OR_ACCOUNT_CAP')
        if risk>budget or risk<policy.minimum_risk_budget_usdt: why.append('FINAL_RISK_OUT_OF_BOUNDS')
        out.update(derived_setup=derived.model_dump(mode='json'),lineage=lineage,rr=rr.model_dump(mode='json'),scorecard=card.model_dump(mode='json'),
            evaluated_quantity=str(q),required_net_rr=str(required),tier=None if tier is None else tier.name,
            caps={k:str(v) for k,v in rc.items()},quantity_caps={k:str(v) for k,v in qc.items()})
        ev=plan=None
        if not why:
            try:
                plan=build_plan(candidate,derived,bundle,model,pc,function,q)
                unapproved=plan_snapshot(derived,rr,card,digest({'scenario_only':lineage}),'REJECT')
                ev=evaluate_scenarios(plan,derived,unapproved,q)
                risk=max(risk,ev.conservative_initial_risk_usdt)
                if risk>budget: why.append('S0_EXCEEDS_RISK_CEILING')
                s3=ev.scenarios[3]
                if s3.status!='SUPPORTED': why.extend(('S3_UNAVAILABLE',*s3.reason_codes))
                elif s3.scenario_net_rr<required: why.append('S3_NET_RR_BELOW_ORIGINAL_CONTEXT_FLOOR')
            except (ValueError,ArithmeticError) as error: why.append(str(error))
        out['evaluated_quantities'].append(dict(q=str(q),grade=out['tier'],funding=lineage['materialization']['funding_budget_usdt'],
            static_net_rr=None if _rr_min(rr) is None else str(_rr_min(rr)),reason_codes=list(dict.fromkeys(why))))
        out.update(exit_plan=None if plan is None else plan.model_dump(mode='json'),scenarios=None if ev is None else ev.model_dump(mode='json'))
        if not why:
            out.update(result='REDUCE' if budget<request.risk_budget_usdt or n<high else 'APPROVE',
                reason_codes=['QUANTITY_CONSISTENT_PAPER_ELIGIBILITY'],quantity=str(q),risk=str(risk),
                margin=str(q*max_price/request.leverage),fee_reserve=str(q*fee),search_status='CONSISTENT_SOLUTION',risk_ceiling=str(budget))
            return out
        out['reason_codes']=list(dict.fromkeys(why))
        # Proven bound only for static linear cost and a required global floor;
        # floor never decreases below market/policy. No inference about S3.
        if n==high and only_quantity is None and _rr_min(rr) is not None and _rr_min(rr)<max(policy.minimum_net_rr,market.minimum_net_rr):
            out.update(search_status='PROVEN_STATIC_RR_DOMAIN_REJECT',proof=dict(kind='NONNEGATIVE_FIXED_COST_LINEAR_STATIC_RR',
                maximum_quantity=str(q),rr_at_max=str(_rr_min(rr)),global_floor=str(max(policy.minimum_net_rr,market.minimum_net_rr)),
                derivative_sign='f0*(g+l)/(l*q+f0)^2; if g+l<=0 all rewards nonpositive',
                fixed_usdt=str(function.fixed_usdt),quantity_linear_cost=str(function.per_unit_usdt)))
            return out
        if old_prices:
            out.update(search_status='QUANTITY_INDEPENDENT_PRICE_REJECT');return out
    if only_quantity is not None: out['search_status']='SELECTED_QUANTITY_REJECT'
    elif len(out['evaluated_quantities'])==high-low+1: out['search_status']='ENUMERATED_DOMAIN_REJECT'
    else: out.update(result='UNRESOLVED',search_status='EVALUATION_BUDGET_EXHAUSTED',reason_codes=['QUANTIFICATION_UNRESOLVED',*out['reason_codes']])
    return out


def evaluate(store,db,candidate_id,request_id,risk_budget):
    now=get(db,'account','account')['now'];c,_=review(store,db,candidate_id,now=now)
    b=store.bundle(db);m=store.model(db);settings=store.settings(db);a=snapshot(store,db);v=exchange_snapshot(now)
    req=admission_request(store,db,candidate_id,request_id,risk_budget,settings.leverage,now)
    pc=prices(c,m,get(db,'account','account')['quote'],v.price_tick,now=now)
    qp=QuantificationPolicy.model_validate(store.run(db)['quantification'])
    result=quantify(c,b,m,settings,a,v,req,pc,qp,now=now)
    result['quote']=get(db,'account','account')['quote']
    return result
