"""Real new CLI on explicit synthetic archive format data, no network."""
import json
import subprocess
import sys
from test_stage08c_active_cli import cli_instance
from test_stage08c_historical import ROOT


def command(root,cmd,*args):
    p=subprocess.run([sys.executable,'-m','app.execution_costs.cli',cmd,'--workspace',str(root),
        '--dataset',str(root/'format-test-data'),'--run-id','test-8d',*args],cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert p.returncode==0,(p.stdout,p.stderr)
    remaining=p.stdout.lstrip();out=None
    while remaining:
        out,n=json.JSONDecoder().raw_decode(remaining);remaining=remaining[n:].lstrip()
    return out


def test_new_cli_keeps_old_instance_and_recovers_independent_ledger(tmp_path):
    old=cli_instance(tmp_path)  # only reuse its tiny synthetic file FORMAT
    manifest=str(tmp_path/'format-test-8d.json')
    frozen=command(tmp_path,'freeze','--manifest',manifest,'--kind','engineering','--code-commit','b'*40)
    assert frozen['version']=='quantified-historical-run/v1'
    command(tmp_path,'init','--manifest',manifest)
    out=command(tmp_path,'run','--max-events','6')
    assert out['segment']['new_events']==6 and out['result']['metrics']['cash_usdt']=='500'
    for _ in range(2):
        recovered=command(tmp_path,'recover')['result']
        assert recovered['counts']==out['result']['counts']
        assert recovered['market_prefix_digest']==out['result']['market_prefix_digest']
    assert old.summary()['counts']['orders']==0
