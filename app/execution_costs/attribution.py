"""Read-only A/B/C/D attribution of identical past candidates; never orders.

Uses a frozen 500-USDT zero-position counterfactual, not the mutable D account.
It cannot issue an instance approval or consume an execution intent.
"""
from decimal import Decimal as D, ROUND_CEILING
from datetime import datetime,timezone
import json
import sqlite3
from app.admission.engine import admit_trade
from app.admission.models import PaperRiskSnapshot,AdmissionRequest,fingerprint
from app.configuration.compiler import policy_from
from app.configuration.contracts import validate_contract,declare_plan_binding
from app.configuration.inputs import PlanInputs
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from app.admitted_paper.provider import number
from app.offline_paper.storage import digest
from app.offline_paper.broker import synthetic_rules
from app.historical_replay.storage import HistoricalStore,hget as old_get
from app.historical_replay.configuration import INSTANCE as OLD_INSTANCE
from app.historical_replay.data import verify_seal,seal,HistoricalError
from app.historical_replay.provider import describe
from app.historical_replay.replay import quote_from
from .evidence import VERIFIER
from .funding import exchange_snapshot,RISK_SOURCE
from .prices import prices
from .models import QuantificationPolicy
from .gate import quantify


def readonly_account(settings,now):
    day=int(now//86400)*86400
    return PaperRiskSnapshot(instance_id=settings.instance_id,snapshot_revision=0,mode='paper',status='confirmed',source=RISK_SOURCE,
        observed_at=now,day_started_at=day,day_ends_at=day+86400,equity_usdt=settings.initial_balance,
        available_margin_usdt=settings.initial_balance,margin_used_usdt='0',day_realized_loss_usdt='0',unrealized_loss_usdt='0',
        reserved_risk_usdt='0',trades_today=0,consecutive_losses=0,positions=(),pending_entries=(),paused=False,reconciliation_clear=True,
        margin_mode='ISOLATED',configured_leverage=settings.leverage,auto_add_margin_enabled=False,martingale_enabled=False,limits=settings.limits)


def request_for(candidate,settings):
    s=candidate.setup
    return AdmissionRequest(request_id='read-only:'+candidate.candidate_id,risk_budget_usdt=settings.limits.max_loss_per_trade_usdt,
        action='OPEN',leverage=settings.leverage,margin_mode='ISOLATED',auto_add_margin=False,loss_recovery_sizing=False,sizing_basis='quality_risk_budget',
        confirmations=tuple(dict(evidence_id=e.evidence_id,evidence_digest=fingerprint(e),verified=True,verifier=VERIFIER,checked_at=s.created_at) for e in s.structure_evidence),
        invalidation_review=dict(setup_digest=fingerprint(s),all_conditions_clear=True,verifier=VERIFIER,checked_at=s.created_at))


def compact(result):
    return {k:result.get(k) for k in ('result','reason_codes','quantity','evaluated_quantity','search_status','proof','domain','tier',
        'required_net_rr','risk','risk_ceiling','evaluated_quantities')}


def compare(candidate,bundle,model,settings,qp):
    now=candidate.setup.created_at;account=readonly_account(settings,now);v=exchange_snapshot(now)
    req=request_for(candidate,settings);s=candidate.setup
    raw=candidate.evidence['window'][-1]['last']['SOLUSDT'];pc=prices(candidate,model,quote_from(raw,model),v.price_tick,now=now)
    probe=(max(v.min_quantity,v.min_notional_usdt/D(str(s.entry.reference_price)))/v.quantity_step).to_integral_value(rounding=ROUND_CEILING)*v.quantity_step
    rr=calculate_rr(s,quantity=number(probe));card=score_trade_setup(s,rr,evaluated_at=now,max_data_age_seconds=float(bundle.manifest.score_context_max_age_seconds))
    decision=admit_trade(s,rr,card,account=account,exchange=v,request=req,policy=policy_from(bundle,'admission'),evaluated_at=now)
    inputs=PlanInputs(setup=s,rr=rr,scorecard=card,account=account,exchange=v,request=req,exit_rules=synthetic_rules())
    binding=declare_plan_binding(bundle,inputs,declared_at=now);inputs=inputs.model_copy(update={'admission':decision,'configuration_binding':binding})
    legacy=validate_contract(bundle,inputs,evaluated_at=now)
    results={'A':dict(result=decision.result,reason_codes=list(decision.reason_codes),quantity=str(decision.max_quantity),
        diagnostic_quantity=str(probe),diagnostic_net_rr=str(rr.reference.net_rr),search_status='LEGACY_DIAGNOSTIC_PATH',
        legacy_validation=legacy.model_dump(mode='json'))}
    for name,old_prices,old_fixed in (('B',False,True),('C',True,False),('D',False,False)):
        res=quantify(candidate,bundle,model,settings,account,v,req,pc,qp,now=now,old_prices=old_prices,old_fixed=old_fixed)
        results[name]=compact(res)
        if res.get('lineage'):
            results[name]['evaluated_funding_usdt']=res['lineage']['materialization']['funding_budget_usdt']
        if res.get('scenarios'):
            results[name]['s3_net_rr']=res['scenarios']['scenarios'][3]['scenario_net_rr']
    return dict(candidate_id=candidate.candidate_id,original_setup_digest=fingerprint(s),side=s.side,at=now,
        snapshot_digest=fingerprint(account),scope='READ_ONLY_SAME_SNAPSHOT_COUNTERFACTUAL_NOT_EXECUTION',
        independent_samples=1,groups=results)


def run_attribution(workspace,source_run,bundle,settings,run,model,*,limit=None):
    """Read ONLY the explicitly named validated 8C history DB, never an account API.

    No Store.connect (which is read/write), no initialization, no WAL/journal
    cleanup, no source SQL updates. Output is a compact report for the caller.
    """
    path=HistoricalStore(workspace,source_run,OLD_INSTANCE).path
    if not path.is_file(): raise HistoricalError('EXPLICIT_FROZEN_8C_SOURCE_REQUIRED')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        if (db.execute('PRAGMA application_id').fetchone()[0],db.execute('PRAGMA user_version').fetchone()[0])!=(1397705786,3):
            raise HistoricalError('ATTRIBUTION_REQUIRES_FROZEN_8C_IDENTITY')
        source=verify_seal(old_get(db,'history_meta','run'));dataset=verify_seal(old_get(db,'history_meta','dataset'))
        if dataset['content_digest']!=run['dataset_digest']: raise HistoricalError('ATTRIBUTION_DATASET_MISMATCH')
        qp=QuantificationPolicy.model_validate(run['quantification'])
        totals={k:dict(results={},reasons={},search={},samples=[]) for k in 'ABCD'};pairs={};n=0;prefix='0'*64
        begin,end=run['evaluation_range_ms']
        for row in db.execute('SELECT id,payload FROM history_candidates ORDER BY rowid'):
            raw=json.loads(row[1]);s=raw['setup'];ev=raw['evidence'];at=s['created_at']
            if not begin<=at*1000<end:continue
            c=describe(bundle,model,run,s['side'],ev['window'],ev['level'],now=at)
            compared=compare(c,bundle,model,settings,qp);n+=1;prefix=digest({'prefix':prefix,'source_id':row[0],'comparison':compared})
            transition='/'.join(compared['groups'][key]['result'] for key in 'ABCD');pairs[transition]=pairs.get(transition,0)+1
            for key,value in compared['groups'].items():
                t=totals[key];t['results'][value['result']]=t['results'].get(value['result'],0)+1
                for code in set(value['reason_codes']):t['reasons'][code]=t['reasons'].get(code,0)+1
                status=value['search_status'];t['search'][status]=t['search'].get(status,0)+1
                if len(t['samples'])<3:t['samples'].append(dict(candidate_id=c.candidate_id,side=c.setup.side,**value))
            if n%1000==0:print(json.dumps({'attribution_candidates':n,'at_ms':int(at*1000)}),flush=True)
            if limit is not None and n>=limit:break
        return seal(dict(version='same-candidate-attribution/v1',source_run_digest=source['content_digest'],source_code=source['code_commit'],
            new_run_digest=run['content_digest'],data_digest=dataset['content_digest'],candidates=n,comparison_digest=prefix,groups=totals,
            transitions=pairs,independent_samples=n,counts_must_not_be_summed_across_groups=True,source_read_only=True,
            snapshot='Explicit empty 500-USDT read-only counterfactual, SAME for each group; not D continuous state',
            configured_range_ms=run['evaluation_range_ms'],limit=limit,live_allowed=False,orders_created=0))
    finally:db.close()
