"""Stateful offline composition. The original reducers remain pure and unchanged."""
from decimal import Decimal as D
import os

from app.configuration.compiler import verify_bundle, policy_from, main_values
from app.configuration.contracts import validate_contract, declare_plan_binding
from app.configuration.inputs import PlanInputs
from app.setups.models import TradeSetup
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup
from app.admission.models import AdmissionRequest
from app.admission.engine import admit_trade
from app.exits.models import EntryFill, EntrySealed, ExitFill, ExitSeed, PlanSnapshot, RecoveryRequired, MarketEvent
from app.exits.engine import initialize_exit, apply_event, EVENTS, digest as exit_digest
from app.exits.persistence import ExitCheckpoint, checkpoint
from app.exits.policy import ExitPolicy
from .broker import Broker, synthetic_rules
from .models import OfflineError, FixtureEntry, Quote
from .storage import get, put, rows, digest
from .risk import snapshot, exchange_snapshot, reserve_fixture


class OfflinePaper:
    def __init__(self,store):
        self.store=store;self.broker=Broker(store);self.ready=False

    def _crash(self,point,fault):
        if point==fault:
            with self.store.transaction() as db:
                if not self.store.settings(db).allow_fixtures: raise OfflineError('Fault injection requires fixture instance')
            os._exit(91)

    def _save(self,db,position,cp):
        position['checkpoint']=cp.model_dump(mode='json')
        state=cp.state;pid=state.position_id
        r=get(db,'reservations',pid)
        if state.entry_sealed: r['entry_sealed']=True
        r['released']=state.entry_sealed and state.remaining_quantity==0 and not state.faults
        if r['released'] and position.get('closed_at') is None:
            position['closed_at']=get(db,'account','account')['now']
        if state.remaining_quantity: position['closed_at']=None
        put(db,'reservations',pid,r);put(db,'positions',pid,position)
        for action in state.actions:
            if get(db,'outbox',action.action_id) is None:
                put(db,'outbox',action.action_id,{'action':action.model_dump(mode='json'),
                    'origin':r['origin'],'status':'PENDING','attempts':0})

    def _event(self,db,event):
        pid=event.position_id;position=get(db,'positions',pid)
        previous=None if position is None else ExitCheckpoint.model_validate(position['checkpoint'])
        before=None if previous is None else previous.state
        if isinstance(event,(EntryFill,ExitFill)):
            fact=get(db,'broker_fills',event.fill_id)
            if fact is None: raise OfflineError('Fill not in this synthetic Broker ledger')
            action_id=event.entry_action_id if isinstance(event,EntryFill) else event.action_id
            if (fact['position_id']!=pid or fact['action_id']!=action_id or
                any(D(fact[k])!=getattr(event,k) for k in ('quantity','price','fee_usdt','occurred_at'))):
                raise OfflineError('Confirmed fill content mismatch')
        if previous is None:
            if not isinstance(event,EntryFill): raise OfflineError('Position not initialized by confirmed fill')
            context=get(db,'reservations',pid)
            seed=ExitSeed(position_id=pid,first_fill=event,plan=context['plan'],rules=context['rules'])
            policy=ExitPolicy.model_validate(context['exit_policy']);result=initialize_exit(seed,policy)
            cp=ExitCheckpoint(seed=seed,policy=policy,journal=(),state=result.state,state_digest=exit_digest(result.state))
            position={'origin':context['origin'],'checkpoint':None,'entry_fee_remaining':'0','closed_at':None}
        else:
            result=apply_event(previous.seed,previous.policy,previous.state,event)
            cp=ExitCheckpoint(seed=previous.seed,policy=previous.policy,journal=(*previous.journal,event),
                              state=result.state,state_digest=exit_digest(result.state))
        state=cp.state
        if isinstance(event,(EntryFill,ExitFill)) and get(db,'fills',event.fill_id) is None:
            if event.fill_id not in {f.fill_id for f in state.fill_facts}:
                raise OfflineError('Confirmed fill rejected by reducer; quarantine, never discard evidence')
            remaining_fees=D(position['entry_fee_remaining'])
            if isinstance(event,EntryFill):
                remaining_fees+=event.fee_usdt;net_outcome=D(0)
            else:
                allocated=remaining_fees if event.quantity==before.remaining_quantity else remaining_fees*event.quantity/before.remaining_quantity
                remaining_fees-=allocated
                net_outcome=state.realized_gross_pnl-before.realized_gross_pnl-event.fee_usdt-allocated
            position['entry_fee_remaining']=str(remaining_fees)
            ledger_fact=dict(fact,net_outcome=str(net_outcome))
            put(db,'fills',event.fill_id,ledger_fact)
        self._save(db,position,cp)
        # Confirmed adverse fills are kept. Cancel unfinished entry if its actual
        # stop risk exceeds the frozen reservation; never roll back a real fact.
        if isinstance(event,EntryFill):
            r=get(db,'reservations',pid)
            exit_notional=state.remaining_quantity*state.original_stop
            stop_risk=abs(state.remaining_entry_cost-exit_notional)+state.entry_fees+exit_notional*(
                cp.policy.expected_exit_fee_rate+cp.policy.expected_exit_slippage_bps/10000)
            if stop_risk>D(r['risk']) and not state.entry_sealed:
                key=digest({'risk-breach-entry':state.entry_action_id})
                if get(db,'outbox',key) is None:
                    put(db,'outbox',key,{'origin':r['origin'],'status':'PENDING','attempts':0,'action':dict(
                        action_id=key,kind='CANCEL',position_id=pid,target_action_id=state.entry_action_id,side='SELL' if state.side=='LONG' else 'BUY')})
                a=get(db,'account','account');a['paused']=True;put(db,'account','account',a)
                r['risk_breach']='ACTUAL_INITIAL_STOP_RISK_EXCEEDS_RESERVATION';put(db,'reservations',pid,r)

    def _seal(self,db,pid):
        r=get(db,'reservations',pid);p=get(db,'positions',pid)
        if not r or r['entry_sealed'] or r['entry_terminal'] is None: return
        details=sum((D(f['quantity']) for _,f in rows(db,'fills') if f['position_id']==pid and f['kind']=='ENTRY_FILL'),D(0))
        if details!=D(r['entry_high_water']) or details!=D(r['entry_terminal_quantity']): return
        if details==0:
            r['entry_sealed']=True;r['released']=True;put(db,'reservations',pid,r);return
        self._event(db,EntrySealed(event_id='seal:'+r['entry_action_id'],position_id=pid,
            received_at=get(db,'account','account')['now'],entry_action_id=r['entry_action_id'],total_filled_quantity=details))

    def _cash(self,db):
        account=get(db,'account','account')
        account['cash']=str(self.store.settings(db).initial_balance+sum((D(p['checkpoint']['state']['realized_net_pnl']) for _,p in rows(db,'positions')),D(0)))
        account['version']+=1;put(db,'account','account',account)

    def _assert_cash(self,db):
        expected=self.store.settings(db).initial_balance+sum((D(p['checkpoint']['state']['realized_net_pnl']) for _,p in rows(db,'positions')),D(0))
        if D(get(db,'account','account')['cash'])!=expected:
            raise OfflineError('Cash/checkpoint conservation mismatch; never top up on error')

    def deliver(self,delivery_id,*,fault=None):
        try:
            with self.store.transaction() as db:
                self._assert_cash(db)
                record=get(db,'broker_events',delivery_id)
                if record is None: raise OfflineError('Unknown Broker delivery; no external self-certified JSON')
                body=dict(record['payload']);old=get(db,'inbox',delivery_id)
                if old is not None:
                    if old['digest']!=digest(body): raise OfflineError('Delivery content collision')
                    return 'DUPLICATE'
                account=get(db,'account','account');pid=body['position_id']
                if body['kind']=='ENTRY_STATUS':
                    r=get(db,'reservations',pid);quantity=D(body['cumulative_filled_quantity'])
                    known=max(D(r['entry_high_water']),quantity)
                    if body['status'] in ('FILLED','CANCELED','REJECTED'):
                        if quantity<D(r['entry_high_water']) or (r['entry_terminal'] is not None and
                            (body['status']!=r['entry_terminal'] or quantity!=D(r['entry_terminal_quantity']))):
                            raise OfflineError('ENTRY_TERMINAL_CUMULATIVE_CONTRADICTION')
                        r['entry_terminal']=body['status'];r['entry_terminal_quantity']=str(quantity)
                    r['entry_high_water']=str(known);put(db,'reservations',pid,r)
                else:
                    # Adapter owns receive time. Historical execution time is retained.
                    body.update(event_id=delivery_id,received_at=account['now'])
                    # Control actions for the opening leg need no fake ExitAction ACK.
                    target_action=get(db,'outbox',body.get('action_id',''))
                    position=get(db,'positions',pid)
                    reducer_ids=set() if position is None else {a['action_id'] for a in position['checkpoint']['state']['actions']}
                    synthetic_control=(body['kind']=='RECEIPT' and target_action is not None and
                        body['action_id'] not in reducer_ids and target_action['action']['kind']=='CANCEL' and
                        target_action['action']['target_action_id']==get(db,'reservations',pid)['entry_action_id'])
                    if not synthetic_control: self._event(db,EVENTS.validate_python(body))
                self._seal(db,pid)
                put(db,'inbox',delivery_id,{'digest':digest(record['payload']),'kind':record['payload']['kind']})
                record['consumed']=True;put(db,'broker_events',delivery_id,record)
                action_id=record['payload'].get('action_id')
                item=get(db,'outbox',action_id) if action_id else None
                if item is not None:
                    item['status']='CONFIRMED';put(db,'outbox',action_id,item)
                self._cash(db)
            self._crash('after_receipt_commit',fault)
            return 'CONSUMED'
        except (ValueError,ArithmeticError) as error:
            self.store.quarantine(type(error).__name__+': '+str(error));raise

    def pump(self,*,fault=None,limit=512):
        for _ in range(limit):
            with self.store.transaction() as db:
                pending=next((key for key,item in rows(db,'broker_events') if not item['consumed']),None)
                action=next(((key,item) for key,item in rows(db,'outbox') if item['status'] in ('PENDING','INFLIGHT')),None)
            if pending is not None:
                self.deliver(pending,fault=fault);continue
            if action is None: return
            key,item=action
            with self.store.transaction() as db:
                current=get(db,'outbox',key);current['status']='INFLIGHT';current['attempts']+=1;put(db,'outbox',key,current)
            if item['status']=='INFLIGHT':
                status=self.broker.reconcile(key,'dispatch-recovery:'+key)
                if status=='CONFIRMED_NOT_EXECUTED': self.broker.execute(key)
            else: self.broker.execute(key)
            self._crash('after_broker_execution',fault)
            with self.store.transaction() as db:
                current=get(db,'outbox',key)
                if current['status']!='CONFIRMED': current['status']='SUBMITTED'
                put(db,'outbox',key,current)
        raise OfflineError('Bounded pump exhausted; inspect pending reconciliation')

    def fixture_entry(self,fixture,*,fault=None):
        if not self.ready: raise OfflineError('Startup recovery required')
        if type(fixture) is not FixtureEntry: raise OfflineError('Explicit typed fixture required')
        with self.store.transaction() as db:
            self._assert_cash(db)
            key='fixture:'+fixture.fixture_id;old=get(db,'requests',key)
            if old is not None:
                if old['digest']!=digest(fixture): raise OfflineError('Fixture request ID conflict')
                return old['entry_action_id']
            bundle=self.store.bundle(db);policy=policy_from(bundle,'exit');rules=synthetic_rules()
            costs=main_values(bundle)['risk']
            entry_costs=dict(fee_rate=costs['taker_fee_rate'],slippage_bps=costs['slippage_bps'])
            plan=PlanSnapshot(setup_id=fixture.fixture_id,plan_version='fixture-only/8a',symbol='SOLUSDT',side=fixture.side,
                original_stop=fixture.initial_stop,original_targets=(),setup_digest=digest(fixture),
                rr_digest=digest('FIXTURE_NO_RR'),scorecard_digest=digest('FIXTURE_NO_SCORE'),
                admission_digest=digest('FIXTURE_NOT_ADMITTED'),admission_result='REJECT')
            pid='fixture-position:'+fixture.fixture_id
            r=reserve_fixture(self.store,db,fixture,policy,plan,rules,entry_costs)
            action_id=digest({'fixture-opening-leg':fixture.fixture_id,'bundle':fixture.bundle_digest})
            r['entry_action_id']=action_id;put(db,'reservations',pid,r)
            action=dict(action_id=action_id,kind='ENTRY',position_id=pid,side='BUY' if fixture.side=='LONG' else 'SELL',quantity=str(fixture.quantity))
            put(db,'outbox',action_id,{'action':action,'origin':fixture.origin,'status':'PENDING','attempts':0})
            put(db,'requests',key,{'digest':digest(fixture),'entry_action_id':action_id,'origin':fixture.origin})
            self._cash(db)
            # This fault is inside the transaction: no nested connection/commit.
            if fault=='before_intent_commit': os._exit(91)
        self._crash('after_intent_commit',fault)
        return action_id

    def request_entry(self,request_id,setup,request,*,expected_revision,bundle_digest,at):
        """A: ordinary path. All Runner combinations remain blocked in 8A.

        Descriptive calculations/admission are retained as rejection diagnostics,
        never as permission overriding the earlier configuration/plan check.
        """
        if not self.ready: raise OfflineError('Startup recovery required')
        if type(setup) is not TradeSetup or type(request) is not AdmissionRequest:
            raise OfflineError('No Signal/dict fallback or external account snapshot')
        content=digest({'setup':setup.model_dump(mode='json'),'request':request.model_dump(mode='json'),
            'expected_revision':expected_revision,'bundle_digest':bundle_digest,'at':at})
        with self.store.transaction() as db:
            self._assert_cash(db)
            old=get(db,'requests','normal:'+request_id)
            if old is not None:
                if old['digest']!=content: raise OfflineError('Signal ID content conflict')
                return old
            a=get(db,'account','account');bundle=self.store.bundle(db)
            if type(at) is not int or at<a['now']: raise OfflineError('Logical time must not move backwards')
            a['now']=at;put(db,'account','account',a)
            policy=policy_from(bundle,'admission');account=snapshot(self.store,db)
            venue=exchange_snapshot(at);pre=validate_contract(verify_bundle(bundle),PlanInputs(setup=setup),evaluated_at=at)
            rr=calculate_rr(setup,quantity=None)
            card=score_trade_setup(setup,rr,evaluated_at=at,max_data_age_seconds=float(bundle.manifest.score_context_max_age_seconds))
            inputs=PlanInputs(setup=setup,rr=rr,scorecard=card,account=account,exchange=venue,request=request,exit_rules=synthetic_rules())
            binding=None
            if at>=setup.created_at: binding=declare_plan_binding(bundle,inputs,declared_at=at)
            decision=admit_trade(setup,rr,card,account=account,exchange=venue,request=request,policy=policy,evaluated_at=at)
            inputs=inputs.model_copy(update={'configuration_binding':binding,'admission':decision})
            validation=validate_contract(verify_bundle(bundle),inputs,evaluated_at=at)
            reasons=[i.reason_code for i in validation.issues if i.severity!='WARNING']
            if bundle_digest!=bundle.bundle_digest: reasons.append('CONFIGURATION_BINDING_CHANGED')
            if expected_revision!=a['version']: reasons.append('ACCOUNT_VERSION_CHANGED')
            if setup.valid_until is None or at>=setup.valid_until: reasons.append('PLAN_EXPIRED_OR_MISSING_VALIDITY')
            if a['quarantined'] or not a['reconciliation_clear']: reasons.append('STARTUP_RECONCILIATION_REQUIRED')
            # Explicit capability fence, not a bypass when a future validator changes.
            reasons.append('NORMAL_ENTRY_FULL_EXIT_CONTRACT_UNSUPPORTED_8A')
            result=dict(origin='A_NORMAL_ADMISSION',digest=content,result='REJECT',reason_codes=list(dict.fromkeys(reasons)),
                snapshot_revision=account.snapshot_revision,bundle_digest=bundle.bundle_digest,
                setup=setup.model_dump(mode='json'),rr=rr.model_dump(mode='json'),scorecard=card.model_dump(mode='json'),
                admission=decision.model_dump(mode='json'),validation=validation.model_dump(mode='json'),
                preliminary_config_plan=pre.model_dump(mode='json'),execution_authority='none')
            put(db,'requests','normal:'+request_id,result);self._cash(db)
            return result

    def tick(self,quote):
        if type(quote) is not Quote: raise OfflineError('Explicit bid/ask event required, not an OHLC path')
        with self.store.transaction() as db:
            self._assert_cash(db)
            key='quote:'+quote.event_id;old=get(db,'inbox',key)
            if old is not None:
                if old['digest']!=digest(quote): raise OfflineError('Market event ID conflict')
                return
            a=get(db,'account','account')
            if quote.at<a['now']: raise OfflineError('Backward logical time forbidden')
            a['now']=quote.at;a['quote']=quote.model_dump(mode='json');put(db,'account','account',a)
            for pid,p in rows(db,'positions'):
                if p['checkpoint']['state']['phase']=='CLOSED': continue
                self._event(db,MarketEvent(event_id=key+':'+pid,position_id=pid,received_at=quote.at,
                    observed_at=quote.at,bid=quote.bid,ask=quote.ask,confirmed=True,new_entries_paused=a['paused'],
                    daily_halted=a['paused'],admission_rejected=True,score_available=False))
            put(db,'inbox',key,{'digest':digest(quote),'kind':'MARKET'});self._cash(db)

    def recover(self):
        self.ready=False
        try:
            with self.store.transaction() as db:
                a=get(db,'account','account');self.store.bundle(db)
                expected=self.store.settings(db).initial_balance
                for _,p in rows(db,'positions'):
                    saved=ExitCheckpoint.model_validate(p['checkpoint'])
                    if checkpoint(saved.seed,saved.policy,saved.journal)!=saved: raise OfflineError('Checkpoint replay mismatch')
                    expected+=saved.state.realized_net_pnl
                    ledger_ids={k for k,f in rows(db,'fills') if f['position_id']==saved.state.position_id}
                    if ledger_ids!={f.fill_id for f in saved.state.fill_facts}: raise OfflineError('Fill/checkpoint mismatch')
                    for fact in saved.state.fill_facts:
                        ledger=get(db,'fills',fact.fill_id);broker=get(db,'broker_fills',fact.fill_id)
                        if broker is None or any(ledger.get(k)!=v for k,v in broker.items()):
                            raise OfflineError('Ledger/Broker fill evidence mismatch')
                        if any(D(ledger[k])!=getattr(fact,k) for k in ('quantity','price','fee_usdt','occurred_at')):
                            raise OfflineError('Fill/checkpoint content mismatch')
                    for action in saved.state.actions:
                        item=get(db,'outbox',action.action_id)
                        receipt_fields={'status','filled_quantity','acknowledged_quantity','stop_confirmed',
                            'terminal_status','terminal_quantity','confirmed_coverage','target_confirmed'}
                        expected_action={k:v for k,v in action.model_dump(mode='json').items() if k not in receipt_fields}
                        if (item is None or item['action'].get('status')!='INTENT' or
                            {k:v for k,v in item['action'].items() if k not in receipt_fields}!=expected_action):
                            raise OfflineError('Checkpoint/outbox mismatch')
                    reservation=get(db,'reservations',saved.state.position_id)
                    if reservation is None or reservation['exit_policy']!=saved.policy.model_dump(mode='json'):
                        raise OfflineError('Frozen position policy mismatch')
                for key,command in rows(db,'broker_commands'):
                    item=get(db,'outbox',key)
                    if item is None or command['action_digest']!=digest(item['action']):
                        raise OfflineError('Executed command lacks matching durable intent')
                if D(a['cash'])!=expected: raise OfflineError('Cash/checkpoint conservation mismatch')
                a['recovery_count']+=1;a['reconciliation_clear']=False;put(db,'account','account',a)
                for pid,p in rows(db,'positions'):
                    self._event(db,RecoveryRequired(event_id='recovery:'+str(a['recovery_count'])+':'+pid,
                        position_id=pid,received_at=a['now']))
                self._cash(db)
            self.pump()
            with self.store.transaction() as db:
                a=get(db,'account','account')
                clear=not a['quarantined'] and all(not p['checkpoint']['state']['faults'] and
                    not p['checkpoint']['state']['recovery_pending_action_ids'] for _,p in rows(db,'positions'))
                clear=clear and not any(not e['consumed'] for _,e in rows(db,'broker_events'))
                a['reconciliation_clear']=clear;put(db,'account','account',a)
                self.ready=clear
        except (ValueError,ArithmeticError,KeyError,TypeError) as error:
            self.store.quarantine('RECOVERY_FAILED: '+str(error));raise
        return self.summary()

    def summary(self):
        with self.store.transaction() as db:
            a=get(db,'account','account');positions={pid:p['checkpoint']['state'] for pid,p in rows(db,'positions')}
            return dict(scope='8A_SYNTHETIC_OFFLINE',normal_entry_complete=False,live_allowed=False,account=a,
                risk_snapshot=snapshot(self.store,db).model_dump(mode='json'),positions=positions,
                orders={k:o for k,o in rows(db,'broker_orders')},fills={k:f for k,f in rows(db,'fills')},
                counts={'orders':len(rows(db,'broker_orders')),'confirmed_ledger_fills':len(rows(db,'fills'))},
                fees_usdt=str(sum((D(f['fee_usdt']) for _,f in rows(db,'fills')),D(0))),
                pending_actions=[k for k,o in rows(db,'outbox') if o['status']!='CONFIRMED'],
                pending_reconciliation=[action for s in positions.values() for action in s['actions']
                    if action['kind']=='RECONCILE' and not action['target_confirmed']],
                requests={k:r for k,r in rows(db,'requests')},reservations={k:r for k,r in rows(db,'reservations')})
