"""Pure Stage 5 admission: fail-closed hard gates, soft ceilings, risk-first sizing.

No account fetching, reservation, order submission, stop modification or runtime
integration. RR mathematics is delegated exclusively to the accepted Stage 3.
"""

from decimal import Context, Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext

from pydantic import ValidationError

from app.setups.models import TradeSetup
from app.setups.rr import calculate_rr
from app.setups.rr_models import RRCalculation
from app.setups.scorecard import score_trade_setup
from app.setups.scorecard_models import DIMENSIONS, Scorecard, ScoringContext
from .models import (AdmissionContractError, AdmissionDecision, AdmissionRequest,
    ConstraintResult, DecisionReason, ExchangeConstraints, InputBinding, PaperRiskSnapshot,
    PlanTerms, fingerprint)
from .policy import AdmissionPolicy

D = Decimal
ZERO = D(0)


def _d(value):
    return D(str(value))


def _reason(code, explanation, *fields, layer='hard'):
    return DecisionReason(code=code, layer=layer, explanation=explanation, fields=fields)


def _terms(setup):
    return PlanTerms(setup_id=setup.setup_id,plan_version=setup.plan_version,symbol=setup.symbol,
        side=setup.side,order_type=setup.entry.order_type,entry_reference=_d(setup.entry.reference_price),
        entry_lower=_d(setup.entry.lower_price),entry_upper=_d(setup.entry.upper_price),
        initial_stop=_d(setup.initial_stop.price),
        targets=tuple((t.target_id,_d(t.price),_d(t.fraction)) for t in setup.targets))


def _finish(result, policy, now, reasons, *, binding=None, terms=None, hard=False, **extra):
    deadline=extra.pop('valid_until',None)
    decision=AdmissionDecision(decision_id='0'*64,policy_version=policy.policy_version,
        result=result,evaluated_at=now,valid_until=deadline,binding=binding,plan_terms=terms,
        hard_gates_passed=hard,reason_codes=tuple(dict.fromkeys(r.code for r in reasons)),
        reasons=tuple(reasons),**extra)
    return decision.model_copy(update={'decision_id':fingerprint(decision)})


def _fresh(reasons, field, stamp, now, age, *, source=None, status=True):
    if status is not True or stamp is None or source is None:
        reasons.append(_reason('UNCONFIRMED_DATA','关键状态缺少可用确认、来源或时间戳',field))
    elif stamp > now or _d(now)-_d(stamp)>_d(age):
        reasons.append(_reason('STALE_OR_FUTURE_DATA','数据超过配置年龄窗口或来自未来',field))


def _rr_min(rr):
    scenarios=(rr.reference,rr.entry_lower,rr.entry_upper)
    if rr.status!='complete' or any(s is None or s.net_rr is None for s in scenarios):
        return None
    return min(s.net_rr for s in scenarios)


def _used_evidence(setup):
    return tuple(dict.fromkeys((*setup.entry.evidence_ids,*setup.initial_stop.evidence_ids,
                               *(key for t in setup.targets for key in t.evidence_ids))))


