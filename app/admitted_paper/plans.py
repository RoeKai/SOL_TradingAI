"""Separate actual exit instructions from immutable historical structure targets."""
from decimal import Decimal as D
from app.admission.models import fingerprint
from app.configuration.compiler import policy_from
from app.offline_paper.broker import synthetic_rules
from app.offline_paper.storage import digest
from app.offline_paper.models import OfflineError
from .models import ExitPlan, Trigger, Candidate, FUNDING


def build_exit_plan(candidate,bundle):
    if type(candidate) is not Candidate: raise OfflineError('Typed candidate required')
    setup=candidate.setup;ex=policy_from(bundle,'exit');rules=synthetic_rules()
    if candidate.bundle_digest!=bundle.bundle_digest: raise OfflineError('EXIT_PLAN_CONFIG_MISMATCH')
    if ex.runner_strategy!='fixed_r': raise OfflineError('SCENARIO_RUNNER_STRATEGY_UNSUPPORTED')
    if setup.stop_movement.rules: raise OfflineError('ADDITIONAL_SETUP_STOP_PATH_UNSUPPORTED')
    if setup.entry.order_type!='MARKET': raise OfflineError('SCENARIO_ENTRY_METHOD_UNSUPPORTED')
    costs=setup.cost_assumptions
    names=('entry_fee_rate','exit_fee_rate','entry_slippage_bps','exit_slippage_bps','funding_cost_usdt')
    if any(getattr(costs,n) is None for n in names): raise OfflineError('SCENARIO_COSTS_MISSING')
    if (costs.source!=FUNDING or costs.funding_cost_usdt!=0 or costs.assumed_holding_seconds is None or
        D(str(costs.assumed_holding_seconds))!=ex.max_holding_seconds or ex.max_holding_seconds>candidate.evidence.funding_horizon_seconds):
        raise OfflineError('SYNTHETIC_FUNDING_MODEL_OR_HORIZON_UNSUPPORTED')
    # These are the actual simulated fee/slippage settings, not duplicated sums.
    if D(str(costs.exit_fee_rate))!=ex.expected_exit_fee_rate or D(str(costs.exit_slippage_bps))!=ex.expected_exit_slippage_bps:
        raise OfflineError('SCENARIO_EXECUTED_COST_MODEL_MISMATCH')
    entry=D(str(setup.entry.reference_price));stop=D(str(setup.initial_stop.price));r=abs(entry-stop)
    sign=1 if setup.side=='LONG' else -1
    ev={e.evidence_id:e for e in setup.structure_evidence}
    refs=[]
    for target in setup.targets:
        if target.kind!='structure' or not target.evidence_ids: raise OfflineError('REAL_STRUCTURE_REQUIRED')
        for key in target.evidence_ids:
            e=ev[key]
            if e.status!='available' or e.price is None or D(str(e.price))!=D(str(target.price)):
                raise OfflineError('STRUCTURE_PRICE_MAPPING_MISSING')
            refs.append((D(str(e.price)),key))
    profit_kind='swing_high' if setup.side=='LONG' else 'swing_low'
    for e in setup.structure_evidence:
        if e.kind==profit_kind and e.status=='available' and e.price is not None and (D(str(e.price))-entry)*sign>0:
            refs.append((D(str(e.price)),e.evidence_id))
    trigger_records=[]
    for name,multiple,fraction in (('TP1',ex.tp1_r,ex.tp1_fraction),('TP2',ex.tp2_r,ex.tp2_fraction)):
        price=entry+sign*r*multiple
        corridor=sorted((p,key) for p,key in refs if (p-price)*sign>=0)
        if price<=0 or not corridor: raise OfflineError('EXIT_TRIGGER_OUTSIDE_EVIDENCED_CORRIDOR')
        nearest=min(corridor,key=lambda v:abs(v[0]-entry))
        trigger_records.append(Trigger(name=name,r_multiple=multiple,original_fraction=fraction,
            reference_price=price,corridor_evidence_ids=(nearest[1],)))
    supported=[(p,k) for p,k in refs if (p-entry)*sign>=r*ex.runner_activation_r]
    reference=None if not supported else min(supported,key=lambda v:abs(v[0]-entry))
    plan=ExitPlan(plan_id='0'*64,setup_digest=fingerprint(setup),evidence_digest=digest(candidate.evidence),
        bundle_digest=bundle.bundle_digest,side=setup.side,reference_entry=entry,entry_lower=D(str(setup.entry.lower_price)),
        entry_upper=D(str(setup.entry.upper_price)),initial_stop=stop,stop_evidence_ids=setup.initial_stop.evidence_ids,
        triggers=tuple(trigger_records),runner_fraction=ex.runner_fraction,
        runner_reference_price=None if reference is None else reference[0],runner_reference_evidence_id=None if reference is None else reference[1],
        policy=ex,policy_digest=fingerprint(ex),rules=rules,rules_digest=fingerprint(rules),
        costs=tuple((name,D(str(getattr(costs,name)))) for name in names),holding_seconds=int(ex.max_holding_seconds),
        created_at=int(setup.created_at),valid_until=int(setup.valid_until))
    return plan.model_copy(update={'plan_id':digest(plan)})


def validate_exit_plan(plan,candidate,bundle):
    if type(plan) is not ExitPlan or plan!=build_exit_plan(candidate,bundle):
        raise OfflineError('EXIT_PLAN_CONTENT_OR_POLICY_BINDING_CHANGED')
    return True
