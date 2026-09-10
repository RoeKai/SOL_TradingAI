"""8B transaction composition over the accepted 8A store/Broker/ledger/recovery."""
from decimal import Decimal as D
import os
from app.offline_paper.engine import OfflinePaper
from app.offline_paper.broker import Broker, message
from app.offline_paper.storage import get, put, rows, digest
from app.offline_paper.models import OfflineError, amount
from app.configuration.inputs import PlanInputs
from app.configuration.models import ConfigBundle
from app.configuration.compiler import policy_from, verify_bundle
from app.admission.contract import require_paper_admission
from app.exits.bindings import capture_plan
from app.exits.models import EntryFill
from .models import Candidate, ExitPlan, ScenarioEvaluation
from .storage import AdmittedStore
from .provider import ingest, build_candidate, registry
from .plans import validate_exit_plan
from .scenarios import evaluate_scenarios
from .gate import evaluate, scenario_admission, VERSION

ORIGIN='NORMAL_ADMITTED_8B'


def reservation_binding(r):
    return {k:r[k] for k in ('origin','side','quantity','risk','margin','fee_reserve','reference_price','expires_at',
        'bundle_digest','account_revision','plan','exit_policy','rules','entry_costs','approval_id',
        'exit_plan_id','approved_quantity','entry_action_id')}


def grant(store,db,approval_id,*,historical=False):
    """Database-owned grant verification; a standalone JSON/hash is not a grant."""
    record=get(db,'paper_approvals',approval_id)
    if record is None: raise OfflineError('NO_INSTANCE_ISSUED_APPROVAL')
    body=record['body']
    if record['body_digest']!=digest(body) or approval_id!=digest({'issuer':VERSION,'body':body}):
        raise OfflineError('APPROVAL_CONTENT_CHANGED')
    if body['result']=='REJECT' or body['version']!=VERSION or body['scope']!='SYNTHETIC_OFFLINE' or body['instance_id']!=store.instance_id:
        raise OfflineError('APPROVAL_SCOPE_OR_RESULT_INVALID')
    candidate=Candidate.model_validate(body['candidate']);inputs=PlanInputs.model_validate(body['input_binding'])
    if get(db,'paper_candidates',candidate.candidate_id)!=candidate.model_dump(mode='json'):
        raise OfflineError('APPROVAL_CANDIDATE_REGISTRY_MISMATCH')
    bundle=ConfigBundle.model_validate(get(db,'configurations',body['bundle_digest']))
    if verify_bundle(bundle).consistency!='PASS': raise OfflineError('APPROVAL_FROZEN_BUNDLE_INVALID')
    rebuilt=build_candidate(store,db,candidate.setup.side,candidate.evidence.evaluated_at,
        input_count=candidate.evidence.input_count,bound_bundle=bundle,check_registry=not historical)
    if candidate!=rebuilt: raise OfflineError('APPROVAL_EVIDENCE_REPLAY_MISMATCH')
    if inputs.setup!=candidate.setup or body['bundle_digest']!=candidate.bundle_digest:
        raise OfflineError('APPROVAL_SETUP_BINDING_CHANGED')
    plan=ExitPlan.model_validate(body['exit_plan']);validate_exit_plan(plan,candidate,bundle)
    evaluation=ScenarioEvaluation.model_validate(body['scenarios'])
    snapshot=capture_plan(inputs.setup,inputs.rr,inputs.scorecard,inputs.admission)
    if evaluation!=evaluate_scenarios(plan,inputs.setup,snapshot,inputs.admission.max_quantity):
        raise OfflineError('APPROVAL_SCENARIO_BINDING_CHANGED')
    reasons,risk=scenario_admission(plan,evaluation,inputs.admission,inputs.account,store.settings(db))
    if reasons or D(body['risk'])!=risk or D(body['quantity'])!=inputs.admission.max_quantity:
        raise OfflineError('APPROVAL_SCENARIO_ADMISSION_INVALID')
    require_paper_admission(inputs.admission,setup=inputs.setup,rr=inputs.rr,scorecard=inputs.scorecard,
        account=inputs.account,exchange=inputs.exchange,request=inputs.request,policy=policy_from(bundle,'admission'),
        evaluated_at=body['issued_at'],quantity=D(body['quantity']))
    return body,snapshot


