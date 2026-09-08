"""Pure, opt-in Stage 3 RR arithmetic. No IO, config, clock or runtime wiring.

For direction d (+1 long / -1 short), gross risk is d*(entry-stop).
Gross target reward is d*(target-entry). Adverse slippage changes fill prices;
fees are charged on those effective prices. Net RR divides net reward by the
cost-adjusted INITIAL stop loss, not by margin or a score-selected risk unit.
Funding is signed USDT for the full hypothetical quantity. See stage report.
"""

from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from .models import TradeSetup
from .rr_models import (CalculationQuantity, RRCalculation, RRCostBreakdown,
                        RRIssue, RRScenario, RRTargetResult)

ZERO = Decimal('0')
ONE = Decimal('1')
BPS = Decimal('10000')


def _decimal(value):
    return None if value is None else Decimal(str(value))


def _amount(per_unit, quantity):
    return None if per_unit is None or quantity is None else per_unit * quantity


def _costs(entry, exit_price, direction, assumptions, funding, scope, issues):
    """Known components are retained; missing is NEVER replaced by zero."""
    entry_bps = _decimal(assumptions.entry_slippage_bps)
    exit_bps = _decimal(assumptions.exit_slippage_bps)
    entry_slip = None if entry_bps is None else entry * entry_bps / BPS
    exit_slip = None if exit_bps is None else exit_price * exit_bps / BPS
    entry_fill = None if entry_slip is None else entry + direction * entry_slip
    exit_fill = None if exit_slip is None else exit_price - direction * exit_slip
    if entry_fill is not None and entry_fill <= ZERO:
        issues.append(RRIssue(code='NON_POSITIVE_EFFECTIVE_PRICE', scope=scope,
            message='Adverse entry slippage produces a non-positive modeled fill',
            fields=('cost_assumptions.entry_slippage_bps',)))
        entry_fill = None
    if exit_fill is not None and exit_fill <= ZERO:
        issues.append(RRIssue(code='NON_POSITIVE_EFFECTIVE_PRICE', scope=scope,
            message='Adverse exit slippage produces a non-positive modeled fill',
            fields=('cost_assumptions.exit_slippage_bps',)))
        exit_fill = None
    entry_rate = _decimal(assumptions.entry_fee_rate)
    exit_rate = _decimal(assumptions.exit_fee_rate)
    entry_fee = None if entry_rate is None or entry_fill is None else entry_fill * entry_rate
    exit_fee = None if exit_rate is None or exit_fill is None else exit_fill * exit_rate
    components = (entry_fee, exit_fee, entry_slip, exit_slip, funding)
    total = None if any(c is None for c in components) else sum(components, ZERO)
    return entry_fill, exit_fill, RRCostBreakdown(entry_fee_per_unit=entry_fee,
        exit_fee_per_unit=exit_fee, entry_slippage_per_unit=entry_slip,
        exit_slippage_per_unit=exit_slip, funding_per_unit=funding, total_per_unit=total)


def _scenario(setup, name, entry, quantity, funding, complete_weights, issues):
    direction = ONE if setup.side == 'LONG' else -ONE
    stop = _decimal(setup.initial_stop.price)
    risk = direction * (entry - stop)
    entry_fill, stop_fill, stop_cost = _costs(entry, stop, direction,
        setup.cost_assumptions, funding, name, issues)
    net_risk = None if stop_cost.total_per_unit is None else risk + stop_cost.total_per_unit
    if net_risk is not None and net_risk <= ZERO:
        issues.append(RRIssue(code='NON_POSITIVE_NET_STOP_LOSS', scope=name,
            message='The signed funding/cost assumption leaves no positive stop-loss denominator; net RR is undefined',
            fields=('cost_assumptions.funding_cost_usdt',)))
    targets = []
    for target in setup.targets:
        price, fraction = _decimal(target.price), _decimal(target.fraction)
        gross_reward = direction * (price - entry)
        _, exit_fill, costs = _costs(entry, price, direction,
            setup.cost_assumptions, funding, name, issues)
        net_reward = None if costs.total_per_unit is None else gross_reward - costs.total_per_unit
        net_rr = None if net_reward is None or net_risk is None or net_risk <= ZERO else net_reward / net_risk
        target_quantity = _amount(fraction, quantity)
        targets.append(RRTargetResult(target_id=target.target_id, target_price=price,
            fraction=fraction, target_kind=target.kind, gross_reward_per_unit=gross_reward,
            gross_rr=gross_reward / risk, weighted_gross_rr=fraction * gross_reward / risk,
            effective_exit_price=exit_fill, costs=costs, net_reward_per_unit=net_reward,
            net_rr=net_rr, weighted_net_rr=None if net_rr is None else fraction * net_rr,
            hypothetical_quantity=target_quantity, gross_pnl_usdt=_amount(gross_reward, target_quantity),
            net_pnl_usdt=_amount(net_reward, target_quantity)))
    gross_total = sum((t.fraction * t.gross_reward_per_unit for t in targets), ZERO) if complete_weights else None
    net_total = (sum((t.fraction * t.net_reward_per_unit for t in targets), ZERO)
                 if complete_weights and all(t.net_reward_per_unit is not None for t in targets) else None)
    return RRScenario(name=name, entry_price=entry, initial_stop_price=stop,
        effective_entry_price=entry_fill, effective_stop_price=stop_fill,
        gross_risk_per_unit=risk, stop_costs=stop_cost, net_stop_loss_per_unit=net_risk,
        gross_risk_usdt=_amount(risk, quantity), net_stop_loss_usdt=_amount(net_risk, quantity),
        targets=tuple(targets), weighted_gross_reward_per_unit=gross_total,
        weighted_net_reward_per_unit=net_total,
        gross_rr=None if gross_total is None else gross_total / risk,
        net_rr=None if net_total is None or net_risk is None or net_risk <= ZERO else net_total / net_risk,
        gross_pnl_usdt=_amount(gross_total, quantity), net_pnl_usdt=_amount(net_total, quantity))


