"""Stage 2 contracts only: synthetic values, no RR/scoring/admission algorithm."""

import ast
from dataclasses import asdict, fields, replace
from pathlib import Path
import socket
import sqlite3

import pytest
from pydantic import ValidationError

from app.models import Signal
from app.setups import TradeSetup, adapt_legacy_signal
from app.setups.models import (COVERAGE_KEYS, SCORE_DIMENSIONS, CostAssumptions, DataAvailability,
    DataCoverage, Evidence, MarketSnapshot, ScoreBreakdown, ScoreComponent, StopMovementPlan,
    StopMoveRule)

NOW = 1_800_000_000.0


def description(side='LONG'):
    direction = 1 if side == 'LONG' else -1
    return dict(plan_version='synthetic-description/v1', setup_id='plan-fixture-1', symbol='SOLUSDT',
        strategy_name='fixture', strategy_type='custom', side=side,
        structure_evidence=[dict(evidence_id='range', kind='range_boundary',
            description='Synthetic test range, not an observed market', status='unverified',
            source='synthetic_fixture', observed_at=NOW-1)],
        entry=dict(order_type='MARKET', reference_price=100, lower_price=100, upper_price=100,
                   basis='structure_zone', evidence_ids=['range']),
        initial_stop=dict(price=100-direction*2, basis='Synthetic structural stop', evidence_ids=['range']),
        targets=[dict(target_id=f'tp-{index}', price=100+direction*2*index, fraction=fraction,
                      kind='structure', basis='Synthetic range target', evidence_ids=['range'],
                      theoretical_rr=float(index), net_rr=float(index)-.2)
                 for index, fraction in enumerate((.3, .4, .3), 1)],
        reward_risk=dict(gross_rr=2, net_rr=1.8, calculation_method='precomputed test fixture, not an algorithm'),
        created_at=NOW, data_as_of=NOW-1, valid_until=NOW+30)


def old_signal(strategy='panic_rebound', side='LONG', order_type='MARKET'):
    sign = 1 if side == 'LONG' else -1
    return Signal('sol-legacy-fixture', strategy, 'SOLUSDT', side, 100, 100-sign,
                  [{'price': 100+sign, 'fraction': .5}, {'price': 100+sign*2, 'fraction': .5}],
                  NOW, order_type=order_type, reason='Legacy observation, not structural evidence', score=72)


@pytest.mark.parametrize('side', ['LONG', 'SHORT'])
def test_native_description_json_roundtrip_and_units(side):
    setup = TradeSetup.model_validate(description(side))
    restored = TradeSetup.model_validate_json(setup.model_dump_json())
    assert restored == setup
    assert restored.schema_version == 'trade-setup/v1'
    assert restored.targets[1].fraction == .4
    assert restored.reward_risk.gross_rr == 2
    assert restored.execution_authority == 'none' and restored.admission_status == 'not_evaluated'
    assert restored.grade is None and restored.confidence.value is None


def test_public_json_schema_defines_all_required_groups():
    schema = TradeSetup.model_json_schema()
    assert set(schema['properties']) == {
        'schema_version', 'plan_version', 'setup_id', 'origin', 'symbol', 'strategy_name', 'strategy_type',
        'side', 'structure_evidence', 'entry', 'invalidation_conditions', 'initial_stop', 'targets',
        'reward_risk', 'market_state', 'score', 'grade', 'grade_interpretation', 'confidence',
        'position_limit_advice', 'risk_budget', 'cost_assumptions', 'data_coverage', 'stop_movement',
        'created_at', 'data_as_of', 'valid_until', 'rejection_reasons', 'compatibility_notes',
        'admission_status', 'execution_authority'}
    assert schema['additionalProperties'] is False


def test_frozen_nested_records_and_copied_collections():
    raw = description()
    setup = TradeSetup.model_validate(raw)
    raw['targets'][0]['price'] = 999
    assert setup.targets[0].price == 102 and isinstance(setup.targets, tuple)
    with pytest.raises(ValidationError):
        setup.grade = 'S'
    with pytest.raises(ValidationError):
        setup.targets[0].price = 999