def _check_structure(setup, request, policy, now, reasons):
    records={e.evidence_id:e for e in setup.structure_evidence}
    confirmations={c.evidence_id:c for c in request.confirmations}
    refs=_used_evidence(setup)
    if setup.origin!='native_plan' or setup.entry.basis!='structure_zone' or not setup.entry.evidence_ids:
        reasons.append(_reason('ENTRY_STRUCTURE_UNCONFIRMED','需要原生结构入场计划；旧 Signal 适配不是结构认证','entry','origin'))
    if not setup.initial_stop.evidence_ids:
        reasons.append(_reason('STOP_STRUCTURE_MISSING','必须提供初始止损的结构依据','initial_stop.evidence_ids'))
    if not setup.targets:
        reasons.append(_reason('TARGETS_MISSING','必须提供真实目标，不补造目标','targets'))
    for target in setup.targets:
        if target.kind!='structure' or not target.evidence_ids:
            reasons.append(_reason('TARGET_STRUCTURE_MISSING','每个目标必须是有依据的结构目标','targets.'+target.target_id))
    for key in refs:
        evidence=records[key]
        _fresh(reasons,'evidence.'+key,evidence.observed_at,now,policy.max_structure_age_seconds,
               source=evidence.source,status=evidence.status=='available')
        proof=confirmations.get(key)
        if (proof is None or proof.verified is not True or proof.verifier not in policy.allowed_evidence_verifiers
            or proof.evidence_digest!=fingerprint(evidence)):
            reasons.append(_reason('EVIDENCE_NOT_CONFIRMED','结构必须有绑定当前内容的独立确认记录，高 RR 不能代替确认','evidence.'+key))
        else:
            _fresh(reasons,'confirmation.'+key,proof.checked_at,now,policy.max_structure_age_seconds,source=proof.verifier)
            if evidence.observed_at is not None and proof.checked_at is not None and proof.checked_at<evidence.observed_at:
                reasons.append(_reason('EVIDENCE_CONFIRMATION_PREDATES_DATA','确认时间早于被确认数据','confirmation.'+key))
        if evidence.price is None:
            reasons.append(_reason('STRUCTURE_PRICE_MISSING','结构价格未知，不能默认合法','evidence.'+key))
    loss_kinds=('support','swing_low','range_boundary') if setup.side=='LONG' else ('resistance','swing_high','range_boundary')
    profit_kinds=('resistance','swing_high','range_boundary') if setup.side=='LONG' else ('support','swing_low','range_boundary')
    for key in setup.entry.evidence_ids:
        e=records[key]
        if e.price is None or not setup.entry.lower_price<=e.price<=setup.entry.upper_price:
            reasons.append(_reason('ENTRY_STRUCTURE_INVALID','引用结构价格不在给定入场区间内','entry.evidence_ids'))
    for key in setup.initial_stop.evidence_ids:
        e=records[key]
        valid=e.price is not None and e.kind in loss_kinds and (
            setup.initial_stop.price<=e.price<setup.entry.lower_price if setup.side=='LONG'
            else setup.entry.upper_price<e.price<=setup.initial_stop.price)
        if not valid:
            reasons.append(_reason('STOP_STRUCTURE_INVALID','止损不在适当亏损侧结构以外，禁止通过高分覆盖','initial_stop'))
    for t in setup.targets:
        for key in t.evidence_ids:
            e=records[key]
            valid=e.price is not None and e.kind in profit_kinds and (
                setup.entry.upper_price<t.price<=e.price if setup.side=='LONG'
                else e.price<=t.price<setup.entry.lower_price)
            if not valid:
                reasons.append(_reason('TARGET_STRUCTURE_INVALID','目标超出引用结构支持；不能为提高 RR 拉远目标','targets.'+t.target_id))
    invalidations=[c for c in setup.invalidation_conditions if c.kind=='price']
    if not invalidations or not all(
        c.operator in ('lt','lte') and setup.initial_stop.price<=c.price<setup.entry.lower_price if setup.side=='LONG'
        else c.operator in ('gt','gte') and setup.entry.upper_price<c.price<=setup.initial_stop.price
        for c in invalidations):
        reasons.append(_reason('STOP_INVALIDATION_INCONSISTENT','缺少与方向和初始止损一致的价格失效条件','invalidation_conditions'))
    review=request.invalidation_review
    if (review is None or review.all_conditions_clear is not True or review.setup_digest!=fingerprint(setup)
        or review.verifier not in policy.allowed_evidence_verifiers):
        reasons.append(_reason('INVALIDATION_STATE_UNCONFIRMED','全部失效条件必须经当前计划内容绑定的确认，叙述性条件不能默认未触发','request.invalidation_review'))
    else:
        _fresh(reasons,'invalidation_review',review.checked_at,now,policy.max_market_age_seconds,source=review.verifier)
        if review.checked_at is not None and review.checked_at<setup.created_at:
            reasons.append(_reason('INVALIDATION_REVIEW_PREDATES_PLAN','失效审查不能早于计划创建','request.invalidation_review'))
    quotes=[x for x in (setup.market_state.reference_price,
            setup.market_state.ask if setup.side=='LONG' else setup.market_state.bid) if x is not None]
    for c in setup.invalidation_conditions:
        if c.kind=='time' and c.at<=now:
            reasons.append(_reason('PLAN_INVALIDATED','已达到明确的时间失效条件','invalidation_conditions.'+c.condition_id))
        elif c.kind=='price':
            triggered=any({'lt':x<c.price,'lte':x<=c.price,'gt':x>c.price,'gte':x>=c.price,'eq':x==c.price}[c.operator] for x in quotes)
            if triggered:
                reasons.append(_reason('PLAN_INVALIDATED','参考价或可成交报价已触发价格失效，确认记录不能覆盖事实','invalidation_conditions.'+c.condition_id))