def calculate_rr(setup: TradeSetup, *, quantity: float | None = None) -> RRCalculation:
    """Return a separate immutable arithmetic result; never amend TradeSetup.

    `quantity` is optional, explicit hypothetical base-asset units, NOT a size
    recommendation. Nonzero funding in USDT requires it. No quantity is inferred
    from max_quantity, margin, leverage, budget or account data. Missing required
    inputs yield structured calculation issues, not trading rejection reasons.

    Normal validation is repeated even for model_copy/model_construct inputs.
    No market verification, expiry evaluation or RR/score thresholds are applied.
    """
    if type(setup) is not TradeSetup:
        raise TypeError('calculate_rr expects a TradeSetup, not a Signal or order request')
    setup = TradeSetup.model_validate(setup)
    supplied_quantity = CalculationQuantity(quantity=quantity).quantity
    # Fully specified context: no dependency on the caller's precision/traps.
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        q = _decimal(supplied_quantity)
        fractions = sum((_decimal(t.fraction) for t in setup.targets), ZERO)
        issues = []
        assumptions = setup.cost_assumptions
        missing = tuple('cost_assumptions.' + field for field in
            ('entry_fee_rate', 'exit_fee_rate', 'entry_slippage_bps', 'exit_slippage_bps', 'funding_cost_usdt')
            if getattr(assumptions, field) is None)
        if missing:
            issues.append(RRIssue(code='MISSING_COST_ASSUMPTIONS',
                message='Net RR is unknown; omitted costs are not zero', fields=missing))
        funding_total = _decimal(assumptions.funding_cost_usdt)
        if funding_total is None:
            funding = None
        elif funding_total == ZERO:
            funding = ZERO  # Explicit zero is scale independent; not a default.
        elif q is None:
            funding = None
            issues.append(RRIssue(code='QUANTITY_REQUIRED_FOR_FUNDING',
                message='Nonzero full-position USDT funding needs an explicit hypothetical base quantity',
                fields=('quantity',)))
        else:
            funding = funding_total / q
        complete_weights = bool(setup.targets) and fractions == ONE
        if not setup.targets:
            issues.append(RRIssue(code='NO_TARGETS', message='No targets were supplied; no target or total RR is fabricated', fields=('targets',)))
        elif fractions != ONE:
            issues.append(RRIssue(code='FRACTIONS_NOT_EXACTLY_ONE',
                message='Individual target ratios are available, but total RR requires a complete allocation; fractions are not normalized or topped up',
                fields=('targets.fraction',)))
        common = dict(setup_id=setup.setup_id, plan_version=setup.plan_version,
            symbol=setup.symbol, side=setup.side, hypothetical_quantity=q,
            fraction_sum=fractions, cost_assumptions=assumptions,
            adverse_entry='upper' if setup.side == 'LONG' else 'lower',
            input_missing_items=setup.data_coverage.missing_items,
            data_as_of=setup.data_as_of, valid_until=setup.valid_until)
        if not setup.symbol.endswith('USDT'):
            issues.append(RRIssue(code='UNSUPPORTED_QUOTE',
                message='This calculator only describes linear USDT-quoted contracts; no FX or inverse-contract conversion is inferred', fields=('symbol',)))
            return RRCalculation(**common, status='unavailable', reference=None,
                entry_lower=None, entry_upper=None, issues=tuple(issues))
        scenarios = [_scenario(setup, name, _decimal(price), q, funding, complete_weights, issues)
            for name, price in (('reference', setup.entry.reference_price),
                                ('lower', setup.entry.lower_price), ('upper', setup.entry.upper_price))]
        status = ('unavailable' if not setup.targets else 'complete'
                  if all(s.gross_rr is not None and s.net_rr is not None for s in scenarios) else 'partial')
        # Repeated price errors in several targets need only one issue per scope.
        unique = {issue.model_dump_json(): issue for issue in issues}
        return RRCalculation(**common, status=status, reference=scenarios[0],
            entry_lower=scenarios[1], entry_upper=scenarios[2], issues=tuple(unique.values()))
