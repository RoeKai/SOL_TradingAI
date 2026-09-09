"""Stage 5 risk semantics supplied by this run's ledger, never external JSON."""
from decimal import Decimal as D

from app.admission.models import PaperRiskSnapshot, ExchangeConstraints
from .storage import get, put, rows
from .models import OfflineError, amount


def snapshot(store,db):
    settings=store.settings(db);a=get(db,'account','account');now=a['now']
    day=now//86400*86400;positions=[];margin=D(0);floating=D(0);float_loss=D(0);missing=False
    outcomes=[];closed=[];trades=set();pending=[];reserved=D(0);pending_margin=D(0)
    for key,p in rows(db,'positions'):
        state=p['checkpoint']['state'];q=D(state['remaining_quantity'])
        if q:
            positions.append(dict(symbol=state['symbol'],side=state['side'],quantity=q))
            cost=D(state['remaining_entry_cost']);margin+=cost/settings.leverage
            quote=a['quote']
            if quote is None or quote['bid'] is None or quote['ask'] is None or now-quote['at']>5:
                missing=True
            else:
                price=D(quote['bid'] if state['side']=='LONG' else quote['ask'])
                pnl=(price*q-cost)*(1 if state['side']=='LONG' else -1)
                floating+=pnl;float_loss+=max(D(0),-pnl)
        if p.get('closed_at') is not None: closed.append((p['closed_at'],key,D(state['realized_net_pnl'])))
    for _,f in rows(db,'fills'):
        if f['kind']=='ENTRY_FILL' and day<=f['occurred_at']<day+86400: trades.add(f['position_id'])
        if f['kind']=='EXIT_FILL' and day<=f['occurred_at']<day+86400: outcomes.append(D(f['net_outcome']))
    for key,r in rows(db,'reservations'):
        if not r['released']:
            reserved+=D(r['risk'])
            if not r['entry_sealed']:
                filled=sum((D(f['quantity']) for _,f in rows(db,'fills') if f['position_id']==key and f['kind']=='ENTRY_FILL'),D(0))
                fraction=max(D(0),1-filled/D(r['quantity']))
                pending_margin+=(D(r['margin'])+D(r['fee_reserve']))*fraction
                if not filled: pending.append(dict(intent_id=r['entry_action_id'],symbol='SOLUSDT',side=r['side']))
    consecutive=0
    for _,_,pnl in sorted(closed,reverse=True):
        if pnl>=0: break
        consecutive+=1
    equity=D(a['cash'])+floating
    return PaperRiskSnapshot(instance_id=settings.instance_id,snapshot_revision=a['version'],mode='paper',
        status='incomplete' if missing or equity<=0 else 'confirmed',source='paper-ledger-snapshot/v1',
        observed_at=now,day_started_at=day,day_ends_at=day+86400,
        equity_usdt=equity if equity>0 else None,available_margin_usdt=max(D(0),equity-margin-pending_margin),
        margin_used_usdt=margin,day_realized_loss_usdt=sum((max(D(0),-v) for v in outcomes),D(0)),
        unrealized_loss_usdt=None if missing else float_loss,reserved_risk_usdt=reserved,
        trades_today=len(trades),consecutive_losses=consecutive,positions=tuple(positions),pending_entries=tuple(pending),
        paused=a['paused'],reconciliation_clear=a['reconciliation_clear'] and not a['quarantined'] and
            not any(not event['consumed'] for _,event in rows(db,'broker_events')),
        margin_mode='ISOLATED',configured_leverage=settings.leverage,auto_add_margin_enabled=False,
        martingale_enabled=False,limits=settings.limits)


def exchange_snapshot(now):
    return ExchangeConstraints(exchange='BINANCE_USDT_M',symbol='SOLUSDT',order_type='MARKET',
        contract_type='linear_usdt',status='confirmed',source='synthetic-broker-rules/8a',observed_at=now,
        quantity_step='.001',min_quantity='.001',max_quantity='1000',min_notional_usdt='5',price_tick='.01',max_leverage=5)


