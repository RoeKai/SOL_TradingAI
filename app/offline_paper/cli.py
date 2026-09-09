"""Separate offline entry point; never imports main, runtime or a network client."""
import argparse
import json
from pathlib import Path
import sqlite3

from .engine import OfflinePaper
from .storage import Store, get, rows
from .models import OfflineError
from .fixtures import example_bundle, example_settings, run_scenario, opening, quote
from .review import markdown

ROOT=Path(__file__).resolve().parents[2]
FAULTS=('before_intent_commit','after_intent_commit','after_broker_execution','after_receipt_commit')
SCENARIOS=('admission-rejection','partial-cover','partial-cancel','tp-runner','stop-gap','unknown-reconcile')


def main(argv=None):
    parser=argparse.ArgumentParser(description='Synthetic offline only; no orders outside this dedicated ledger')
    parser.add_argument('command',choices=('init','run','resume','status','review','crash-probe'))
    parser.add_argument('--workspace',type=Path,default=ROOT,help='Explicit isolated module root, never searched')
    parser.add_argument('--run',required=True)
    parser.add_argument('--instance',default='offline-paper-demo')
    parser.add_argument('--allow-fixtures',action='store_true')
    parser.add_argument('--scenario',choices=SCENARIOS,default='admission-rejection')
    parser.add_argument('--side',choices=('LONG','SHORT'),default='LONG')
    parser.add_argument('--point',choices=FAULTS,default='after_intent_commit')
    parser.add_argument('--compact',action='store_true',help='Small machine-readable acceptance summary')
    args=parser.parse_args(argv)
    try:
        if args.command=='init':
            if args.instance!='offline-paper-demo': raise OfflineError('Example bundle has explicit offline-paper-demo instance')
            store=Store.create(args.workspace,args.run,example_settings(fixtures=args.allow_fixtures),example_bundle(args.workspace))
        else:
            store=Store(args.workspace,args.run,args.instance)
        runner=OfflinePaper(store)
        if args.command in ('init','resume','run','crash-probe'): runner.recover()
        if args.command=='run': result=run_scenario(runner,args.scenario,args.side)
        elif args.command=='crash-probe':
            with store.transaction() as db:
                if not store.settings(db).allow_fixtures or rows(db,'requests'):
                    raise OfflineError('Crash probe requires a fresh explicit fixture run')
            quote(runner,'100','crash-price')
            request=opening(runner,args.side,'crash-probe')
            entry=runner.fixture_entry(request,fault=args.point)
            runner.pump(fault=args.point if args.point=='after_broker_execution' else None)
            if args.point=='after_receipt_commit':
                runner.broker.fill(entry,'.5','crash-fill')
                with store.transaction() as db:
                    fill_event=next(k for k,e in rows(db,'broker_events') if e['payload']['kind']=='ENTRY_FILL')
                runner.deliver(fill_event,fault=args.point)
            raise OfflineError('Requested fault did not fire')
        elif args.command=='review':
            print(markdown(runner.summary()));return 0
        else: result=runner.summary()
        if args.compact:
            summary=result.get('summary',result)
            result=dict(scope=summary['scope'],origin=result.get('origin','RECOVERY_OR_STATUS'),
                counts=summary['counts'],cash_usdt=summary['account']['cash'],fees_usdt=summary['fees_usdt'],
                remaining={pid:s['remaining_quantity'] for pid,s in summary['positions'].items()},
                protection={pid:{k:s[k] for k in ('protection_status','protection_covered_quantity','phase','faults')} for pid,s in summary['positions'].items()},
                pending_actions=len(summary['pending_actions']),pending_reconciliation=len(summary['pending_reconciliation']),
                reason_codes=result.get('result',{}).get('reason_codes',[]),
                normal_entry_complete=False,live_allowed=False)
        print(json.dumps(result,sort_keys=True,indent=2,allow_nan=False))
        return 0
    except (ValueError,ArithmeticError,sqlite3.Error,OSError) as error:
        print(json.dumps({'ok':False,'error_type':type(error).__name__,'error':str(error),
                          'live_allowed':False,'execution_scope':'synthetic_offline'},sort_keys=True))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
