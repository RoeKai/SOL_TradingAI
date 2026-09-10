"""8C composer. All trade accounting/receipts/recovery stay in the 8A reducer.

Only this explicit schema-3 entry can issue historical Paper grants. Neither
an external JSON permission nor an 8B/fixture origin is an 8C entry authority.
"""
from decimal import Decimal as D, ROUND_FLOOR
import os
from app.offline_paper.engine import OfflinePaper
from app.offline_paper.broker import Broker, TERMINAL
from app.offline_paper.storage import get, put, rows, digest
from app.offline_paper.models import amount
from app.offline_paper.pricing import execution_price
from app.configuration.inputs import PlanInputs
from app.configuration.compiler import policy_from
from app.admission.contract import require_paper_admission
from app.exits.bindings import capture_plan
from app.exits.models import EntryFill
from app.admitted_paper.engine import reservation_binding
from app.admitted_paper.gate import scenario_admission
from .storage import HistoricalStore, hget, hput, hrows
from .models import HistoricalExitPlan, HistoricalScenarios
from .evidence import review
from .plans import validate_plan
from .scenarios import evaluate_scenarios
from .funding import net_funding
from .gate import evaluate, VERSION, SCOPE
from .data import HistoricalError

ORIGIN='NORMAL_HISTORICAL_PAPER_8C'


def grant(store,db,key):
    record=hget(db,'history_approvals',key)
    if record is None: raise HistoricalError('NO_INSTANCE_ISSUED_HISTORICAL_APPROVAL')
    b=record['body']
    if record['body_digest']!=digest(b) or key!=digest({'issuer':VERSION,'body':b}):
        raise HistoricalError('APPROVAL_CONTENT_CHANGED')
    if b['result']=='REJECT' or b['version']!=VERSION or b['scope']!=SCOPE or b['instance_id']!=store.instance_id:
        raise HistoricalError('APPROVAL_SCOPE_INVALID')
    c,_=review(store,db,b['candidate_id'],now=b['issued_at'],allow_expired_history=True)
    inputs=PlanInputs.model_validate(b['input_binding']);bundle=store.bundle(db);model=store.model(db)
    if (c.model_dump(mode='json')!=b['candidate'] or c.setup!=inputs.setup or b['bundle_digest']!=bundle.bundle_digest or
        b['run_digest']!=c.run_digest or b['dataset_digest']!=c.dataset_digest): raise HistoricalError('APPROVAL_INPUT_BINDING_CHANGED')
    plan=HistoricalExitPlan.model_validate(b['exit_plan']);validate_plan(plan,c,bundle,model)
    snapshot=capture_plan(inputs.setup,inputs.rr,inputs.scorecard,inputs.admission)
    ev=HistoricalScenarios.model_validate(b['scenarios'])
    if ev!=evaluate_scenarios(plan,inputs.setup,snapshot,inputs.admission.max_quantity): raise HistoricalError('APPROVAL_SCENARIO_CHANGED')
    reasons,risk=scenario_admission(plan,ev,inputs.admission,inputs.account,store.settings(db))
    if reasons or D(b['risk'])!=risk or D(b['quantity'])!=inputs.admission.max_quantity: raise HistoricalError('APPROVAL_RISK_CHANGED')
    require_paper_admission(inputs.admission,setup=inputs.setup,rr=inputs.rr,scorecard=inputs.scorecard,account=inputs.account,
        exchange=inputs.exchange,request=inputs.request,policy=policy_from(bundle,'admission'),evaluated_at=b['issued_at'],quantity=D(b['quantity']))
    return b,snapshot


def economic_digest(db):
    # Quote-only clock ticks do not alter this financial-state digest. Any
    # confirmed fill, funding or another reservation invalidates entry acceptance.
    return digest({'fills':rows(db,'fills'),'funding':hget(db,'history_meta','funding'),
        'reservations':[(k,reservation_binding(r)) for k,r in rows(db,'reservations')],
        'bundle':get(db,'identity','identity')['bundle_digest']})


