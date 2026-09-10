"""Offline content/semantic checks. Never an Admission or execution replacement."""

from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from app.admission.engine import admit_trade
from app.admission.models import PlanTerms, fingerprint
from app.exits.engine import digest
from app.setups.rr import calculate_rr
from app.setups.scorecard import score_trade_setup

from .compiler import issue, main_values, policy_from, verify_bundle
from .encoding import content_hash
from .inputs import PlanInputs
from .models import (ConfigBundle, ConfigCompilation, ConfigurationError, ContractValidationResult,
    ExitComparison, FreshnessObservation, PlanConfigurationBinding, decimal_input)

D=Decimal


def _d(value):
    return None if value is None else D(str(value))


def _quantity(value):
    if value is None: return None
    result=float(value)
    if D(str(result))!=value:
        raise ConfigurationError('RR_QUANTITY_REPRESENTATION_UNSUPPORTED: accepted Stage 3 input is float')
    return result


def _time(value):
    try:
        result=decimal_input(value)
    except (ValueError, TypeError, ArithmeticError):
        raise ConfigurationError('Explicit finite UTC seconds required') from None
    if result<0: raise ConfigurationError('UTC timestamp must be nonnegative')
    return result


def declare_plan_binding(bundle, inputs, *, declared_at):
    """Pure declared lineage, to be persisted BEFORE approval by a future supplier.

    Can describe unapproved/incomplete plans. Never authenticates lineage or
    retroactively adds config authority to an old AdmissionDecision.
    """
    checked=verify_bundle(bundle)
    if checked.consistency!='PASS': raise ConfigurationError('Cannot bind conflicting configuration')
    inputs=PlanInputs.model_validate(inputs.model_dump())
    if inputs.rr is None: raise ConfigurationError('Explicit RR required for content binding')
    now=_time(declared_at)
    if now<_d(inputs.setup.created_at) or (inputs.scorecard is not None and now<_d(inputs.scorecard.context.evaluated_at)):
        raise ConfigurationError('Cannot declare binding before its supplied records exist')
    if inputs.admission is not None and now>_d(inputs.admission.evaluated_at):
        raise ConfigurationError('Cannot retroactively bind an old approval to new configuration')
    return PlanConfigurationBinding(bundle_digest=bundle.bundle_digest,instance_id=bundle.manifest.instance_id,
        setup_digest=fingerprint(inputs.setup),rr_digest=fingerprint(inputs.rr),
        scorecard_digest=None if inputs.scorecard is None else fingerprint(inputs.scorecard),
        admission_policy_digest=fingerprint(policy_from(bundle,'admission')),
        exit_policy_digest=fingerprint(policy_from(bundle,'exit')),declared_at=now)


