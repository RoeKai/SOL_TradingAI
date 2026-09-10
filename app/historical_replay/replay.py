"""Streaming historical clock, atomic cursor/effects and bounded observations.

The ingestor accepts seekable validated dataset records, not self-attested plan
JSON. Every 15s sample is a completed [t-15s,t) prefix. Original observation
times remain unchanged. Funding is ordered before market and then sampling
when timestamps coincide, as frozen in the run manifest.
"""
from decimal import Decimal as D, ROUND_FLOOR
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from app.utils.paths import read_text_nofollow
from app.offline_paper.storage import get, put, rows, digest
from app.offline_paper.models import Quote
from app.exits.models import MarketEvent
from .storage import hget, hput
from .data import funding_records, HistoricalError, seal, verify_seal, START, END, WARMUP
from .provider import describe
from .funding import settle, snapshot
from .index import merged_events


def saved_cursor(db,cursor):
    value={k:v for k,v in cursor.items() if k!='content_digest'}
    hput(db,'history_cursor','cursor',seal(value))


def quote_from(event,model):
    p=D(event['price']);half=model.spread_bps/20000
    return dict(event_id=event['event_id'],at=event['event_time_ms']/1000,bid=str(p*(1-half)),ask=str(p*(1+half)))


def _counts(stats,at,category,side=None):
    day=datetime.fromtimestamp(at/1000,timezone.utc).strftime('%Y-%m-%d')
    stats['categories'][category]=stats['categories'].get(category,0)+1
    daily=stats['days'].setdefault(day,{'samples':0,'categories':{},'candidates':0,'accepted':0})
    daily['categories'][category]=daily['categories'].get(category,0)+1
    if side is not None:
        s=stats.setdefault('by_side',{}).setdefault(side,{})
        s[category]=s.get(category,0)+1