class AdmittedBroker(Broker):
    def _authorize_entry(self,db,item):
        if item['origin']=='FIXTURE_EXISTING_POSITION': return super()._authorize_entry(db,item)
        if item['origin']!=ORIGIN or not isinstance(self.store,AdmittedStore): raise OfflineError('UNKNOWN_ENTRY_AUTHORITY')
        action=item['action'];r=get(db,'reservations',action['position_id'])
        if r is None or not r.get('approval_id'): raise OfflineError('NORMAL_ENTRY_REQUIRES_APPROVAL_RESERVATION')
        body,snapshot=grant(self.store,db,r['approval_id'])
        consumed=get(db,'paper_consumptions',r['approval_id'])
        if (consumed is None or consumed['action_id']!=action['action_id'] or consumed['position_id']!=action['position_id'] or
            consumed['approval_digest']!=digest(body) or consumed['reservation_digest']!=digest(reservation_binding(r)) or
            consumed['intent_digest']!=digest(action) or consumed['state']!='CONSUMED' or
            r['entry_action_id']!=action['action_id'] or r['plan']!=snapshot.model_dump(mode='json') or
            action['quantity']!=body['quantity'] or action['side']!=('BUY' if body['candidate']['setup']['side']=='LONG' else 'SELL')):
            raise OfflineError('NORMAL_ENTRY_CONSUMPTION_BINDING_INVALID')
        a=get(db,'account','account')
        if a['version']!=consumed['post_reservation_revision']: return 'POST_RESERVATION_ACCOUNT_CHANGED'
        if a['quote']!=body['quote']: return 'POST_RESERVATION_QUOTE_CHANGED'
        if a['now']>=body['expires_at']: return 'APPROVAL_EXPIRED_BEFORE_BROKER_ACCEPTANCE'
        if get(db,'identity','identity')['bundle_digest']!=body['bundle_digest']: return 'POST_RESERVATION_CONFIG_CHANGED'
        return None

    def fault_receipt(self,key,action_id,status,cumulative=None,*,protection_lost=False):
        """Explicit synthetic transport fault; cannot manufacture fill facts/orders."""
        if status not in ('UNKNOWN','ACCEPTED','CANCELED','FILLED','REJECTED'):
            raise OfflineError('Unsupported fault status')
        with self.store.transaction() as db:
            order=get(db,'broker_orders',action_id)
            if order is None: raise OfflineError('Fault needs an actual synthetic order')
            kind=order['action']['kind'];pid=order['action']['position_id']
            body=dict(kind='ENTRY_STATUS' if kind=='ENTRY' else 'PROTECTION_LOST' if protection_lost else 'RECEIPT',
                action_id=action_id,position_id=pid,status=status,cumulative_filled_quantity=cumulative)
            if body['kind']=='RECEIPT':
                if cumulative is None: raise OfflineError('ActionReceipt needs explicit cumulative; ProtectionLost preserves None')
                body['reduce_only_verified']=True
            message(db,'8b-transport-fault:'+key,body)