def _plan_checks(bundle, inputs, now, *, exit_usage='legacy_full_policy'):
    if exit_usage not in ('legacy_full_policy','conditional_paper_8b','conditional_historical_8c'):
        raise ConfigurationError('Unknown exit valuation purpose')
    setup,rr,card,decision=inputs.setup,inputs.rr,inputs.scorecard,inputs.admission
    policy,ex=policy_from(bundle,'admission'),policy_from(bundle,'exit')
    main=main_values(bundle)
    problems=[]
    def add(code,path,actual,expected,suggestion,severity='ERROR'):
        problems.append(issue(code,path,'plan + declared configuration',actual,expected,suggestion,severity))
    if setup.symbol!=bundle.manifest.trade_symbol:
        add('PLAN_SYMBOL_CONFLICT','setup.symbol',setup.symbol,bundle.manifest.trade_symbol,'Use the correct scope; never remap orders')
    if setup.strategy_type not in main['strategies'] or not main['strategies'][setup.strategy_type]['enabled']:
        add('PLAN_STRATEGY_SCOPE_UNSUPPORTED','setup.strategy_type',setup.strategy_type,'explicit enabled existing strategy',
            'Do not invent or enable a strategy through a TradeSetup',severity='UNSUPPORTED')
    if setup.entry.order_type!=main['execution']['entry_order_type']:
        add('PLAN_ENTRY_METHOD_CONFLICT','setup.entry.order_type',setup.entry.order_type,main['execution']['entry_order_type'],
            'Use matching entry/rule assumptions; keep the original plan immutable')
    if rr is None:
        add('RR_RESULT_MISSING','rr',None,'explicit Stage 3 result','Supply the actual original result',severity='PREREQUISITE')
    else:
        try:
            if calculate_rr(setup,quantity=_quantity(rr.hypothetical_quantity))!=rr:
                add('RR_CONTENT_MISMATCH','rr','supplied result','calculate_rr of entire supplied plan and original quantity',
                    'Recalculate a separately identified scenario; never overwrite the historical result')
        except (ValueError,TypeError,ArithmeticError) as error:
            add('RR_RECOMPUTATION_UNSUPPORTED','rr',type(error).__name__,'representable accepted Stage 3 inputs',
                'Keep original evidence; do not round incompatible quantity silently',severity='UNSUPPORTED')
        if rr.status!='complete' or any(s is None or s.net_rr is None for s in (rr.reference,rr.entry_lower,rr.entry_upper)):
            add('NET_RR_INCOMPLETE','rr.status',rr.status,'complete cost-adjusted RR for all three entry scenarios',
                'No gross RR fallback or missing-funding zero',severity='PREREQUISITE')
    if card is None:
        add('SCORECARD_MISSING','scorecard',None,'original descriptive Scorecard','No fabricated score/coverage',severity='PREREQUISITE')
    elif rr is not None:
        if _d(card.context.max_data_age_seconds)!=bundle.manifest.score_context_max_age_seconds:
            add('SCORE_CONTEXT_POLICY_MISMATCH','scorecard.context.max_data_age_seconds',card.context.max_data_age_seconds,
                str(bundle.manifest.score_context_max_age_seconds),'A changed scoring context requires a new bound description')
        try:
            expected=score_trade_setup(setup,rr,evaluated_at=card.context.evaluated_at,
                                      max_data_age_seconds=card.context.max_data_age_seconds)
            if expected!=card:
                add('SCORECARD_CONTENT_MISMATCH','scorecard','supplied content','unchanged accepted rubric on matching inputs',
                    'Keep RR independent; regenerate description from genuine inputs')
        except (ValueError,TypeError,ArithmeticError):
            add('SCORECARD_INPUT_MISMATCH','scorecard','mismatched or unsupported RR/plan','same complete input content',
                'Do not trust same setup_id/version alone')
    binding=inputs.configuration_binding
    if binding is None:
        add('PREAPPROVAL_CONFIG_BINDING_MISSING','configuration_binding',None,'original preapproval bundle lineage',
            'Old approvals cannot be silently rebound; a future trusted supplier must issue a new linked plan/approval',severity='PREREQUISITE')
    else:
        expected=(bundle.bundle_digest,bundle.manifest.instance_id,fingerprint(setup),
            None if rr is None else fingerprint(rr),None if card is None else fingerprint(card),
            fingerprint(policy),fingerprint(ex))
        actual=(binding.bundle_digest,binding.instance_id,binding.setup_digest,binding.rr_digest,
                binding.scorecard_digest,binding.admission_policy_digest,binding.exit_policy_digest)
        if actual!=expected:
            add('CONFIG_PLAN_BINDING_MISMATCH','configuration_binding','content or policy changed','all content digests and scope identical',
                'A same-version content change requires new lineage, not policy replacement on an old approval')
        if (binding.declared_at>now or binding.declared_at<_d(setup.created_at)
            or card is not None and binding.declared_at<_d(card.context.evaluated_at)
            or decision is not None and binding.declared_at>_d(decision.evaluated_at)):
            add('CONFIG_BINDING_TIME_INVALID','configuration_binding.declared_at',binding.declared_at,
                'after inputs exist and no later than approval/evaluation','Do not backdate or retroactively wrap old approvals')
    if decision is None:
        add('ADMISSION_RECORD_MISSING','admission',None,'original AdmissionDecision','This check cannot replace Admission',severity='PREREQUISITE')
    else:
        if decision.decision_id!=fingerprint(decision.model_copy(update={'decision_id':'0'*64})):
            add('ADMISSION_CONTENT_DIGEST_MISMATCH','admission.decision_id','altered content','original complete content',
                'Preserve the historical decision; hashes are not signatures')
        b=decision.binding
        if b is None:
            add('ADMISSION_INPUT_BINDING_MISSING','admission.binding',None,'complete historical input binding',
                'Do not infer eligibility from result label',severity='PREREQUISITE')
        else:
            for key,actual,expected in (
                ('setup',b.setup,fingerprint(setup)),('rr',b.rr,None if rr is None else fingerprint(rr)),
                ('scorecard',b.scorecard,None if card is None else fingerprint(card)),
                ('policy',b.policy,fingerprint(policy)),('instance_id',b.instance_id,bundle.manifest.instance_id)):
                if actual!=expected:
                    add('ADMISSION_INPUT_CONTENT_MISMATCH','admission.binding.'+key,'mismatched content/scope','original bound input',
                        'Request a separately issued new approval after configuration change')
            for key,obj in (('account',inputs.account),('exchange',inputs.exchange),('request',inputs.request)):
                if obj is not None and getattr(b,key)!=fingerprint(obj):
                    add('ADMISSION_CONTEXT_MISMATCH','admission.binding.'+key,'different snapshot','identical historical bound context',
                        'Never reuse old approval with a newer or altered snapshot')
        terms=PlanTerms(setup_id=setup.setup_id,plan_version=setup.plan_version,symbol=setup.symbol,side=setup.side,
            order_type=setup.entry.order_type,entry_reference=_d(setup.entry.reference_price),
            entry_lower=_d(setup.entry.lower_price),entry_upper=_d(setup.entry.upper_price),initial_stop=_d(setup.initial_stop.price),
            targets=tuple((t.target_id,_d(t.price),_d(t.fraction)) for t in setup.targets))
        if decision.plan_terms!=terms:
            add('ADMISSION_TERMS_MISMATCH','admission.plan_terms','mismatched terms','same entry/stop/targets/fractions/side',
                'IDs alone do not bind plan content')
        if decision.policy_version!=policy.policy_version:
            add('ADMISSION_POLICY_VERSION_MISMATCH','admission.policy_version',decision.policy_version,policy.policy_version,'Unsupported version is not latest')
        if decision.result=='REJECT':
            add('ADMISSION_REJECT_IS_NOT_ELIGIBILITY','admission.result','REJECT','new entry requires its own valid decision',
                'Existing position protection still continues under its original policy',severity='PREREQUISITE')
        elif decision.final_rr is not None:
            try:
                if calculate_rr(setup,quantity=_quantity(decision.max_quantity))!=decision.final_rr:
                    add('ADMISSION_SIZED_RR_MISMATCH','admission.final_rr','mismatched sized scenario',
                        'accepted calculate_rr at exact approved quantity','Fixed funding must be recalculated at the approved quantity')
            except (TypeError,ValueError,ArithmeticError):
                add('APPROVED_QUANTITY_UNSUPPORTED','admission.max_quantity',decision.max_quantity,'Stage 3 representable quantity',
                    'Do not silently round approvals',severity='UNSUPPORTED')
        if all(x is not None for x in (rr,card,inputs.account,inputs.exchange,inputs.request)):
            expected=admit_trade(setup,rr,card,account=inputs.account,exchange=inputs.exchange,
                request=inputs.request,policy=policy,evaluated_at=decision.evaluated_at)
            if expected!=decision:
                add('HISTORICAL_ADMISSION_REPLAY_MISMATCH','admission','not reproducible','exact accepted Stage 5 function at original decision time',
                    'This is only historical verification, not a fresh decision or risk reservation')
        if decision.result!='REJECT':
            # Validate the supplied decision against additional *declared* main
            # ceilings. Do not resize, issue a new approval or alter old policy.
            caps={c.semantic:c.effective_value for c in bundle.comparable_limits}
            for field,cap in (('allowed_risk_budget_usdt',caps['max_loss_per_trade_usdt']),
                              ('leverage',caps['max_leverage'])):
                actual=getattr(decision,field)
                if actual is not None and actual>cap:
                    add('APPROVAL_EXCEEDS_BUNDLE_CEILING','admission.'+field,actual,str(cap),
                        'Old Admission alone does not consume main ceilings; future assembly must obtain a new bounded approval')
            if inputs.account is not None and inputs.account.equity_usdt is not None:
                used=inputs.account.margin_used_usdt
                if used is not None and used+decision.max_initial_margin_usdt+decision.entry_fee_reserve_usdt>inputs.account.equity_usdt*caps['max_margin_ratio']:
                    add('APPROVAL_MARGIN_EXCEEDS_BUNDLE_CEILING','admission.max_initial_margin_usdt',
                        decision.max_initial_margin_usdt,'existing margin + new margin + entry fee <= configured equity fraction',
                        'Margin is not notional, and a high score cannot override the stricter configured ceiling')
            btc=_d(setup.market_state.btc_return_3m_pct)
            threshold=max(D(str(main['risk']['btc_crash_pct'])),_d(policy.btc_crash_3m_pct))
            if setup.side=='LONG' and btc is not None and btc<=threshold:
                add('APPROVAL_BTC_FILTER_CONFLICT','setup.market_state.btc_return_3m_pct',btc,
                    '> '+str(threshold),'The same <= negative-percent predicate composes with max, not numeric min; no change to original filters')
    # Structure, reference policy levels and RR scenarios stay separate.
    entry,stop=_d(setup.entry.reference_price),_d(setup.initial_stop.price)
    sign=D(1 if setup.side=='LONG' else -1)
    r=abs(entry-stop)
    comparison=ExitComparison(reference_entry=entry,initial_stop=stop,reference_initial_r=r,
        tp_trigger_prices=(entry+sign*r*ex.tp1_r,entry+sign*r*ex.tp2_r),
        original_targets=tuple((t.target_id,_d(t.price),_d(t.fraction)) for t in setup.targets),
        proposed_allocations=(ex.tp1_fraction,ex.tp2_fraction,ex.runner_fraction),
        runner_activation_price=entry+sign*r*ex.runner_activation_r)
    if any(p<=0 for p in (*comparison.tp_trigger_prices,comparison.runner_activation_price)):
        add('EXIT_TRIGGER_PRICE_NONPOSITIVE','exit.reference_geometry',comparison.tp_trigger_prices,
            'positive linear-contract prices','A reference R rule cannot invent an impossible price')
    fractions=tuple(_d(t.fraction) for t in setup.targets)
    if exit_usage=='legacy_full_policy' and fractions!=comparison.proposed_allocations:
        add('EXIT_ALLOCATION_MISMATCH','setup.targets.fraction',fractions,str(comparison.proposed_allocations),
            'Do not normalize, top up or silently replace original structure allocation')
    if sum(fractions,D(0))!=1:
        add('PLAN_FRACTIONS_NOT_EXACTLY_ONE','setup.targets.fraction',fractions,'Decimal sum exactly 1','No tolerance-based normalization')
    for index,trigger in enumerate(comparison.tp_trigger_prices if exit_usage=='legacy_full_policy' else ()):
        if index>=len(setup.targets) or _d(setup.targets[index].price)!=trigger:
            add('STRUCTURE_TARGET_DIFFERS_FROM_EXIT_TRIGGER','setup.targets.'+str(index),
                None if index>=len(setup.targets) else setup.targets[index].price,str(trigger),
                'Reference R trigger is not a structural target; original RR describes a different exit scene')
    if not setup.initial_stop.evidence_ids or not setup.entry.evidence_ids or any(t.kind!='structure' or not t.evidence_ids for t in setup.targets):
        add('STRUCTURE_SUPPORT_INCOMPLETE','setup.structure_evidence','missing/unverified references','real entry/stop/target evidence',
            'High RR and labels cannot fabricate structural support',severity='PREREQUISITE')
    expected_move='cost_adjusted_entry' if ex.break_even_mode=='cost_covered' else 'entry'
    first_target=None if not setup.targets else setup.targets[0].target_id
    for rule in setup.stop_movement.rules:
        if rule.after_target_id==first_target and rule.move_to!=expected_move:
            add('STOP_MOVEMENT_POLICY_CONFLICT','setup.stop_movement.rules',rule.move_to,expected_move,
                'Keep the original declaration; the proposed TP1 protection has a different cost/stop contract')
        if (rule.after_target_id!=first_target or rule.fixed_price is not None
            or rule.trailing_distance_r is not None or rule.buffer_bps not in (None,0)):
            add('STOP_MOVEMENT_PATH_UNSUPPORTED','setup.stop_movement.rules',rule.model_dump(mode='json'),
                'an explicit supported mapping of fill trigger, buffer and path-dependent tail protection',
                'No fixed-price/trailing rule is silently converted into the Stage 6 Runner policy',severity='UNSUPPORTED')
    if exit_usage=='legacy_full_policy':
        add('RUNNER_FULL_POLICY_VALUATION_UNSUPPORTED','exit.runner_strategy',ex.runner_strategy,
            'path-dependent tail execution is not a fixed 3R fill','Keep runner_exit_price and full_policy_net_rr unknown',severity='UNSUPPORTED')
        add('STATIC_RR_NOT_EXIT_POLICY_RETURN','rr','all supplied static targets reached',
            'not guaranteed return or statistical expectation','TP partial fills, stop moves, time/trend exits and funding path need later explicit modeling',severity='WARNING')
        add('ACTUAL_FILL_R_RECHECK_REQUIRED','setup.entry',setup.entry.reference_price,
            'first confirmed entry freezes its own R anchor','Future fill adapter must check price, stop geometry, quantity, costs and target assumptions without rewriting history',severity='PREREQUISITE')
    costs=setup.cost_assumptions
    floors={'entry_fee_rate':max(policy.minimum_entry_fee_rate,D(str(main['risk']['taker_fee_rate']))),
        'exit_fee_rate':max(policy.minimum_exit_fee_rate,ex.expected_exit_fee_rate,D(str(main['risk']['taker_fee_rate']))),
        'entry_slippage_bps':max(policy.minimum_entry_slippage_bps,D(str(main['risk']['slippage_bps']))),
        'exit_slippage_bps':max(policy.minimum_exit_slippage_bps,ex.expected_exit_slippage_bps,D(str(main['risk']['slippage_bps'])))}
    for field,floor in floors.items():
        actual=_d(getattr(costs,field))
        if actual is None:
            add('COST_ASSUMPTION_MISSING','setup.cost_assumptions.'+field,None,'explicit '+str(floor)+' or more',
                'Missing is not zero; no gross/net substitution',severity='PREREQUISITE')
        elif actual<floor:
            add('COST_ASSUMPTION_UNDERBUDGETED','setup.cost_assumptions.'+field,actual,'>= '+str(floor),
                'Compare same leg and unit only; do not sum duplicated fee budgets')
        if actual is not None and field in ('entry_slippage_bps','exit_slippage_bps'):
            ceiling=getattr(policy,'maximum_'+field)
            if actual>ceiling:
                add('COST_ASSUMPTION_OUTSIDE_POLICY','setup.cost_assumptions.'+field,actual,'<= '+str(ceiling),
                    'Do not lower a realistic cost estimate merely to pass configuration validation')
    if costs.funding_cost_usdt is None or costs.assumed_holding_seconds is None:
        add('FUNDING_HORIZON_INCOMPLETE','setup.cost_assumptions.funding_cost_usdt',costs.funding_cost_usdt,
            'explicit signed full-quantity funding and holding horizon','No zero fill-in',severity='PREREQUISITE')
    elif not ex.max_holding_seconds<=_d(costs.assumed_holding_seconds)<=_d(policy.max_holding_assumption_seconds):
        add('FUNDING_HORIZON_INCOMPATIBLE','setup.cost_assumptions.assumed_holding_seconds',costs.assumed_holding_seconds,
            'cover configured exit horizon within the admitted horizon limit','Produce a separately labeled conservative scenario')
    elif exit_usage=='legacy_full_policy':
        add('FUNDING_PATH_NOT_MODELED','setup.cost_assumptions','fixed total allocated pro rata',
            'funding for actual partial-exit/time path','Static cost budget is not confirmed funding or full-policy valuation',severity='UNSUPPORTED')
    if inputs.bound_position is not None:
        p=inputs.bound_position
        if p.state.seed_digest!=digest(p.seed) or p.state.policy_digest!=digest(p.policy):
            add('EXISTING_POSITION_BINDING_MISMATCH','bound_position','mismatched original seed/policy','original unchanged binding',
                'Restore original checkpoint/journal; never clear history or substitute the new bundle')
        add('EXISTING_POSITION_POLICY_RETAINED','bound_position.policy','original binding retained',
            'new config never switches policy or disables protection','Full original-policy checkpoint replay belongs to the existing recovery contract',severity='WARNING')
    return problems,comparison


