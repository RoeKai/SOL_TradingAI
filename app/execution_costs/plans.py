"""New execution geometry, original structural evidence; no invented targets."""
from decimal import Decimal as D
from app.configuration.compiler import policy_from
from app.admission.models import fingerprint
from app.admitted_paper.models import Trigger
from app.offline_paper.broker import synthetic_rules
from app.offline_paper.storage import digest
from app.historical_replay.data import HistoricalError
from .models import QuantifiedExitPlan


def build_plan(candidate,derived,bundle,model,pc,function,q):
    s=candidate.setup;p=policy_from(bundle,'exit');rules=synthetic_rules()
    if p.runner_strategy!='fixed_r' or s.stop_movement.rules or s.entry.order_type!='MARKET':
        raise HistoricalError('QUANTIFIED_EXIT_PATH_UNSUPPORTED')
    if p.max_holding_seconds>3600 or function.holding_seconds!=p.max_holding_seconds:
        raise HistoricalError('FUNDING_HORIZON_UNSUPPORTED')
    if p.expected_exit_fee_rate!=model.fee_rate or p.expected_exit_slippage_bps!=model.slippage_bps:
        raise HistoricalError('EXIT_POLICY_COST_MISMATCH')
    entry=pc.modeled_fill_price;stop=D(str(s.initial_stop.price));sign=1 if s.side=='LONG' else -1
    if (entry-stop)*sign<=0: raise HistoricalError('EXECUTION_PRICE_INVALIDATES_STOP')
    evidence={e.evidence_id:e for e in s.structure_evidence};refs=[]
    for target in s.targets:
        if target.kind!='structure' or not target.evidence_ids: raise HistoricalError('REAL_STRUCTURE_REQUIRED')
        for key in target.evidence_ids:
            e=evidence[key]
            if e.status!='available' or e.price is None or D(str(e.price))!=D(str(target.price)):
                raise HistoricalError('STRUCTURE_PRICE_MAPPING_MISSING')
    kind='swing_high' if s.side=='LONG' else 'swing_low'
    refs=[(D(str(e.price)),e.evidence_id) for e in s.structure_evidence if e.kind==kind and e.status=='available'
          and e.price is not None and e.observed_at is not None and e.observed_at<=s.created_at and (D(str(e.price))-entry)*sign>0]
    r=abs(entry-stop);triggers=[]
    for name,m,f in (('TP1',p.tp1_r,p.tp1_fraction),('TP2',p.tp2_r,p.tp2_fraction)):
        price=entry+sign*r*m
        corridor=[x for x in refs if (x[0]*(1-sign*model.spread_bps/20000)-price)*sign>=0]
        if price<=0 or not corridor: raise HistoricalError('EXIT_TRIGGER_OUTSIDE_EVIDENCED_CORRIDOR')
        nearest=min(corridor,key=lambda x:(abs(x[0]-entry),x[1]))
        triggers.append(Trigger(name=name,r_multiple=m,original_fraction=f,reference_price=price,corridor_evidence_ids=(nearest[1],)))
    supported=[x for x in refs if (x[0]*(1-sign*model.spread_bps/20000)-entry)*sign>=r*p.runner_activation_r]
    reference=min(supported,key=lambda x:(abs(x[0]-entry),x[1])) if supported else None
    value=QuantifiedExitPlan(plan_id='0'*64,setup_digest=fingerprint(derived),evidence_digest=digest(candidate.evidence),
        original_setup_digest=fingerprint(s),price_contract_digest=digest(pc),cost_function_digest=digest(function),
        materialized_quantity=q,geometry_entry_price=entry,bundle_digest=bundle.bundle_digest,side=s.side,
        reference_entry=pc.market_trade_price,entry_lower=pc.market_trade_price,entry_upper=pc.market_trade_price,
        initial_stop=stop,stop_evidence_ids=s.initial_stop.evidence_ids,triggers=tuple(triggers),runner_fraction=p.runner_fraction,
        runner_reference_price=reference[0] if reference else None,runner_reference_evidence_id=reference[1] if reference else None,
        policy=p,policy_digest=fingerprint(p),rules=rules,rules_digest=fingerprint(rules),
        costs=(('entry_fee_rate',model.fee_rate),('exit_fee_rate',model.fee_rate),
               ('entry_slippage_bps',model.slippage_bps),('exit_slippage_bps',model.slippage_bps)),
        holding_seconds=int(p.max_holding_seconds),created_at=int(s.created_at),valid_until=int(s.valid_until),
        dataset_digest=candidate.dataset_digest,run_digest=candidate.run_digest,spread_bps=model.spread_bps,
        funding_model=function.version,pre_entry_funding_budget_usdt=D(str(derived.cost_assumptions.funding_cost_usdt)))
    return value.model_copy(update={'plan_id':digest(value)})
