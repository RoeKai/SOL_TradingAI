"""Original static eligibility AND distinct conditional-policy admission, v1."""
from decimal import Decimal as D
from app.admission.engine import admit_trade
from app.admission.contract import require_paper_admission
from app.admission.models import AdmissionDecision
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from app.configuration.compiler import policy_from, verify_bundle
from app.configuration.contracts import validate_contract, validate_paper_context_8b, declare_plan_binding
from app.configuration.inputs import PlanInputs
from app.exits.bindings import capture_plan
from app.offline_paper.risk import snapshot, exchange_snapshot
from app.offline_paper.broker import synthetic_rules
from app.offline_paper.pricing import execution_price
from app.offline_paper.storage import digest, get
from app.offline_paper.models import OfflineError
from .provider import verify_candidate, admission_request, number
from .plans import build_exit_plan, validate_exit_plan
from .scenarios import evaluate_scenarios

VERSION='paper-scenario-admission/v1'


def scenario_admission(plan,evaluation,decision,account,settings):
    """No rescore/rewrite/threshold reduction. Explicit modeled budget semantics."""
    reasons=[]
    if decision.result=='REJECT': return ('ORIGINAL_ADMISSION_REJECT',),D(0)
    if (evaluation.exit_plan_digest!=digest(plan) or evaluation.setup_digest!=plan.setup_digest or
        evaluation.quantity!=decision.max_quantity): reasons.append('SCENARIO_QUANTITY_OR_PLAN_MISMATCH')
    s3=next(s for s in evaluation.scenarios if s.scenario_id=='S3')
    if s3.status!='SUPPORTED': reasons.extend(('S3_UNAVAILABLE',*s3.reason_codes))
    elif s3.scenario_net_rr is None or decision.required_net_rr is None or s3.scenario_net_rr<decision.required_net_rr:
        reasons.append('S3_NET_RR_BELOW_ORIGINAL_CONTEXT_FLOOR')
    risk=max(evaluation.conservative_initial_risk_usdt,decision.allowed_risk_budget_usdt)
    # Stage 5 allowed_risk_budget is its modeled sized loss. Its explicitly
    # separate policy_risk_ceiling is the authorized budget cap (not leverage).
    # Tick/fee differences consume that same headroom; never increase quantity.
    ceiling=min(decision.policy_risk_ceiling_usdt,settings.limits.max_loss_per_trade_usdt,
        settings.limits.daily_loss_limit_usdt-account.day_realized_loss_usdt-account.unrealized_loss_usdt-account.reserved_risk_usdt)
    if risk>ceiling: reasons.append('S0_EXCEEDS_ORIGINAL_AND_ACCOUNT_RISK_CEILING')
    return tuple(reasons),risk