def validate_paper_context_8b(bundle, inputs, *, evaluated_at):
    """Only shared input/context checks, NOT an ExitPlan approval or execution.

    Explicit new usage dispatch, not suppression of legacy error codes. Stage 7
    validate_contract still reports the full-policy valuation gaps unchanged.
    The 8B composer MUST separately validate allocation mapping, path scenarios,
    funding model, trusted instance evidence and actual-fill adaptation.
    """
    checked=verify_bundle(bundle)
    if checked.parsing!='PASS' or checked.consistency!='PASS':
        return checked.issues
    if type(inputs) is not PlanInputs:
        raise ConfigurationError('Explicit PlanInputs required')
    inputs=PlanInputs.model_validate(inputs.model_dump())
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)):
        p,_=_plan_checks(bundle,inputs,_time(evaluated_at),exit_usage='conditional_paper_8b')
        r,_=_runtime_checks(bundle,inputs,_time(evaluated_at))
    return tuple(p+r)


def validate_historical_context_8c(bundle, inputs, *, evaluated_at):
    """Explicit 8C dispatch: shared checks only, never whole-policy valuation.

    The historical composer additionally requires its instance prefix review,
    cost/funding contract and versioned conditional scenario gate. Legacy and
    8B callers retain their original dispatch and unsupported valuation result.
    """
    checked=verify_bundle(bundle)
    if checked.parsing!='PASS' or checked.consistency!='PASS': return checked.issues
    if type(inputs) is not PlanInputs: raise ConfigurationError('Explicit PlanInputs required')
    inputs=PlanInputs.model_validate(inputs.model_dump())
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)):
        p,_=_plan_checks(bundle,inputs,_time(evaluated_at),exit_usage='conditional_historical_8c')
        r,_=_runtime_checks(bundle,inputs,_time(evaluated_at))
    return tuple(p+r)


