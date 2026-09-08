"""Opt-in plan-quality description; no I/O, clock, configuration, or trading gate.

The seven primitive dimensions are equally weighted into the eighth (overall).
The Stage 2 market-factor ScoreBreakdown is a different, unchanged schema.
"""

from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from .models import TradeSetup
from .rr import calculate_rr
from .rr_models import RRCalculation
from .scorecard_models import (DIMENSIONS, DimensionScore, OverallQuality, Scorecard,
                               ScoreCheck, ScoreIssue, ScoringContext)

D = Decimal
NAMES = ('方向置信度', '入场质量', '止损质量', '止盈质量', '盈亏比质量', '仓位质量', '执行清晰度')


def _d(value):
    return D(str(value))


def _rounded(value):
    return float(_d(value).quantize(D('.01'), rounding=ROUND_HALF_EVEN))


def _label(score):
    if score is None:
        return 'invalid'
    return 'excellent' if score >= 85 else 'good' if score >= 70 else 'fair' if score >= 50 else 'poor'


def _issue(field, code='missing', explanation='缺少明确输入，不能推定为安全或已确认'):
    return ScoreIssue(field=field, code=code, explanation=explanation)


def _check(code, maximum, fraction, explanation, inputs, issues=()):
    return ScoreCheck(code=code, max_points=maximum,
        points=None if issues else _rounded(_d(maximum) * fraction),
        explanation=explanation, inputs=tuple(inputs), issues=tuple(issues))


def _bounded(value):
    return max(D(0), min(D(1), value))


def _describe(checks, name):
    checks = tuple(checks)
    if sum(_d(c.max_points) for c in checks) != 100:
        raise ValueError('Descriptive rubric weights must total 100')
    issues = tuple(i for c in checks for i in c.issues)
    known = _rounded(sum((_d(c.points) for c in checks if c.points is not None), D(0)))
    covered = sum((_d(c.max_points) for c in checks if c.points is not None), D(0))
    score = None if issues else known
    return DimensionScore(score=score, label=_label(score), known_points=known,
        coverage=float(covered / 100), issues=issues, checks=checks,
        status='complete' if not issues else 'partial' if covered else 'unavailable',
        explanation=f'{name}：' + ('输入不足，未给出最终分。' if issues else f'描述分 {score:.2f}/100。')
                    + ' '.join(c.explanation for c in checks))


def _freshness(field, status, source, stamp, context):
    if status != 'available':
        return [_issue(field, status, '该输入未被调用方明确标记为可用')]
    if source is None or stamp is None:
        return [_issue(field, 'missing', '缺少来源或数据时间戳')]
    if _d(context.evaluated_at) - _d(stamp) > _d(context.max_data_age_seconds):
        return [_issue(field, 'stale', '超过本次显式指定的数据年龄窗口；不代表交易拒绝')]
    return []


def _evidence(setup, refs, context, *, needs_price=True):
    coverage = next(i for i in setup.data_coverage.items if i.name == 'structure')
    issues = _freshness('data_coverage.structure', coverage.status, coverage.source, coverage.observed_at, context)
    records = {item.evidence_id: item for item in setup.structure_evidence}
    # References are unique for scoring, even if a caller repeats an ID.
    evidence = [records[key] for key in dict.fromkeys(refs)]
    if not evidence:
        issues.append(_issue('structure_evidence.references'))
    for item in evidence:
        field = 'structure_evidence.' + item.evidence_id
        issues.extend(_freshness(field, item.status, item.source, item.observed_at, context))
        if needs_price and item.price is None:
            issues.append(_issue(field + '.price'))
    return evidence, issues


def _cost_issues(setup, context):
    cost = setup.cost_assumptions
    issues = [_issue('cost_assumptions.' + name) for name in (
        'entry_fee_rate', 'exit_fee_rate', 'entry_slippage_bps',
        'exit_slippage_bps', 'funding_cost_usdt') if getattr(cost, name) is None]
    issues.extend(_freshness('cost_assumptions.provenance', 'available', cost.source, cost.observed_at, context))
    return issues


def _direction(setup, context):
    _, issues = _evidence(setup, (*setup.entry.evidence_ids, *setup.initial_stop.evidence_ids), context,
                          needs_price=False)
    if setup.confidence.value is None:
        issues.append(_issue('confidence.value'))
    return _describe([_check('supplied_evidence_confidence', 100,
        _d(setup.confidence.value) if setup.confidence.value is not None else D(0),
        f'已选方向 {setup.side}；只转述调用方的证据置信度，不从方向、策略名或 RR 推断胜率；'
        '结构依据仅检查来源、状态和时间。',
        ('confidence.value', 'confidence.basis', 'side', 'structure_evidence'), issues)], NAMES[0])


