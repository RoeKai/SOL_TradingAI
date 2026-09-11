"""Author-written synthetic regressions. No review counts or accounts reused."""
from decimal import Decimal as D, Context,localcontext
import ast
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import pytest
from test_stage08d_quantification import inputs
from app.execution_costs.gate import quantify,plan_snapshot
from app.execution_costs.prices import derive,cost_function
from app.execution_costs.plans import build_plan
from app.execution_costs.scenarios import evaluate_scenarios as old_scenarios
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from app.offline_paper.storage import digest,get,rows
from app.scenario_diagnostics.scenarios import describe_scenarios,describe_path
from app.scenario_diagnostics.proofs import verify_certificate,lattice_crosscheck,UnsupportedProof,coefficients
from app.scenario_diagnostics.quantities import quantity_grid
from app.scenario_diagnostics.source import frozen_source
from app.scenario_diagnostics.costs import cost_row,distribution,primary_class

ROOT=Path(__file__).resolve().parents[1]
NARROW=[108,109,108,110,111,108,100,99.98,99.99,99.99,99.99,99.99,99.99,100]
LOW_REWARD=[100.1,100.2,100.1,100.3,100.4,100.1,100,99.98,99.99,99.99,99.99,99.99,99.99,100]


def plan_case(tmp_path,side,values=None):
    p,r,args,now=inputs(tmp_path,side,values or NARROW)
    c,b,m,settings,a,v,req,pc,qp=args;q=D('5.329')
    with localcontext(Context(prec=50)):
        f=cost_function(c,pc,m,qp);s,_=derive(c,pc,f,q,m);rr=calculate_rr(s,quantity=float(q))
        card=score_trade_setup(s,rr,evaluated_at=now)
        plan=build_plan(c,s,b,m,pc,f,q)
        snap=plan_snapshot(s,rr,card,digest({'only':'description'}),'REJECT')
    return p,r,args,now,plan,s,snap,q


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_reproduce_old_tp2_expectation_and_new_legal_exit(tmp_path,side):
    p,r,args,now,plan,s,snap,q=plan_case(tmp_path,side)
    with pytest.raises(ValueError,match='SCENARIO_EXPECTED_ACTION_MISSING:TP2'):
        old_scenarios(plan,s,snap,q)
    original=plan.model_dump();out=describe_scenarios(plan,snap,q)
    for item in out[1:]:
        assert item.premise=='EARLY_PROTECTIVE_TERMINATION'
        assert item.reached==('ENTRY','TP1')
        assert item.scenario_net_rr is None and item.remaining_quantity==0
        assert 'PROPOSED_STOP_ALREADY_CROSSED' in item.reason_codes
        assert not any(leg.action=='TP2' for leg in item.legs)
        assert sum(leg.quantity for leg in item.legs if leg.action!='ENTRY')==q
        assert sum(leg.gross_cash_flow for leg in item.legs)==item.gross_pnl
        assert item.conditional_net_cash==item.gross_pnl-item.fees_usdt-item.funding_budget_usdt
    assert plan.model_dump()==original


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_tp2_early_protection_configurable_policy_not_fake_runner(tmp_path,side):
    # A legitimate alternative synthetic policy exercises TP2 after-cost protection.
    # The frozen production 30/40/30 file is never changed.
    values=NARROW.copy();values[7:13]=[99.72,99.8,99.85,99.9,99.95,99.99]
    *_,plan,s,snap,q=plan_case(tmp_path,side,values)
    policy=plan.policy.model_copy(update={'tp2_fraction':D('.6'),'runner_fraction':D('.1')})
    policy=type(policy).model_validate(policy.model_dump())
    triggers=tuple(t.model_copy(update={'original_fraction':policy.tp1_fraction if t.name=='TP1' else policy.tp2_fraction})
                   for t in plan.triggers)
    plan=plan.model_copy(update={'plan_id':'0'*64,'policy':policy,'policy_digest':digest(policy),
                                'runner_fraction':D('.1'),'triggers':triggers})
    plan=plan.model_copy(update={'plan_id':digest(plan)})
    out=describe_scenarios(plan,snap,q)
    assert out[3].premise=='EARLY_PROTECTIVE_TERMINATION'
    assert out[3].reached==('ENTRY','TP1','TP2')
    assert out[3].remaining_quantity==0 and out[3].scenario_net_rr is None


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_duplicate_confirmation_does_not_duplicate_cost_or_quantity(tmp_path,side):
    *_,plan,s,snap,q=plan_case(tmp_path,side)
    assert describe_scenarios(plan,snap,q)==describe_scenarios(plan,snap,q,duplicate_confirmations=True)


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_normal_runner_keeps_retrace_and_independent_version(tmp_path,side):
    *_,plan,s,snap,q=plan_case(tmp_path,side,[150,155,149,158,160,150,100,90,95,96,97,98,99,100])
    original=old_scenarios(plan,s,snap,q);current=describe_scenarios(plan,snap,q)
    for old,new in zip(original.scenarios,current):
        assert new.premise=='FULFILLED'
        assert old.net_pnl==new.conditional_net_cash and old.gross_pnl==new.gross_pnl
    assert 'RUNNER_REFERENCE' in current[3].reached
    assert current[3].legs[-1].fill_price!=plan.runner_reference_price
    assert original.version=='quantity-consistent-scenarios/v1'
    absent=plan.model_copy(update={'runner_reference_price':None})
    assert describe_path(absent,snap,q,plan.reference_entry,'S3').premise=='UNAVAILABLE'