def _runtime_checks(bundle, inputs, now):
    policy=policy_from(bundle,'admission')
    problems,observations=[],[]
    def inspect(path,obj,stamp,ttl,invalidates,available=True):
        t=_d(stamp)
        status=('INCOMPLETE' if obj is None or t is None or not available else
                'FAIL' if t>now or now-t>=_d(ttl) else 'PASS')
        observations.append(FreshnessObservation(field_path=path,status=status,observed_at=t,
            ttl_seconds=_d(ttl),expires_at=None if t is None else t+_d(ttl),invalidates=invalidates))
        if status!='PASS':
            problems.append(issue('DATA_MISSING_OR_UNVERIFIED' if status=='INCOMPLETE' else 'DATA_STALE_OR_FUTURE',
                path,'explicit record metadata',stamp,'fresh declared metadata, not authentication',
                'Supply/review this input only; do not assign a global TTL or stop existing protection',
                severity='PREREQUISITE' if status=='INCOMPLETE' else 'ERROR'))
    setup=inputs.setup
    inspect('setup.data_as_of',setup,setup.data_as_of,policy.max_market_age_seconds,('plan','admission'))
    inspect('setup.market_state',setup.market_state,setup.market_state.observed_at,policy.max_market_age_seconds,
            ('plan','admission'),setup.market_state.status=='available' and setup.market_state.source is not None)
    for e in setup.structure_evidence:
        inspect('structure.'+e.evidence_id,e,e.observed_at,policy.max_structure_age_seconds,('structure','admission'),
                e.status=='available' and e.source is not None)
    for data in setup.data_coverage.items:
        if data.required or data.name in policy.required_data:
            inspect('coverage.'+data.name,data,data.observed_at,
                policy.max_structure_age_seconds if data.name=='structure' else policy.max_market_age_seconds,
                ('admission',),data.status=='available' and data.source is not None)
    absent=set(policy.required_data)-{d.name for d in setup.data_coverage.items}
    for name in sorted(absent):
        inspect('coverage.'+name,None,None,policy.max_market_age_seconds,('admission',),False)
    c=setup.cost_assumptions
    inspect('costs',c,c.observed_at,policy.max_cost_age_seconds,('net_rr_applicability','admission'),c.source is not None)
    card,decision,account,venue=inputs.scorecard,inputs.admission,inputs.account,inputs.exchange
    inspect('scorecard',card,None if card is None else card.context.evaluated_at,policy.max_scorecard_age_seconds,('admission',))
    inspect('admission',decision,None if decision is None else decision.evaluated_at,policy.decision_ttl_seconds,('eligibility',))
    inspect('account',account,None if account is None else account.observed_at,policy.max_account_age_seconds,('admission','reservation'),
            account is not None and account.status=='confirmed' and account.source in policy.allowed_risk_sources)
    inspect('exchange',venue,None if venue is None else venue.observed_at,policy.max_exchange_age_seconds,('rules','sizing'),
            venue is not None and venue.status=='confirmed' and venue.source is not None)
    for path,deadline in (('setup.valid_until',setup.valid_until),('admission.valid_until',None if decision is None else decision.valid_until)):
        if deadline is None or now>=_d(deadline):
            problems.append(issue('VALIDITY_MISSING_OR_EXPIRED',path,'declared validity',deadline,'strictly later than evaluated_at',
                'A stale result is not new eligibility',severity='PREREQUISITE' if deadline is None else 'ERROR'))
    if account is not None:
        if account.instance_id!=bundle.manifest.instance_id or account.mode!='paper':
            problems.append(issue('ACCOUNT_SCOPE_CONFLICT','account','record metadata','wrong instance/mode',
                bundle.manifest.instance_id+' / paper','Never inherit host account identity'))
        required=('day_started_at','day_ends_at','equity_usdt','available_margin_usdt','margin_used_usdt',
            'day_realized_loss_usdt','unrealized_loss_usdt','reserved_risk_usdt','trades_today','consecutive_losses',
            'positions','pending_entries','paused','reconciliation_clear','margin_mode','configured_leverage',
            'auto_add_margin_enabled','martingale_enabled')
        missing=[k for k in required if getattr(account,k) is None]
        missing += ['limits.'+k for k in type(account.limits).model_fields if getattr(account.limits,k) is None]
        if missing:
            problems.append(issue('ACCOUNT_SAFETY_METADATA_INCOMPLETE','account','record metadata',missing,'all critical fields explicit',
                'Configuration ceilings are not a trusted AccountHardLimits snapshot',severity='PREREQUISITE'))
        if account.day_started_at is not None and account.day_ends_at is not None and not _d(account.day_started_at)<=now<_d(account.day_ends_at):
            problems.append(issue('RISK_DAY_METADATA_MISMATCH','account.day_started_at','record metadata','outside declared day',
                'current matching risk day','Net-cash and cumulative-loss day semantics must be supplied independently'))
        for field,expected in (('margin_mode','ISOLATED'),('auto_add_margin_enabled',False),('martingale_enabled',False)):
            value=getattr(account,field)
            if value is not None and value!=expected:
                problems.append(issue('UNSAFE_DECLARED_ACCOUNT_MODE','account.'+field,'record metadata',value,str(expected),
                    'Describes a conflict only; no account mutation or admission override'))
    request=inputs.request
    if request is None:
        problems.append(issue('REQUEST_METADATA_MISSING','request','caller',None,'explicit single-trade application',
            'Never infer leverage, risk budget or margin mode from high score',severity='PREREQUISITE'))
    else:
        for field in ('risk_budget_usdt','action','leverage','margin_mode','auto_add_margin','loss_recovery_sizing'):
            if getattr(request,field) is None:
                problems.append(issue('REQUEST_METADATA_MISSING','request.'+field,'caller',None,'explicit requested value',
                    'Not a configured default or permission',severity='PREREQUISITE'))
        for field,expected in (('action','OPEN'),('margin_mode','ISOLATED'),('auto_add_margin',False),
                               ('loss_recovery_sizing',False),('sizing_basis','quality_risk_budget')):
            if getattr(request,field) is not None and getattr(request,field)!=expected:
                problems.append(issue('REQUEST_MODE_CONFLICT','request.'+field,'caller',getattr(request,field),str(expected),
                    'Existing strategy does not support loss adding or automatic margin topups'))
        cap=next(l.effective_value for l in bundle.comparable_limits if l.semantic=='max_leverage')
        if request.leverage is not None and request.leverage>cap:
            problems.append(issue('REQUEST_LEVERAGE_CONFLICT','request.leverage','caller',request.leverage,str(cap),
                'No score-to-leverage mapping or automatic cap override'))
        # Check receipt metadata independently of claimed verified=true. This
        # does not authenticate any signer, source or exchange capability.
        confirmations={c.evidence_id:c for c in request.confirmations}
        refs=set(setup.entry.evidence_ids)|set(setup.initial_stop.evidence_ids)
        refs.update(e for t in setup.targets for e in t.evidence_ids)
        evidence={e.evidence_id:e for e in setup.structure_evidence}
        for ref in sorted(refs):
            c=confirmations.get(ref)
            inspect('confirmation.'+ref,c,None if c is None else c.checked_at,
                policy.max_structure_age_seconds,('structure','admission'),
                c is not None and c.verified is True and c.verifier in policy.allowed_evidence_verifiers
                and ref in evidence and c.evidence_digest==fingerprint(evidence[ref]))
        review=request.invalidation_review
        inspect('invalidation_review',review,None if review is None else review.checked_at,
            policy.max_market_age_seconds,('plan','admission'),review is not None
            and review.all_conditions_clear is True and review.verifier in policy.allowed_evidence_verifiers
            and review.setup_digest==fingerprint(setup))
    if venue is not None:
        if (venue.exchange,venue.symbol,venue.contract_type,venue.order_type)!=(bundle.manifest.exchange,setup.symbol,'linear_usdt',setup.entry.order_type):
            problems.append(issue('EXCHANGE_RULE_SCOPE_CONFLICT','exchange','record metadata','wrong exchange/symbol/contract/order type',
                'matching linear USDT rule scope','No inferred symbol or account mapping'))
        for field in ('quantity_step','min_quantity','max_quantity','min_notional_usdt','price_tick','max_leverage'):
            if getattr(venue,field) is None:
                problems.append(issue('EXCHANGE_RULES_INCOMPLETE','exchange.'+field,'record metadata',None,
                    'explicit rule in original units','Do not copy paper defaults as verified rules',severity='PREREQUISITE'))
    if inputs.exit_rules is None:
        problems.append(issue('EXIT_RULES_MISSING','exit_rules','future adapter',None,'explicit original-quantity/protection rules',
            'Capabilities need authentication and execution verification',severity='PREREQUISITE'))
    else:
        er=inputs.exit_rules
        if er.symbol!=setup.symbol:
            problems.append(issue('EXIT_RULE_SYMBOL_CONFLICT','exit_rules.symbol','rule metadata',er.symbol,setup.symbol,
                'Do not remap rules from another instrument'))
        if er.verified is not True:
            problems.append(issue('EXIT_RULE_METADATA_UNVERIFIED','exit_rules.verified','rule metadata',False,
                'explicit evidence pending; true would still not authenticate','Future adapter must verify the rule source',severity='PREREQUISITE'))
        pairs=() if venue is None else ((er.symbol,venue.symbol),(er.quantity_step,venue.quantity_step),(er.price_tick,venue.price_tick),
               (er.min_quantity,venue.min_quantity),(er.min_notional,venue.min_notional_usdt),(er.max_quantity,venue.max_quantity))
        if any(a!=b for a,b in pairs):
            problems.append(issue('ENTRY_EXIT_RULES_CONFLICT','exit_rules','rule snapshots','different units or values',
                'same declared venue rules, exemptions stated separately','Do not silently use entry precision for a different exit contract'))
    return problems,observations


