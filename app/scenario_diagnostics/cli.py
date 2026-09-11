"""python -m app.scenario_diagnostics.cli --source EXPLICIT_8D_DB --output NEW_DIR

No account instance/approval/Broker is constructed. All output is new artifacts.
"""
import argparse
from decimal import Decimal as D
import json
from pathlib import Path
import sys
from .source import frozen_source,bodies
from .proofs import verify_certificate,lattice_crosscheck,UnsupportedProof
from .costs import cost_row,distribution
from .quantities import quantity_grid,describe_quantity,reference_check
from app.offline_paper.storage import digest


def serial(value):
    if isinstance(value,D):return str(value)
    if hasattr(value,'model_dump'):return value.model_dump(mode='json')
    if isinstance(value,dict):return {k:serial(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [serial(v) for v in value]
    return value


def emit(value):
    print(json.dumps(serial(value),sort_keys=True),flush=True)


def write(path,value):
    with path.open('x',encoding='utf-8') as f:
        json.dump(serial(value),f,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)


def sample_candidates(samples,body,cert):
    """Fixed before seeing outcomes: per-side extremes, then stable ID tie-break."""
    c=cert['coefficients'];d=cert['domain'];side=body['candidate']['setup']['side']
    rules={'nearest_floor':abs(cert['rr_upper']-d['k']),'narrowest_stop':c['structural_risk'],
           'lowest_cost':c['target_cost_ex_funding']+c['funding_per_unit'],
           'highest_cost':-(c['target_cost_ex_funding']+c['funding_per_unit']),
           'smallest_domain':d['size'],'largest_domain':-d['size'],
           'smallest_q_max':d['q_max'],'largest_q_max':-d['q_max']}
    for name,value in rules.items():
        key=side+':'+name;rank=(value,body['candidate_id'])
        if key not in samples or rank<samples[key][0]:samples[key]=(rank,body,cert)


def run(source,output,*,sample_points=64,quantity_limit=None,record_limit=None,unresolved_reference=None):
    source=Path(source).absolute();output=Path(output).absolute()
    if output.exists() or any(x in output.parts for x in ('quantified-runs','historical-runs','offline-runs','admitted-runs')):
        raise ValueError('NEW_SEPARATE_DIAGNOSTIC_DIRECTORY_REQUIRED')
    if sample_points<2 or record_limit is not None and record_limit<=0:raise ValueError('INVALID_AUDIT_LIMIT')
    output.mkdir(parents=True,exist_ok=False)
    with frozen_source(source) as (db,run,bundle,model):
        statistics=dict(candidate_records=0,certificates=0,valid=0,unsupported=0,inconsistent=0)
        samples={};costs=[];differences=[];unresolved=[];prefix='0'*64
        inventory=int(db.execute('SELECT count(*) FROM history_approvals').fetchone()[0])
        with (output/'certificates.jsonl').open('x') as full:
            for body in bodies(db):
                statistics['candidate_records']+=1
                if body.get('search_status')=='PROVEN_STATIC_RR_DOMAIN_REJECT':
                    statistics['certificates']+=1
                    try:cert=verify_certificate(body,bundle)
                    except UnsupportedProof as error:
                        cert=dict(candidate_id=body['candidate_id'],status='UNSUPPORTED',reason=str(error),input_digest=digest(body))
                    key=cert['status'].lower();statistics[key]+=1
                    raw=serial(cert);full.write(json.dumps(raw,sort_keys=True)+'\n')
                    prefix=digest({'previous':prefix,'certificate':raw})
                    if cert['status']!='VALID':differences.append(raw)
                    else:sample_candidates(samples,body,cert)
                missing=None
                if not body.get('candidate'):
                    row=db.execute('SELECT payload FROM history_candidates WHERE id=?',(body['candidate_id'],)).fetchone()
                    if row is None:raise ValueError('FROZEN_CANDIDATE_MISSING')
                    missing=json.loads(row[0])
                costs.append(cost_row(body,bundle,model,unquantified_candidate=missing,quantification=run['quantification']))
                if body.get('search_status')=='EVALUATION_BUDGET_EXHAUSTED':unresolved.append(body)
                if statistics['candidate_records']%1000==0:emit(statistics)
                if record_limit is not None and statistics['candidate_records']>=record_limit:break
        sample_results=[];seen=set()
        for name,(_,body,cert) in sorted(samples.items()):
            cid=body['candidate_id']
            if cid in seen:continue
            seen.add(cid)
            result=lattice_crosscheck(body,cert,max_points=sample_points)
            result['selection_rules']=[k for k,v in samples.items() if v[1]['candidate_id']==cid]
            sample_results.append(result)
            if result['differences']:differences.append(result)
            emit(dict(lattice_sample=cid,checked=result['points_checked']))
        write(output/'cost-distributions.json',distribution(costs))
        with (output/'cost-rows.jsonl').open('x') as f:
            for row in costs:f.write(json.dumps(serial(row),sort_keys=True)+'\n')
        grids=[]
        for body in unresolved:
            reference=None if unresolved_reference is None else reference_check(body,unresolved_reference)
            first=describe_quantity(body,bundle,model,D(body['domain']['high']))
            write(output/('quantity-first-'+body['candidate_id']+'.json'),first)
            grid=quantity_grid(body,bundle,model,limit=quantity_limit,progress=emit)
            write(output/('quantity-grid-'+body['candidate_id']+'.json'),grid)
            grids.append(dict({k:v for k,v in grid.items() if k!='rows'},disclosed_reference=reference))
        summary=dict(version='stage08e-independent-diagnostics/v1',source_run_digest=run['content_digest'],
            source_code_commit=run['code_commit'],source_dataset_digest=run['dataset_digest'],
            source_bundle_digest=bundle.bundle_digest,statistics=statistics,certificate_chain_digest=prefix,
            inventory_count=inventory,inventory_complete=statistics['candidate_records']==inventory,
            differences=differences,samples=sample_results,quantity_reviews=grids,
            sample_rule='per side: nearest RR floor, narrowest stop, highest/lowest cost, largest/smallest domain and maximum quantity; tie-break candidate ID',
            lattice_rule='evenly spaced integer indices including both endpoints; explicitly NOT exhaustive unless count==domain',
            orders_created=0,approvals_created=0,source_read_only=True,live_allowed=False,
            limitations=['Certificates concern static structural targets only, not rounded/path-dependent S3',
                        'Full-policy expectation and probability UNKNOWN; budgets are not realized costs',
                        'No continuous monthly account replay performed by this command'])
        write(output/'summary.json',summary);emit(summary)
        return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--sample-points',type=int,default=64)
    parser.add_argument('--quantity-limit',type=int);parser.add_argument('--record-limit',type=int)
    parser.add_argument('--unresolved-reference',type=Path)
    args=parser.parse_args()
    try:
        reference=None
        if args.unresolved_reference is not None:
            if args.unresolved_reference.is_symlink() or args.unresolved_reference.suffix!='.json':
                raise ValueError('EXPLICIT_JSON_REFERENCE_REQUIRED')
            reference=json.loads(args.unresolved_reference.read_text())
        value=run(args.source,args.output,sample_points=args.sample_points,
                  quantity_limit=args.quantity_limit,record_limit=args.record_limit,unresolved_reference=reference)
    except (ValueError,OSError) as error:
        emit(dict(status='ERROR',error=str(error),execution_authority='NONE'));return 2
    return 1 if value['differences'] or not value['inventory_complete'] or any(not x['complete'] for x in value['quantity_reviews']) else 0


if __name__=='__main__':sys.exit(main())