def _check_plan_data(setup, rr, card, request, policy, now, reasons):
    if not policy.enabled:
        reasons.append(_reason('ADMISSION_DISABLED','准入策略已停用'))
    if setup.symbol not in policy.allowed_symbols:
        reasons.append(_reason('SYMBOL_NOT_ALLOWED','标的不在显式配置范围','symbol'))
    if setup.created_at>now or setup.valid_until is None or now>=setup.valid_until:
        reasons.append(_reason('PLAN_EXPIRED_OR_UNTIMED','计划过期、时间未知或来自未来','created_at','valid_until'))
    _fresh(reasons,'data_as_of',setup.data_as_of,now,policy.max_market_age_seconds,source='plan')
    _fresh(reasons,'scorecard.context',card.context.evaluated_at,now,policy.max_scorecard_age_seconds,source='scorecard')
    items={i.name:i for i in setup.data_coverage.items}
    required=set(policy.required_data)|{i.name for i in items.values() if i.required}
    for name in sorted(required):
        item=items.get(name)
        if item is None:
            reasons.append(_reason('REQUIRED_DATA_MISSING','配置要求的数据项不存在','data_coverage.'+name))
            continue
        age=policy.max_structure_age_seconds if name=='structure' else policy.max_market_age_seconds
        _fresh(reasons,'data_coverage.'+name,item.observed_at,now,age,source=item.source,status=item.status=='available')
    if setup.data_coverage.coverage_ratio is None or _d(setup.data_coverage.coverage_ratio)<policy.minimum_data_coverage:
        reasons.append(_reason('DATA_COVERAGE_LOW','整体数据覆盖度低于配置门槛','data_coverage.coverage_ratio'))
    market=setup.market_state
    _fresh(reasons,'market_state',market.observed_at,now,policy.max_market_age_seconds,
           source=market.source,status=market.status=='available')
    market_fields=('reference_price','bid','ask','volatility_pct','btc_return_3m_pct','eth_return_3m_pct')
    for name in market_fields:
        if getattr(market,name) is None:
            reasons.append(_reason('MARKET_STATE_UNKNOWN','关键市场状态未知','market_state.'+name))
    if market.volatility_pct is not None and market.volatility_pct>policy.max_volatility_pct:
        reasons.append(_reason('ABNORMAL_VOLATILITY','波动率超过硬限制','market_state.volatility_pct'))
    if setup.side=='LONG' and market.btc_return_3m_pct is not None and market.btc_return_3m_pct<=policy.btc_crash_3m_pct:
        reasons.append(_reason('BTC_CRASH_LONG_BLOCK','BTC 同步急跌，禁止 SOL 做多','market_state.btc_return_3m_pct'))
    if market.bid is not None and market.ask is not None:
        spread=(_d(market.ask)-_d(market.bid)) / ((_d(market.ask)+_d(market.bid))/2) * 10000
        if spread>_d(policy.max_spread_bps):
            reasons.append(_reason('ABNORMAL_SPREAD','盘口价差超过硬限制','market_state.bid','market_state.ask'))
    if market.reference_price is not None:
        distance=max(_d(setup.entry.lower_price)-_d(market.reference_price),
                     _d(market.reference_price)-_d(setup.entry.upper_price),ZERO)
        if distance/_d(setup.entry.reference_price)*10000>_d(policy.max_entry_deviation_bps):
            reasons.append(_reason('ENTRY_DEVIATION_LIMIT','当前价格偏离计划区间，不能自动改入场价','market_state.reference_price'))
    executable=market.ask if setup.side=='LONG' else market.bid
    if executable is not None:
        distance=max(_d(setup.entry.lower_price)-_d(executable),_d(executable)-_d(setup.entry.upper_price),ZERO)
        if (setup.entry.order_type=='MARKET' and distance>0) or distance/_d(setup.entry.reference_price)*10000>_d(policy.max_entry_deviation_bps):
            reasons.append(_reason('EXECUTABLE_QUOTE_OUTSIDE_PLAN','可成交报价超出市价入场区间或限价偏离上限，不能忽略报价重算风险','market_state.bid','market_state.ask'))
    costs=setup.cost_assumptions
    _fresh(reasons,'cost_assumptions',costs.observed_at,now,policy.max_cost_age_seconds,source=costs.source)
    for field,floor,ceiling in (
        ('entry_fee_rate',policy.minimum_entry_fee_rate,None),('exit_fee_rate',policy.minimum_exit_fee_rate,None),
        ('entry_slippage_bps',policy.minimum_entry_slippage_bps,policy.maximum_entry_slippage_bps),
        ('exit_slippage_bps',policy.minimum_exit_slippage_bps,policy.maximum_exit_slippage_bps)):
        value=getattr(costs,field)
        if value is None:
            reasons.append(_reason('COSTS_UNKNOWN','成本缺失，不能以零或毛 RR 代替','cost_assumptions.'+field))
        elif _d(value)<floor or (ceiling is not None and _d(value)>ceiling):
            reasons.append(_reason('COST_ASSUMPTION_OUT_OF_BOUNDS','成本假设低于配置下界或滑点超过配置上界','cost_assumptions.'+field))
    if costs.funding_cost_usdt is None or costs.assumed_holding_seconds is None:
        reasons.append(_reason('FUNDING_HORIZON_UNKNOWN','必须明确资金费及其持仓时长假设','cost_assumptions'))
    elif costs.assumed_holding_seconds>policy.max_holding_assumption_seconds:
        reasons.append(_reason('COST_HORIZON_TOO_LONG','资金费假设时间跨度超过配置范围','cost_assumptions.assumed_holding_seconds'))
    if _rr_min(rr) is None:
        reasons.append(_reason('NET_RR_UNAVAILABLE','参考和区间两端的完整整单净 RR 均为必需；不使用毛 RR 或最远 TP','rr'))
    elif _rr_min(rr)<policy.minimum_net_rr:
        reasons.append(_reason('NET_RR_BELOW_HARD_FLOOR','整单净 RR 的最差入场场景不足硬门槛','rr'))
    if setup.risk_budget.max_loss_usdt is None or setup.risk_budget.max_loss_usdt<=0:
        reasons.append(_reason('PLAN_RISK_BUDGET_UNKNOWN','计划必须明确正数止损风险预算','risk_budget.max_loss_usdt'))
    if setup.rejection_reasons:
        reasons.append(_reason('PRIOR_REJECTION_UNRESOLVED','计划保留未解除的拒绝记录；不能通过评分清除','rejection_reasons'))
    _check_structure(setup,request,policy,now,reasons)