def _entry(setup, context):
    e = setup.entry
    evidence, issues = _evidence(setup, e.evidence_ids, context)
    if e.basis != 'structure_zone':
        issues.append(_issue('entry.basis', 'unverified', '入场没有声明可引用的结构区间依据'))
    supported = sum(_d(e.lower_price) <= _d(x.price) <= _d(e.upper_price)
                    for x in evidence if x.price is not None)
    structural = D(supported) / len(evidence) if evidence else D(0)
    market = setup.market_state
    market_issues = _freshness('market_state', market.status, market.source, market.observed_at, context)
    coverage = next(i for i in setup.data_coverage.items if i.name == 'market_price')
    market_issues.extend(_freshness('data_coverage.market_price', coverage.status,
                                    coverage.source, coverage.observed_at, context))
    if market.reference_price is None:
        market_issues.append(_issue('market_state.reference_price'))
    risk = abs(_d(e.reference_price) - _d(setup.initial_stop.price))
    distance = max(_d(e.lower_price) - _d(market.reference_price or e.reference_price),
                   _d(market.reference_price or e.reference_price) - _d(e.upper_price), D(0))
    return _describe([
        _check('entry_structure', 40, structural, '40 分检查引用的结构价格是否在声明入场区间内。',
               ('entry.evidence_ids', 'entry.basis', 'structure_evidence'), issues),
        _check('market_distance', 40, _bounded(1 - distance / risk),
               f'40 分描述给定市场价距入场区间的距离 / 初始 R = {distance / risk:.4f}；不是限价成交预测。',
               ('market_state.reference_price', 'entry', 'initial_stop'), market_issues),
        _check('entry_range_precision', 20, _bounded(1 - (_d(e.upper_price) - _d(e.lower_price)) / risk),
               '20 分描述区间宽度相对初始 R 的精确程度；窄区间不等于易成交。', ('entry', 'initial_stop'))
    ], NAMES[1])


def _invalidation_quality(setup):
    conditions = [c for c in setup.invalidation_conditions if c.kind == 'price']
    if not conditions:
        return D(0), [_issue('invalidation_conditions.price', explanation='缺少可与初始止损核对的价格失效条件')]
    if setup.side == 'LONG':
        matched = all(c.operator in ('lt', 'lte') and setup.initial_stop.price <= c.price < setup.entry.lower_price
                      for c in conditions)
    else:
        matched = all(c.operator in ('gt', 'gte') and setup.entry.upper_price < c.price <= setup.initial_stop.price
                      for c in conditions)
    return D(int(matched)), []


def _stop(setup, context):
    evidence, issues = _evidence(setup, setup.initial_stop.evidence_ids, context)
    kinds = ('support', 'swing_low', 'range_boundary') if setup.side == 'LONG' else ('resistance', 'swing_high', 'range_boundary')
    for item in evidence:
        if item.kind not in kinds:
            issues.append(_issue('structure_evidence.' + item.evidence_id + '.kind', 'unsupported',
                                 '该依据类型不能在本评分规则中解释这一侧的结构止损'))
    def anchored(x):
        if x.price is None:
            return False
        return (setup.initial_stop.price <= x.price < setup.entry.lower_price if setup.side == 'LONG'
                else setup.entry.upper_price < x.price <= setup.initial_stop.price)
    fraction = D(sum(anchored(x) for x in evidence)) / len(evidence) if evidence else D(0)
    invalidation, invalidation_issues = _invalidation_quality(setup)
    return _describe([
        _check('loss_side_geometry', 20, D(1), '20 分：已验证初始止损位于整个入场区间的亏损侧；不证明抗噪能力。',
               ('initial_stop.price', 'entry', 'side')),
        _check('stop_structure', 50, fraction, '50 分：止损在给定亏损侧结构位以外或相等，不创造新止损。',
               ('initial_stop.evidence_ids', 'structure_evidence'), issues),
        _check('stop_invalidation_consistency', 30, invalidation, '30 分：价格失效条件的方向、触发价与初始止损一致性。',
               ('invalidation_conditions', 'initial_stop', 'side'), invalidation_issues)
    ], NAMES[2])