def _status(issues):
    if any(i.severity=='ERROR' for i in issues): return 'FAIL'
    if any(i.severity=='UNSUPPORTED' for i in issues): return 'UNSUPPORTED'
    if any(i.severity=='PREREQUISITE' for i in issues): return 'INCOMPLETE'
    return 'PASS'


def validate_contract(compilation, inputs=None, *, evaluated_at=None):
    """Separate parsing, config, plan and metadata statuses; NEVER execution permission."""
    if type(compilation) is ConfigBundle:
        compilation=verify_bundle(compilation)
    elif type(compilation) is ConfigCompilation and compilation.bundle is not None:
        compilation=verify_bundle(compilation.bundle)
    elif type(compilation) is not ConfigCompilation:
        raise ConfigurationError('Explicit compilation or bundle required')
    elif compilation.bundle is None and compilation.parsing!='FAIL':
        raise ConfigurationError('Successful parsing cannot omit its immutable bundle')
    problems=list(compilation.issues)
    bundle=compilation.bundle
    plan_status=runtime_status='NOT_EVALUATED'
    comparison=None
    observations=()
    now=None if evaluated_at is None else _time(evaluated_at)
    if inputs is not None and compilation.parsing=='PASS' and compilation.consistency=='PASS':
        if type(inputs) is not PlanInputs: raise ConfigurationError('Explicit PlanInputs required; never Signal/order')
        inputs=PlanInputs.model_validate(inputs.model_dump())
        if now is None:
            problems.append(issue('EVALUATION_TIME_REQUIRED','evaluated_at','caller',None,'explicit UTC seconds',
                'No implicit system clock',severity='PREREQUISITE'))
            plan_status=runtime_status='INCOMPLETE'
        else:
            with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)):
                p,comparison=_plan_checks(bundle,inputs,now)
                r,observations=_runtime_checks(bundle,inputs,now)
                problems.extend(p+r)
                plan_status,runtime_status=_status(p),_status(r)
    problems.append(issue('TRUSTED_RUNTIME_NOT_CONNECTED','runtime_trust','Stage 7 boundary','not connected',
        'trusted suppliers, locked recheck, atomic reservation and reconciliation',
        'Hashes/verified flags/metadata checks do not authenticate inputs',severity='PREREQUISITE'))
    return ContractValidationResult(bundle_digest=None if bundle is None else bundle.bundle_digest,
        config_parsing=compilation.parsing,config_consistency=compilation.consistency,plan_consistency=plan_status,
        runtime_metadata=runtime_status,issues=tuple(problems),freshness=tuple(observations),
        exit_comparison=comparison,evaluated_at=now)