@pytest.mark.parametrize('bad', [True, '100', float('nan'), float('inf'), -1, 0])
def test_invalid_numeric_prices_fail_schema_not_trading_gate(bad):
    raw = description()
    raw['entry']['reference_price'] = bad
    with pytest.raises(ValidationError):
        TradeSetup.model_validate(raw)


@pytest.mark.parametrize('changes', [
    {'schema_version': 'trade-setup/v2'}, {'execution_authority': 'live'},
    {'admission_status': 'approved'}, {'side': 'BUY'}, {'symbol': 'solusdt'},
    {'unexpected_order_payload': {}}, {'created_at': True}, {'plan_version': ' '},
    {'valid_until': NOW}, {'data_as_of': NOW+1}, {'grade': '95%'},
    {'confidence': {'value': .8, 'basis': 'fixture', 'interpretation': 'win_probability'}},
    {'confidence': {'value': .8}}, {'position_limit_advice': {'leverage_cap': True}},
    {'position_limit_advice': {'max_margin_fraction_of_equity': 20}},
])
def test_invalid_contract_fields_rejected(changes):
    with pytest.raises(ValidationError):
        TradeSetup.model_validate({**description(), **changes})


@pytest.mark.parametrize('defect', ['stop', 'range', 'fraction', 'target_order', 'target_id',
    'evidence_id', 'evidence_ref', 'structure_target', 'structure_entry', 'future_evidence'])
def test_price_geometry_references_and_time_consistency(defect):
    raw = description()
    if defect == 'stop': raw['initial_stop']['price'] = 101
    elif defect == 'range': raw['entry']['upper_price'] = 99
    elif defect == 'fraction': raw['targets'][0]['fraction'] = .4
    elif defect == 'target_order': raw['targets'].reverse()
    elif defect == 'target_id': raw['targets'][1]['target_id'] = raw['targets'][0]['target_id']
    elif defect == 'evidence_id': raw['structure_evidence'] *= 2
    elif defect == 'evidence_ref': raw['initial_stop']['evidence_ids'] = ['absent']
    elif defect == 'structure_target': raw['targets'][0]['evidence_ids'] = []
    elif defect == 'structure_entry': raw['entry']['evidence_ids'] = []
    elif defect == 'future_evidence': raw['structure_evidence'][0]['observed_at'] = NOW
    with pytest.raises(ValidationError):
        TradeSetup.model_validate(raw)


@pytest.mark.parametrize('grade,total', [('S', 95), ('A', 82), ('B', 73), ('C', 12)])
def test_score_grade_and_advice_never_change_targets_or_rr(grade, total):
    raw = description()
    before = TradeSetup.model_validate(raw)
    raw.update(grade=grade, score={'total': total, 'method_version': 'supplied-test-only'},
               confidence={'value': .5, 'basis': 'synthetic evidence completeness'},
               position_limit_advice={'max_quantity': 0, 'max_margin_fraction_of_equity': .01})
    after = TradeSetup.model_validate(raw)
    assert after.entry == before.entry and after.initial_stop == before.initial_stop
    assert after.targets == before.targets and after.reward_risk == before.reward_risk
    assert after.data_coverage.coverage_ratio == 0
    assert after.grade_interpretation == 'policy_tier_not_win_probability'
    assert after.execution_authority == 'none' and after.admission_status == 'not_evaluated'


def test_low_or_unknown_rr_is_storable_not_approved_or_recalculated():
    raw = description()
    raw['reward_risk'] = {'gross_rr': .2, 'net_rr': -.1}
    raw['targets'][0].update(theoretical_rr=None, net_rr=None)
    raw['rejection_reasons'] = [{'code': 'EXAMPLE_LOW_RR', 'message': 'Recorded example, not an evaluated gate',
                                 'source': 'synthetic_fixture'}]
    setup = TradeSetup.model_validate(raw)
    assert setup.reward_risk.net_rr == -.1
    assert setup.targets[0].theoretical_rr is None
    assert setup.rejection_reasons[0].code == 'EXAMPLE_LOW_RR'
    assert setup.execution_authority == 'none'