def _take_profit(setup, rr, context):
    checks = []
    if not setup.targets:
        return _describe([_check('targets_not_supplied', 100, D(0), '没有目标位，不能评价止盈质量。',
                                ('targets',), [_issue('targets')])], NAMES[3])
    issues, supported = [], D(0)
    kinds = ('resistance', 'swing_high', 'range_boundary') if setup.side == 'LONG' else ('support', 'swing_low', 'range_boundary')
    for target in setup.targets:
        evidence, missing = _evidence(setup, target.evidence_ids, context)
        issues.extend(missing)
        if target.kind != 'structure':
            issues.append(_issue('targets.' + target.target_id + '.kind', 'unverified'))
        for item in evidence:
            if item.kind not in kinds:
                issues.append(_issue('structure_evidence.' + item.evidence_id + '.kind', 'unsupported'))
        matched = [x for x in evidence if x.price is not None and (
            setup.entry.upper_price < target.price <= x.price if setup.side == 'LONG'
            else x.price <= target.price < setup.entry.lower_price)]
        if evidence:
            supported += _d(target.fraction) * D(len(matched)) / len(evidence)
    checks.append(_check('target_structure', 50, _bounded(supported),
        '50 分：按原始分批权重描述目标是否有盈利侧结构位支持；不估算到达概率。',
        ('targets', 'structure_evidence'), issues))
    checks.append(_check('profit_side_ordering', 20, D(1), '20 分：目标已按盈利方向排序且位于入场区间外。', ('targets', 'entry')))
    checks.append(_check('allocation_exactness', 20, D(int(rr.fraction_sum == 1)),
                         f'20 分：原始分批比例和为 {rr.fraction_sum}，不自动归一化。', ('targets.fraction',)))
    net_issues = _cost_issues(setup, context)
    if rr.reference is None or not rr.reference.targets or any(t.net_reward_per_unit is None for t in rr.reference.targets):
        net_issues.append(_issue('rr.reference.targets.net_reward_per_unit'))
    positive = sum((t.fraction for t in rr.reference.targets if t.net_reward_per_unit is not None and t.net_reward_per_unit > 0), D(0)) if rr.reference else D(0)
    checks.append(_check('positive_net_target_weight', 10, _bounded(positive),
        '10 分：给定成本下净目标收益为正的分批权重；负收益只作描述，不拒绝交易。',
        ('rr.reference.targets', 'cost_assumptions'), net_issues))
    return _describe(checks, NAMES[3])


def _rr_band(rr):
    # Descriptive bands only. >=3R saturates: arbitrarily distant targets earn no bonus.
    for floor, points in ((D(3), 100), (D(2), 80), (D('1.5'), 60), (D(1), 45), (D(0), 25)):
        if rr >= floor and rr > 0:
            return D(points) / 100
    return D(0)


def _rr_quality(setup, rr, context):
    costs = _cost_issues(setup, context)
    checks = []
    for code, scenarios, paths in (
        ('reference_net_rr', (rr.reference,), ('rr.reference.net_rr',)),
        ('worst_entry_edge_net_rr', (rr.entry_lower, rr.entry_upper),
         ('rr.entry_lower.net_rr', 'rr.entry_upper.net_rr'))):
        issues = list(costs)
        values = [s.net_rr for s in scenarios if s is not None and s.net_rr is not None]
        for scenario, path in zip(scenarios, paths):
            if scenario is None or scenario.net_rr is None:
                issues.append(_issue(path, explanation='缺少完整净 RR；不以毛 RR 或声明的旧 RR 代替'))
        value = min(values) if len(values) == len(scenarios) else None
        checks.append(_check(code, 50, _rr_band(value) if value is not None else D(0),
            f'50 分：{code} = {value}；净 RR 分档仅用于描述，3R 以上不额外加分，不产生门槛。',
            (*paths, 'cost_assumptions'), issues))
    return _describe(checks, NAMES[4])


def _position(setup, rr, context):
    advice = setup.position_limit_advice
    q = rr.hypothetical_quantity
    scenarios = (rr.reference, rr.entry_lower, rr.entry_upper)
    quantity_issues = [] if q is not None else [_issue('rr.hypothetical_quantity', explanation='未提供计算用数量；不能从建议上限反推实际数量')]
    notional = max((s.effective_entry_price * q for s in scenarios
                    if s is not None and s.effective_entry_price is not None and q is not None), default=None)
    losses = [s.net_stop_loss_usdt for s in scenarios if s is not None and s.net_stop_loss_usdt is not None]
    loss = max(losses) if len(losses) == 3 and all(x > 0 for x in losses) else None
    checks = []
    for code, weight, value, limit, field in (
        ('quantity_description', 35, q, advice.max_quantity, 'position_limit_advice.max_quantity'),
        ('notional_description', 35, notional, advice.max_notional_usdt, 'position_limit_advice.max_notional_usdt'),
        ('loss_budget_description', 30, loss, setup.risk_budget.max_loss_usdt, 'risk_budget.max_loss_usdt')):
        issues = list(quantity_issues)
        if limit is None:
            issues.append(_issue(field))
        if value is None:
            issues.append(_issue('rr.' + code))
        if code != 'quantity_description':
            issues.extend(_cost_issues(setup, context))
        if code == 'notional_description' and not all(s is not None and s.effective_entry_price is not None for s in scenarios):
            issues.append(_issue('rr.entry_edges.effective_entry_price'))
        ratio = D(int(value <= _d(limit))) if value is not None and limit is not None else D(0)
        checks.append(_check(code, weight, ratio,
            f'{weight} 分：计算值 {value} 与调用方声明上限 {limit} 的一致性；不生成数量、不检查账户或作准入。',
            ('rr.hypothetical_quantity', field), issues))
    return _describe(checks, NAMES[5])


