"""Independent linear STATIC-target certificates, NOT an S3/Runner proof.

No quantify/admit/score calls. Coefficients and all-grade outer quantity bounds
are reconstructed from inputs, not from the producer's proof or selected grade.
Original calculate_rr is used ONLY for separately reported lattice cross-checks.
"""
from decimal import Decimal as D, Context, localcontext, ROUND_CEILING, ROUND_FLOOR
from app.configuration.compiler import policy_from
from app.offline_paper.storage import digest
from app.admission.models import fingerprint
from app.setups.models import TradeSetup
from app.setups.rr import calculate_rr
from app.admitted_paper.provider import number


class UnsupportedProof(ValueError):
    """Declared audit capability/input gap, not a certificate of infeasibility."""


def dec(value):
    if value is None or isinstance(value,bool): raise UnsupportedProof('MISSING_OR_INVALID_NUMBER')
    x=D(str(value))
    if not x.is_finite(): raise UnsupportedProof('NONFINITE_NUMBER')
    return x


def coefficients(setup,function):
    """USDT per base unit, original target weights, all explicit cost components."""
    s=setup;entry=s['entry'];c=s['cost_assumptions'];p=dec(entry['reference_price'])
    if entry['lower_price']!=entry['reference_price'] or entry['upper_price']!=entry['reference_price']:
        raise UnsupportedProof('POINT_ENTRY_REQUIRED_FOR_THIS_CERTIFICATE')
    sign=1 if s['side']=='LONG' else -1
    stop=dec(s['initial_stop']['price']);risk=sign*(p-stop)
    if risk<=0: raise UnsupportedProof('INVALID_STRUCTURAL_STOP')
    ef,xf=dec(c['entry_fee_rate']),dec(c['exit_fee_rate'])
    eb,xb=dec(c['entry_slippage_bps'])/10000,dec(c['exit_slippage_bps'])/10000
    if min(ef,xf,eb,xb)<0 or max(eb,xb)>=1: raise UnsupportedProof('INVALID_COST_RATE')
    effective=p+sign*p*eb
    def costs(x):
        out=x-sign*x*xb
        if out<=0: raise UnsupportedProof('NONPOSITIVE_COST_ADJUSTED_PRICE')
        return p*eb+x*xb+effective*ef+out*xf
    weights=[dec(t['fraction']) for t in s['targets']]
    if sum(weights)!=1 or not weights or min(weights)<=0:
        raise UnsupportedProof('EXACT_COMPLETE_WEIGHTS_REQUIRED')
    targets=[dec(t['price']) for t in s['targets']]
    if any(sign*(x-p)<=0 for x in targets): raise UnsupportedProof('INVALID_TARGET')
    fixed=dec(function['fixed_usdt']);linear=dec(function['per_unit_usdt'])
    if min(fixed,linear)<0: raise UnsupportedProof('NEGATIVE_COST')
    reward=sum((w*sign*(x-p) for w,x in zip(weights,targets)),D(0))
    targetcost=sum((w*costs(x) for w,x in zip(weights,targets)),D(0))
    g=reward-targetcost-linear;l=risk+costs(stop)+linear
    if l<=0: raise UnsupportedProof('POSITIVE_LINEAR_RISK_REQUIRED')
    return dict(g=g,l=l,f0=fixed,structural_risk=risk,weighted_reward=reward,
        target_cost_ex_funding=targetcost,funding_per_unit=linear,
        effective_entry=effective,entry_fee_per_unit=effective*ef)


