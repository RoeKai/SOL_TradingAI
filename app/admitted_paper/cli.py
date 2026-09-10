"""Explicit offline command, never imports main/runtime/Telegram/Bridge."""
import argparse
import json
from .demo import initialize, normal_open, complete_exit, compact, refusal, INSTANCE
from .engine import AdmittedPaper
from .storage import AdmittedStore
from .review import review


def main(argv=None):
    p=argparse.ArgumentParser(description='8B SYNTHETIC_OFFLINE only; no live or realtime capability')
    p.add_argument('command',choices=('demo','reject','recover','status','review'))
    p.add_argument('--workspace',default='.')
    p.add_argument('--run-id',required=True)
    p.add_argument('--side',choices=('LONG','SHORT'),default='LONG')
    p.add_argument('--path',choices=('runner','gap'),default='runner')
    p.add_argument('--reason',choices=('evidence','risk','scenario'),default='scenario')
    p.add_argument('--fault',choices=('before_intent_commit','after_intent_commit','after_broker_execution','after_receipt_commit','unnotified-entry'))
    args=p.parse_args(argv)
    if args.command!='demo' and args.fault: p.error('--fault is only valid for the explicit synthetic demo')
    if args.command=='reject':
        paper=initialize(args.workspace,args.run_id)
        result=refusal(paper,args.side,args.reason)
    elif args.command=='demo':
        paper=initialize(args.workspace,args.run_id)
        normal_open(paper,args.side,fault=args.fault);complete_exit(paper,path=args.path)
        before=compact(paper)
        paper=AdmittedPaper(AdmittedStore(args.workspace,args.run_id,INSTANCE));paper.recover()
        result={'entry_kind':'ORDINARY_FROM_ZERO_NOT_FIXTURE','before_restart':before,'after_restart':compact(paper)}
    else:
        paper=AdmittedPaper(AdmittedStore(args.workspace,args.run_id,INSTANCE))
        if args.command=='recover': paper.recover()
        result=review(paper.summary()) if args.command=='review' else compact(paper)
    print(json.dumps(result,indent=2,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