def rejection(tmp_path,side):
    p,r,args,now=inputs(tmp_path,side,LOW_REWARD)
    body=quantify(*args,now=now)
    assert body['search_status']=='PROVEN_STATIC_RR_DOMAIN_REJECT',body['reason_codes']
    return p,r,args,body


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_independent_proof_full_selected_grid_and_no_side_effects(tmp_path,side):
    p,r,args,body=rejection(tmp_path,side)
    before=copy.deepcopy(body)
    with p.store.transaction() as db:state=[rows(db,t) for t in ('account','broker_orders','broker_fills','reservations','outbox')]
    certificate=verify_certificate(body,args[1]);assert certificate['status']=='VALID'
    check=lattice_crosscheck(body,certificate)
    assert not check['differences'] and check['exhaustive']
    assert body==before
    with p.store.transaction() as db:assert state==[rows(db,t) for t in ('account','broker_orders','broker_fills','reservations','outbox')]


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('field',['maximum_quantity','global_floor','rr_at_max','fixed_usdt','quantity_linear_cost'])
def test_tampered_proof_is_not_self_validating(tmp_path,side,field):
    _,_,args,body=rejection(tmp_path,side)
    bad=copy.deepcopy(body);bad['proof'][field]=str(D(bad['proof'][field])+D('.1'))
    assert verify_certificate(bad,args[1])['status']=='INCONSISTENT'


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('change',['version','target','funding','fraction','entry','missing'])
def test_changed_or_unsupported_inputs_never_prove_rejection(tmp_path,side,change):
    _,_,args,body=rejection(tmp_path,side);bad=copy.deepcopy(body)
    if change=='version':bad['rr']['calculation_version']='future/v9'
    elif change=='target':bad['derived_setup']['targets'][0]['price']+=1
    elif change=='funding':bad['derived_setup']['cost_assumptions']['funding_cost_usdt']+=1
    elif change=='fraction':bad['derived_setup']['targets'][0]['fraction']=.3
    elif change=='entry':bad['derived_setup']['entry']['lower_price']-=1
    else:bad['derived_setup']['cost_assumptions']['exit_fee_rate']=None
    with pytest.raises((UnsupportedProof,ValueError)):verify_certificate(bad,args[1])


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_cost_attribution_exclusive_and_quantities_consistent(tmp_path,side):
    _,_,args,body=rejection(tmp_path,side)
    row=cost_row(body,args[1],args[2]);m=row['metrics'];q=m['quantity']
    assert row['primary']=='A'
    with localcontext(Context(prec=50)):
        assert m['net_target_usdt']==q*m['weighted_target_reward_distance']-sum(m[k] for k in
            ('total_fee_budget','total_spread_budget','total_slippage_budget','total_rounding_budget','funding_budget'))
    stats=distribution([row])[side]
    assert sum(stats['primary'].values())==1
    assert row['execution_authority']=='NONE'


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_one_quantity_failure_is_not_whole_domain_proof(tmp_path,side):
    _,r,args,now,*_=plan_case(tmp_path,side)
    body=quantify(*args,now=now)
    grid=quantity_grid(body,args[1],args[2],limit=1)
    assert grid['checked']==1 and not grid['complete'] and grid['unchecked_count']>0
    assert grid['formal_search_budget']==32 and grid['execution_authority']=='NONE'
    assert 'proof' not in grid and grid['rows'][0]['s3_classification']=='EARLY_PROTECTIVE_TERMINATION'


def test_source_readonly_and_unknown_database_refused(tmp_path):
    p,r,args,body=rejection(tmp_path,'LONG')
    with frozen_source(p.store.path) as (db,*_):
        with pytest.raises(sqlite3.DatabaseError):db.execute("DELETE FROM account")
        with pytest.raises(sqlite3.DatabaseError):db.execute("UPDATE history_approvals SET payload='{}'")
    with pytest.raises(ValueError,match='EXPLICIT_8D'): 
        with frozen_source(tmp_path/'other.sqlite3'):pass