def reserve_fixture(store,db,fixture,policy,plan,rules,entry_costs):
    """Real atomic reservation component exercised by labelled fixtures only.

    It is NOT a capability allowing callers to skip normal contract validation.
    Normal new entries cannot invoke this path in Stage 8A.
    """
    settings=store.settings(db);a=get(db,'account','account');s=snapshot(store,db)
    if not settings.allow_fixtures: raise OfflineError('Explicit fixture instance required')
    identity=get(db,'identity','identity')
    if fixture.bundle_digest!=identity['bundle_digest']: raise OfflineError('CONFIGURATION_BINDING_CHANGED')
    if fixture.expected_revision!=a['version']: raise OfflineError('ACCOUNT_VERSION_CHANGED')
    if a['now']>=fixture.expires_at: raise OfflineError('REQUEST_EXPIRED')
    if a['paused'] or a['quarantined'] or not s.reconciliation_clear: raise OfflineError('ACCOUNT_NOT_READY')
    limits=settings.limits
    if len(s.positions)+len(s.pending_entries)>=limits.max_positions: raise OfflineError('POSITION_SLOT_RESERVED')
    if s.trades_today+len(s.pending_entries)>=limits.max_trades_per_day: raise OfflineError('DAILY_TRADE_LIMIT')
    if s.consecutive_losses>=limits.max_consecutive_losses: raise OfflineError('CONSECUTIVE_LOSS_HALT')
    if s.unrealized_loss_usdt is None or s.equity_usdt is None: raise OfflineError('MISSING_MARK_TO_MARKET')
    remaining=limits.daily_loss_limit_usdt-s.day_realized_loss_usdt-s.unrealized_loss_usdt-s.reserved_risk_usdt
    risk=amount(fixture.risk_budget)
    if risk>limits.max_loss_per_trade_usdt or risk>remaining: raise OfflineError('RISK_BUDGET_EXHAUSTED')
    notional=fixture.quantity*fixture.reference_price;margin=notional/settings.leverage
    fee=notional*D(str(entry_costs['fee_rate']))
    planned_loss=fixture.quantity*abs(fixture.reference_price-fixture.initial_stop)
    planned_cost=fee+fixture.quantity*fixture.initial_stop*policy.expected_exit_fee_rate
    planned_cost+=notional*D(str(entry_costs['slippage_bps']))/10000+fixture.quantity*fixture.initial_stop*policy.expected_exit_slippage_bps/10000
    if risk<planned_loss+planned_cost: raise OfflineError('FIXTURE_RISK_REQUEST_UNDERFUNDED')
    if fixture.quantity%rules.quantity_step or fixture.quantity<rules.min_quantity or notional<rules.min_notional:
        raise OfflineError('FIXTURE_EXCHANGE_QUANTITY_OR_NOTIONAL_INVALID')
    if (fixture.quantity>limits.max_position_quantity or notional>limits.max_position_notional_usdt or
        margin+s.margin_used_usdt>min(limits.max_margin_usdt,s.equity_usdt*limits.max_margin_ratio) or
        margin+fee>s.available_margin_usdt): raise OfflineError('MARGIN_OR_QUANTITY_LIMIT')
    return dict(origin=fixture.origin,side=fixture.side,quantity=str(fixture.quantity),risk=str(risk),margin=str(margin),
        fee_reserve=str(fee),reference_price=str(fixture.reference_price),expires_at=fixture.expires_at,
        bundle_digest=fixture.bundle_digest,account_revision=a['version'],released=False,entry_sealed=False,
        entry_high_water='0',entry_terminal=None,entry_terminal_quantity=None,plan=plan.model_dump(mode='json'),
        entry_costs=entry_costs,
        exit_policy=policy.model_dump(mode='json'),rules=rules.model_dump(mode='json'))
