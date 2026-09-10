"""Historical-instance eligibility; no legacy result is promoted to PASS."""
from decimal import Decimal as D, ROUND_CEILING
from app.admission.engine import admit_trade
from app.admission.contract import require_paper_admission
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from app.configuration.compiler import policy_from
from app.configuration.contracts import validate_contract, validate_historical_context_8c, declare_plan_binding
from app.configuration.inputs import PlanInputs
from app.exits.bindings import capture_plan
from app.offline_paper.broker import synthetic_rules
from app.offline_paper.pricing import execution_price
from app.offline_paper.storage import get
from app.admitted_paper.provider import number
from app.admitted_paper.gate import scenario_admission
from .evidence import review, admission_request
from .funding import snapshot, exchange_snapshot
from .plans import build_plan, validate_plan
from .scenarios import evaluate_scenarios
from .data import HistoricalError

VERSION='historical-scenario-admission/v1'
SCOPE='HISTORICAL_OBSERVATIONS_SIMULATED_ACCOUNT'


def evaluate(store,db,candidate_id,request_id,risk_budget):
    now=get(db,'account','account')['now'];c,_=review(store,db,candidate_id,now=now)
    b=store.bundle(db);settings=store.settings(db);s=c.setup;m=store.model(db)
    policy=policy_from(b,'admission');account=snapshot(store,db);venue=exchange_snapshot(now)
    req=admission_request(store,db,candidate_id,request_id,risk_budget,settings.leverage,now)
    probe=max(venue.min_quantity,venue.min_notional_usdt/D(str(s.entry.reference_price)))
    # A diagnostic order quantity must be expressible on the already-declared
    # quantity lattice. Do not pass repeating 5/price decimals through Stage 3's
    # strict legacy Number boundary. This changes no RR/score/risk algorithm.
    probe=(probe/venue.quantity_step).to_integral_value(rounding=ROUND_CEILING)*venue.quantity_step
    rr=calculate_rr(s,quantity=number(probe));card=score_trade_setup(s,rr,evaluated_at=now,max_data_age_seconds=float(b.manifest.score_context_max_age_seconds))
    d=admit_trade(s,rr,card,account=account,exchange=venue,request=req,policy=policy,evaluated_at=now)
    if d.result!='REJECT':
        first=d.max_quantity;rr=calculate_rr(s,quantity=number(first))
        card=score_trade_setup(s,rr,evaluated_at=now,max_data_age_seconds=float(b.manifest.score_context_max_age_seconds))
        d=admit_trade(s,rr,card,account=account,exchange=venue,request=req,policy=policy,evaluated_at=now)
        if d.result!='REJECT' and d.max_quantity!=first: raise HistoricalError('SIZED_SCORE_QUANTITY_UNSTABLE_REQUIRES_NEW_PLAN')
    inputs=PlanInputs(setup=s,rr=rr,scorecard=card,account=account,exchange=venue,request=req,exit_rules=synthetic_rules())
    binding=declare_plan_binding(b,inputs,declared_at=now)
    inputs=inputs.model_copy(update={'configuration_binding':binding,'admission':d})
    legacy=validate_contract(b,inputs,evaluated_at=now)
    shared=validate_historical_context_8c(b,inputs,evaluated_at=now)
    reasons=[i.reason_code for i in shared if i.severity!='WARNING']
    if d.result=='REJECT': reasons.extend(r.code for r in d.reasons)
    plan=ev=None;risk=margin=fee=D(0)
    if not reasons:
        plan=build_plan(c,b,m);validate_plan(plan,c,b,m)
        ev=evaluate_scenarios(plan,s,capture_plan(s,rr,card,d),d.max_quantity)
        extra,risk=scenario_admission(plan,ev,d,account,settings);reasons.extend(extra)
        side='BUY' if s.side=='LONG' else 'SELL';sign=1 if s.side=='LONG' else -1
        maximum=max(execution_price(p*(1+sign*m.spread_bps/20000),side,m.slippage_bps,plan.rules.price_tick)
            for p in (plan.entry_lower,plan.entry_upper))
        margin=d.max_quantity*maximum/settings.leverage;fee=d.max_quantity*maximum*m.fee_rate
        if (margin+fee>account.available_margin_usdt or margin+fee+account.margin_used_usdt>
            min(account.equity_usdt*settings.limits.max_margin_ratio,settings.limits.max_margin_usdt,policy.max_margin_usdt,account.equity_usdt*policy.max_margin_ratio)):
            reasons.append('ACTUAL_PRICE_MODEL_MARGIN_LIMIT')
        if not reasons:
            require_paper_admission(d,setup=s,rr=rr,scorecard=card,account=account,exchange=venue,request=req,policy=policy,evaluated_at=now,quantity=d.max_quantity)
    return dict(version=VERSION,scope=SCOPE,instance_id=store.instance_id,result='REJECT' if reasons else d.result,
        reason_codes=list(dict.fromkeys(reasons)) or ['HISTORICAL_STATIC_AND_CONDITIONAL_ELIGIBILITY'],
        candidate_id=candidate_id,candidate=c.model_dump(mode='json'),bundle_digest=b.bundle_digest,
        dataset_digest=c.dataset_digest,run_digest=c.run_digest,input_binding=inputs.model_dump(mode='json'),
        legacy_validation=legacy.model_dump(mode='json'),shared_context_issues=[i.model_dump(mode='json') for i in shared],
        exit_plan=None if plan is None else plan.model_dump(mode='json'),scenarios=None if ev is None else ev.model_dump(mode='json'),
        risk=str(risk),margin=str(margin),fee_reserve=str(fee),quantity=str(d.max_quantity),leverage=settings.leverage,
        account_revision=account.snapshot_revision,issued_at=now,expires_at=now if d.valid_until is None else min(int(d.valid_until),c.evidence['expires_at']),
        quote=get(db,'account','account')['quote'],request_id=request_id,requested_risk=str(risk_budget),
        full_policy_expected_return=None,win_probability=None,real_runtime_connected=False,live_allowed=False)
