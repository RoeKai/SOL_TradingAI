"""Finite read-only per-quantity descriptions. Not the formal 32-step solver."""
from decimal import Decimal as D, Context, localcontext
from app.historical_replay.models import HistoricalCandidate
from app.execution_costs.models import PriceContract,CostFunction
from app.execution_costs.prices import derive
from app.execution_costs.plans import build_plan
from app.admitted_paper.provider import number
from app.setups.rr import calculate_rr
from app.exits.models import PlanSnapshot
from app.admission.models import fingerprint
from app.offline_paper.storage import digest
from app.offline_paper.models import OfflineError
from .scenarios import describe_scenarios


def reference_check(body,reference):
    """8C attribution IDs differ from continuous 8D IDs: compare actual geometry."""
    s=body['candidate']['setup'];pc=body['price_contract']
    actual=dict(at=s['created_at'],side=s['side'],signal_reference=pc['signal_reference_price'],
        original_stop=s['initial_stop']['price'],quantity=body['domain']['high'],
        modeled_entry=pc['modeled_fill_price'],frozen_initial_r=abs(D(pc['modeled_fill_price'])-D(str(s['initial_stop']['price']))))
    for key,value in actual.items():
        if key=='side':equal=value==reference[key]
        else:equal=D(str(value))==D(str(reference[key]))
        if not equal:raise ValueError('DISCLOSED_REFERENCE_MISMATCH:'+key)
    return dict(reference_digest=digest(reference),source_candidate_id=reference['source_candidate_id'],
                continuous_candidate_id=body['candidate_id'],geometry_matched=True)


def describe_quantity(body,bundle,model,q,*,duplicates=False):
    with localcontext(Context(prec=50)):
        q=D(q);c=HistoricalCandidate.model_validate(body['candidate'])
        pc=PriceContract.model_validate(body['price_contract']);f=CostFunction.model_validate(body['cost_function'])
        s,lineage=derive(c,pc,f,q,model);rr=calculate_rr(s,quantity=number(q))
        plan=build_plan(c,s,bundle,model,pc,f,q)
        snapshot=PlanSnapshot(setup_id=s.setup_id,plan_version=s.plan_version,symbol=s.symbol,side=s.side,
            original_stop=str(s.initial_stop.price),original_targets=tuple((t.target_id,str(t.price),str(t.fraction)) for t in s.targets),
            setup_digest=fingerprint(s),rr_digest=fingerprint(rr),scorecard_digest=digest({'not_evaluated':'diagnostic'}),
            admission_digest=digest({'read_only':lineage}),admission_result='REJECT')
        descriptions=describe_scenarios(plan,snapshot,q,duplicate_confirmations=duplicates)
        return dict(quantity=str(q),plan_digest=digest(plan),static_net_rr=str(rr.reference.net_rr),
            s3_classification=descriptions[3].premise,descriptions=descriptions,
            formal_solver_unchanged=True,admission_evaluated=False,execution_authority='NONE')


def quantity_grid(body,bundle,model,*,limit=None,progress=None):
    domain=body['domain'];low,high,step=(D(domain[k]) for k in ('low','high','step'))
    if step<=0 or low<=0 or high<low or (high-low)%step:raise ValueError('INVALID_DIAGNOSTIC_DOMAIN')
    size=int((high-low)/step)+1
    if limit is not None and limit<=0:raise ValueError('POSITIVE_LIMIT_REQUIRED')
    count=size if limit is None else min(limit,size);rows=[];classes={};prefix='0'*64
    # Descending, deterministic. The first point is the disclosed 5.329 example.
    for index in range(count):
        q=high-step*index
        try:
            value=describe_quantity(body,bundle,model,q)
        except OfflineError as error:
            # Only explicitly declared model gaps become UNSUPPORTED. A missing TP
            # or arbitrary ValueError is a real error and is NOT swallowed here.
            if str(error) not in ('STAGE3_NUMBER_REPRESENTATION_UNSUPPORTED','SCENARIO_REQUIRES_UNMODELED_RECONCILIATION'):
                raise
            value=dict(quantity=str(q),s3_classification='MODEL_UNSUPPORTED',reason=str(error),descriptions=())
        compact=dict(quantity=str(q),s3_classification=value['s3_classification'],static_net_rr=value.get('static_net_rr'),
            paths=[dict(id=x.scenario_id,premise=x.premise,reached=x.reached,quantity=str(x.quantity),
                remaining=str(x.remaining_quantity),gross=str(x.gross_pnl),fees=str(x.fees_usdt),
                funding=str(x.funding_budget_usdt),net=str(x.conditional_net_cash),cash_rr=str(x.conditional_cash_rr),
                scenario_rr=None if x.scenario_net_rr is None else str(x.scenario_net_rr),reason_codes=x.reason_codes,
                event_digest=x.events_digest) for x in value['descriptions']])
        rows.append(compact);prefix=digest({'previous':prefix,'row':compact})
        key=value['s3_classification'];classes[key]=classes.get(key,0)+1
        if progress and (index+1)%100==0:progress(dict(quantities=index+1,domain=size))
    return dict(version='finite-readonly-quantity-review/v1',source_input_digest=digest(body),domain=domain,
        checked=count,complete=count==size,unchecked_count=size-count,
        unchecked_interval=None if count==size else [str(low),str(high-count*step)],
        classifications=classes,rows=rows,rows_digest=prefix,
        claim='Conditional descriptions only; NOT an admission or whole-domain feasibility proof',
        formal_search_budget=body['quantification']['max_evaluations'],execution_authority='NONE')
