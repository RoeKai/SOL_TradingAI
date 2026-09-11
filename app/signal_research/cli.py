"""Explicit read-only frozen-August research; no trading entry/fixture/approval.

python -m app.signal_research.cli --source .../quantified-runs/.../ledger.sqlite3
  --dataset .../historical-data/august-2026-v1 --cost-rows .../cost-rows.jsonl
  --output .../research-runs/NEW --code-commit FULL_SHA
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import resource
import sys
import time
from decimal import Decimal as D

from app.historical_replay.data import load_manifest, sha_file, START, END, WARMUP
from app.historical_replay.index import verify_index, TradeStream
from app.scenario_diagnostics.source import frozen_source, bodies
from app.offline_paper.storage import digest
from app.utils.paths import check_owned, read_text_nofollow
from .features import extract, price_units
from .io import serial, write, emit
from .labels import ObservationIndex, HORIZONS
from .statistics import compact, summarize as summarize_labels
from .costs import research_row, CostSummary

PROTOCOL_COMMIT='d00bf544eec2c5cd918af2cf478c7c0081a4803d'
PROTOCOL_SHA256='1374bcdb5580f86c5752bcf25c23c159f8f610a18358a2ca165ff93cd203d743'
DATASET_DIGEST='2bd4ab5b4cdca36938dbb32aa01214d8cf5cfd236f153734b25f400d4e7fb098'
RUN_DIGEST='4814773e5b96237582512da461358ae837100362d4ba16d7aa7906e725bdf8bc'
COST_SHA256='56177d9579b687b42f5c0cc31af0019d2e9e8d3ced936ff4bf475747dc461546'


def peak_bytes():
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform=='darwin' else rss*1024


def budget(start):
    if time.perf_counter()-start>3600 or peak_bytes()>2*1024**3:
        raise ValueError('RESEARCH_RESOURCE_BOUND_EXCEEDED')


def source_status(db):
    counts={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in
        ('broker_orders','broker_fills','positions','reservations','outbox','history_consumptions')}
    counts['account_payload_digest']=hashlib.sha256(db.execute('SELECT payload FROM account').fetchone()[0].encode()).hexdigest()
    return counts


def sol_events(root,index,delay,metrics):
    files=sorted((f for f in index['files'] if f['symbol']=='SOLUSDT'),key=lambda f:f['first_ms'])
    for metadata in files:
        stream=TradeStream(root,metadata,delay)
        count=0
        try:
            while (e:=stream.next()) is not None:
                count+=1;metrics['parsed_bytes']+=e['next_offset']-metrics.pop('_offset',0)
                metrics['_offset']=e['next_offset'];metrics['parsed_records']+=1
                yield e['event_time_ms'],e['available_at_ms'],price_units(e['price']),e['raw_sequence']
            if count!=metadata['records']:raise ValueError('INDEX_STREAM_COUNT_CHANGED')
        finally:
            stream.close();metrics.pop('_offset',None)


def export_features(db,destination):
    result=[];chain='0'*64
    with destination.open('x') as out:
        for cid,payload in db.execute('SELECT id,payload FROM history_candidates ORDER BY rowid'):
            raw=json.loads(payload);f=extract(raw)
            if cid!=f['candidate_id']:raise ValueError('SOURCE_CANDIDATE_ID_MISMATCH')
            out.write(json.dumps(serial(f),sort_keys=True)+'\n');result.append(f)
            chain=digest({'previous':chain,'feature':f})
            if len(result)>40000:raise ValueError('FROZEN_POPULATION_BOUND_EXCEEDED')
    return result,chain


def write_labels(index,features,output,start):
    records=[];representatives={};chain='0'*64
    # The future-bearing output cannot be mistaken for candidate features.
    with (output/'future-labels.jsonl').open('x') as out:
        for group in (features,index.background()):
            for f in group:
                for horizon in HORIZONS:
                    label=index.label(f,horizon);raw=serial(label)
                    out.write(json.dumps(raw,sort_keys=True)+'\n')
                    records.append(compact(label));chain=digest({'previous':chain,'label':raw})
                    if f['kind']=='SIGNAL':
                        keys=[f"{f['side']}:{horizon}:status:{label['status']}"]
                        if label['later_favorable_after_invalidation']:keys.append(f"{f['side']}:{horizon}:invalidated_then_favorable")
                        for key in keys:
                            if key not in representatives or f['candidate_id']<representatives[key]['candidate_id']:
                                representatives[key]=raw
                if len(records)%4000==0:budget(start)
    # Median selection is deterministic from the registered primary return, not MFE.
    medians={}
    for side in ('LONG','SHORT'):
        eligible=sorted((r for r in records if r['kind']=='SIGNAL' and r['side']==side and r['horizon']==900 and r['eligible']),key=lambda r:(r['r'],r['id']))
        if eligible:
            middle=eligible[(len(eligible)-1)//2]['r']
            chosen=min(eligible,key=lambda r:(abs(r['r']-middle),r['id']))
            medians[side]=chosen
    return records,dict(fixed_class_examples=representatives,primary_median_examples=medians),chain


def cost_pass(db,cost_path,model,output,start):
    summary=CostSummary();count=0;chain='0'*64
    with cost_path.open() as old_rows,(output/'cost-sensitivities.jsonl').open('x') as out:
        for body in bodies(db):
            line=old_rows.readline()
            if not line:raise ValueError('MISSING_8E_COST_ROW')
            old=json.loads(line)
            if old['candidate_id']!=body['candidate_id']:raise ValueError('8E_ROW_IDENTITY_OR_ORDER_CHANGED')
            candidate=body.get('candidate')
            if not candidate:
                candidate=json.loads(db.execute('SELECT payload FROM history_candidates WHERE id=?',(body['candidate_id'],)).fetchone()[0])
            # The 27 pre-quantification stale records had no rule receipt. The
            # old 8E descriptor explicitly used the frozen assumed .01 rule;
            # reuse that declared assumption, never label it venue-verified.
            tick=D(body['exchange']['price_tick']) if body.get('exchange') else D('.01')
            row=research_row(candidate,old,spread_bps=model.spread_bps,tick=tick)
            row['price_rule_basis']='FROZEN_8D_DECLARED_SNAPSHOT' if body.get('exchange') else '8E_RETROSPECTIVE_DECLARED_TICK_0.01_NO_RECEIPT'
            row['price_tick']=tick
            raw=serial(row);out.write(json.dumps(raw,sort_keys=True)+'\n');summary.add(row)
            chain=digest({'previous':chain,'row':raw});count+=1
            if count%1000==0:emit(dict(cost_candidates=count));budget(start)
        if old_rows.readline():raise ValueError('EXTRA_8E_COST_ROWS')
    return summary.result(),count,chain


def run(source,dataset,cost_rows,output,*,code_commit):
    start=time.perf_counter()
    output=check_owned(Path(output).absolute());root=Path(__file__).absolute().parents[2]
    if output.parent.name!='research-runs' or output.exists():raise ValueError('NEW_RESEARCH_DIRECTORY_REQUIRED')
    if not re.fullmatch('[0-9a-f]{40}',code_commit):raise ValueError('EXPLICIT_CODE_COMMIT_REQUIRED')
    protocol=root/'docs/STAGE_08FA_RESEARCH_PROTOCOL.md'
    if sha_file(protocol)!=PROTOCOL_SHA256:raise ValueError('FROZEN_PROTOCOL_CHANGED')
    source=check_owned(Path(source).absolute());dataset=check_owned(Path(dataset).absolute());cost_rows=check_owned(Path(cost_rows).absolute())
    if sha_file(cost_rows)!=COST_SHA256:raise ValueError('FROZEN_8E_COST_ARTIFACT_CHANGED')
    output.mkdir(parents=True,exist_ok=False)
    versions=dict(version='stage08fa-readonly-research/v1',protocol_commit=PROTOCOL_COMMIT,protocol_sha256=PROTOCOL_SHA256,
        code_commit=code_commit,research_code_digest=digest({p.name:sha_file(p) for p in sorted((root/'app/signal_research').glob('*.py'))}),
        declared_data_use='DEVELOPMENT / ALREADY_EXAMINED',holdout='PENDING',execution_authority='NONE',live_allowed=False)
    write(output/'run-start.json',versions)
    try:
        manifest=load_manifest(dataset);index=verify_index(dataset,manifest)
        if manifest['content_digest']!=DATASET_DIGEST:raise ValueError('UNREGISTERED_DATASET')
        hash_bytes=sum(f['bytes'] for f in index['files'])+sum((dataset/f['file']).stat().st_size for f in manifest['files'])+cost_rows.stat().st_size
        metrics=dict(parsed_records=0,parsed_bytes=0,verification_bytes=hash_bytes)
        with frozen_source(source) as (db,old_run,bundle,model):
            if old_run['content_digest']!=RUN_DIGEST or model.observation_delay_ms!=250 or model.slippage_bps!=10:
                raise ValueError('FROZEN_RUN_OR_MODEL_CHANGED')
            before=source_status(db)
            features,feature_chain=export_features(db,output/'candidate-features.jsonl')
            if len(features)!=37141:raise ValueError('FROZEN_CANDIDATE_COUNT_CHANGED')
            def progress(value):
                budget(start);emit(value)
            observations=ObservationIndex(sol_events(dataset,index,model.observation_delay_ms,metrics),features,
                start=START,end=END,warmup=WARMUP,progress=progress)
            records,reps,label_chain=write_labels(observations,features,output,start)
            labels=summarize_labels(records,START,END)
            write(output/'direction-summary.json',labels);write(output/'label-representatives.json',reps)
            del records
            costs,cost_count,cost_chain=cost_pass(db,cost_rows,model,output,start)
            write(output/'cost-boundary-summary.json',costs)
            if source_status(db)!=before:raise AssertionError('READ_ONLY_SOURCE_MUTATED')
            budget(start)
            summary=dict(versions,status='COMPLETE',candidate_count=len(features),cost_count=cost_count,
                sensitivity_rows=cost_count*6,source_run_digest=old_run['content_digest'],source_bundle_digest=bundle.bundle_digest,
                dataset_digest=manifest['content_digest'],index_digest=index['content_digest'],frozen_cost_rows_sha256=COST_SHA256,
                feature_chain_digest=feature_chain,label_chain_digest=label_chain,cost_chain_digest=cost_chain,
                performance=dict(metrics,seconds=time.perf_counter()-start,peak_rss_bytes=peak_bytes(),
                    observation_bins=len(observations.bars),outside_month_visibility=observations.outside_visibility,
                    scan='ONE_SOL_SCAN; other symbol files hash-verified only'),
                source_state_unchanged=True,source_check='READ_ONLY_TRANSACTION_AND_COUNTS_ACCOUNT_DIGEST_NOT_BINARY_DB_HASH',
                orders_created=0,approvals_created=0,simulated_profit=None,equity_drawdown=None,
                primary_result=labels['windows']['900']['results']['POOLED']['comparison'])
            write(output/'summary.json',summary)
            write(output/'artifact-manifest.json',dict(versions,artifacts=[dict(file=p.name,sha256=sha_file(p),bytes=p.stat().st_size)
                for p in sorted(output.iterdir()) if p.is_file()]))
            emit(summary);return summary
    except Exception as error:
        write(output/'FAILED.json',dict(versions,status='FAILED',error_type=type(error).__name__,error=str(error),
            elapsed_seconds=time.perf_counter()-start,peak_rss_bytes=peak_bytes()))
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('source','dataset','cost-rows','output','code-commit'):parser.add_argument('--'+arg,required=True)
    args=parser.parse_args()
    run(args.source,args.dataset,args.cost_rows,args.output,code_commit=args.code_commit)


if __name__=='__main__':main()