def test_cli_readonly_integration_and_no_old_entry_wiring(tmp_path):
    p,r,args,body=rejection(tmp_path,'LONG')
    # Synthetic candidates above were deliberately not submitted. Persisting a
    # synthetic audit envelope is test setup only, never done by the CLI.
    from app.execution_costs.storage import hput
    with p.store.transaction() as db:
        hput(db,'history_approvals','audit-only',dict(body=body,body_digest=digest(body)))
        before=rows(db,'account')
    out=tmp_path/'audit-output'
    proc=subprocess.run([sys.executable,'-m','app.scenario_diagnostics.cli','--source',str(p.store.path),
        '--output',str(out),'--sample-points','8'],cwd=ROOT,capture_output=True,text=True,timeout=40)
    assert proc.returncode==0,(proc.stdout,proc.stderr)
    result=json.loads((out/'summary.json').read_text())
    assert result['statistics']['valid']>=1 and not result['differences']
    with p.store.transaction() as db:assert before==rows(db,'account')
    for path in (ROOT/'app').rglob('*.py'):
        if path==ROOT/'app/signal_research/cli.py':
            # Stage 8F-A may reuse only the narrow read-only source boundary;
            # this does not permit wiring diagnostics into any old entry path.
            tree=ast.parse(path.read_text())
            imports=[node for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)
                     and (node.module or '').startswith('app.scenario_diagnostics')]
            assert [(node.module,[(alias.name,alias.asname) for alias in node.names]) for node in imports]==[
                ('app.scenario_diagnostics.source',[('frozen_source',None),('bodies',None)])]
            assert not any(isinstance(node,ast.Import) and any(
                alias.name.startswith('app.scenario_diagnostics') for alias in node.names)
                for node in ast.walk(tree))
            continue
        if 'scenario_diagnostics' not in path.parts:
            assert 'app.scenario_diagnostics' not in path.read_text()
    for file in (ROOT/'app/scenario_diagnostics').glob('*.py'):
        tree=ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                assert node.module not in {'app.execution_costs.gate','app.admission.engine','app.offline_paper.engine',
                    'app.execution_costs.engine','app.offline_paper.broker','app.execution.bridge_client'}
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name):
                assert node.func.id not in {'quantify','admit_trade','require_paper_admission','fixture_entry'}


@pytest.mark.parametrize('exfund,net,rr,incomplete,expected',[
    ('0','-.2','-.5',False,'A'),('-.1','-.3','-.6',False,'A'),
    ('.1','-.1','-.2',False,'B'),('.1','0','0',False,'B'),
    ('.5','.3','1.99',False,'C'),('.5','.3','2',False,'D'),
    ('.5','.3','3',False,'D'),('-.1','-.3','-.6',True,'E')])
def test_all_primary_classes_are_exclusive(exfund,net,rr,incomplete,expected):
    assert primary_class(D(exfund),D(net),D(rr),D(2),incomplete=incomplete)==expected


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('fixed',['0','.1','1'])
def test_independent_linear_equation_against_original_rr_with_fixed_cost(tmp_path,side,fixed):
    _,_,args,body=rejection(tmp_path,side)
    raw=copy.deepcopy(body['derived_setup']);function=copy.deepcopy(body['cost_function'])
    function['fixed_usdt']=fixed
    with localcontext(Context(prec=50)):
        c=coefficients(raw,function)
        for q in (D('.05'),D('.101'),D('1.777'),D('5.329')):
            raw['cost_assumptions']['funding_cost_usdt']=float(D(fixed)+q*c['funding_per_unit'])
            from app.setups.models import TradeSetup
            rr=calculate_rr(TradeSetup.model_validate(raw),quantity=float(q)).reference.net_rr
            expected=(c['g']*q-c['f0'])/(c['l']*q+c['f0'])
            assert abs(rr-expected)<D('1e-44')
            for k in (D('0'),D('1.5'),D('2')):
                assert (rr>=k)==((c['g']-k*c['l'])*q>=(1+k)*c['f0'])


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_grade_changes_and_missing_costs_do_not_widen_certified_domain(tmp_path,side):
    _,_,args,body=rejection(tmp_path,side)
    cert=verify_certificate(body,args[1]);d=cert['domain']
    assert len(d['branches'])==4
    assert all(x['q_max']<=d['q_max'] for x in d['branches'])
    assert all(k>=d['k'] for k in d['tier_floors'])
    bad=copy.deepcopy(body);bad['domain']['high']=str(D(bad['domain']['high'])-D('.001'))
    assert verify_certificate(bad,args[1])['status']=='INCONSISTENT'
    raw=copy.deepcopy(body['derived_setup']);raw['cost_assumptions']['entry_fee_rate']=None
    with pytest.raises(UnsupportedProof,match='MISSING'):coefficients(raw,body['cost_function'])


def test_real_programming_error_is_not_search_exhaustion(tmp_path,monkeypatch):
    _,_,args,now,*_=plan_case(tmp_path,'SHORT')
    body=quantify(*args,now=now)
    from app.scenario_diagnostics import quantities
    def broken(*a,**k):raise ValueError('REAL_BUG_NOT_CAPABILITY')
    monkeypatch.setattr(quantities,'describe_scenarios',broken)
    with pytest.raises(ValueError,match='REAL_BUG_NOT_CAPABILITY'):
        quantities.quantity_grid(body,args[1],args[2],limit=1)