def _check_account(account, exchange, request, setup, policy, now, reasons):
    _fresh(reasons,'account',account.observed_at,now,policy.max_account_age_seconds,source=account.source,
           status=account.status=='confirmed' and account.source in policy.allowed_risk_sources)
    if account.mode!='paper':
        reasons.append(_reason('PAPER_ONLY','本阶段仅接受 Paper 风险快照，实盘不可开启','account.mode'))
    for name in ('day_started_at','day_ends_at','equity_usdt','available_margin_usdt','margin_used_usdt',
                 'day_realized_loss_usdt','unrealized_loss_usdt','reserved_risk_usdt','trades_today',
                 'consecutive_losses','positions','pending_entries','paused','reconciliation_clear',
                 'margin_mode','configured_leverage','auto_add_margin_enabled','martingale_enabled'):
        if getattr(account,name) is None:
            reasons.append(_reason('ACCOUNT_STATE_UNKNOWN','关键账户安全状态不得默认为通过','account.'+name))
    for name in type(account.limits).model_fields:
        if getattr(account.limits,name) is None:
            reasons.append(_reason('ACCOUNT_HARD_LIMIT_UNKNOWN','必须显式提供当前账户硬上限，不能只依赖评分配置','account.limits.'+name))
    for name in ('risk_budget_usdt','action','leverage','margin_mode','auto_add_margin','loss_recovery_sizing'):
        if getattr(request,name) is None:
            reasons.append(_reason('REQUEST_STATE_UNKNOWN','开仓意图的安全属性未知','request.'+name))
    if account.paused is not False or account.reconciliation_clear is not True:
        reasons.append(_reason('ACCOUNT_HALTED_OR_UNRECONCILED','账户已暂停或对账状态未明确就绪','account'))
    if request.action!='OPEN':
        reasons.append(_reason('ADDING_FORBIDDEN','本阶段只准入新开仓，不允许加仓（包括亏损加仓）','request.action'))
    if request.margin_mode!='ISOLATED' or account.margin_mode!='ISOLATED':
        reasons.append(_reason('ISOLATED_MARGIN_REQUIRED','必须明确使用逐仓，不能自动更改保证金模式','margin_mode'))
    if request.auto_add_margin is not False or account.auto_add_margin_enabled is not False:
        reasons.append(_reason('AUTO_MARGIN_FORBIDDEN','禁止自动追加保证金，未知状态同样拒绝','auto_add_margin'))
    if request.loss_recovery_sizing is not False or account.martingale_enabled is not False or request.sizing_basis!='quality_risk_budget':
        reasons.append(_reason('MARTINGALE_OR_RECOVERY_FORBIDDEN','禁止马丁、追损放大或未知仓位依据','sizing_basis'))
    if account.day_started_at is not None and account.day_ends_at is not None:
        if not (account.day_started_at<=now<account.day_ends_at) or account.observed_at is None or not (account.day_started_at<=account.observed_at<account.day_ends_at):
            reasons.append(_reason('RISK_DAY_MISMATCH','计数与风险预算不属于当前完整风险日','account.day_started_at','account.day_ends_at'))
    _fresh(reasons,'exchange',exchange.observed_at,now,policy.max_exchange_age_seconds,
           source=exchange.source,status=exchange.status=='confirmed')
    if exchange.exchange not in policy.allowed_exchanges or exchange.symbol!=setup.symbol or exchange.order_type!=setup.entry.order_type or exchange.contract_type!='linear_usdt':
        reasons.append(_reason('EXCHANGE_RULES_MISMATCH','必须提供当前标的及订单类型的线性 USDT 合约约束','exchange'))
    for name in ('quantity_step','min_quantity','max_quantity','min_notional_usdt','price_tick','max_leverage'):
        if getattr(exchange,name) is None:
            reasons.append(_reason('EXCHANGE_RULES_UNKNOWN','交易所数量/精度约束未知','exchange.'+name))
    if exchange.min_quantity is not None and exchange.max_quantity is not None and exchange.min_quantity>exchange.max_quantity:
        reasons.append(_reason('EXCHANGE_RULES_INVALID','交易所最小数量超过最大数量','exchange'))
    if exchange.price_tick is not None:
        prices=[setup.initial_stop.price,*(t.price for t in setup.targets)]
        if setup.entry.order_type=='LIMIT': prices.append(setup.entry.reference_price)
        if any(_d(price)%exchange.price_tick!=0 for price in prices):
            reasons.append(_reason('PRICE_PRECISION_INVALID','止损、目标或限价不符合价格精度；禁止自动改价凑规则','exchange.price_tick'))
    # Numeric checks only once all required safety state is explicit.
    needed=('equity_usdt','available_margin_usdt','margin_used_usdt','day_realized_loss_usdt','unrealized_loss_usdt',
            'reserved_risk_usdt','trades_today','consecutive_losses','positions','pending_entries','configured_leverage')
    if (any(getattr(account,n) is None for n in needed) or any(getattr(account.limits,n) is None for n in type(account.limits).model_fields)
        or request.risk_budget_usdt is None or request.leverage is None or exchange.max_leverage is None):
        return None
    limits={name:min(getattr(policy,name),getattr(account.limits,name)) for name in type(account.limits).model_fields}
    if account.margin_used_usdt+account.available_margin_usdt>account.equity_usdt:
        reasons.append(_reason('ACCOUNT_BALANCE_INCONSISTENT','已用与可用保证金超过权益，快照不一致','account'))
    if request.risk_budget_usdt>limits['max_loss_per_trade_usdt']:
        reasons.append(_reason('SINGLE_TRADE_RISK_LIMIT','显式申请风险超过账户/配置单笔硬上限，不能以高分覆盖','request.risk_budget_usdt'))
    leverage_limit=min(limits['max_leverage'],exchange.max_leverage)
    if setup.position_limit_advice.leverage_cap is not None:
        leverage_limit=min(leverage_limit,setup.position_limit_advice.leverage_cap)
    if request.leverage>leverage_limit or account.configured_leverage!=request.leverage:
        reasons.append(_reason('LEVERAGE_LIMIT_OR_MISMATCH','杠杆超过最严上限或与确认状态不一致；评分不调整杠杆','leverage'))
    if account.day_realized_loss_usdt+account.unrealized_loss_usdt>=limits['daily_loss_limit_usdt']:
        reasons.append(_reason('DAILY_LOSS_LIMIT','达到当日亏损硬上限','account.day_realized_loss_usdt','account.unrealized_loss_usdt'))
    if account.consecutive_losses>=limits['max_consecutive_losses']:
        reasons.append(_reason('CONSECUTIVE_LOSS_HALT','连续亏损熔断','account.consecutive_losses'))
    if account.trades_today+len(account.pending_entries)>=limits['max_trades_per_day']:
        reasons.append(_reason('DAILY_TRADE_LIMIT','已用交易次数加待入场名额达到每日上限','account.trades_today','account.pending_entries'))
    if len(account.positions)+len(account.pending_entries)>=limits['max_positions']:
        reasons.append(_reason('MAX_POSITIONS_LIMIT','持仓与待入场数量达到硬上限','account.positions','account.pending_entries'))
    if any(x.symbol==setup.symbol for x in (*account.positions,*account.pending_entries)):
        reasons.append(_reason('CONFLICTING_POSITION_OR_ORDER','同币种持仓/待入场冲突，禁止重复开仓或亏损加仓','account'))
    if account.margin_used_usdt>min(account.equity_usdt*limits['max_margin_ratio'],limits['max_margin_usdt']):
        reasons.append(_reason('EXISTING_MARGIN_LIMIT','当前已用保证金已超过硬上限','account.margin_used_usdt'))
    return limits