def test_missing_defaults_are_unknown_not_zero_cost_or_safe():
    setup = TradeSetup.model_validate(description())
    assert setup.data_coverage.missing_items == COVERAGE_KEYS
    assert all(i.status == 'missing' for i in setup.data_coverage.items)
    assert setup.cost_assumptions.entry_fee_rate is None
    assert setup.cost_assumptions.funding_cost_usdt is None
    assert setup.score.total is None and setup.grade is None
    assert all(i.points is None and i.max_points is None for i in setup.score.components)
    assert setup.market_state.regime == 'unknown' and setup.market_state.status == 'missing'
    assert setup.risk_budget.max_loss_usdt is None


def test_coverage_summary_includes_stale_and_unverified_and_roundtrips():
    items = [DataAvailability(name=name) for name in COVERAGE_KEYS]
    items[0] = DataAvailability(name=COVERAGE_KEYS[0], status='available', source='fixture', observed_at=NOW-1)
    items[1] = DataAvailability(name=COVERAGE_KEYS[1], status='stale', source='fixture', observed_at=NOW-100)
    items[2] = DataAvailability(name=COVERAGE_KEYS[2], status='unverified')
    coverage = DataCoverage.from_items(tuple(items))
    assert coverage.coverage_ratio == .1 and coverage.missing_items == COVERAGE_KEYS[1:]
    assert DataCoverage.model_validate_json(coverage.model_dump_json()) == coverage
    with pytest.raises(ValidationError):
        DataCoverage.model_validate({**coverage.model_dump(), 'coverage_ratio': 1})
    with pytest.raises(ValidationError):
        DataCoverage.model_validate({**coverage.model_dump(), 'missing_items': []})


def test_incomplete_duplicate_and_all_inapplicable_coverage():
    with pytest.raises(ValidationError):
        DataCoverage.from_items((DataAvailability(name='market_price'),))
    with pytest.raises(ValidationError):
        DataCoverage.from_items(DataCoverage().items + (DataAvailability(name='market_price'),))
    coverage = DataCoverage.from_items(tuple(DataAvailability(name=n, status='not_applicable', required=False)
                                            for n in COVERAGE_KEYS))
    assert coverage.coverage_ratio is None  # Not 100% and not an admission.


@pytest.mark.parametrize('factory,payload', [
    (DataAvailability, {'name': 'news', 'status': 'available'}),
    (DataAvailability, {'name': 'news', 'status': 'not_applicable'}),
    (Evidence, {'evidence_id': 'x', 'kind': 'other', 'description': 'fixture', 'status': 'available'}),
    (MarketSnapshot, {'status': 'available'}),
    (MarketSnapshot, {'bid': 101, 'ask': 100}),
    (ScoreComponent, {'dimension': 'news', 'status': 'missing', 'points': 5, 'max_points': 10}),
    (ScoreComponent, {'dimension': 'news', 'status': 'unverified', 'points': 11, 'max_points': 10}),
    (CostAssumptions, {'entry_fee_rate': -1}),
    (CostAssumptions, {'entry_slippage_bps': True}),
    (StopMoveRule, {'after_target_id': 'tp-1', 'move_to': 'entry', 'requires_confirmed_fill': False}),
    (StopMoveRule, {'after_target_id': 'tp-1', 'move_to': 'fixed_price'}),
    (StopMoveRule, {'after_target_id': 'tp-1', 'move_to': 'trailing'}),
    (StopMovementPlan, {'allow_widening': True}),
])
def test_nested_record_invariants(factory, payload):
    with pytest.raises(ValidationError):
        factory.model_validate(payload)


def test_score_dimensions_require_all_eight_without_defining_weights():
    score = ScoreBreakdown()
    assert tuple(i.dimension for i in score.components) == SCORE_DIMENSIONS
    with pytest.raises(ValidationError):
        ScoreBreakdown(components=score.components[:-1])
    with pytest.raises(ValidationError):
        ScoreBreakdown(components=score.components[:-1]+(score.components[0],))


def test_exit_rules_are_only_serializable_descriptions():
    raw = description()
    raw['stop_movement'] = {'rules': [
        {'after_target_id': 'tp-1', 'move_to': 'cost_adjusted_entry'},
        {'after_target_id': 'tp-2', 'move_to': 'trailing', 'trailing_distance_r': 1}]}
    setup = TradeSetup.model_validate(raw)
    assert setup.initial_stop.price == 98 and setup.targets[0].fraction == .3
    assert TradeSetup.model_validate_json(setup.model_dump_json()) == setup
    raw['stop_movement']['rules'][0]['after_target_id'] = 'missing-target'
    with pytest.raises(ValidationError):
        TradeSetup.model_validate(raw)