class HistoricalBroker(Broker):
    def _authorize_entry(self,db,item):
        if type(self.store) is not HistoricalStore or item['origin']!=ORIGIN:
            raise HistoricalError('HISTORICAL_ENTRY_AUTHORITY_REQUIRED')
        action=item['action'];r=get(db,'reservations',action['position_id'])
        if r is None or not r.get('approval_id'): raise HistoricalError('APPROVAL_RESERVATION_REQUIRED')
        b,plan=grant(self.store,db,r['approval_id']);c=hget(db,'history_consumptions',r['approval_id'])
        if (c is None or c['state']!='CONSUMED' or c['action_id']!=action['action_id'] or c['position_id']!=action['position_id'] or
            c['approval_digest']!=digest(b) or c['reservation_digest']!=digest(reservation_binding(r)) or
            c['intent_digest']!=digest(action) or r['plan']!=plan.model_dump(mode='json') or action['quantity']!=b['quantity'] or
            action['side']!=('BUY' if b['candidate']['setup']['side']=='LONG' else 'SELL')):
            raise HistoricalError('HISTORICAL_CONSUMPTION_BINDING_INVALID')
        now=hget(db,'history_cursor','cursor')['at_ms']
        if now>=b['expires_at']*1000: return 'APPROVAL_EXPIRED_BEFORE_ACCEPTANCE'
        if c['economic_digest']!=economic_digest(db): return 'FINANCIAL_STATE_CHANGED_BEFORE_ACCEPTANCE'
        return None

    def execute(self,key):
        with self.store.transaction() as db:
            item=get(db,'outbox',key);cursor=hget(db,'history_cursor','cursor')
            if item is None: raise HistoricalError('DURABLE_INTENT_REQUIRED')
            timing=hget(db,'history_execution','order:'+key)
            if timing is None or cursor['at_ms']<timing['due_at_ms']: raise HistoricalError('ACCEPTANCE_DELAY_NOT_ELAPSED')
            result=super().execute(key)
            timing['accepted_at_ms']=cursor['at_ms'];hput(db,'history_execution','order:'+key,timing)
            return result

    def fill(self,key,quantity,execution_id,*,defer_details=False,defer_receipt=False):
        with self.store.transaction() as db:
            event=hget(db,'history_meta','active_market')
            if event is None or event['symbol']!='SOLUSDT' or execution_id!=event['event_id']:
                raise HistoricalError('FILL_REQUIRES_CURRENT_ARCHIVE_LIQUIDITY_EVENT')
            cursor=hget(db,'history_cursor','cursor');timing=hget(db,'history_execution','order:'+key)
            if (timing is None or timing.get('accepted_at_ms') is None or cursor['at_ms']!=event['available_at_ms'] or
                event['available_at_ms']<=timing['accepted_at_ms'] or event['event_time_ms']<=timing['submitted_at_ms']):
                raise HistoricalError('SAME_EVENT_OR_PRE_ACCEPTANCE_FILL_FORBIDDEN')
            fid=digest({'order':key,'execution':execution_id});old=get(db,'broker_fills',fid)
            if old is not None:
                if D(old['requested_quantity'])!=amount(quantity): raise HistoricalError('EXECUTION_QUANTITY_CONFLICT')
                return fid
            m=self.store.model(db);budget=D(event['quantity'])*m.participation
            use=hget(db,'history_meta','liquidity') or {'event_id':event['event_id'],'used':'0'}
            if use['event_id']!=event['event_id']: raise HistoricalError('LIQUIDITY_CURSOR_MISMATCH')
            q=amount(quantity)
            if q>D(budget)-D(use['used']): raise HistoricalError('MARKET_PARTICIPATION_BUDGET_EXCEEDED')
            fid=super().fill(key,q,execution_id,defer_details=defer_details,defer_receipt=defer_receipt)
            f=get(db,'broker_fills',fid);filled=D(f['quantity']);use['used']=str(D(use['used'])+filled)
            hput(db,'history_meta','liquidity',use)
            a=get(db,'broker_orders',key)['action'];sign=1 if a['side']=='BUY' else -1
            mid=D(event['price']);quote=mid*(1+sign*m.spread_bps/20000)
            hput(db,'history_execution','fill:'+fid,dict(fill_id=fid,market_event_id=execution_id,source_digest=event['source_digest'],
                market_at_ms=event['event_time_ms'],visible_at_ms=event['available_at_ms'],quantity=str(filled),
                observed_trade_price=event['price'],modelled_quote=str(quote),fill_price=f['price'],
                spread_cost_usdt=str(abs(quote-mid)*filled),slippage_and_rounding_cost_usdt=str(abs(D(f['price'])-quote)*filled),
                provenance='SIMULATED_FILL_NOT_HISTORICAL_ACCOUNT_TRADE'))
            return fid


