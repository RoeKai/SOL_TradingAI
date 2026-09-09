"""Durable synthetic venue: order acceptance is NOT a fill.

Each command is committed independently of ledger receipt consumption. That
deliberate boundary permits testing execute-before-receipt crash recovery.
"""
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING

from app.exits.models import ExitVenueRules
from .models import OfflineError, amount
from .storage import get, put, rows, digest

TERMINAL=('FILLED','CANCELED','REJECTED')


def synthetic_rules():
    # These capabilities describe ONLY the concrete implementation below.
    return ExitVenueRules(symbol='SOLUSDT',verified=True,quantity_step='.001',price_tick='.01',
        min_quantity='.001',min_notional='5',max_quantity='1000',reduce_only_min_quantity_exempt=False,
        reduce_only_min_notional_exempt=True,exact_close_remainder=False,atomic_stop_replace=True,
        dynamic_full_position_stop=False)


def message(db,key,payload):
    previous=get(db,'broker_events',key)
    if previous is not None:
        if previous['payload']!=payload: raise OfflineError('Broker delivery ID conflict')
        return
    put(db,'broker_events',key,{'payload':payload,'consumed':False})


def inventory(db,position_id):
    return sum((D(f['quantity'])*(1 if f['kind']=='ENTRY_FILL' else -1)
                for _,f in rows(db,'broker_fills') if f['position_id']==position_id),D(0))