def evaluate(store,db,candidate_id,request_id,risk_budget):
    now=get(db,'account','account')['now'];candidate=verify_candidate(store,db,candidate_id,now)
    bundle=store.bundle(db);settings=store.settings(db);setup=candidate.setup
    policy=policy_from(bundle,'admission');account=snapshot(store,db);venue=exchange_snapshot(now)
    request=admission_request(candidate,request_id,risk_budget,settings.leverage,now)
    # Diagnostic quantity only. Bounded second evaluation at the ORIGINAL
    # Admission-selected exact size. Never search targets or policy to pass.
    probe=max(venue.min_quantity,venue.min_notional_usdt/D(str(setup.entry.reference_price)))
    rr=calculate_rr(setup,quantity=number(probe))
    card=score_trade_setup(setup,rr,evaluated_at=now,max_data_age_seconds=float(bundle.manifest.score_context_max_age_seconds))
    decision=admit_trade(setup,rr,card,account=account,exchange=venue,request=request,policy=policy,evaluated_at=now)
    if decision.result!='REJECT':
        first_qty=decision.max_quantity
        rr=calculate_rr(setup,quantity=number(first_qty))
        card=score_trade_setup(setup,rr,evaluated_at=now,max_data_age_seconds=float(bundle.manifest.score_context_max_age_seconds))
        decision=admit_trade(setup,rr,card,account=account,exchange=venue,request=request,policy=policy,evaluated_at=now)
        if decision.result!='REJECT' and decision.max_quantity!=first_qty:
            raise OfflineError('SIZED_SCORE_QUANTITY_UNSTABLE_REQUIRES_NEW_PLAN')
    inputs=PlanInputs(setup=setup,rr=rr,scorecard=card,account=account,exchange=venue,request=request,exit_rules=synthetic_rules())
    binding=declare_plan_binding(bundle,inputs,declared_at=now)
    inputs=inputs.model_copy(update={'configuration_binding':binding,'admission':decision})
    legacy=validate_contract(bundle,inputs,evaluated_at=now)  # retained, not promoted to PASS
    shared=validate_paper_context_8b(bundle,inputs,evaluated_at=now)
    reasons=[i.reason_code for i in shared if i.severity!='WARNING']
    if decision.result=='REJECT': reasons.extend(r.code for r in decision.reasons)
    plan=evaluation=None;reserved_risk=D(0);margin=D(0);fee=D(0)
    if not reasons:
        plan=build_exit_plan(candidate,bundle);validate_exit_plan(plan,candidate,bundle)
        evaluation=evaluate_scenarios(plan,setup,capture_plan(setup,rr,card,decision),decision.max_quantity)
        extra,reserved_risk=scenario_admission(plan,evaluation,decision,account,settings);reasons.extend(extra)
        cost=dict(plan.costs);side='BUY' if setup.side=='LONG' else 'SELL'
        max_price=max(execution_price(p,side,cost['entry_slippage_bps'],plan.rules.price_tick)
            for p in (plan.entry_lower,plan.entry_upper))
        margin=decision.max_quantity*max_price/settings.leverage
        fee=decision.max_quantity*max_price*cost['entry_fee_rate']
        if (margin+fee>account.available_margin_usdt or margin+fee+account.margin_used_usdt>
            min(account.equity_usdt*settings.limits.max_margin_ratio,settings.limits.max_margin_usdt,policy.max_margin_usdt,
                account.equity_usdt*policy.max_margin_ratio)):
            reasons.append('ACTUAL_PRICE_MODEL_MARGIN_LIMIT')
        if not reasons:
            require_paper_admission(decision,setup=setup,rr=rr,scorecard=card,account=account,exchange=venue,
                request=request,policy=policy,evaluated_at=now,quantity=decision.max_quantity)
    return dict(version=VERSION,scope='SYNTHETIC_OFFLINE',instance_id=store.instance_id,
        result='REJECT' if reasons else decision.result,reason_codes=list(dict.fromkeys(reasons)) or ['STATIC_AND_CONDITIONAL_PAPER_ELIGIBILITY'],
        candidate_id=candidate_id,candidate=candidate.model_dump(mode='json'),bundle_digest=bundle.bundle_digest,
        input_binding=inputs.model_dump(mode='json'),legacy_validation=legacy.model_dump(mode='json'),
        shared_context_issues=[i.model_dump(mode='json') for i in shared],
        exit_plan=None if plan is None else plan.model_dump(mode='json'),
        scenarios=None if evaluation is None else evaluation.model_dump(mode='json'),
        risk=str(reserved_risk),margin=str(margin),fee_reserve=str(fee),
        quantity=str(decision.max_quantity),leverage=settings.leverage,account_revision=account.snapshot_revision,
        issued_at=now,expires_at=now if decision.valid_until is None else min(int(decision.valid_until),candidate.evidence.expires_at),
        quote=get(db,'account','account')['quote'],request_id=request_id,requested_risk=str(risk_budget),
        full_policy_expected_return=None,win_probability=None,real_runtime_connected=False,live_allowed=False)