class HistoricalPaper(OfflinePaper):
    def __init__(self,store):
        if type(store) is not HistoricalStore: raise HistoricalError('EXPLICIT_SCHEMA_3_INSTANCE_REQUIRED')
        super().__init__(store);self.broker=HistoricalBroker(store)

    def _cash_origin(self,db):
        return self.store.settings(db).initial_balance+net_funding(db)

    def prepare(self,candidate_id,request_id,*,risk_budget='5'):
        if not self.ready or self.store.failed: raise HistoricalError('RECOVERY_NOT_COMPLETE')
        content=dict(candidate_id=candidate_id,request_id=request_id,risk_budget=str(amount(risk_budget)))
        with self.store.transaction() as db:
            self._assert_cash(db);old=get(db,'requests','historical-proposal:'+request_id)
            if old is not None:
                if old['request_digest']!=digest(content): raise HistoricalError('REQUEST_ID_CONTENT_CONFLICT')
                return hget(db,'history_approvals',old['approval_id'])
            try: body=evaluate(self.store,db,candidate_id,request_id,amount(risk_budget))
            except (ValueError,ArithmeticError) as error:
                body=dict(version=VERSION,scope=SCOPE,instance_id=self.store.instance_id,result='REJECT',reason_codes=[str(error)],
                    candidate_id=candidate_id,request_id=request_id,requested_risk=str(risk_budget),live_allowed=False)
            key=digest({'issuer':VERSION,'body':body});record=dict(approval_id=key,body=body,body_digest=digest(body))
            hput(db,'history_approvals',key,record)
            put(db,'requests','historical-proposal:'+request_id,dict(request_digest=digest(content),approval_id=key))
            return record

    def submit(self,key,*,fault=None):
        if type(key) is not str or not self.ready or self.store.failed: raise HistoricalError('PERSISTED_APPROVAL_AND_READY_INSTANCE_REQUIRED')
        with self.store.transaction() as db:
            self._assert_cash(db);record=hget(db,'history_approvals',key)
            if record is None: raise HistoricalError('NO_INSTANCE_ISSUED_HISTORICAL_APPROVAL')
            b=record['body'];rid=b['request_id'];old=get(db,'requests','historical-admitted:'+rid)
            if old is not None:
                if old['approval_id']!=key: raise HistoricalError('REQUEST_ID_CONTENT_CONFLICT')
                return old
            reasons=list(b['reason_codes']) if b['result']=='REJECT' else []
            if not reasons:
                try:
                    b,snapshot=grant(self.store,db,key);a=get(db,'account','account')
                    if a['now']>=b['expires_at']: reasons.append('APPROVAL_EXPIRED')
                    if a['version']!=b['account_revision']: reasons.append('ACCOUNT_REVISION_CHANGED_REISSUE')
                    if get(db,'identity','identity')['bundle_digest']!=b['bundle_digest']: reasons.append('CONFIG_CHANGED_REISSUE')
                    if not reasons:
                        fresh=evaluate(self.store,db,b['candidate_id'],rid,D(b['requested_risk']))
                        if fresh['result']=='REJECT': reasons.extend(fresh['reason_codes'])
                        elif fresh!=b: reasons.append('CONTEXT_CHANGED_REISSUE')
                except (ValueError,ArithmeticError) as error: reasons.append(str(error))
            if reasons:
                result=dict(result='REJECT',reason_codes=reasons,approval_id=key,origin=ORIGIN,entry_action_id=None)
                put(db,'requests','historical-admitted:'+rid,result);return result
            a=get(db,'account','account');plan=HistoricalExitPlan.model_validate(b['exit_plan'])
            pid='historical-position:'+digest({'request':rid,'instance':self.store.instance_id})
            action_id=digest({'historical-entry':key,'position':pid})
            r=dict(origin=ORIGIN,side=plan.side,quantity=b['quantity'],risk=b['risk'],margin=b['margin'],fee_reserve=b['fee_reserve'],
                reference_price=str(plan.reference_entry),expires_at=b['expires_at'],bundle_digest=b['bundle_digest'],account_revision=a['version'],
                released=False,entry_sealed=False,entry_high_water='0',entry_terminal=None,entry_terminal_quantity=None,
                entry_status_unknown=False,entry_faults=[],entry_reconciliation_required=False,entry_pending_reasons=[],
                plan=snapshot.model_dump(mode='json'),exit_policy=plan.policy.model_dump(mode='json'),rules=plan.rules.model_dump(mode='json'),
                entry_costs=dict(fee_rate=str(dict(plan.costs)['entry_fee_rate']),slippage_bps=str(dict(plan.costs)['entry_slippage_bps'])),
                approval_id=key,exit_plan_id=plan.plan_id,approved_quantity=b['quantity'],entry_action_id=action_id)
            item=dict(origin=ORIGIN,status='PENDING',attempts=0,action=dict(action_id=action_id,kind='ENTRY',position_id=pid,
                side='BUY' if plan.side=='LONG' else 'SELL',quantity=b['quantity']))
            result=dict(result=b['result'],reason_codes=b['reason_codes'],approval_id=key,origin=ORIGIN,entry_action_id=action_id,position_id=pid)
            put(db,'reservations',pid,r);put(db,'outbox',action_id,item);put(db,'requests','historical-admitted:'+rid,result)
            self._cash(db)
            hput(db,'history_consumptions',key,dict(state='CONSUMED',action_id=action_id,position_id=pid,
                approval_digest=digest(b),reservation_digest=digest(reservation_binding(r)),intent_digest=digest(item['action']),
                economic_digest=economic_digest(db)))
            self._schedule(db,action_id)
            self._crash('before_intent_commit',fault)
        self._crash('after_intent_commit',fault)
        return result

    def _schedule(self,db,key):
        if hget(db,'history_execution','order:'+key) is None:
            at=hget(db,'history_cursor','cursor')['at_ms'];m=self.store.model(db)
            hput(db,'history_execution','order:'+key,dict(submitted_at_ms=at,due_at_ms=at+m.acceptance_delay_ms,accepted_at_ms=None))

    def _crash(self,point,fault):
        if point==fault: os._exit(91)  # explicit argument only, no env/live bypass

    def pump(self,*,fault=None,limit=512):
        for _ in range(limit):
            with self.store.transaction() as db:
                pending=next((k for k,e in rows(db,'broker_events') if not e['consumed']),None)
                at=hget(db,'history_cursor','cursor')['at_ms'];action=None
                for k,item in rows(db,'outbox'):
                    if item['status'] not in ('PENDING','INFLIGHT'): continue
                    self._schedule(db,k)
                    if hget(db,'history_execution','order:'+k)['due_at_ms']<=at:
                        action=(k,item);break
            if pending is not None:
                self.deliver(pending,fault=fault);continue
            if action is None:
                if self._recovered:
                    with self.store.transaction() as db: self._refresh_ready(db)
                return
            k,item=action
            with self.store.transaction() as db:
                current=get(db,'outbox',k);current['status']='INFLIGHT';current['attempts']+=1;put(db,'outbox',k,current)
            if item['status']=='INFLIGHT':
                status=self.broker.reconcile(k,'historical-dispatch-recovery:'+k)
                if status=='CONFIRMED_NOT_EXECUTED': self.broker.execute(k)
            else: self.broker.execute(k)
            self._crash('after_broker_execution',fault)
            with self.store.transaction() as db:
                current=get(db,'outbox',k)
                if current['status']!='CONFIRMED': current['status']='SUBMITTED'
                put(db,'outbox',k,current)
        raise HistoricalError('BOUNDED_HISTORICAL_PUMP_EXHAUSTED')

    def cancel_opening(self,entry_id,reason):
        with self.store.transaction() as db:
            item=get(db,'outbox',entry_id)
            if item is None or item['action']['kind']!='ENTRY': raise HistoricalError('UNKNOWN_OPENING_LEG')
            key=digest({'cancel-original-entry':entry_id,'reason':reason})
            if get(db,'outbox',key) is None:
                put(db,'outbox',key,dict(origin=ORIGIN,status='PENDING',attempts=0,
                    action=dict(action_id=key,kind='CANCEL',position_id=item['action']['position_id'],target_action_id=entry_id)))
                self._schedule(db,key)
            return key

    def _save(self,db,position,cp):
        super()._save(db,position,cp)
        r=get(db,'reservations',cp.state.position_id)
        if r['origin']!=ORIGIN: raise HistoricalError('MIXED_INSTANCE_ORIGIN')
        actual=cp.state.original_quantity;approved=D(r['approved_quantity'])
        r['confirmed_opening_quantity']=str(actual);r['unfilled_approved_quantity']=str(max(D(0),approved-actual))
        r['scenario_applicability']='REFERENCE_ONLY' if actual!=approved or not cp.state.entry_sealed or r.get('fill_deviation_reason') else 'CONDITIONAL_MODEL_NOT_EXPECTATION'
        put(db,'reservations',cp.state.position_id,r)
        for a in cp.state.actions: self._schedule(db,a.action_id)

    def _event(self,db,event):
        super()._event(db,event)
        if isinstance(event,EntryFill):
            r=get(db,'reservations',event.position_id);b=hget(db,'history_approvals',r['approval_id'])['body']
            plan=HistoricalExitPlan.model_validate(b['exit_plan']);m=self.store.model(db)
            sign=1 if plan.side=='LONG' else -1;side='BUY' if sign==1 else 'SELL'
            bounds=sorted(execution_price(p*(1+sign*m.spread_bps/20000),side,m.slippage_bps,plan.rules.price_tick)
                for p in (plan.entry_lower,plan.entry_upper))
            if not bounds[0]<=event.price<=bounds[-1]:
                r['fill_deviation_reason']='ACTUAL_FILL_OUTSIDE_APPROVED_GEOMETRY';r['scenario_applicability']='REFERENCE_ONLY_ACTUAL_FILL_DEVIATED'
                put(db,'reservations',event.position_id,r)
                a=get(db,'account','account');a['paused']=True;put(db,'account','account',a)
                self.cancel_opening(r['entry_action_id'],'FILL_DEVIATION')

    def recover(self):
        try:
            with self.store.transaction() as db:
                net_funding(db);self.store.run(db)
                for pid,r in rows(db,'reservations'):
                    if r['origin']!=ORIGIN: raise HistoricalError('MIXED_INSTANCE_ORIGIN')
                    b,plan=grant(self.store,db,r['approval_id']);c=hget(db,'history_consumptions',r['approval_id']);item=get(db,'outbox',r['entry_action_id'])
                    if (c is None or c['position_id']!=pid or c['approval_digest']!=digest(b) or
                        c['reservation_digest']!=digest(reservation_binding(r)) or item is None or c['intent_digest']!=digest(item['action']) or
                        r['plan']!=plan.model_dump(mode='json')): raise HistoricalError('RECOVERY_APPROVAL_BINDING_INVALID')
            return super().recover()
        except (ValueError,ArithmeticError,KeyError,TypeError) as error:
            self.store.quarantine('8C_RECOVERY_FAILED: '+str(error));raise

    def summary(self):
        with self.store.transaction() as db:
            from .funding import snapshot
            positions={pid:p['checkpoint']['state'] for pid,p in rows(db,'positions')}
            # Never materialize all candidate/grant bodies in a month summary.
            result=dict(scope=SCOPE,normal_entry_complete=any(s['phase']=='CLOSED' for s in positions.values()),
                live_allowed=False,allow_fixtures=False,account=get(db,'account','account'),positions=positions,
                risk_snapshot=snapshot(self.store,db).model_dump(mode='json'),
                counts={name:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for name,table in
                    (('orders','broker_orders'),('confirmed_ledger_fills','fills'),('approvals','history_approvals'))},
                fees_usdt=str(sum((D(f['fee_usdt']) for _,f in rows(db,'fills')),D(0))),
                pending_actions=[k for k,o in rows(db,'outbox') if o['status']!='CONFIRMED'],
                pending_reconciliation=[k for k,o in rows(db,'outbox') if o.get('entry_reconciliation') and not o['target_confirmed']],
                funding_net_cash_usdt=str(net_funding(db)),funding=hrows(db,'history_funding'),
                run=self.store.run(db),cursor=hget(db,'history_cursor','cursor'),
                statistics=hget(db,'history_meta','statistics'),model_limit=hget(db,'history_meta','model_limit'))
        return result