def _size(setup, rr, card, account, exchange, request, policy, limits, market, tier, now, binding, reasons):
    terms=_terms(setup)
    required_rr=max(policy.minimum_net_rr,market.minimum_net_rr,tier.minimum_net_rr)
    if _rr_min(rr)<required_rr:
        return _finish('REJECT',policy,now,[*reasons,_reason('NET_RR_BELOW_CONTEXT_FLOOR','市场/等级要求只能提高净 RR 底线，不得降低硬门槛','rr')],binding=binding,terms=terms)
    remaining=max(ZERO,limits['daily_loss_limit_usdt']-account.day_realized_loss_usdt-account.unrealized_loss_usdt-account.reserved_risk_usdt)
    caps={'REQUEST':request.risk_budget_usdt,'SINGLE_TRADE':limits['max_loss_per_trade_usdt'],
          'PLAN':_d(setup.risk_budget.max_loss_usdt),'DAILY_REMAINING':remaining,
          'EQUITY_RISK':account.equity_usdt*policy.max_risk_fraction_of_equity}
    if setup.risk_budget.remaining_daily_loss_usdt is not None:
        caps['PLAN_DAILY_REMAINING']=_d(setup.risk_budget.remaining_daily_loss_usdt)
    if setup.risk_budget.max_risk_fraction_of_equity is not None:
        caps['PLAN_EQUITY_RISK']=account.equity_usdt*_d(setup.risk_budget.max_risk_fraction_of_equity)
    base=min(request.risk_budget_usdt,limits['max_loss_per_trade_usdt'])
    caps.update(SCORE=base*tier.risk_fraction,MARKET=base*market.risk_fraction)
    budget=min(caps.values())
    if budget<=0 or budget<policy.minimum_risk_budget_usdt:
        return _finish('REJECT',policy,now,[*reasons,_reason('RISK_BUDGET_EXHAUSTED','扣除既有亏损/预留及各上限后预算不足','risk_budget',layer='sizing')],binding=binding,terms=terms,opportunity_tier=tier.name)
    scenarios=(rr.reference,rr.entry_lower,rr.entry_upper)
    # Sizing inversion only. Costs come from Stage 3, not a second RR algorithm.
    # Positive fixed funding consumes budget; expected funding credits never finance extra size.
    base_loss=max(s.gross_risk_per_unit+s.stop_costs.entry_fee_per_unit+s.stop_costs.exit_fee_per_unit
                  +s.stop_costs.entry_slippage_per_unit+s.stop_costs.exit_slippage_per_unit for s in scenarios)
    fixed_funding=max(ZERO,_d(setup.cost_assumptions.funding_cost_usdt))
    if budget<=fixed_funding:
        return _finish('REJECT',policy,now,[*reasons,_reason('FIXED_COST_EXCEEDS_BUDGET','固定资金费已耗尽允许风险预算',layer='sizing')],binding=binding,terms=terms,opportunity_tier=tier.name)
    max_price=max(s.effective_entry_price for s in scenarios)
    min_price=min(s.entry_price for s in scenarios)
    entry_fee=max(s.stop_costs.entry_fee_per_unit for s in scenarios)
    margin_room=min(account.available_margin_usdt,
                    account.equity_usdt*limits['max_margin_ratio']-account.margin_used_usdt,
                    limits['max_margin_usdt']-account.margin_used_usdt)
    advice=setup.position_limit_advice
    if advice.max_margin_usdt is not None: margin_room=min(margin_room,_d(advice.max_margin_usdt))
    if advice.max_margin_fraction_of_equity is not None: margin_room=min(margin_room,account.equity_usdt*_d(advice.max_margin_fraction_of_equity))
    margin_room=max(ZERO,margin_room)
    quantity_caps={
        'RISK_BUDGET':(budget-fixed_funding)/base_loss,
        'AVAILABLE_MARGIN':margin_room/(max_price/_d(request.leverage)+entry_fee),
        'GRADE_NOTIONAL':account.equity_usdt*tier.max_notional_equity_ratio/max_price,
        'POSITION_NOTIONAL':limits['max_position_notional_usdt']/max_price,
        'POSITION_QUANTITY':limits['max_position_quantity'],
        'EXCHANGE_QUANTITY':exchange.max_quantity}
    if advice.max_quantity is not None: quantity_caps['PLAN_QUANTITY']=_d(advice.max_quantity)
    if advice.max_notional_usdt is not None: quantity_caps['PLAN_NOTIONAL']=_d(advice.max_notional_usdt)/max_price
    raw_quantity=min(quantity_caps.values())
    quantity=(raw_quantity/exchange.quantity_step).to_integral_value(rounding=ROUND_FLOOR)*exchange.quantity_step
    constraints=tuple(ConstraintResult(name=k,quantity_cap=v,explanation='数量不得超过该独立约束上限') for k,v in quantity_caps.items())
    if quantity<=0 or quantity<exchange.min_quantity or quantity*min_price<exchange.min_notional_usdt:
        return _finish('REJECT',policy,now,[*reasons,_reason('EXCHANGE_MINIMUM_EXCEEDS_BUDGET','向下取整后低于交易所最小数量/名义额；绝不向上凑单',layer='sizing')],binding=binding,terms=terms,opportunity_tier=tier.name,constraints=constraints)
    if _d(float(quantity))!=quantity:
        return _finish('REJECT',policy,now,[*reasons,_reason('QUANTITY_NOT_REPRESENTABLE','数量无法无损传给已验收 RR 接口，不隐式舍入',layer='contract')],binding=binding,terms=terms)
    final_rr=calculate_rr(setup,quantity=float(quantity))
    final_min=_rr_min(final_rr)
    if final_min is None or final_min<required_rr:
        return _finish('REJECT',policy,now,[*reasons,_reason('RESIZED_NET_RR_BELOW_FLOOR','按最终数量重算后净 RR 不足或无定义，不能沿用缩仓前 RR',layer='sizing')],binding=binding,terms=terms,opportunity_tier=tier.name,constraints=constraints)
    modeled=max(s.net_stop_loss_usdt for s in (final_rr.reference,final_rr.entry_lower,final_rr.entry_upper))
    risk=max(base_loss*quantity+fixed_funding,modeled)
    if risk>budget or risk<policy.minimum_risk_budget_usdt:
        return _finish('REJECT',policy,now,[*reasons,_reason('FINAL_RISK_OUT_OF_BOUNDS','最终量的保守损失预算超限或小于最小预算',layer='sizing')],binding=binding,terms=terms,opportunity_tier=tier.name)
    reduced=False
    for name,cap in caps.items():
        if cap<request.risk_budget_usdt:
            reduced=True
            reasons.append(_reason('RISK_REDUCED_'+name,'该约束将风险限制在申请值以下：'+str(cap)+' USDT',layer='soft' if name in ('SCORE','MARKET') else 'sizing'))
    for name,cap in quantity_caps.items():
        if cap<quantity_caps['RISK_BUDGET']:
            reduced=True
            reasons.append(_reason('SIZE_REDUCED_'+name,'该约束将数量限制在风险预算反推数量以下',layer='sizing'))
    if quantity<raw_quantity:
        reasons.append(_reason('QUANTITY_ROUNDED_DOWN','按已确认步长向下取整，单独的精度舍入不降机会等级',layer='information'))
    reasons.append(_reason('REDUCED_PAPER_ELIGIBILITY' if reduced else 'STANDARD_PAPER_ELIGIBILITY',
        '硬门槛满足，按缩小后的风险预算具备 Paper 资格' if reduced else '硬门槛满足，按正常计算风险预算具备 Paper 资格',layer='information'))
    deadlines=[setup.valid_until,now+policy.decision_ttl_seconds,account.observed_at+policy.max_account_age_seconds,
               account.day_ends_at,exchange.observed_at+policy.max_exchange_age_seconds,
               setup.data_as_of+policy.max_market_age_seconds,setup.market_state.observed_at+policy.max_market_age_seconds,
               setup.cost_assumptions.observed_at+policy.max_cost_age_seconds,card.context.evaluated_at+policy.max_scorecard_age_seconds]
    deadlines.append(request.invalidation_review.checked_at+policy.max_market_age_seconds)
    used=_used_evidence(setup)
    deadlines.extend(c.checked_at+policy.max_structure_age_seconds for c in request.confirmations if c.evidence_id in used)
    deadlines.extend(e.observed_at+policy.max_structure_age_seconds for e in setup.structure_evidence if e.evidence_id in used)
    deadlines.extend(i.observed_at+(policy.max_structure_age_seconds if i.name=='structure' else policy.max_market_age_seconds)
                     for i in setup.data_coverage.items if i.observed_at is not None and (i.required or i.name in policy.required_data))
    deadline=min(deadlines)
    if deadline<=now:
        return _finish('REJECT',policy,now,[_reason('DECISION_LIFETIME_EXHAUSTED','当前快照已无可用准入有效期')],binding=binding,terms=terms)
    return _finish('REDUCE' if reduced else 'APPROVE',policy,now,reasons,binding=binding,terms=terms,hard=True,
        valid_until=deadline,opportunity_tier=tier.name,policy_risk_ceiling_usdt=budget,
        allowed_risk_budget_usdt=risk,max_quantity=quantity,max_notional_usdt=quantity*max_price,
        max_initial_margin_usdt=quantity*max_price/_d(request.leverage),entry_fee_reserve_usdt=quantity*entry_fee,
        modeled_stop_loss_usdt=modeled,leverage=request.leverage,required_net_rr=required_rr,
        final_min_net_rr=final_min,final_rr=final_rr,constraints=constraints)


