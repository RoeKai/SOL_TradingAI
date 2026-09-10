"""Offline-only isolated command. Never imports downloader or main/runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import time
from app.utils.paths import ModulePaths, read_text_nofollow, check_owned
from app.offline_paper.storage import get
from .data import load_manifest, immutable_json, verify_seal, HistoricalError, END, START
from .models import ExecutionModel
from .configuration import configuration, run_manifest, INSTANCE
from .storage import HistoricalStore, hget, hput
from .index import build_index, verify_index
from .engine import HistoricalPaper
from .replay import Replay
from .report import result


def code_digest(workspace):
    paths=ModulePaths(workspace);h=hashlib.sha256()
    files=sorted(paths.file('app').rglob('*.py'))
    files += [paths.file(p) for p in ('admission.yaml','exit-policy.yaml','examples/admitted-paper/main.yaml','examples/admitted-paper/manifest.yaml','isolation-policy.json')]
    for f in files:
        h.update(str(f.relative_to(paths.root)).encode());h.update(read_text_nofollow(f).encode())
    return h.hexdigest()


def main(argv=None):
    p=argparse.ArgumentParser(description='8C verified historical OFFLINE replay; no account or order network')
    p.add_argument('command',choices=('index','freeze','init','run','recover','report'))
    p.add_argument('--workspace',default='.')
    p.add_argument('--dataset',required=True)
    p.add_argument('--run-id',default='august-baseline-v1')
    p.add_argument('--manifest')
    p.add_argument('--code-commit')
    p.add_argument('--kind',choices=('engineering','baseline','stress','missing_restart'),default='baseline')
    p.add_argument('--max-events',type=int)
    p.add_argument('--fault',choices=('before_market_cursor_commit','after_market_cursor_commit','before_funding_cursor_commit','scheduled_restart'))
    args=p.parse_args(argv)
    try:
        if args.command=='index':
            output=build_index(args.dataset)
        elif args.command=='freeze':
            if not args.manifest or not args.code_commit: raise HistoricalError('EXPLICIT_MANIFEST_PATH_AND_CODE_COMMIT_REQUIRED')
            dataset=load_manifest(args.dataset)
            model=ExecutionModel(slippage_bps='20',acceptance_delay_ms=3000) if args.kind=='stress' else ExecutionModel()
            bundle,settings=configuration(args.workspace,model)
            end=END if args.kind in ('baseline','stress') else START+3600000
            output=run_manifest(experiment_id=args.run_id,code_commit=args.code_commit,code_digest=code_digest(args.workspace),
                dataset=dataset,bundle=bundle,settings=settings,model=model,kind=args.kind,end_ms=end)
            immutable_json(check_owned(args.manifest),output)
        elif args.command=='init':
            if not args.manifest: raise HistoricalError('FROZEN_MANIFEST_REQUIRED')
            run=verify_seal(json.loads(read_text_nofollow(args.manifest)));model=ExecutionModel.model_validate(run['execution_model'])
            if run['code_content_digest']!=code_digest(args.workspace): raise HistoricalError('RUN_CODE_CONTENT_CHANGED_CREATE_NEW_EXPERIMENT')
            bundle,settings=configuration(args.workspace,model)
            store=HistoricalStore.initialize(args.workspace,args.run_id,settings,bundle,load_manifest(args.dataset),run)
            paper=HistoricalPaper(store);paper.recover();output={'initialized':True,'run_digest':run['content_digest'],'live_allowed':False}
        else:
            store=HistoricalStore(args.workspace,args.run_id,INSTANCE);paper=HistoricalPaper(store)
            with store.transaction() as db: run=store.run(db);dataset=hget(db,'history_meta','dataset')
            if run['code_content_digest']!=code_digest(args.workspace): raise HistoricalError('RUN_CODE_CONTENT_CHANGED_CREATE_NEW_EXPERIMENT')
            if args.command=='report': output=result(paper)
            else:
                live_dataset=load_manifest(args.dataset)
                if live_dataset!=dataset: raise HistoricalError('RUN_DATASET_CHANGED')
                index=verify_index(args.dataset,dataset)
                start=time.perf_counter();paper.recover();elapsed=time.perf_counter()-start
                with store.transaction() as db:
                    pmeta=hget(db,'history_meta','recoveries') or {'seconds':[]}
                    pmeta['seconds'].append(elapsed);hput(db,'history_meta','recoveries',pmeta)
                if args.command=='recover': output={'recovery_seconds':elapsed,'result':result(paper)}
                else:
                    segment=Replay(paper).run_stream(args.dataset,index,max_events=args.max_events,fault=args.fault)
                    with store.transaction() as db:
                        perf=hget(db,'history_meta','performance')
                        perf['peak_rss_platform_units']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                        hput(db,'history_meta','performance',perf)
                    output={'segment':segment,'result':result(paper)}
        print(json.dumps(output,indent=2,sort_keys=True,allow_nan=False));return 0
    except (ValueError,ArithmeticError,OSError) as error:
        print(json.dumps({'ok':False,'error':str(error),'type':type(error).__name__,'live_allowed':False}));return 2


if __name__=='__main__': raise SystemExit(main())