def quantity_domain(body,bundle,c):
    """Union's enclosing interval: independently calculate EVERY allowed tier cap.

    This includes quantities below tier-specific minimum risk, a conservative
    superset. Grade transitions may remove points; they cannot add outside it.
    """
    p=policy_from(bundle,'admission');a=body['account'];s=body['candidate']['setup'];r=body['request'];v=body['exchange']
    market=next((m for m in p.markets if m.regime==s['market_state']['regime']),None)
    if market is None or not market.allowed: raise UnsupportedProof('MARKET_NOT_ALLOWED')
    limits={k:min(dec(x),dec(getattr(p,k))) for k,x in a['limits'].items()}
    for limit in bundle.comparable_limits:
        if limit.semantic in limits: limits[limit.semantic]=min(limits[limit.semantic],limit.effective_value)
    leverage=dec(r['leverage']);adv=s['position_limit_advice'];risk=s['risk_budget']
    levmax=min(limits['max_leverage'],dec(v['max_leverage']))
    if adv.get('leverage_cap') is not None:levmax=min(levmax,dec(adv['leverage_cap']))
    if leverage<=0 or leverage>levmax or leverage!=dec(a['configured_leverage']):
        raise UnsupportedProof('LEVERAGE_OR_UNITS_UNSUPPORTED')
    price=max(c['effective_entry'],dec(body['price_contract']['modeled_fill_price']))
    fee=max(c['entry_fee_per_unit'],dec(body['price_contract']['modeled_fill_price'])*dec(body['price_contract']['fee_rate']))
    equity=dec(a['equity_usdt']);used=dec(a['margin_used_usdt'])
    room=min(dec(a['available_margin_usdt']),equity*limits['max_margin_ratio']-used,limits['max_margin_usdt']-used)
    if adv['max_margin_usdt'] is not None:room=min(room,dec(adv['max_margin_usdt']))
    if adv['max_margin_fraction_of_equity'] is not None:room=min(room,equity*dec(adv['max_margin_fraction_of_equity']))
    step=dec(v['quantity_step'])
    if step<=0:raise UnsupportedProof('INVALID_QUANTITY_STEP')
    lower=max(dec(v['min_quantity']),dec(v['min_notional_usdt'])/min(dec(body['price_contract']['modeled_fill_price']),dec(body['price_contract']['signal_reference_price'])))
    low=(lower/step).to_integral_value(rounding=ROUND_CEILING)*step
    branches=[];floors=[]
    for tier in p.tiers:
        base=min(dec(r['risk_budget_usdt']),limits['max_loss_per_trade_usdt'])
        ceilings=[base,dec(risk['max_loss_usdt']),max(D(0),limits['daily_loss_limit_usdt']-sum((dec(a[k]) for k in ('day_realized_loss_usdt','unrealized_loss_usdt','reserved_risk_usdt')),D(0))),equity*p.max_risk_fraction_of_equity,base*tier.risk_fraction,base*market.risk_fraction]
        if risk['remaining_daily_loss_usdt'] is not None:ceilings.append(dec(risk['remaining_daily_loss_usdt']))
        if risk['max_risk_fraction_of_equity'] is not None:ceilings.append(equity*dec(risk['max_risk_fraction_of_equity']))
        caps=[max(D(0),room)/(price/leverage+fee),equity*tier.max_notional_equity_ratio/price,
              limits['max_position_notional_usdt']/price,limits['max_position_quantity'],dec(v['max_quantity']),
              max(D(0),(min(ceilings)-c['f0'])/c['l'])]
        if adv['max_quantity'] is not None:caps.append(dec(adv['max_quantity']))
        if adv['max_notional_usdt'] is not None:caps.append(dec(adv['max_notional_usdt'])/price)
        branches.append(dict(tier=tier.name,q_max=(min(caps)/step).to_integral_value(rounding=ROUND_FLOOR)*step,
                             risk_ceiling=min(ceilings)))
        floors.append(max(p.minimum_net_rr,market.minimum_net_rr,tier.minimum_net_rr))
    high=max(x['q_max'] for x in branches)
    if low<=0 or high<low:raise UnsupportedProof('EMPTY_DOMAIN')
    # Sufficient exact Stage-3 input representability for EVERY lattice value:
    # integers at a common decimal exponent have <=15 significant digits.
    # Refuse wider cases; endpoint-only checks would not prove the interior.
    for x,unit in ((high,step),(c['f0']+high*c['funding_per_unit'],step*c['funding_per_unit'])):
        exponent=min(x.as_tuple().exponent,unit.as_tuple().exponent,c['f0'].as_tuple().exponent)
        if len(str(abs(int(x.scaleb(-exponent)))))>15:
            raise UnsupportedProof('WHOLE_DOMAIN_LOSSLESS_REPRESENTATION_NOT_PROVEN')
    k=max(p.minimum_net_rr,market.minimum_net_rr)
    if any(x<k for x in floors):raise AssertionError('GRADE_FLOOR_BELOW_OUTER_FLOOR')
    return dict(q_min=low,q_max=high,step=step,size=int((high-low)/step)+1,
                global_floor=p.minimum_net_rr,market_floor=market.minimum_net_rr,k=k,
                branches=branches,tier_floors=floors)


