# 8D versioned adapter; based on accepted 8C code. Identity remains exact.
"""Signed funding cash ledger; never an ExitFill or a rewritten RR result."""
from decimal import Decimal as D
from app.offline_paper.storage import get, put, rows, digest
from app.offline_paper.risk import snapshot as trade_snapshot, exchange_snapshot as paper_exchange
from .storage import hget, hput, hrows
from app.historical_replay.data import HistoricalError

RISK_SOURCE='historical-paper-ledger-with-funding/v1'


def net_funding(db):
    meta=hget(db,'history_meta','funding')
    facts=hrows(db,'history_funding')
    total=sum((D(f['cash_usdt']) for _,f in facts),D(0))
    if meta is None or total!=D(meta['net_cash']) or len(facts)!=meta['count']:
        raise HistoricalError('FUNDING_LEDGER_CONSERVATION_FAILED')
    return total


def snapshot(store,db):
    old=trade_snapshot(store,db)
    facts=hrows(db,'history_funding');day=int(old.day_started_at)
    loss=sum((max(D(0),-D(f['cash_usdt'])) for _,f in facts if day*1000<=f['at_ms']<(day+86400)*1000),D(0))
    by_position={}
    for _,f in facts:
        for leg in f['legs']:
            pid=leg['position_id'];by_position[pid]=by_position.get(pid,D(0))+D(leg['cash_usdt'])
    closed=[]
    for pid,p in rows(db,'positions'):
        if p.get('closed_at') is not None:
            closed.append((p['closed_at'],pid,D(p['checkpoint']['state']['realized_net_pnl'])+by_position.get(pid,D(0))))
    consecutive=0
    for _,_,pnl in sorted(closed,reverse=True):
        if pnl>=0: break
        consecutive+=1
    # The base snapshot already uses current cash INCLUDING settled funding.
    # Keep reserved stop budget until lifecycle settlement (conservative overlap
    # with paid costs), never release it just because a debit has occurred.
    return old.model_copy(update={'source':RISK_SOURCE,'day_realized_loss_usdt':old.day_realized_loss_usdt+loss,
        'consecutive_losses':consecutive})


def exchange_snapshot(now):
    return paper_exchange(now).model_copy(update={'source':'assumed-historical-paper-rules/v1'})


def settle(paper,db,fact):
    active=hget(db,'history_meta','active_funding')
    if active!=fact: raise HistoricalError('FUNDING_REQUIRES_CURRENT_VERIFIED_DATASET_EVENT')
    cursor=hget(db,'history_cursor','cursor')
    if fact['at_ms']!=cursor['at_ms']: raise HistoricalError('FUNDING_TIME_NOT_CURRENT')
    key=digest({'dataset':cursor['dataset_digest'],'time_ms':fact['at_ms'],'symbol':'SOLUSDT'})
    old=hget(db,'history_funding',key)
    if old is not None:
        if old['fact_digest']!=digest(fact): raise HistoricalError('FUNDING_ID_CONTENT_CONFLICT')
        return old
    legs=[]
    for pid,p in rows(db,'positions'):
        s=p['checkpoint']['state'];q=D(s['remaining_quantity'])
        if q<=0: continue
        sign=1 if s['side']=='LONG' else -1
        cash=-sign*q*D(fact['mark_price'])*D(fact['rate'])
        legs.append(dict(position_id=pid,side=s['side'],quantity=str(q),cash_usdt=str(cash)))
    total=sum((D(x['cash_usdt']) for x in legs),D(0))
    value=dict(version='historical-funding-cash/v1',funding_id=key,fact_digest=digest(fact),at_ms=fact['at_ms'],
        rate=fact['rate'],mark_price=fact['mark_price'],source_digest=fact['source_digest'],
        provenance='OFFICIAL_HISTORICAL_SETTLEMENT_ON_SIMULATED_INVENTORY',legs=legs,cash_usdt=str(total))
    hput(db,'history_funding',key,value)
    meta=hget(db,'history_meta','funding');meta['net_cash']=str(D(meta['net_cash'])+total);meta['count']+=1
    hput(db,'history_meta','funding',meta)
    paper._cash(db);paper._assert_cash(db)
    # Actual signed settlements always enter cash first. Only debits consume
    # the frozen allowance; credits cannot finance new risk or cancel breaches.
    for leg in legs:
        pid=leg['position_id'];r=get(db,'reservations',pid)
        if r is None or not r.get('approval_id'): raise HistoricalError('FUNDING_APPROVAL_BINDING_MISSING')
        approval=hget(db,'history_approvals',r['approval_id'])
        if approval is None: raise HistoricalError('FUNDING_APPROVAL_BINDING_MISSING')
        budget=D(approval['body']['lineage']['materialization']['funding_budget_usdt'])
        debit=sum((max(D(0),-D(x['cash_usdt'])) for _,f in hrows(db,'history_funding') for x in f['legs'] if x['position_id']==pid),D(0))
        if debit>budget and hget(db,'history_meta','funding-limit:'+pid) is None:
            hput(db,'history_meta','funding-limit:'+pid,dict(version='funding-budget-breach/v1',at_ms=fact['at_ms'],
                position_id=pid,approval_id=r['approval_id'],budget_usdt=str(budget),actual_debits_usdt=str(debit),
                reason_code='ACTUAL_FUNDING_DEBIT_EXCEEDS_FROZEN_BUDGET',new_entries_paused=True,existing_protection_continues=True))
            account=get(db,'account','account');account['paused']=True;account['version']+=1;put(db,'account','account',account)
    return value
