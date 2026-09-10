"""Bounded read-only validation driver, not a trading runtime or new algorithm.

Run with python -m scripts.attribute_8d_parallel. Workers receive only immutable
candidate inputs; no DB connection, account or Broker is passed to a worker.
Same original compare() for every row; ordered aggregation is worker-count
independent. At most 128 pending records and four processes.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from app.execution_costs.attribution import compare
from app.execution_costs.configuration import configuration
from app.execution_costs.cli import code_digest
from app.execution_costs.models import QuantificationPolicy
from app.historical_replay.models import ExecutionModel
from app.historical_replay.provider import describe
from app.historical_replay.storage import HistoricalStore,hget
from app.historical_replay.configuration import INSTANCE
from app.historical_replay.data import verify_seal,seal,immutable_json,HistoricalError
from app.offline_paper.storage import digest
from app.utils.paths import read_text_nofollow,check_owned

_CONTEXT=None


def initialize(context):
    global _CONTEXT
    _CONTEXT=context


def evaluate_row(job):
    n,source_id,raw=job;b,s,m,run=_CONTEXT
    candidate=describe(b,m,run,raw['setup']['side'],raw['evidence']['window'],raw['evidence']['level'],now=raw['setup']['created_at'])
    value=compare(candidate,b,m,s,QuantificationPolicy.model_validate(run['quantification']))
    groups={k:{field:v.get(field) for field in ('result','reason_codes','search_status','evaluated_funding_usdt')}
        for k,v in value['groups'].items()}
    return dict(index=n,source_id=source_id,at_ms=int(candidate.setup.created_at*1000),
        comparison_digest=digest(value),groups=groups,sample=value if n<3 else None)


def run(workspace,source_run,manifest,*,workers=4,limit=None):
    if workers not in (1,2,3,4) or limit is not None and limit<=0:raise HistoricalError('BOUNDED_VALIDATION_ARGUMENTS_REQUIRED')
    frozen=verify_seal(json.loads(read_text_nofollow(manifest)))
    if frozen['code_content_digest']!=code_digest(workspace):raise HistoricalError('CODE_CONTENT_CHANGED')
    m=ExecutionModel.model_validate(frozen['execution_model']);b,s=configuration(workspace,m)
    if b.bundle_digest!=frozen['config_digest']:raise HistoricalError('CONFIG_CHANGED')
    path=HistoricalStore(workspace,source_run,INSTANCE).path
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON')
    try:
        source=verify_seal(hget(db,'history_meta','run'));data=verify_seal(hget(db,'history_meta','dataset'))
        if data['content_digest']!=frozen['dataset_digest']:raise HistoricalError('DATASET_MISMATCH')
        begin,end=frozen['evaluation_range_ms'];n=0;prefix='0'*64;transitions={};samples=[];last=None
        totals={k:dict(results={},reasons={},search={},funding_min=None,funding_max=None) for k in 'ABCD'}
        started=time.perf_counter();batch=[]
        def consume(values):
            nonlocal n,prefix,last
            for value in values:
                assert value['index']==n
                n+=1;last=value['at_ms'];prefix=digest({'prefix':prefix,'source_id':value['source_id'],'comparison_digest':value['comparison_digest']})
                route='/'.join(value['groups'][k]['result'] for k in 'ABCD');transitions[route]=transitions.get(route,0)+1
                if value['sample'] is not None:samples.append(value['sample'])
                for k,v in value['groups'].items():
                    t=totals[k]
                    for field,key in (('results',v['result']),('search',v['search_status'])):t[field][key]=t[field].get(key,0)+1
                    for reason in set(v['reason_codes']):t['reasons'][reason]=t['reasons'].get(reason,0)+1
                    f=v['evaluated_funding_usdt']
                    if f is not None:
                        t['funding_min']=str(min(D(f),D(t['funding_min']))) if t['funding_min'] is not None else f
                        t['funding_max']=str(max(D(f),D(t['funding_max']))) if t['funding_max'] is not None else f
        with ProcessPoolExecutor(max_workers=workers,initializer=initialize,initargs=((b,s,m,frozen),)) as pool:
            submitted=0
            for key,payload in db.execute('SELECT id,payload FROM history_candidates ORDER BY rowid'):
                raw=json.loads(payload);at=raw['setup']['created_at']*1000
                if not begin<=at<end:continue
                batch.append((submitted,key,raw));submitted+=1
                if len(batch)==128:
                    consume(pool.map(evaluate_row,batch));batch=[]
                    print(json.dumps({'attribution_candidates':n,'at_ms':last,'seconds':round(time.perf_counter()-started,3)}),flush=True)
                if limit is not None and submitted>=limit:break
            if batch:consume(pool.map(evaluate_row,batch))
        return seal(dict(version='same-candidate-attribution-parallel-driver/v1',
            source_run_digest=source['content_digest'],source_code=source['code_commit'],new_run_digest=frozen['content_digest'],
            code_commit=frozen['code_commit'],code_content_digest=frozen['code_content_digest'],
            driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),dataset_digest=data['content_digest'],
            candidates=n,groups=totals,samples=samples,transitions=transitions,comparison_digest=prefix,
            configured_range_ms=[begin,end],completed_through_ms=last,limit=limit,full_candidate_set_evaluated=limit is None,
            independent_samples=n,counts_must_not_be_summed_across_groups=True,source_read_only=True,orders_created=0,
            workers=workers,batch_limit=128,seconds=time.perf_counter()-started,
            snapshot='Same fixed empty 500-USDT counterfactual for all groups; not continuous account state',live_allowed=False))
    finally:db.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',default='.');p.add_argument('--source-run',required=True)
    p.add_argument('--manifest',required=True);p.add_argument('--workers',type=int,default=4)
    p.add_argument('--limit',type=int);p.add_argument('--output',required=True);a=p.parse_args()
    value=run(a.workspace,a.source_run,a.manifest,workers=a.workers,limit=a.limit)
    immutable_json(check_owned(a.output),value)
    print(json.dumps({k:value[k] for k in ('candidates','transitions','seconds','comparison_digest')}))


if __name__=='__main__':main()