def validate_inputs(body,bundle):
    if (body['version']!='quantity-consistent-admission/v1' or
        body['price_contract']['version']!='execution-price-layers/v1' or
        body['cost_function']['version']!='quantity-funding-budget/v1' or
        body['rr']['calculation_version']!='linear-usdt-rr/v1' or
        body['lineage']['version']!='execution-priced-static-input/v1' or
        body['old_fixed'] or body['old_prices']):
        raise UnsupportedProof('UNKNOWN_OR_UNSUPPORTED_VERSION')
    original=TradeSetup.model_validate(body['candidate']['setup'])
    derived=TradeSetup.model_validate(body['derived_setup'])
    pc=body['price_contract'];f=body['cost_function'];lin=body['lineage']
    if (body['bundle_digest']!=bundle.bundle_digest or body['policy_digest']!=fingerprint(policy_from(bundle,'admission')) or
        body['exit_policy_digest']!=fingerprint(policy_from(bundle,'exit')) or
        fingerprint(original)!=body['original_setup_digest'] or fingerprint(derived)!=lin['derived_setup_digest'] or
        pc['original_setup_digest']!=fingerprint(original) or f['original_setup_digest']!=fingerprint(original) or
        digest(pc)!=lin['price_contract_digest'] or digest(f)!=lin['cost_function_digest']):
        raise UnsupportedProof('INPUT_BINDING_MISMATCH')
    for key in ('initial_stop','targets','side','symbol','entry','structure_evidence'):
        if original.model_dump()[key]!=derived.model_dump()[key]:raise UnsupportedProof('STRUCTURE_CHANGED')
    if any(body['rr'][key]!=getattr(derived,key) for key in ('setup_id','plan_version','symbol','side')):
        raise UnsupportedProof('RR_INPUT_BINDING_MISMATCH')
    if (dec(f['budget_price'])!=max(dec(x[1]) for x in f['price_basis']) or
        dec(f['per_unit_usdt'])!=dec(f['budget_price'])*dec(f['rate_allowance'])*dec(f['event_allowance']) or
        f['price_contract_digest']!=digest(pc)):
        raise UnsupportedProof('LINEAR_FUNDING_BUDGET_BINDING_MISMATCH')
    c=derived.cost_assumptions
    if dec(c.entry_slippage_bps)!=dec(lin['entry_combined_bps']) or dec(c.exit_slippage_bps)!=dec(lin['exit_combined_bps']):
        raise UnsupportedProof('PRICE_BUDGET_BINDING_MISMATCH')
    q=dec(body['evaluated_quantity'])
    if (dec(c.funding_cost_usdt)!=dec(f['fixed_usdt'])+q*dec(f['per_unit_usdt']) or
        dec(body['rr']['hypothetical_quantity'])!=q):raise UnsupportedProof('COST_QUANTITY_MISMATCH')