class Broker:
    def __init__(self,store): self.store=store

    def _snapshot(self,db,order,cause):
        action=order['action'];kind=action['kind'];key=cause+':'+action['action_id']+':receipt'
        if kind=='ENTRY':
            body=dict(kind='ENTRY_STATUS',position_id=action['position_id'],action_id=action['action_id'],
                      status=order['status'],cumulative_filled_quantity=order['cumulative'])
        else:
            body=dict(kind='RECEIPT',position_id=action['position_id'],action_id=action['action_id'],
                      status=order['status'],cumulative_filled_quantity=order['cumulative'],reduce_only_verified=True)
            if kind in ('ARM_STOP','MOVE_STOP'):
                body.update(stop_price=action['stop_price'],covers_remaining=False,
                    old_stop_retired=kind=='MOVE_STOP',retired_stop_cumulative_filled=order.get('retired_cumulative'),
                    coverage=dict(mode='fixed_quantity',quantity=action['quantity'],
                        quantity_version=action['position_quantity_version'],evidence_id=key,dynamic_contract_id=None))
        message(db,key,body)

    def _details(self,db,order,cause):
        for fid,fact in rows(db,'broker_fills'):
            if fact['action_id']!=order['action']['action_id']: continue
            body=dict(fact)
            body.pop('requested_quantity')
            if body['kind']=='ENTRY_FILL': body['entry_action_id']=body.pop('action_id')
            message(db,cause+':fill:'+fid,body)

    def execute(self,action_id):
        with self.store.transaction() as db:
            item=get(db,'outbox',action_id)
            if item is None: raise OfflineError('Durable intent required before Broker execution')
            action=item['action'];old=get(db,'broker_commands',action_id)
            if old is not None:
                if old['action_digest']!=digest(action): raise OfflineError('Action ID content conflict')
                return 'ALREADY_EXECUTED'
            account=get(db,'account','account');now=account['now']
            kind=action['kind'];target=action.get('target_action_id')
            if kind in ('CANCEL','RECONCILE'):
                order=get(db,'broker_orders',target)
                status='ACCEPTED' if order is not None else 'REJECTED'
                message(db,action_id+':control',dict(kind='RECEIPT',position_id=action['position_id'],
                    action_id=action_id,status=status,cumulative_filled_quantity='0'))
                if order is not None:
                    if kind=='CANCEL' and order['status'] not in TERMINAL:
                        order['status']='CANCELED';put(db,'broker_orders',target,order)
                    self._snapshot(db,order,action_id)
                    self._details(db,order,action_id)
            else:
                if kind not in ('ENTRY','ARM_STOP','MOVE_STOP','TP1','TP2','TP_COMBINED','CLOSE_ALL'):
                    raise OfflineError('Unsupported simulated order capability')
                quantity=amount(action['quantity']);rules=synthetic_rules()
                if quantity<=0 or quantity>rules.max_quantity: raise OfflineError('Invalid simulated quantity')
                exact=action.get('close_exact_remainder',False)
                if exact: raise OfflineError('Off-grid exact remainder capability is not implemented')
                if quantity%rules.quantity_step:
                    raise OfflineError('Quantity precision unsupported')
                if kind=='ENTRY' and (item['origin']!='FIXTURE_EXISTING_POSITION' or not self.store.settings(db).allow_fixtures):
                    # Stage 7 cannot yet supply a compatible full Runner plan.
                    raise OfflineError('NORMAL_ENTRY_CONTRACT_NOT_SUPPORTED_8A')
                order=dict(action=action,status='ACCEPTED',cumulative='0',created_at=now);replaced=None
                if kind=='ENTRY':
                    reservation=get(db,'reservations',action['position_id'])
                    if reservation is None: raise OfflineError('Entry has no atomic reservation')
                    identity=get(db,'identity','identity')
                    if (now>=reservation['expires_at'] or account['paused'] or account['quarantined'] or
                        identity['bundle_digest']!=reservation['bundle_digest']):
                        order['status']='REJECTED'
                if kind in ('ARM_STOP','MOVE_STOP'):
                    if action.get('protection_mode')!='fixed_quantity': raise OfflineError('Dynamic stops unsupported')
                    stop=amount(action['stop_price'])
                    if not stop or stop%rules.price_tick: raise OfflineError('Invalid stop tick')
                    if kind=='MOVE_STOP':
                        replaced=get(db,'broker_orders',action['replaces_action_id'])
                        if replaced is None or replaced['action']['position_id']!=action['position_id']:
                            raise OfflineError('Unknown stop replacement target')
                        old_price=D(replaced['action']['stop_price'])
                        if (action['side']=='SELL' and stop<old_price or action['side']=='BUY' and stop>old_price):
                            raise OfflineError('Stop widening forbidden at Broker')
                        # New order + old retirement + both facts share ONE commit.
                        if replaced['status'] not in TERMINAL: replaced['status']='CANCELED'
                        order['retired_cumulative']=replaced['cumulative']
                        put(db,'broker_orders',action['replaces_action_id'],replaced)
                put(db,'broker_orders',action_id,order)
                self._snapshot(db,order,action_id)
                if replaced is not None:
                    # Canonical atomic-replace ACK confirms BOTH facts first.
                    # A separate old-order snapshot is only later corroboration.
                    self._snapshot(db,replaced,action_id+':retired')
                    self._details(db,replaced,action_id+':retired')
            put(db,'broker_commands',action_id,{'action_digest':digest(action),'at':now})
            return 'EXECUTED'

    def reconcile(self,action_id,cause):
        """Read durable original identity, never mint a replacement order ID."""
        with self.store.transaction() as db:
            command=get(db,'broker_commands',action_id)
            if command is None: return 'CONFIRMED_NOT_EXECUTED'
            order=get(db,'broker_orders',action_id)
            if order is not None:
                self._snapshot(db,order,cause);self._details(db,order,cause)
            return 'EXECUTED'

    def fill(self,action_id,quantity,execution_id,*,defer_details=False,defer_receipt=False):
        """Explicit liquidity event, not a kline's most favorable assumed path."""
        with self.store.transaction() as db:
            key=digest({'order':action_id,'execution':execution_id})
            previous=get(db,'broker_fills',key)
            requested=amount(quantity)
            if previous is not None:
                if D(previous['requested_quantity'])!=requested: raise OfflineError('Execution ID quantity conflict')
                return key
            order=get(db,'broker_orders',action_id)
            if order is None or order['status'] in TERMINAL: raise OfflineError('Order is not executable')
            action=order['action'];account=get(db,'account','account');quote=account['quote']
            if quote is None or quote['bid'] is None or quote['ask'] is None or account['now']-quote['at']>5:
                raise OfflineError('NO_VALID_QUOTE_NO_SYNTHETIC_FILL')
            exit_order=action['kind']!='ENTRY'
            quantity=min(requested,D(action['quantity'])-D(order['cumulative']))
            if exit_order: quantity=min(quantity,inventory(db,action['position_id']))
            exact=action.get('close_exact_remainder',False)
            if not exact: quantity=(quantity/D('.001')).to_integral_value(rounding=ROUND_FLOOR)*D('.001')
            if quantity<=0: raise OfflineError('No remaining reducible quantity')
            side=action['side'];quoted=D(quote['ask'] if side=='BUY' else quote['bid'])
            if action['kind'] in ('ARM_STOP','MOVE_STOP'):
                if side=='SELL' and quoted>D(action['stop_price']) or side=='BUY' and quoted<D(action['stop_price']):
                    raise OfflineError('Stop price not triggered')
            # Adverse slippage; entry/exit costs are explicit frozen assumptions.
            context=get(db,'reservations',action['position_id'])
            policy=context['exit_policy']
            slippage=D(policy['expected_exit_slippage_bps'] if exit_order else str(context['entry_costs']['slippage_bps']))/10000
            price=quoted*(1+slippage if side=='BUY' else 1-slippage)
            tick=synthetic_rules().price_tick
            price=(price/tick).to_integral_value(rounding=ROUND_CEILING if side=='BUY' else ROUND_FLOOR)*tick
            if not exit_order and quantity*price<5: raise OfflineError('Entry minimum notional')
            fee=quantity*price*D(policy['expected_exit_fee_rate'] if exit_order else str(context['entry_costs']['fee_rate']))
            fact=dict(kind='EXIT_FILL' if exit_order else 'ENTRY_FILL',fill_id=key,action_id=action_id,
                position_id=action['position_id'],quantity=str(quantity),price=str(price),fee_usdt=str(fee),
                occurred_at=account['now'],requested_quantity=str(requested))
            put(db,'broker_fills',key,fact)
            total=D(order['cumulative'])+quantity;order['cumulative']=str(total)
            order['status']='FILLED' if total==D(action['quantity']) else 'ACCEPTED'
            if exit_order and inventory(db,action['position_id'])==0 and order['status']!='FILLED': order['status']='CANCELED'
            put(db,'broker_orders',action_id,order)
            if not defer_receipt: self._snapshot(db,order,key)
            if not defer_details: self._details(db,order,key)
            return key

    def fixture_delivery(self,key,payload):
        """Explicit fault-injection ONLY in labelled fixture instances, not JSON trust."""
        with self.store.transaction() as db:
            if not self.store.settings(db).allow_fixtures: raise OfflineError('Fixture fault injection disabled')
            message(db,'fixture-fault:'+key,payload)