def _execution(setup, rr, context):
    timing_issues = []
    if setup.valid_until is None:
        timing_issues.append(_issue('valid_until'))
    if setup.data_as_of is None:
        timing_issues.append(_issue('data_as_of'))
    timely = (setup.valid_until is not None and context.evaluated_at < setup.valid_until
              and setup.data_as_of is not None
              and _d(context.evaluated_at) - _d(setup.data_as_of) <= _d(context.max_data_age_seconds))
    invalidation, invalidation_issues = _invalidation_quality(setup)
    return _describe([
        _check('declared_entry_method', 20, D(1), f'20 分：明确声明 {setup.entry.order_type} 入场及价格区间；不提交订单。', ('entry',)),
        _check('explicit_validity', 20, D(int(timely)), '20 分：明确数据截止和计划有效期；过期记低分而不作交易判断。',
               ('data_as_of', 'valid_until', 'context'), timing_issues),
        _check('documented_costs', 20, D(1), '20 分：成本数值、来源及时间完整；不验证交易所真实费用。',
               ('cost_assumptions',), _cost_issues(setup, context)),
        _check('explicit_allocation', 20, D(int(rr.fraction_sum == 1)), '20 分：已声明分批比例，检查精确比例和；不执行分批退出。',
               ('targets.fraction',), [] if setup.targets else [_issue('targets')]),
        _check('explicit_invalidation', 20, invalidation, '20 分：声明可解释且与止损一致的价格失效条件；不移动止损。',
               ('invalidation_conditions',), invalidation_issues)
    ], NAMES[6])


def _overall(dimensions):
    issues = tuple(i for d in dimensions for i in d.issues)
    known = _rounded(sum(_d(d.known_points) for d in dimensions) / 7)
    coverage = float(sum(_d(d.coverage) for d in dimensions) / 7)
    score = None if issues else known
    descriptions = [f'{name}={d.score if d.score is not None else "未知"} ({d.label})'
                    for name, d in zip(NAMES, dimensions)]
    summary = '；'.join(descriptions) + '。'
    summary += ('存在缺失/未验证输入，综合分未知，不将可评分项重新归一化成高分。' if issues
                else f'前七维等权综合分 {score:.2f}/100；整体质量是计划描述，不是策略胜率或交易许可。')
    return OverallQuality(score=score, label=_label(score), known_points=known, coverage=coverage,
        status='complete' if not issues else 'partial' if coverage else 'unavailable', issues=issues,
        explanation='前七维各占 1/7；第八维不再次参与自身加权。缺项不按零、满分或安全值补齐。', summary=summary)


def score_trade_setup(setup: TradeSetup, rr: RRCalculation, *, evaluated_at: float,
                      max_data_age_seconds: float = 300) -> Scorecard:
    """Describe a supplied plan and its matching Stage 3 calculation, without effects.

    Invalid types, geometry or a mismatched RR raise input-contract errors, never
    a trading decision. Missing/stale scoring evidence returns structured issues.
    Low and zero scores are returned normally; no gate, callback or order exists.
    """
    if type(setup) is not TradeSetup or type(rr) is not RRCalculation:
        raise TypeError('Expected exact TradeSetup and RRCalculation records')
    setup = TradeSetup.model_validate(setup.model_dump())
    rr = RRCalculation.model_validate(rr.model_dump())
    context = ScoringContext(evaluated_at=evaluated_at, max_data_age_seconds=max_data_age_seconds)
    if context.evaluated_at < setup.created_at:
        raise ValueError('Evaluation cannot precede creation of the supplied plan')
    quantity = None if rr.hypothetical_quantity is None else float(rr.hypothetical_quantity)
    if calculate_rr(setup, quantity=quantity) != rr:
        raise ValueError('RR input does not match the supplied TradeSetup and hypothetical quantity')
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        dimensions = (_direction(setup, context), _entry(setup, context), _stop(setup, context),
                      _take_profit(setup, rr, context), _rr_quality(setup, rr, context),
                      _position(setup, rr, context), _execution(setup, rr, context))
        return Scorecard(setup_id=setup.setup_id, plan_version=setup.plan_version, symbol=setup.symbol,
            side=setup.side, context=context, data_as_of=setup.data_as_of, valid_until=setup.valid_until,
            rr_status=rr.status, **dict(zip(DIMENSIONS, dimensions)), overall_trade_quality=_overall(dimensions),
            input_missing_items=setup.data_coverage.missing_items,
            recorded_rejection_codes=tuple(r.code for r in setup.rejection_reasons))