def admit_trade(setup: TradeSetup, rr: RRCalculation, scorecard: Scorecard, *, account: PaperRiskSnapshot,
                exchange: ExchangeConstraints, request: AdmissionRequest, policy: AdmissionPolicy,
                evaluated_at: float) -> AdmissionDecision:
    """Pure decision; callers cannot pass Signal/dicts and acquire Paper eligibility."""
    inputs=(setup,rr,scorecard,account,exchange,request,policy)
    classes=(TradeSetup,RRCalculation,Scorecard,PaperRiskSnapshot,ExchangeConstraints,AdmissionRequest,AdmissionPolicy)
    if any(type(value) is not cls for value,cls in zip(inputs,classes)):
        raise AdmissionContractError('Exact Stage 2/3/4 records and explicit Paper risk context are required; Signal is not admission')
    try:
        now=ScoringContext(evaluated_at=evaluated_at).evaluated_at
    except ValidationError as error:
        raise AdmissionContractError('Explicit finite evaluation time is required') from error
    try:
        setup,rr,card,account,exchange,request,policy=(cls.model_validate(value.model_dump()) for cls,value in zip(classes,inputs))
    except (ValidationError,ValueError,TypeError):
        return _finish('REJECT',AdmissionPolicy(),now,[_reason('INPUT_CONTRACT_INVALID','输入记录未通过严格类型/结构/配置校验',layer='contract')])
    binding=InputBinding(**{key:fingerprint(value) for key,value in zip(
        ('setup','rr','scorecard','account','exchange','request','policy'),(setup,rr,card,account,exchange,request,policy))},
        instance_id=account.instance_id,snapshot_revision=account.snapshot_revision)
    terms=_terms(setup)
    reasons=[]
    try:
        q=None if rr.hypothetical_quantity is None else float(rr.hypothetical_quantity)
        if calculate_rr(setup,quantity=q)!=rr:
            reasons.append(_reason('RR_PLAN_MISMATCH','RR 不属于当前计划内容/版本，或计算结果被修改',layer='contract'))
        elif score_trade_setup(setup,rr,evaluated_at=card.context.evaluated_at,
                               max_data_age_seconds=card.context.max_data_age_seconds)!=card:
            reasons.append(_reason('SCORECARD_PLAN_MISMATCH','Scorecard 不属于当前计划/RR/版本或分数被修改',layer='contract'))
    except (ValidationError,ValueError,TypeError,ArithmeticError):
        reasons.append(_reason('UPSTREAM_CALCULATION_INVALID','上游计算记录无法按已验收纯函数复验',layer='contract'))
    if reasons:
        return _finish('REJECT',policy,now,reasons,binding=binding,terms=terms)
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)):
        _check_plan_data(setup,rr,card,request,policy,now,reasons)
        limits=_check_account(account,exchange,request,setup,policy,now,reasons)
        market=next(m for m in policy.markets if m.regime==setup.market_state.regime)
        if not market.allowed:
            reasons.append(_reason('MARKET_REGIME_NOT_ALLOWED','当前市场状态不允许准入','market_state.regime'))
        if reasons:
            return _finish('REJECT',policy,now,reasons,binding=binding,terms=terms)
        # Hard gates have priority. Scores never erase or compensate for them.
        total=card.overall_trade_quality
        if total.score is None or total.status!='complete' or _d(total.coverage)<policy.minimum_score_coverage:
            reasons.append(_reason('SCORECARD_INCOMPLETE','综合分/评分覆盖度不足，不将未知分数默认安全',layer='soft'))
        elif total.score<policy.minimum_total_score:
            reasons.append(_reason('TOTAL_SCORE_BELOW_MINIMUM','综合分低于配置门槛',layer='soft'))
        for floor in policy.dimension_floors:
            component=getattr(card,floor.dimension)
            if component.score is None or component.status!='complete' or component.score<floor.minimum:
                reasons.append(_reason('DIMENSION_SCORE_BELOW_MINIMUM','关键单维不足：'+floor.dimension,'scorecard.'+floor.dimension,layer='soft'))
        tier=next((t for t in policy.tiers if total.score is not None and total.score>=t.minimum_total),None)
        if tier is None:
            reasons.append(_reason('NO_ELIGIBLE_SCORE_TIER','评分不属于配置允许的机会等级',layer='soft'))
        if reasons:
            return _finish('REJECT',policy,now,reasons,binding=binding,terms=terms,hard=True)
        return _size(setup,rr,card,account,exchange,request,policy,limits,market,tier,now,binding,reasons)
