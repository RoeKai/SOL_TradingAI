"""8C plan mapping: original evidence is never rewritten to create runner RR."""
from decimal import Decimal as D
from app.admission.models import fingerprint
from app.configuration.compiler import policy_from
from app.offline_paper.broker import synthetic_rules
from app.offline_paper.storage import digest
from app.admitted_paper.models import Trigger
from .models import HistoricalCandidate, HistoricalExitPlan
from .data import HistoricalError


def build_plan(candidate,bundle,model):
    if type(candidate) is not HistoricalCandidate: raise HistoricalError('HISTORICAL_CANDIDATE_REQUIRED')
    s=candidate.setup;p=policy_from(bundle,'exit');c=s.cost_assumptions
    if candidate.bundle_digest!=bundle.bundle_digest: raise HistoricalError('CONFIG_BINDING_CHANGED')
    if p.runner_strategy!='fixed_r' or s.stop_movement.rules or s.entry.order_type!='MARKET':
        raise HistoricalError('HISTORICAL_PATH_UNSUPPORTED')
    if any(getattr(c,k) is None for k in ('entry_fee_rate','exit_fee_rate','entry_slippage_bps','exit_slippage_bps','funding_cost_usdt','assumed_holding_seconds')):
        raise HistoricalError('HISTORICAL_COST_DATA_MISSING')
    if (c.source!=model.funding_model or c.assumed_holding_seconds!=p.max_holding_seconds or
        p.max_holding_seconds>3600 or D(str(c.funding_cost_usdt))<=0):
        raise HistoricalError('FUNDING_BUDGET_OR_HORIZON_UNSUPPORTED')
    required_slip=model.slippage_bps+model.spread_bps/2+model.slippage_bps*model.spread_bps/20000
    if (D(str(c.entry_slippage_bps))<required_slip or D(str(c.exit_slippage_bps))<required_slip or
        D(str(c.entry_fee_rate))<model.fee_rate or D(str(c.exit_fee_rate))<model.fee_rate or
        p.expected_exit_fee_rate!=model.fee_rate or p.expected_exit_slippage_bps!=model.slippage_bps):
        raise HistoricalError('COST_BUDGET_UNDERESTIMATES_EXECUTION_MODEL')
    rules=synthetic_rules()  # actual Paper capabilities; historical venue rules remain ASSUMED
    entry=D(str(s.entry.reference_price));stop=D(str(s.initial_stop.price));r=abs(entry-stop)
    sign=1 if s.side=='LONG' else -1;ev={e.evidence_id:e for e in s.structure_evidence};refs=[]
    for target in s.targets:
        if target.kind!='structure' or not target.evidence_ids: raise HistoricalError('REAL_STRUCTURE_REQUIRED')
        for key in target.evidence_ids:
            e=ev.get(key)
            if e is None or e.status!='available' or e.price is None or D(str(e.price))!=D(str(target.price)):
                raise HistoricalError('STRUCTURE_PRICE_MAPPING_MISSING')
            refs.append((D(str(e.price)),key))
    profit_kind='swing_high' if s.side=='LONG' else 'swing_low'
    refs.extend((D(str(e.price)),e.evidence_id) for e in s.structure_evidence if e.kind==profit_kind and
        e.status=='available' and e.price is not None and (D(str(e.price))-entry)*sign>0)
    triggers=[]
    for name,m,f in (('TP1',p.tp1_r,p.tp1_fraction),('TP2',p.tp2_r,p.tp2_fraction)):
        price=entry+sign*r*m;corridor=[(a,k) for a,k in refs if (a-price)*sign>=0]
        if price<=0 or not corridor: raise HistoricalError('EXIT_TRIGGER_OUTSIDE_EVIDENCED_CORRIDOR')
        nearest=min(corridor,key=lambda x:abs(x[0]-entry))
        triggers.append(Trigger(name=name,r_multiple=m,original_fraction=f,reference_price=price,corridor_evidence_ids=(nearest[1],)))
    supported=[x for x in refs if (x[0]-entry)*sign>=r*p.runner_activation_r]
    reference=min(supported,key=lambda x:abs(x[0]-entry)) if supported else None
    # Actual trade costs below are distinct from the conservative static-RR
    # combined spread+slippage allowance. Funding is a separate cash adjustment.
    value=HistoricalExitPlan(plan_id='0'*64,setup_digest=fingerprint(s),evidence_digest=digest(candidate.evidence),
        bundle_digest=bundle.bundle_digest,side=s.side,reference_entry=entry,entry_lower=D(str(s.entry.lower_price)),
        entry_upper=D(str(s.entry.upper_price)),initial_stop=stop,stop_evidence_ids=s.initial_stop.evidence_ids,
        triggers=tuple(triggers),runner_fraction=p.runner_fraction,runner_reference_price=reference[0] if reference else None,
        runner_reference_evidence_id=reference[1] if reference else None,policy=p,policy_digest=fingerprint(p),
        rules=rules,rules_digest=fingerprint(rules),costs=(('entry_fee_rate',model.fee_rate),('exit_fee_rate',model.fee_rate),
            ('entry_slippage_bps',model.slippage_bps),('exit_slippage_bps',model.slippage_bps)),
        holding_seconds=int(p.max_holding_seconds),created_at=int(s.created_at),valid_until=int(s.valid_until),
        dataset_digest=candidate.dataset_digest,run_digest=candidate.run_digest,spread_bps=model.spread_bps,
        pre_entry_funding_budget_usdt=D(str(c.funding_cost_usdt)))
    return value.model_copy(update={'plan_id':digest(value)})


def validate_plan(plan,candidate,bundle,model):
    if type(plan) is not HistoricalExitPlan or plan!=build_plan(candidate,bundle,model):
        raise HistoricalError('HISTORICAL_EXIT_PLAN_CONTENT_CHANGED')
