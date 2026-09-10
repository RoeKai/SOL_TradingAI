"""Instance-owned, replayable synthetic evidence; no external assertion of trust."""
from decimal import Decimal as D
from app.setups.models import TradeSetup, DataCoverage, DataAvailability, COVERAGE_KEYS
from app.admission.models import AdmissionRequest, fingerprint
from app.configuration.compiler import main_values, policy_from
from app.offline_paper.storage import get, put, rows, digest
from app.offline_paper.models import OfflineError
from .models import MarketInput, EvidenceSnapshot, Candidate, PROVIDER
from .storage import provider_record


def number(value):
    result=float(value)
    if D(str(result))!=D(str(value)):
        raise OfflineError('STAGE3_NUMBER_REPRESENTATION_UNSUPPORTED')
    return result


def registry(store,db):
    expected=provider_record(store.instance_id)
    if get(db,'paper_providers',PROVIDER)!=dict(expected,digest=digest(expected)):
        raise OfflineError('PROVIDER_REGISTRATION_MISSING_OR_CHANGED')
    return expected


def ingest(store,db,item):
    registry(store,db)
    if type(item) is not MarketInput: raise OfflineError('Typed synthetic observations only; no verified/source override')
    a=get(db,'account','account')
    if item.available_at>a['now']: raise OfflineError('FUTURE_INPUT_NOT_AVAILABLE')
    existing=rows(db,'paper_inputs');key=str(item.sequence)
    old=get(db,'paper_inputs',key)
    if old is not None:
        if old['input']!=item.model_dump(mode='json'): raise OfflineError('INPUT_SEQUENCE_CONTENT_COLLISION')
        return old['digest']
    previous='0'*64
    if existing:
        last=existing[-1][1];prior=MarketInput.model_validate(last['input'])
        if item.sequence!=prior.sequence+1 or item.available_at<prior.available_at or item.observed_at<prior.observed_at:
            raise OfflineError('INPUT_SEQUENCE_OR_TIME_BACKWARDS')
        previous=last['digest']
    elif item.sequence!=1: raise OfflineError('INPUT_CHAIN_MUST_START_AT_ONE')
    payload=dict(input=item.model_dump(mode='json'),previous_digest=previous,provider=PROVIDER,instance_id=store.instance_id)
    put(db,'paper_inputs',key,dict(payload,digest=digest(payload)))
    return digest(payload)


def chain(store,db,now,*,limit=None,check_registry=True):
    if check_registry: registry(store,db)
    result=[];previous='0'*64;last_at=last_observed=0
    stored=rows(db,'paper_inputs')
    if limit is not None:
        if limit>len(stored): raise OfflineError('EVIDENCE_PREFIX_MISSING')
        stored=stored[:limit]
    for index,(key,row) in enumerate(stored,1):
        obj=MarketInput.model_validate(row['input'])
        payload={k:v for k,v in row.items() if k!='digest'}
        if (row['digest']!=digest(payload) or row['previous_digest']!=previous or
            row['provider']!=PROVIDER or row['instance_id']!=store.instance_id or str(index)!=key or
            obj.sequence!=index or obj.available_at<last_at or obj.observed_at<last_observed or obj.available_at>now):
            raise OfflineError('INPUT_CHAIN_CORRUPT_OR_FUTURE')
        previous=row['digest'];last_at=obj.available_at;last_observed=obj.observed_at;result.append(obj)
    return result,previous


