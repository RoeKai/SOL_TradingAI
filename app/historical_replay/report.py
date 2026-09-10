"""Metrics describe this assumed execution experiment, not strategy validation."""
from decimal import Decimal as D, Context, localcontext
import json
from app.offline_paper.storage import get, rows
from .storage import hget, hrows
from .funding import snapshot, net_funding


def result(paper):
    with localcontext(Context(prec=50)),paper.store.transaction() as db:
        run=paper.store.run(db);cursor=hget(db,'history_cursor','cursor');a=get(db,'account','account')
        risk=snapshot(paper.store,db);initial=paper.store.settings(db).initial_balance
        equity=risk.equity_usdt;funding={}
        for _,f in hrows(db,'history_funding'):
            for leg in f['legs']: funding[leg['position_id']]=funding.get(leg['position_id'],D(0))+D(leg['cash_usdt'])
        trades=[];gross=fees=unrealized=D(0);positions=[]
        for pid,p in rows(db,'positions'):
            s=p['checkpoint']['state'];gross+=D(s['realized_gross_pnl']);fees+=D(s['entry_fees'])+D(s['exit_fees'])
            net=D(s['realized_net_pnl'])+funding.get(pid,D(0));r=get(db,'reservations',pid)
            record=dict(position_id=pid,side=s['side'],remaining_quantity=s['remaining_quantity'],remaining_cost=s['remaining_entry_cost'],
                gross_pnl=s['realized_gross_pnl'],trade_fees=str(D(s['entry_fees'])+D(s['exit_fees'])),funding_cash=str(funding.get(pid,D(0))),
                net_realized=str(net),opened_at=s['opened_at'],closed_at=p.get('closed_at'),approval_id=r['approval_id'],
                holding_seconds=None if p.get('closed_at') is None else p['closed_at']-s['opened_at'],
                evidence_chain=dict(exit_plan_id=r['exit_plan_id'],entry_action_id=r['entry_action_id'],
                    action_ids=[x['action_id'] for x in s['actions']],fill_ids=[x['fill_id'] for x in s['fill_facts']]))
            positions.append(record)
            if p.get('closed_at') is not None and D(s['remaining_quantity'])==0: trades.append(record)
        wins=[D(t['net_realized']) for t in trades if D(t['net_realized'])>0]
        losses=[-D(t['net_realized']) for t in trades if D(t['net_realized'])<0]
        peak=initial;max_dd=D(0);equity_points=0;missing_equity=0
        for row in db.execute('SELECT payload FROM history_equity ORDER BY id'):
            point=json.loads(row[0]);value=point['equity']
            if value is None: missing_equity+=1;continue
            value=D(value);peak=max(peak,value);max_dd=max(max_dd,(peak-value)/peak);equity_points+=1
        if equity is not None: peak=max(peak,equity);max_dd=max(max_dd,(peak-equity)/peak)
        sequence=longest=0
        for t in sorted(trades,key=lambda t:(t['closed_at'],t['position_id'])):
            sequence=sequence+1 if D(t['net_realized'])<0 else 0;longest=max(longest,sequence)
        spread=slippage=D(0)
        for row in db.execute("SELECT payload FROM history_execution WHERE id LIKE 'fill:%'"):
            f=json.loads(row[0]);spread+=D(f['spread_cost_usdt']);slippage+=D(f['slippage_and_rounding_cost_usdt'])
        counts={name:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for name,table in
            (('simulated_orders','broker_orders'),('confirmed_fills','fills'),('candidates','history_candidates'),('approval_records','history_approvals'),('consumed_approvals','history_consumptions'))}
        all_reasons={}
        for row in db.execute('SELECT payload FROM history_signals'):
            for reason in set(json.loads(row[0])['reason_codes']): all_reasons[reason]=all_reasons.get(reason,0)+1
        metrics=dict(cash_usdt=a['cash'],equity_usdt=None if equity is None else str(equity),
            unrealized_pnl_usdt=None if equity is None else str(equity-D(a['cash'])),
            net_return=None if equity is None else str((equity-initial)/initial),
            maximum_equity_drawdown=str(max_dd),equity_points=equity_points,missing_equity_points=missing_equity,
            completed_positions=len(trades),win_rate=None if not trades else str(D(len(wins))/len(trades)),
            average_win_loss_ratio=None if not wins or not losses else str((sum(wins)/len(wins))/(sum(losses)/len(losses))),
            profit_factor=None if not losses else str(sum(wins,D(0))/sum(losses)),
            gross_realized_pnl=str(gross),trade_fees_usdt=str(fees),funding_net_cash_usdt=str(net_funding(db)),
            modeled_spread_cost_usdt=str(spread),modeled_slippage_and_rounding_cost_usdt=str(slippage),
            average_holding_seconds=None if not trades else str(sum((D(str(t['holding_seconds'])) for t in trades),D(0))/len(trades)),
            maximum_holding_seconds=max((t['holding_seconds'] for t in trades),default=None),max_consecutive_losses=longest)
        sides={side:dict(completed=sum(t['side']==side for t in trades),net_realized=str(sum((D(t['net_realized']) for t in trades if t['side']==side),D(0)))) for side in ('LONG','SHORT')}
        size=sum(p.stat().st_size for p in paper.store.root.iterdir() if p.is_file())
        return dict(version='historical-report/v1',scope=run['scope'],experiment_id=run['experiment_id'],run_digest=run['content_digest'],
            dataset_digest=cursor['dataset_digest'],finished=cursor['finished'],completed_through_ms=cursor['at_ms'],
            evaluation_range_ms=run['evaluation_range_ms'],warmup_range_ms=run['warmup_range_ms'],
            consumed_market_records=cursor['event_count'],consumed_by_symbol=cursor.get('record_counts',{}),
            dropped_records=cursor.get('explicitly_dropped',0),market_prefix_digest=cursor['prefix'],
            counts=counts,metrics=metrics,sides=sides,statistics=hget(db,'history_meta','statistics'),
            reason_occurrences_not_independent_trades=all_reasons,positions=positions,
            best=sorted(trades,key=lambda t:D(t['net_realized']),reverse=True)[:3],worst=sorted(trades,key=lambda t:D(t['net_realized']))[:3],
            performance=hget(db,'history_meta','performance'),database_and_sidecar_bytes=size,
            model_limit=hget(db,'history_meta','model_limit'),reconciliation_clear=a['reconciliation_clear'],
            limitations=['Sample metrics with zero denominator are null (NA), not favorable ratios',
                'Zero trades or zero drawdown does not prove strategy safety',
                'Observed aggregate trades, not actual book liquidity or historical account execution',
                'Tick/step/minimum/fee/rule capabilities are explicit assumptions, not dated exchange snapshots',
                'Original Broker requires >=5 USDT per entry partial fill; stricter than order-only minimum interpretation',
                'Funding budget is conservative ex ante; actual funding cash differs and never rewrites approval',
                'No liquidation, maintenance-margin schedule, ADL, probabilities or statistical expectation'],
            strategy_effectiveness_verified=False,real_runtime_connected=False,live_allowed=False)