class Replay:
    def __init__(self,paper):
        self.paper=paper;self.store=paper.store
        with self.store.transaction() as db:
            self.run=self.store.run(db);self.model=self.store.model(db);self.bundle=self.store.bundle(db)
        self.evaluations=0

    def _set_clock(self,db,cursor,at):
        if at<cursor['at_ms']: raise HistoricalError('LOGICAL_TIME_BACKWARDS')
        cursor['at_ms']=at;hput(db,'history_cursor','cursor',cursor)
        a=get(db,'account','account')
        seconds=at/1000
        if seconds<a['now'] and at!=round(a['now']*1000): raise HistoricalError('ACCOUNT_TIME_BACKWARDS')
        # Exact original milliseconds remain in the cursor/events. The modeled
        # quote clock has an ordinal for same-ms observations so the accepted
        # reducer does not discard a later aggTrade sharing its timestamp.
        a['now']=seconds if seconds>a['now'] else math.nextafter(a['now'],math.inf)
        cursor['logical_quote_time']=a['now'];put(db,'account','account',a)

    def _active(self,db):
        return any(not r['released'] for _,r in rows(db,'reservations'))

    def _protect(self,db,event):
        """No new-entry gate can prevent an existing position's management."""
        at=get(db,'account','account')['now'];quote=quote_from(event,self.model)
        for pid,p in rows(db,'positions'):
            s=p['checkpoint']['state']
            if D(s['remaining_quantity'])<=0: continue
            # Preserve every meaningful changed quote. Repeated identical
            # within-second ticks cannot change the deterministic reducer.
            previous=hget(db,'history_execution','last-protection:'+pid)
            visible=dict(at=at,bid=quote['bid'],ask=quote['ask'])
            if previous==visible: continue
            self.paper._event(db,MarketEvent(event_id='historical-market:'+event['event_id']+':'+pid,
                position_id=pid,received_at=at,observed_at=at,bid=quote['bid'],ask=quote['ask'],confirmed=True))
            hput(db,'history_execution','last-protection:'+pid,visible)

    def _fills(self,db,event):
        available=(D(event['quantity'])*self.model.participation/D('.001')).to_integral_value(rounding=ROUND_FLOOR)*D('.001')
        if available<=0: return
        orders=rows(db,'broker_orders')
        # Previously accepted protection gets first use of this one finite
        # liquidity budget when stop and TP compete. No beneficial OHLC path.
        def priority(pair):
            k,o=pair;kind=o['action']['kind']
            return (0 if kind in ('ARM_STOP','MOVE_STOP','CLOSE_ALL') else 1 if kind!='ENTRY' else 2,o['created_at'],k)
        for k,o in sorted(orders,key=priority):
            if o['status'] in ('CANCELED','FILLED','REJECTED'): continue
            timing=hget(db,'history_execution','order:'+k)
            if timing is None or timing.get('accepted_at_ms') is None or event['available_at_ms']<=timing['accepted_at_ms'] or event['event_time_ms']<=timing['submitted_at_ms']:
                continue
            action=o['action'];kind=action['kind'];quote=get(db,'account','account')['quote']
            if kind in ('ARM_STOP','MOVE_STOP'):
                price=D(quote['bid'] if action['side']=='SELL' else quote['ask'])
                if (action['side']=='SELL' and price>D(action['stop_price']) or action['side']=='BUY' and price<D(action['stop_price'])): continue
            q=min(available,D(action['quantity'])-D(o['cumulative']))
            q=(q/D('.001')).to_integral_value(rounding=ROUND_FLOOR)*D('.001')
            if q<=0: continue
            # Inherited 8A Broker requires this fill's entry notional >=5.
            # This is a disclosed stricter simulation limitation, not claiming
            # actual venue partial-fill minimum semantics.
            if kind=='ENTRY' and q*D(quote['bid'])<5: continue
            fid=self.paper.broker.fill(k,str(q),event['event_id'])
            actual=D(get(db,'broker_fills',fid)['quantity']);available-=actual
            self.paper.pump()
            if available<D('.001'): break

    def _model_limit(self,db,at):
        a=get(db,'account','account');q=a['quote']
        if q is None: return
        funding_by={}
        from .storage import hrows
        for _,f in hrows(db,'history_funding'):
            for leg in f['legs']: funding_by[leg['position_id']]=funding_by.get(leg['position_id'],D(0))+D(leg['cash_usdt'])
        for pid,p in rows(db,'positions'):
            s=p['checkpoint']['state'];quantity=D(s['remaining_quantity'])
            if quantity<=0: continue
            cost=D(s['remaining_entry_cost']);price=D(q['bid'] if s['side']=='LONG' else q['ask'])
            floating=(price*quantity-cost)*(1 if s['side']=='LONG' else -1)
            isolated=cost/self.store.settings(db).leverage+floating+min(D(0),funding_by.get(pid,D(0)))
            if isolated<=0:
                hput(db,'history_meta','model_limit',dict(reason_code='MODEL_LIMIT_EXCEEDED',at_ms=at,position_id=pid,
                    basis='isolated allocated margin exhausted; maintenance/liquidation/ADL not modeled',valid_after=False))
                a['paused']=True;put(db,'account','account',a)

    def trade(self,db,cursor,event,*,active):
        if event['available_at_ms']<cursor['at_ms']: raise HistoricalError('MARKET_STREAM_BACKWARDS')
        previous=cursor.setdefault('sequences',{}).get(event['symbol'])
        signature=[event['raw_sequence'],event['event_time_ms'],event['price'],event['quantity']]
        duplicate=previous is not None and signature[0]==previous[0]
        if duplicate and signature!=previous: raise HistoricalError('DUPLICATE_ID_CONTENT_CONFLICT')
        if previous is not None and signature[0]<previous[0]: raise HistoricalError('MARKET_ID_BACKWARDS')
        if previous is not None and signature[0]>previous[0]+1: raise HistoricalError('UNDECLARED_MARKET_SEQUENCE_GAP')
        cursor['by_file'][event['stream_file']]=event['next_offset'];cursor['event_count']+=1
        cursor['at_ms']=event['available_at_ms'];cursor['sequences'][event['symbol']]=signature
        cursor['prefix']=hashlib.sha256((cursor['prefix']+'|'+event['event_id']+'|'+str(event['event_time_ms'])+'|'+event['price']+'|'+event['quantity']).encode()).hexdigest()
        counts=cursor.setdefault('record_counts',{})
        counts[event['symbol']]=counts.get(event['symbol'],0)+1
        if duplicate:
            cursor['duplicates']=cursor.get('duplicates',0)+1;return active
        missing=self.run['missing_data']
        if missing and event['symbol']==missing['symbol'] and missing['start_ms']<=event['event_time_ms']<missing['end_ms']:
            cursor['explicitly_dropped']=cursor.get('explicitly_dropped',0)+1;return active
        cursor['last'][event['symbol']]=event
        cursor['volume'][event['symbol']]=str(D(cursor['volume'].get(event['symbol'],'0'))+D(event['quantity']))
        if event['symbol']!='SOLUSDT' or not active: return active
        self._set_clock(db,cursor,event['available_at_ms'])
        a=get(db,'account','account');a['quote']=quote_from(event,self.model);put(db,'account','account',a)
        hput(db,'history_meta','active_market',event);hput(db,'history_meta','liquidity',dict(event_id=event['event_id'],used='0'))
        self.paper.pump();self._fills(db,event);self._protect(db,event);self.paper.pump()
        self._model_limit(db,event['available_at_ms'])
        return self._active(db)

    def sample(self,db,cursor,at):
        self._set_clock(db,cursor,at)
        # Copy: later market events must not mutate an earlier sample/evidence.
        sample=dict(at_ms=at,last=dict(cursor['last']),volume_sol=cursor['volume'].get('SOLUSDT','0'))
        cursor['volume']={};cursor['window']=[*cursor['window'],sample][-21:]
        hput(db,'history_samples',str(at),sample)
        # Keep most recent ELIGIBLE prior exact level, excluding last 4 points.
        if len(cursor['window'])>=5:
            eligible=cursor['window'][-5];sol=eligible['last'].get('SOLUSDT')
            if sol: hput(db,'history_levels',sol['price'],dict(at_ms=eligible['at_ms'],price=sol['price'],event_id=sol['event_id']))
        a=get(db,'account','account');last=cursor['last'].get('SOLUSDT')
        if last is not None: a['quote']=quote_from(last,self.model)
        a['version']+=1;put(db,'account','account',a)
        self.paper.pump()
        cursor['next_sample_ms']=at+15000;hput(db,'history_cursor','cursor',cursor)
        if at<START: return
        stats=hget(db,'history_meta','statistics');day=datetime.fromtimestamp(at/1000,timezone.utc).strftime('%Y-%m-%d')
        daily=stats['days'].setdefault(day,{'samples':0,'categories':{},'candidates':0,'accepted':0});daily['samples']+=1
        missing=[s for s in ('SOLUSDT','BTCUSDT','ETHUSDT') if s not in sample['last'] or at-sample['last'][s]['event_time_ms']>=15000]
        if missing: daily['stale_samples']=daily.get('stale_samples',0)+1
        else: daily['complete_samples']=daily.get('complete_samples',0)+1
        for side in ('LONG','SHORT'):
            try:
                level=hget(db,'history_levels',last['price']) if last else None
                c=describe(self.bundle,self.model,self.run,side,cursor['window'],level,now=at//1000)
            except (ValueError,ArithmeticError) as error:
                _counts(stats,at,str(error),side);continue
            hput(db,'history_candidates',c.candidate_id,c);stats['candidates']+=1;daily['candidates']+=1
            request_id='market-plan:'+c.candidate_id
            if not self.paper.ready:
                reason=['RECOVERY_OR_PROTECTION_NOT_READY'];outcome='REJECT';approval=None
            else:
                approval=self.paper.prepare(c.candidate_id,request_id)
                result=self.paper.submit(approval['approval_id'])
                reason=result['reason_codes'];outcome=result['result']
            category='ACCEPTED' if outcome!='REJECT' else reason[0]
            _counts(stats,at,category,side)
            if outcome!='REJECT': daily['accepted']+=1
            hput(db,'history_signals',c.candidate_id,dict(at_ms=at,side=side,result=outcome,reason_codes=reason,
                approval_id=None if approval is None else approval['approval_id'],candidate_id=c.candidate_id))
        hput(db,'history_meta','statistics',stats)
        risk=snapshot(self.store,db)
        hput(db,'history_equity',str(at),dict(at_ms=at,cash=get(db,'account','account')['cash'],
            equity=None if risk.equity_usdt is None else str(risk.equity_usdt),unrealized_loss=None if risk.unrealized_loss_usdt is None else str(risk.unrealized_loss_usdt),
            funding_net_cash=hget(db,'history_meta','funding')['net_cash'],risk_revision=risk.snapshot_revision))
        self._model_limit(db,at)

    def funding(self,db,cursor,event):
        self._set_clock(db,cursor,event['at_ms']);hput(db,'history_meta','active_funding',event)
        self.paper.pump();settle(self.paper,db,event)
        cursor['funding_index']=cursor.get('funding_index',0)+1
        self._model_limit(db,event['at_ms'])

    def run_stream(self,root,index,*,max_events=None,fault=None):
        start=time.perf_counter()
        with self.store.transaction() as db:
            cursor=hget(db,'history_cursor','cursor')
            if 'content_digest' in cursor: verify_seal(cursor)
            elif cursor['event_count']: raise HistoricalError('UNSEALED_CURSOR')
            if cursor['finished']: return {'already_finished':True,'event_count':cursor['event_count']}
            dataset=hget(db,'history_meta','dataset')
            if index['dataset_digest']!=dataset['content_digest']: raise HistoricalError('REPLAY_INDEX_DATASET_MISMATCH')
        funding_file=next(f for f in dataset['files'] if f['kind']=='fundingRate')
        # Only the event scheduler reads the file. Provider receives NO funding
        # array/object/next rate; current event enters ledger at settlement only.
        from pathlib import Path
        fund=[dict(f,at_ms=f['event_time_ms'],source_digest=funding_file['sha256']) for f in
            funding_records(json.loads(read_text_nofollow(Path(root)/funding_file['file'])))]
        stream=iter(merged_events(root,index,self.model,cursor));current=next(stream,None)
        processed=0;end=self.run['evaluation_range_ms'][1];done=False
        while not done:
            with self.store.transaction() as db:
                if get(db,'identity','identity')['bundle_digest']!=self.bundle.bundle_digest:
                    raise HistoricalError('CONFIG_CHANGED_DURING_REPLAY')
                active=self._active(db)
                for _ in range(5000):
                    tick_at=current['available_at_ms'] if current is not None else end
                    fi=cursor.get('funding_index',0);f=fund[fi] if fi<len(fund) else None
                    fund_at=f['at_ms'] if f else end
                    sample_at=cursor.get('next_sample_ms',WARMUP+15000)
                    earliest=min(tick_at,fund_at,sample_at)
                    if earliest>=end:
                        cursor['finished']=True;done=True;break
                    if fund_at==earliest:
                        self.funding(db,cursor,f);self.paper._crash('before_funding_cursor_commit',fault)
                    elif tick_at==earliest:
                        active=self.trade(db,cursor,current,active=active);processed+=1
                        current=next(stream,None)
                    else:
                        self.sample(db,cursor,sample_at);active=self._active(db)
                    if active and hget(db,'history_meta','model_limit') is not None:
                        cursor['invalid_after_ms']=cursor['at_ms'];done=True;break
                    if max_events is not None and processed>=max_events: done=True;break
                saved_cursor(db,cursor)
                self.paper._crash('before_market_cursor_commit',fault)
            self.paper._crash('after_market_cursor_commit',fault)
            if processed and processed%500000<5000:
                print(json.dumps({'progress_events':cursor['event_count'],'at_ms':cursor['at_ms'],'seconds':round(time.perf_counter()-start,3)}),flush=True)
        elapsed=time.perf_counter()-start
        with self.store.transaction() as db:
            prior=hget(db,'history_meta','performance') or {'active_seconds':0,'segments':[]}
            prior['active_seconds']+=elapsed;prior['segments'].append(dict(events=processed,seconds=elapsed,end_at_ms=cursor['at_ms']))
            hput(db,'history_meta','performance',prior)
        return dict(event_count=cursor['event_count'],new_events=processed,at_ms=cursor['at_ms'],finished=cursor['finished'],seconds=elapsed)
