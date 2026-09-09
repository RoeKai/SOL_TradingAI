"""Offline CLI/AST checks; no execution routes, host config or credential reads."""

import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.configuration import check
from test_config_bundle import ROOT, FILES, texts


def cli(*args):
    return subprocess.run([sys.executable,'-m','app.configuration.check',*args],
                          cwd=ROOT,text=True,capture_output=True,timeout=20)


@pytest.mark.parametrize('example,exit_code,parse,config,plan',[
    ('config-valid',0,'PASS','PASS','NOT_EVALUATED'),
    ('synonym-conflict',2,'FAIL','NOT_EVALUATED','NOT_EVALUATED'),
    ('allocation-mismatch',3,'PASS','PASS','FAIL'),
    ('runner-unmodeled',3,'PASS','PASS','UNSUPPORTED'),
])
def test_cli_examples_separate_config_plan_trust_and_execution(example,exit_code,parse,config,plan):
    run=cli('--example',example,'--at','1800000000','--json')
    assert run.returncode==exit_code,run.stdout+run.stderr
    r=json.loads(run.stdout)['validation']
    assert (r['config_parsing'],r['config_consistency'],r['plan_consistency'])==(parse,config,plan)
    assert r['execution']=='NOT_INTEGRATED' and r['execution_authority']=='none' and not r['live_allowed']
    assert r['runtime_trust']=='INCOMPLETE'


def test_cli_human_and_parameter_json_outputs_have_explicit_boundary():
    run=cli()
    assert run.returncode==0 and 'NOT_INTEGRATED' in run.stdout and 'live_allowed=false' in run.stdout
    run=cli('--json','--parameters')
    out=json.loads(run.stdout)
    assert out['bundle']['bundle_digest']==out['validation']['bundle_digest']
    assert len(out['bundle']['parameters'])>200


@pytest.mark.parametrize('args',[
    ('--main','.env'),('--main','../config.yaml'),('--main','/config.yaml'),
    ('--main','logs/config.yaml'),('--plan','trades/paper/plan.json'),
    ('--manifest','configuration.yaml','--live'),('--root','/'),
    ('--plan','examples/configuration/missing/plan.json'),
])
def test_no_arbitrary_file_credentials_parent_fallback_or_live_flag(args):
    r=cli(*args,'--json')
    assert r.returncode==2
    assert 'ENABLE_SMALL_CAPITAL_LIVE' not in r.stdout


def test_cli_root_is_fixed_no_upward_search_or_env_loading(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(check,'ROOT',tmp_path)
    opened=[]
    class ExplicitModule:
        def __init__(self,root):
            assert root==tmp_path
        def file(self,path):
            return tmp_path/path
    source=texts()
    def read(path):
        opened.append(Path(path).name)
        return source[next(k for k,f in FILES.items() if f==Path(path).name)+'_text']
    monkeypatch.setattr(check,'ModulePaths',ExplicitModule)
    monkeypatch.setattr(check,'read_text_nofollow',read)
    monkeypatch.setenv('BINANCE_API_KEY','synthetic-inherited-poison')
    monkeypatch.setenv('DRY_RUN','false')
    assert check.main(['--json'])==0
    out=capsys.readouterr().out
    assert opened==['config.yaml','admission.yaml','exit-policy.yaml','configuration.yaml']
    assert 'synthetic-inherited-poison' not in out and 'true' not in json.loads(out)['validation']['execution_authority']
    opened.clear()
    assert check.main(['--main','../config.yaml','--json'])==2 and opened==[]


@pytest.mark.parametrize('link_kind',['symlink','hardlink'])
def test_explicit_file_links_refused_without_reading_target(tmp_path,monkeypatch,link_kind):
    import os
    monkeypatch.setattr(check,'ROOT',tmp_path)
    policy={'application':'sol-ai-trading-system','policy_version':1,'live_runtime_allowed':False}
    (tmp_path/'isolation-policy.json').write_text(json.dumps(policy))
    secret=tmp_path/'not-a-config'
    secret.write_text('synthetic-value-must-not-be-read')
    target=tmp_path/'config.yaml'
    if link_kind=='symlink': target.symlink_to(secret)
    else: os.link(secret,target)
    with pytest.raises(ValueError): check.read_input('main','config.yaml')


def test_pure_modules_exact_dependencies_and_no_integration_anywhere_else():
    area=ROOT/'app/configuration'
    pure={'__init__.py','models.py','encoding.py','registry.py','compiler.py','inputs.py','contracts.py','examples.py'}
    allowed={'__future__','dataclasses','decimal','hashlib','json','re','typing','pydantic','yaml',
        'app.setups.models','app.setups.rr','app.setups.scorecard','app.setups.rr_models','app.setups.scorecard_models',
        'app.admission.models','app.admission.policy','app.admission.engine','app.exits.models','app.exits.policy',
        'app.exits.engine','models','encoding','registry','compiler','inputs'}
    for name in pure:
        tree=ast.parse((area/name).read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Import): assert all(a.name in allowed for a in node.names)
            elif isinstance(node,ast.ImportFrom): assert node.module in allowed
            elif isinstance(node,ast.Call) and isinstance(node.func,ast.Name):
                assert node.func.id not in {'open','exec','eval','__import__'}
    for path in [ROOT/'main.py',*(ROOT/'app').rglob('*.py')]:
        if path.parent==area: continue
        assert 'app.configuration' not in path.read_text()
    assert 'configuration' not in (ROOT/'config.yaml').read_text()
    assert '"live_runtime_allowed": false' in (ROOT/'isolation-policy.json').read_text()
    assert 'dry_run: true' in (ROOT/'config.yaml').read_text()
    # A module-import-only process must not import any trade loop/secret client.
    result=subprocess.run([sys.executable,'-c',
        'import sys; import app.configuration.check; print([m for m in sys.modules if m in '
        '("app.config","app.runtime","app.paper.broker","app.execution.engine","app.dashboard.server","app.alerts.telegram")])'],
        text=True,capture_output=True,cwd=ROOT,timeout=20)
    assert result.returncode==0 and result.stdout.strip()=='[]'