def build_candidate(store,db,side,now,*,input_count=None,bound_bundle=None,check_registry=True):
    if side not in ('LONG','SHORT'): raise OfflineError('Unsupported direction')
    observations,head=chain(store,db,get(db,'account','account')['now'],limit=input_count,check_registry=check_registry)
    if any(p.available_at>now for p in observations): raise OfflineError('FUTURE_EVIDENCE_IN_PLAN')
    if len(observations)<10: raise OfflineError('INSUFFICIENT_PAST_OBSERVATIONS')
    bundle=store.bundle(db) if bound_bundle is None else bound_bundle
    policy=policy_from(bundle,'admission');ex=policy_from(bundle,'exit')
    values=main_values(bundle);latest=observations[-1];recent=observations[-4:]
    if now-latest.observed_at>=policy.max_market_age_seconds: raise OfflineError('MARKET_EVIDENCE_STALE')
    if ex.max_holding_seconds>3600: raise OfflineError('SYNTHETIC_FUNDING_HORIZON_UNSUPPORTED')
    sign=1 if side=='LONG' else -1;price=latest.sol;swings=[]
    for left,mid,right in zip(observations,observations[1:],observations[2:]):
        if now-mid.observed_at>=policy.max_structure_age_seconds: continue
        kind='swing_high' if mid.sol>max(left.sol,right.sol) else 'swing_low' if mid.sol<min(left.sol,right.sol) else None
        if kind:
            swings.append((dict(evidence_id='swing-'+str(mid.sequence),kind=kind,
                description='Confirmed strict three-point '+kind+'; right neighbour available at '+str(right.available_at),
                status='available',source=PROVIDER,timeframe='synthetic_tick',observed_at=float(right.available_at),price=number(mid.sol)),
                (left.sequence,mid.sequence,right.sequence)))
    stop_kind='swing_low' if sign==1 else 'swing_high'
    stops=[s for s in swings if s[0]['kind']==stop_kind and (price-D(str(s[0]['price'])))*sign>0]
    targets=sorted([s for s in swings if s[0]['kind']!=stop_kind and (D(str(s[0]['price']))-price)*sign>0],
        key=lambda s:abs(D(str(s[0]['price']))-price))
    if not stops or len(targets)<2: raise OfflineError('STRUCTURE_STOP_OR_TWO_TARGETS_MISSING')
    stop=min(stops,key=lambda s:abs(price-D(str(s[0]['price']))))
    # A past observed level reclaimed in the chosen direction. This path requires
    # an exact point reference; widening to obtain eligibility is not permitted.
    levels=[p for p in observations[:-4] if p.sol==price]
    if not levels or (price-recent[-2].sol)*sign<=0: raise OfflineError('OBSERVED_RECLAIM_LEVEL_MISSING')
    level=levels[-1]
    entry_evidence=dict(evidence_id='reclaimed-'+str(level.sequence),kind='range_boundary',
        description='Reclaim of previously observed level at sequence '+str(level.sequence),status='available',
        source=PROVIDER,timeframe='synthetic_tick',observed_at=float(latest.available_at),price=number(price))
    chosen=[stop,*targets]
    evidence=[entry_evidence,*[p[0] for p in chosen]]
    checks=(('four_observations_in_direction',all((b.sol-a.sol)*sign>0 for a,b in zip(recent,recent[1:]))),
        ('btc_same_direction',(recent[-1].btc-recent[0].btc)*sign>0),
        ('eth_same_direction',(recent[-1].eth-recent[0].eth)*sign>0),
        ('volume_expansion',recent[-1].volume>recent[0].volume),
        ('reclaimed_past_level',(price-recent[-2].sol)*sign>0))
    confidence=sum(ok for _,ok in checks)/len(checks)
    latest_window=recent[-3:]
    volatility=(max(p.sol for p in latest_window)-min(p.sol for p in latest_window))/price*100
    baseline=next((p for p in observations if p.observed_at==latest.observed_at-180),None)
    if baseline is None: raise OfflineError('EXACT_THREE_MINUTE_REFERENCE_MISSING')
    pct=lambda key: (getattr(latest,key)/getattr(baseline,key)-1)*100
    coverage=DataCoverage.from_items(tuple(DataAvailability(name=k,
        status='not_applicable' if k in ('liquidation_zones','news') else 'available',
        required=k not in ('liquidation_zones','news'),source=PROVIDER,observed_at=float(latest.available_at),
        note='Not modeled in this synthetic isolated contract; never real-market safety evidence' if k in ('liquidation_zones','news')
             else 'Computed from persisted synthetic chain' if k!='funding_rate' else 'synthetic-no-funding/v1: no periodic funding by contract definition')
        for k in COVERAGE_KEYS))
    risk=values['risk'];now_float=float(now)
    setup=TradeSetup(setup_id='synthetic-'+head[:24]+'-'+side,plan_version=PROVIDER,symbol='SOLUSDT',
        strategy_name=PROVIDER,strategy_type='trend_breakout',side=side,structure_evidence=tuple(evidence),
        entry=dict(order_type='MARKET',reference_price=number(price),lower_price=number(price),upper_price=number(price),
            basis='structure_zone',evidence_ids=(entry_evidence['evidence_id'],)),
        initial_stop=dict(price=stop[0]['price'],basis='nearest confirmed loss-side swing',evidence_ids=(stop[0]['evidence_id'],)),
        invalidation_conditions=(dict(condition_id='stop-invalid',kind='price',description='Observed protective swing invalidation',
            operator='lte' if side=='LONG' else 'gte',price=stop[0]['price'],evidence_ids=(stop[0]['evidence_id'],)),),
        targets=tuple(dict(target_id='structure-'+str(i),price=e[0]['price'],fraction=.5,basis='Confirmed profit-side swing',
            kind='structure',evidence_ids=(e[0]['evidence_id'],)) for i,e in enumerate(targets[:2],1)),
        confidence=dict(value=confidence,basis='Rule fraction, not win probability: '+str(checks)),
        market_state=dict(status='available',regime='trend',source=PROVIDER,observed_at=float(latest.observed_at),
            reference_price=number(price),bid=number(price),ask=number(price),btc_return_3m_pct=float(pct('btc')),
            eth_return_3m_pct=float(pct('eth')),volume_ratio=float(latest.volume/recent[0].volume),volatility_pct=float(volatility)),
        position_limit_advice=dict(max_quantity=number(policy.max_position_quantity),max_notional_usdt=number(policy.max_position_notional_usdt),
            max_margin_usdt=number(policy.max_margin_usdt),max_margin_fraction_of_equity=min(float(risk['max_margin_ratio']),float(policy.max_margin_ratio)),
            leverage_cap=min(risk['max_leverage'],policy.max_leverage),basis='Configuration policy advice; account and all hard ceilings still apply'),
        risk_budget=dict(max_loss_usdt=float(risk['max_loss_per_trade']),max_risk_fraction_of_equity=float(policy.max_risk_fraction_of_equity),basis='Main risk ceiling, not approval'),
        cost_assumptions=dict(entry_fee_rate=float(risk['taker_fee_rate']),exit_fee_rate=float(ex.expected_exit_fee_rate),
            entry_slippage_bps=float(risk['slippage_bps']),exit_slippage_bps=float(ex.expected_exit_slippage_bps),
            funding_cost_usdt=0.,assumed_holding_seconds=float(ex.max_holding_seconds),source='synthetic-no-funding/v1',observed_at=now_float),
        data_coverage=coverage,created_at=now_float,data_as_of=float(latest.available_at),valid_until=float(now+5),
        compatibility_notes=('SYNTHETIC_OFFLINE; historical structural allocation is not actual exit allocation',))
    ev=EvidenceSnapshot(instance_id=store.instance_id,input_digest=head,input_count=len(observations),last_sequence=latest.sequence,
        observed_at=latest.observed_at,available_at=latest.available_at,evaluated_at=now,expires_at=now+5,
        direction_checks=checks,structure_sequences=tuple((p[0]['evidence_id'],*p[1]) for p in chosen))
    candidate=Candidate(candidate_id='0'*64,setup=setup,evidence=ev,bundle_digest=bundle.bundle_digest)
    return candidate.model_copy(update={'candidate_id':digest(candidate)})


