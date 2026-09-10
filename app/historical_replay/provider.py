"""Candidate descriptions only. No approval or self-certified evidence here.

Same exact-reclaim, nearest swing and five direction checks as the 8B rule.
Historical visibility/volume sampling is explicit; no funding-file access.
"""
from decimal import Decimal as D, ROUND_CEILING
from app.setups.models import TradeSetup, DataCoverage, DataAvailability, COVERAGE_KEYS
from app.configuration.compiler import policy_from, main_values
from app.offline_paper.storage import digest
from app.admitted_paper.provider import number
from .models import HistoricalCandidate
from .data import HistoricalError

VERSION='historical-exact-reclaim-provider/v1'


def describe(bundle,model,run,side,window,level,*,now,budget_notional='500'):
    if side not in ('LONG','SHORT'): raise HistoricalError('SIDE_INVALID')
    policy=policy_from(bundle,'admission');ex=policy_from(bundle,'exit')
    if len(window)<13: raise HistoricalError('INSUFFICIENT_180_SECOND_HISTORY')
    points=[p for p in window if now*1000-p['at_ms']<=300000]
    if any(p['at_ms']>now*1000 for p in points): raise HistoricalError('FUTURE_SAMPLE_FORBIDDEN')
    latest=points[-1]
    for p in points:
        for symbol in ('SOLUSDT','BTCUSDT','ETHUSDT'):
            tick=p['last'].get(symbol)
            if tick is None or tick['available_at_ms']>p['at_ms'] or p['at_ms']-tick['event_time_ms']>=15000:
                raise HistoricalError('REFERENCE_MISSING_OR_STALE')
    baseline=next((p for p in points if p['at_ms']==latest['at_ms']-180000),None)
    if baseline is None: raise HistoricalError('EXACT_THREE_MINUTE_ENDPOINT_MISSING')
    price=lambda p,s='SOLUSDT':D(p['last'][s]['price'])
    sign=1 if side=='LONG' else -1;entry=price(latest);recent=points[-4:]
    if level is None or level['at_ms']>=recent[0]['at_ms'] or entry!=D(level['price']):
        raise HistoricalError('EXACT_PAST_RECLAIM_LEVEL_MISSING')
    if (entry-price(points[-2]))*sign<=0: raise HistoricalError('RECLAIM_DIRECTION_MISSING')
    swings=[]
    for left,mid,right in zip(points,points[1:],points[2:]):
        p=price(mid)
        kind='swing_high' if p>max(price(left),price(right)) else 'swing_low' if p<min(price(left),price(right)) else None
        if kind:
            swings.append(dict(evidence_id='swing:'+str(mid['at_ms']),kind=kind,
                description='Completed 15s samples; right point visible. Raw refs '+str([x['last']['SOLUSDT']['event_id'] for x in (left,mid,right)]),
                status='available',source=VERSION,timeframe='15s_completed_trade_sample',observed_at=right['at_ms']/1000,price=number(p)))
    stop_kind='swing_low' if side=='LONG' else 'swing_high'
    stops=[e for e in swings if e['kind']==stop_kind and (entry-D(str(e['price'])))*sign>0]
    targets=sorted((e for e in swings if e['kind']!=stop_kind and (D(str(e['price']))-entry)*sign>0),key=lambda e:abs(D(str(e['price']))-entry))
    if not stops or len(targets)<2: raise HistoricalError('STRUCTURE_STOP_OR_TWO_TARGETS_MISSING')
    if targets[0]['price']==targets[1]['price']:
        # Keep the accepted nearest-two rule; DO NOT skip a nearby target and
        # choose a farther one to improve RR. Explain the existing invalid pair.
        raise HistoricalError('NEAREST_STRUCTURAL_TARGETS_HAVE_DUPLICATE_PRICE')
    stop=min(stops,key=lambda e:abs(D(str(e['price']))-entry))
    checks=(('four_samples_direction',all((price(b)-price(a))*sign>0 for a,b in zip(recent,recent[1:]))),
        ('btc_direction',(price(recent[-1],'BTCUSDT')-price(recent[0],'BTCUSDT'))*sign>0),
        ('eth_direction',(price(recent[-1],'ETHUSDT')-price(recent[0],'ETHUSDT'))*sign>0),
        ('volume_expansion',D(recent[-1]['volume_sol'])>D(recent[0]['volume_sol'])),('reclaim_direction',True))
    if D(recent[0]['volume_sol'])<=0: raise HistoricalError('WINDOW_VOLUME_MISSING')
    entry_ev=dict(evidence_id='reclaim:'+str(level['at_ms']),kind='range_boundary',description='Exact prior sampled price reclaimed; no tolerance',
        status='available',source=VERSION,timeframe='15s_completed_trade_sample',observed_at=now,price=number(entry))
    risk=main_values(bundle)['risk'];half=model.spread_bps/D(20000)
    evidence=dict(version='historical-visible-evidence/v1',provider=VERSION,provenance=run['observation_provenance'],
        window=points,level=level,direction_checks=checks,dataset_digest=run['dataset_digest'],run_digest=run['content_digest'],
        available_at=now,expires_at=now+5,execution_quotes='MODELLED_TRADE_PRICE_PLUS_ASSUMED_SPREAD',
        funding_budget_basis=dict(model=model.funding_model,rate=str(model.funding_rate_allowance),events=model.funding_event_allowance,
            notional_basis=str(budget_notional),known_before_run=True))
    cost_budget=D(budget_notional)*model.funding_rate_allowance*model.funding_event_allowance
    # Stage 3 has a slippage field but no separate book spread. Budget BOTH
    # conservatively there; the Broker charges spread via quotes only once.
    combined=(model.slippage_bps+model.spread_bps/2+
        model.slippage_bps*model.spread_bps/20000).to_integral_value(rounding=ROUND_CEILING)
    coverage=DataCoverage.from_items(tuple(DataAvailability(name=k,status='not_applicable' if k in ('liquidation_zones','news') else 'available',
        required=k not in ('liquidation_zones','news'),source=VERSION,observed_at=now,
        note='UNMODELLED; not proof of real safety' if k in ('liquidation_zones','news') else
             'ASSUMED pre-run allowance, not final funding' if k=='funding_rate' else 'Past-visible dataset samples; separate instance verification required') for k in COVERAGE_KEYS))
    volatility=(max(price(p) for p in points[-3:])-min(price(p) for p in points[-3:]))/entry*100
    setup=TradeSetup(setup_id='historical:'+digest(evidence)[:24]+':'+side,plan_version=VERSION,symbol='SOLUSDT',strategy_name=VERSION,
        strategy_type='trend_breakout',side=side,structure_evidence=tuple([entry_ev,stop,*targets]),
        entry=dict(order_type='MARKET',reference_price=number(entry),lower_price=number(entry),upper_price=number(entry),basis='structure_zone',evidence_ids=(entry_ev['evidence_id'],)),
        initial_stop=dict(price=stop['price'],basis='nearest confirmed loss-side swing',evidence_ids=(stop['evidence_id'],)),
        invalidation_conditions=(dict(condition_id='stop-invalid',kind='price',description='Swing invalidation',operator='lte' if side=='LONG' else 'gte',price=stop['price'],evidence_ids=(stop['evidence_id'],)),),
        targets=tuple(dict(target_id='structure-'+str(i),price=e['price'],fraction=.5,basis='Confirmed profit-side swing',kind='structure',evidence_ids=(e['evidence_id'],)) for i,e in enumerate(targets[:2],1)),
        confidence=dict(value=sum(x[1] for x in checks)/5,basis='Observed rule fraction, not win probability: '+str(checks)),
        market_state=dict(status='available',regime='trend',source=VERSION,observed_at=latest['last']['SOLUSDT']['event_time_ms']/1000,
            reference_price=number(entry),bid=number(entry*(1-half)),ask=number(entry*(1+half)),
            btc_return_3m_pct=float((price(latest,'BTCUSDT')/price(baseline,'BTCUSDT')-1)*100),
            eth_return_3m_pct=float((price(latest,'ETHUSDT')/price(baseline,'ETHUSDT')-1)*100),
            volume_ratio=float(D(latest['volume_sol'])/D(recent[0]['volume_sol'])),
            volatility_pct=float(volatility)),
        position_limit_advice=dict(max_quantity=number(policy.max_position_quantity),max_notional_usdt=number(policy.max_position_notional_usdt),
            max_margin_usdt=number(policy.max_margin_usdt),max_margin_fraction_of_equity=float(policy.max_margin_ratio),leverage_cap=policy.max_leverage,basis='Unchanged account/policy ceilings'),
        risk_budget=dict(max_loss_usdt=float(risk['max_loss_per_trade']),max_risk_fraction_of_equity=float(policy.max_risk_fraction_of_equity),basis='Configuration, not approval'),
        cost_assumptions=dict(entry_fee_rate=float(model.fee_rate),exit_fee_rate=float(model.fee_rate),
            entry_slippage_bps=float(combined),exit_slippage_bps=float(combined),funding_cost_usdt=number(cost_budget),
            assumed_holding_seconds=float(ex.max_holding_seconds),source=model.funding_model,observed_at=now),
        data_coverage=coverage,created_at=now,data_as_of=now,valid_until=now+5,
        compatibility_notes=('Historical observations; assumed quotes/fees/rules; this candidate is not an approval',))
    c=HistoricalCandidate(candidate_id='0'*64,provenance='SYNTHETIC_TEST_OBSERVATIONS_ASSUMED_EXECUTION' if
        run['observation_provenance']=='SYNTHETIC_TEST_NOT_HISTORICAL' else 'HISTORICAL_OBSERVATIONS_ASSUMED_EXECUTION',
        setup=setup,evidence=evidence,bundle_digest=bundle.bundle_digest,
        dataset_digest=run['dataset_digest'],run_digest=run['content_digest'])
    return c.model_copy(update={'candidate_id':digest(c)})