class AdmittedPaper(OfflinePaper):
    def __init__(self,store):
        if type(store) is not AdmittedStore: raise OfflineError('8B requires explicitly initialized schema-2 instance')
        super().__init__(store);self.broker=AdmittedBroker(store)

    def ingest(self,observation):
        with self.store.transaction() as db: return ingest(self.store,db,observation)

    def candidate(self,side):
        with self.store.transaction() as db:
            value=build_candidate(self.store,db,side,get(db,'account','account')['now'])
            old=get(db,'paper_candidates',value.candidate_id)
            if old is not None and old!=value.model_dump(mode='json'): raise OfflineError('CANDIDATE_ID_COLLISION')
            put(db,'paper_candidates',value.candidate_id,value)
            return value

    def prepare(self,candidate_id,request_id,*,risk_budget='5'):
        if not self.ready or self.store.failed: raise OfflineError('STARTUP_RECOVERY_REQUIRED')
        risk_budget=amount(risk_budget)
        content=dict(candidate_id=candidate_id,request_id=request_id,risk_budget=str(risk_budget))
        with self.store.transaction() as db:
            self._assert_cash(db)
            old=get(db,'requests','proposal:'+request_id)
            if old is not None:
                if old['request_digest']!=digest(content): raise OfflineError('REQUEST_ID_CONTENT_CONFLICT')
                return get(db,'paper_approvals',old['approval_id'])
            try: body=evaluate(self.store,db,candidate_id,request_id,risk_budget)
            except (ValueError,ArithmeticError) as error:
                body=dict(version=VERSION,scope='SYNTHETIC_OFFLINE',instance_id=self.store.instance_id,result='REJECT',
                    reason_codes=[str(error)],candidate_id=candidate_id,request_id=request_id,requested_risk=str(risk_budget),live_allowed=False)
            key=digest({'issuer':VERSION,'body':body})
            record=dict(approval_id=key,body=body,body_digest=digest(body))
            put(db,'paper_approvals',key,record)
            put(db,'requests','proposal:'+request_id,dict(request_digest=digest(content),approval_id=key))
            return record

    def submit(self,approval_id,*,fault=None):
        if type(approval_id) is not str: raise OfflineError('Only persisted approval ID; never Signal/TradeSetup/JSON')
        if not self.ready or self.store.failed: raise OfflineError('STARTUP_RECOVERY_REQUIRED')
        with self.store.transaction() as db:
            self._assert_cash(db)
            record=get(db,'paper_approvals',approval_id)
            if record is None: raise OfflineError('NO_INSTANCE_ISSUED_APPROVAL')
            body=record['body'];request_id=body['request_id']
            old=get(db,'requests','admitted:'+request_id)
            if old is not None:
                if old['approval_id']!=approval_id: raise OfflineError('REQUEST_ID_CONTENT_CONFLICT')
                return old
            reasons=[]
            if body['result']=='REJECT': reasons=list(body['reason_codes'])
            else:
                try:
                    body,plan_snapshot=grant(self.store,db,approval_id)
                    a=get(db,'account','account')
                    if a['now']>=body['expires_at']: reasons.append('APPROVAL_EXPIRED')
                    if a['version']!=body['account_revision']: reasons.append('ACCOUNT_REVISION_CHANGED_REISSUE')
                    if get(db,'identity','identity')['bundle_digest']!=body['bundle_digest']: reasons.append('CONFIG_CHANGED_REISSUE')
                    if not reasons:
                        refreshed=evaluate(self.store,db,body['candidate_id'],request_id,D(body['requested_risk']))
                        if refreshed['result']=='REJECT': reasons.extend(refreshed['reason_codes'])
                        elif refreshed!=body: reasons.append('APPROVAL_CONTEXT_CHANGED_REISSUE')
                except (ValueError,ArithmeticError) as error: reasons.append(str(error))
            if reasons:
                result=dict(result='REJECT',reason_codes=reasons,approval_id=approval_id,origin=ORIGIN,entry_action_id=None)
                put(db,'requests','admitted:'+request_id,result)
                return result
            # No Broker writes until this intent, grant consumption, reservation
            # and slot are committed together inside BEGIN IMMEDIATE.
            a=get(db,'account','account');plan=ExitPlan.model_validate(body['exit_plan'])
            pid='admitted-position:'+digest({'request':request_id,'instance':self.store.instance_id})
            action_id=digest({'normal-entry':approval_id,'position':pid})
            r=dict(origin=ORIGIN,side=plan.side,quantity=body['quantity'],risk=body['risk'],margin=body['margin'],
                fee_reserve=body['fee_reserve'],reference_price=str(plan.reference_entry),expires_at=body['expires_at'],
                bundle_digest=body['bundle_digest'],account_revision=a['version'],released=False,entry_sealed=False,
                entry_high_water='0',entry_terminal=None,entry_terminal_quantity=None,entry_status_unknown=False,
                entry_faults=[],entry_reconciliation_required=False,entry_pending_reasons=[],
                plan=plan_snapshot.model_dump(mode='json'),exit_policy=plan.policy.model_dump(mode='json'),
                rules=plan.rules.model_dump(mode='json'),entry_costs=dict(fee_rate=str(dict(plan.costs)['entry_fee_rate']),
                    slippage_bps=str(dict(plan.costs)['entry_slippage_bps'])),
                approval_id=approval_id,exit_plan_id=plan.plan_id,approved_quantity=body['quantity'],
                entry_action_id=action_id)
            item=dict(origin=ORIGIN,status='PENDING',attempts=0,action=dict(action_id=action_id,kind='ENTRY',position_id=pid,
                side='BUY' if plan.side=='LONG' else 'SELL',quantity=body['quantity']))
            result=dict(result=body['result'],reason_codes=body['reason_codes'],approval_id=approval_id,
                origin=ORIGIN,entry_action_id=action_id,position_id=pid)
            put(db,'reservations',pid,r);put(db,'outbox',action_id,item)
            put(db,'requests','admitted:'+request_id,result)
            self._cash(db)
            # Store immutable intent fields only (dispatch status/attempts change).
            put(db,'paper_consumptions',approval_id,dict(state='CONSUMED',action_id=action_id,position_id=pid,
                approval_digest=digest(body),reservation_digest=digest(reservation_binding(r)),intent_digest=digest(item['action']),
                post_reservation_revision=get(db,'account','account')['version']))
            if fault=='before_intent_commit': os._exit(91)
        self._crash('after_intent_commit',fault)
        return result

    def _crash(self,point,fault):
        # Explicit opt-in test hook within a synthetic-only schema. No runtime
        # env toggle, fixture promotion, network permission or ordinary trading.
        if point==fault: os._exit(91)

    def cancel_opening(self,entry_id,reason='SYNTHETIC_OPERATOR_CANCEL'):
        with self.store.transaction() as db:
            item=get(db,'outbox',entry_id)
            if item is None or item['action']['kind']!='ENTRY': raise OfflineError('Unknown opening leg')
            key=digest({'cancel-original-entry':entry_id,'reason':reason})
            if get(db,'outbox',key) is None:
                put(db,'outbox',key,dict(origin=item['origin'],status='PENDING',attempts=0,
                    action=dict(action_id=key,kind='CANCEL',position_id=item['action']['position_id'],target_action_id=entry_id)))
            return key

    def _event(self,db,event):
        super()._event(db,event)  # all cost/PnL/high-water/exit logic remains here
        if isinstance(event,EntryFill):
            r=get(db,'reservations',event.position_id)
            if r['origin']!=ORIGIN: return
            body=get(db,'paper_approvals',r['approval_id'])['body'];plan=ExitPlan.model_validate(body['exit_plan'])
            from app.offline_paper.pricing import execution_price
            side='BUY' if plan.side=='LONG' else 'SELL'
            bounds=sorted(execution_price(p,side,dict(plan.costs)['entry_slippage_bps'],plan.rules.price_tick)
                for p in (plan.entry_lower,plan.entry_upper))
            if not bounds[0]<=event.price<=bounds[-1]:
                r['fill_deviation_reason']='ACTUAL_FILL_OUTSIDE_APPROVED_GEOMETRY'
                r['scenario_applicability']='REFERENCE_ONLY_ACTUAL_FILL_DEVIATED'
                put(db,'reservations',event.position_id,r)
                a=get(db,'account','account');a['paused']=True;put(db,'account','account',a)
                key=digest({'deviation-cancel-original':r['entry_action_id']})
                if get(db,'outbox',key) is None:
                    put(db,'outbox',key,dict(origin=ORIGIN,status='PENDING',attempts=0,
                        action=dict(action_id=key,kind='CANCEL',position_id=event.position_id,target_action_id=r['entry_action_id'])))

    def _save(self,db,position,cp):
        super()._save(db,position,cp)
        r=get(db,'reservations',cp.state.position_id)
        if r['origin']==ORIGIN:
            actual=cp.state.original_quantity;approved=D(r['approved_quantity'])
            r['confirmed_opening_quantity']=str(actual)
            r['unfilled_approved_quantity']=str(max(D(0),approved-actual))
            # Historical approved scenarios are never presented as a new size's
            # RR. Existing confirmed inventory still receives protection even
            # when the former full-size planning scenario no longer applies.
            r['scenario_applicability']=('REFERENCE_ONLY_ACTUAL_FILL_DEVIATED' if r.get('fill_deviation_reason') else
                'REFERENCE_ONLY_UNSEALED_ENTRY' if not cp.state.entry_sealed else
                'REFERENCE_ONLY_DIFFERENT_CONFIRMED_QUANTITY' if actual!=approved else
                'CONFIRMED_SIZE_MATCHES_CONDITIONAL_MODEL_NOT_EXPECTATION')
            put(db,'reservations',cp.state.position_id,r)

    def recover(self):
        try:
            with self.store.transaction() as db:
                try: registry(self.store,db)
                except OfflineError as error:
                    # A newly unavailable supplier stops new entries only.
                    # Original approvals replay their frozen input prefix and
                    # policy; unbound future inputs cannot replace that history.
                    account=get(db,'account','account');account['paused']=True
                    account['new_entry_block_reason']=str(error);put(db,'account','account',account)
                for pid,r in rows(db,'reservations'):
                    if r['origin']!=ORIGIN: continue
                    body,plan=grant(self.store,db,r['approval_id'],historical=True)
                    c=get(db,'paper_consumptions',r['approval_id']);item=get(db,'outbox',r['entry_action_id'])
                    if (c is None or c['action_id']!=r['entry_action_id'] or c['position_id']!=pid or
                        c['approval_digest']!=digest(body) or c['reservation_digest']!=digest(reservation_binding(r)) or
                        item is None or c['intent_digest']!=digest(item['action']) or
                        r['plan']!=plan.model_dump(mode='json') or r['approved_quantity']!=body['quantity'] or
                        r['risk']!=body['risk'] or r['exit_plan_id']!=body['exit_plan']['plan_id']):
                        raise OfflineError('RECOVERY_NORMAL_APPROVAL_CONSUMPTION_MISMATCH')
            return super().recover()
        except (ValueError,ArithmeticError,KeyError,TypeError) as error:
            self.store.quarantine('8B_RECOVERY_FAILED: '+str(error));raise

    def summary(self):
        result=super().summary()
        with self.store.transaction() as db:
            approvals={k:v for k,v in rows(db,'paper_approvals')}
            normal_ids={pid for pid,r in rows(db,'reservations') if r['origin']==ORIGIN}
            result.update(scope='8B_SYNTHETIC_OFFLINE',normal_entry_supported=True,
                normal_entry_complete=any(pid in normal_ids and s['phase']=='CLOSED' for pid,s in result['positions'].items()),
                allow_fixtures=self.store.settings(db).allow_fixtures,real_runtime_connected=False,
                strategy_effectiveness_verified=False,approvals=approvals,
                consumption_count=len(rows(db,'paper_consumptions')),
                normal_position_count=sum(r['origin']==ORIGIN for _,r in rows(db,'reservations')),
                fixture_position_count=sum(r['origin']=='FIXTURE_EXISTING_POSITION' for _,r in rows(db,'reservations')))
        return result