def verify_certificate(body,bundle):
    """A rejected certificate is INCONSISTENT; missing capability is UNSUPPORTED."""
    with localcontext(Context(prec=50)):
        validate_inputs(body,bundle)
        c=coefficients(body['derived_setup'],body['cost_function']);d=quantity_domain(body,bundle,c)
        p=body['proof'];mismatches=[]
        if body['search_status']!='PROVEN_STATIC_RR_DOMAIN_REJECT':raise UnsupportedProof('NOT_A_STATIC_CERTIFICATE')
        rr=(c['g']*d['q_max']-c['f0'])/(c['l']*d['q_max']+c['f0'])
        # Exact polynomial comparison, no rounding tolerance at the threshold.
        slope=c['g']-d['k']*c['l'];maxlhs=max(slope*d['q_min'],slope*d['q_max'])
        rhs=(1+d['k'])*c['f0']
        if maxlhs>=rhs:mismatches.append('INDEPENDENT_INEQUALITY_DOES_NOT_PROVE_REJECTION')
        for key,expected in (('maximum_quantity',d['q_max']),('global_floor',d['k']),('fixed_usdt',c['f0']),('quantity_linear_cost',c['funding_per_unit'])):
            if dec(p[key])!=expected:mismatches.append('PROOF_FIELD:'+key)
        for key,expected in (('low',d['q_min']),('high',d['q_max']),('step',d['step']),('size',d['size']),('linear_risk_per_unit',c['l'])):
            if dec(body['domain'][key])!=expected:mismatches.append('DOMAIN_FIELD:'+key)
        # 50-digit Decimal roundoff ONLY for redundant serialized ratios, never for proof.
        if abs(dec(p['rr_at_max'])-rr)>D('1e-45'):mismatches.append('RECORDED_RR_MISMATCH')
        if abs(dec(body['rr']['reference']['net_rr'])-rr)>D('1e-45'):mismatches.append('CALCULATION_RR_MISMATCH')
        return dict(version='independent-static-certificate/v1',candidate_id=body['candidate_id'],
            input_digest=digest(body),bundle_digest=bundle.bundle_digest,
            input_versions=(body['price_contract']['version'],body['derived_setup']['plan_version'],body['cost_function']['version'],body['rr']['calculation_version']),
            status='INCONSISTENT' if mismatches else 'VALID',differences=mismatches,
            coefficients=c,domain=d,rr_upper=rr,inequality_max_lhs=maxlhs,inequality_rhs=rhs,
            conditions=('Exact complete structural target weights, point reference entry, linear per-unit costs and nonnegative fixed cost',
                'L(q)>0; derivative f0*(g+l)/L(q)^2, or g+l<=0 implies G(q)<0',
                'Interval encloses every allowed tier; global/market floor is no higher than any tier floor',
                'No TP lot rounding, early stop, Runner or path-dependent S3 claim'),
            execution_authority='NONE')


def lattice_crosscheck(body,certificate,*,max_points=None):
    """Independent original RR replay; explicitly count sampled vs all grid points."""
    with localcontext(Context(prec=50)):
        d=certificate['domain'];c=certificate['coefficients'];s=TradeSetup.model_validate(body['derived_setup'])
        size=d['size'];count=size if max_points is None else min(size,max_points)
        indexes=range(size) if count==size else sorted({int(i*(size-1)/(count-1)) for i in range(count)}) if count>1 else [0]
        checked=0;differences=[]
        for i in indexes:
            q=d['q_min']+i*d['step'];fund=c['f0']+q*c['funding_per_unit']
            revised=s.model_copy(update={'cost_assumptions':s.cost_assumptions.model_copy(update={'funding_cost_usdt':number(fund)})})
            rr=calculate_rr(revised,quantity=number(q)).reference.net_rr
            expected=(c['g']*q-c['f0'])/(c['l']*q+c['f0'])
            checked+=1
            if rr is None or abs(rr-expected)>D('1e-44') or rr>=d['k']:
                differences.append(dict(q=str(q),rr=str(rr),expected=str(expected)))
        return dict(candidate_id=body['candidate_id'],points_checked=checked,domain_points=size,
                    exhaustive=checked==size,scope='SELECTED_CERTIFICATE_ONLY',differences=differences)