def test_existing_signal_schema_and_payload_unchanged():
    expected = ['id', 'strategy', 'symbol', 'side', 'entry_price', 'stop_price', 'take_profits',
                'created_at', 'order_type', 'reason', 'score']
    signal = old_signal()
    before = asdict(signal)
    assert [item.name for item in fields(Signal)] == expected
    adapt_legacy_signal(signal)
    assert asdict(signal) == before
    assert Signal(**before) == signal


@pytest.mark.parametrize('strategy', ['trend_breakout', 'pullback_entry', 'panic_rebound', 'fake_breakout_reverse'])
@pytest.mark.parametrize('side,order_type', [('LONG', 'MARKET'), ('SHORT', 'LIMIT')])
def test_all_legacy_strategy_records_map_without_new_authority(strategy, side, order_type):
    signal = old_signal(strategy, side, order_type)
    result = adapt_legacy_signal(signal)
    assert result.strategy_type == strategy and result.side == side
    assert result.entry.order_type == order_type and result.entry.reference_price == signal.entry_price
    assert result.initial_stop.price == signal.stop_price
    assert [(t.price, t.fraction) for t in result.targets] == [(t['price'], t['fraction']) for t in signal.take_profits]
    assert all(t.kind == 'legacy_unspecified' and t.theoretical_rr is None and t.net_rr is None for t in result.targets)
    assert result.score.legacy_score == signal.score and result.score.total is None
    assert result.reward_risk.gross_rr is None and result.reward_risk.net_rr is None
    assert result.data_as_of is None and result.valid_until is None
    assert result.grade is None and result.confidence.value is None
    assert result.execution_authority == 'none' and result.admission_status == 'not_evaluated'
    assert not hasattr(result, 'to_signal')
    signal.take_profits[0]['price'] = 500
    assert result.targets[0].price != 500


def test_unknown_strategy_not_invented_and_no_clock_env_io(monkeypatch):
    import app.config as config
    import time
    def forbidden(*args, **kwargs):
        raise AssertionError('Schema/adapter must not touch runtime, secrets, DB, network or clock')
    for name in ('load_config', 'load_secrets'):
        monkeypatch.setattr(config, name, forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(time, 'time', forbidden)
    result = adapt_legacy_signal(old_signal('isolation_smoke'))
    assert result.strategy_name == 'isolation_smoke' and result.strategy_type == 'legacy_unknown'
    assert TradeSetup.model_validate_json(result.model_dump_json()) == result


def test_adapter_rejects_unsupported_legacy_payload_without_mutation():
    signal = old_signal()
    signal.take_profits[0]['unknown_extension'] = 1
    before = asdict(signal)
    with pytest.raises(ValueError, match='no silent field loss'):
        adapt_legacy_signal(signal)
    assert asdict(signal) == before
    with pytest.raises(TypeError):
        adapt_legacy_signal(before)
    with pytest.raises(ValidationError):
        adapt_legacy_signal(replace(old_signal(), stop_price=101))


def test_stage2_has_no_runtime_integration_or_execution_imports():
    root = Path(__file__).resolve().parents[1]
    existing = ['main.py', 'app/models.py', 'app/runtime.py', 'app/strategies/engine.py',
        'app/risk/engine.py', 'app/execution/engine.py', 'app/paper/broker.py', 'app/portfolio/manager.py']
    for relative in existing:
        tree = ast.parse((root / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').startswith('app.setups')
            elif isinstance(node, ast.Import):
                assert not any(item.name.startswith('app.setups') for item in node.names)
    for name in ('models.py', 'adapters.py'):
        tree = ast.parse((root / 'app/setups' / name).read_text())
        allowed = {'__future__', 'typing', 'pydantic', 'dataclasses', 'app.models', 'models'}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom): assert node.module in allowed
            elif isinstance(node, ast.Import): assert all(item.name in allowed for item in node.names)