def verify_candidate(store,db,candidate_id,now):
    record=get(db,'paper_candidates',candidate_id)
    if record is None: raise OfflineError('CANDIDATE_NOT_ISSUED_BY_THIS_INSTANCE')
    candidate=Candidate.model_validate(record)
    if now>=candidate.evidence.expires_at: raise OfflineError('CANDIDATE_EXPIRED')
    rebuilt=build_candidate(store,db,candidate.setup.side,candidate.evidence.evaluated_at)
    if rebuilt!=candidate: raise OfflineError('CANDIDATE_OR_EVIDENCE_CHANGED')
    a=get(db,'account','account');quote=a['quote']
    if (quote is None or quote['bid'] is None or quote['ask'] is None or now-quote['at']>=5 or
        D(quote['bid'])!=D(str(candidate.setup.market_state.bid)) or D(quote['ask'])!=D(str(candidate.setup.market_state.ask))):
        raise OfflineError('QUOTE_CHANGED_REQUIRES_NEW_CANDIDATE')
    return candidate


def admission_request(candidate,request_id,risk_budget,leverage,now):
    setup=candidate.setup
    return AdmissionRequest(request_id=request_id,risk_budget_usdt=risk_budget,action='OPEN',leverage=leverage,
        margin_mode='ISOLATED',auto_add_margin=False,loss_recovery_sizing=False,sizing_basis='quality_risk_budget',
        confirmations=tuple(dict(evidence_id=e.evidence_id,evidence_digest=fingerprint(e),verified=True,
            verifier='paper-structure-review/v1',checked_at=now) for e in setup.structure_evidence),
        invalidation_review=dict(setup_digest=fingerprint(setup),all_conditions_clear=True,
            verifier='paper-structure-review/v1',checked_at=now))
