"""Stage 7 review: main-config semantic key collisions.

Review baseline: 3960dda922f7ddc31a53babf1b5f9111f6f7f56e
Run against the project with its dependencies installed:
    python -m pytest -q /path/to/test_stage07_semantic_key_collisions.py

These tests exercise the real parse_text -> prepare_main normalization path.
They do not start the runtime, access accounts, submit orders, or use a test Harness.
Six review cases are expected to fail before the fix. Control cases must keep passing.
"""
from copy import deepcopy
from decimal import Decimal

import pytest
import yaml

from app.configuration.encoding import parse_text
from app.configuration.models import ConfigurationError
from app.configuration.registry import prepare_main


BASE = {
    'instance_id': 'sol-ai-local',
    'dry_run': True,
    'live': {'enabled': False},
    'risk': {
        'max_loss_per_trade': 1,
        'daily_loss_limit': 20,
        'max_trades_per_day': 3,
        'max_consecutive_losses': 2,
        'max_positions': 1,
        'max_margin_ratio': 0.2,
        'max_leverage': 5,
    },
}


def normalize(raw):
    # The same YAML parser and normalizer used by compile_bundle.
    text = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    return prepare_main(parse_text(text))


def collision_case(name):
    raw = deepcopy(BASE)
    if name == 'risk_budget_1_hidden_by_dotted_5':
        raw['risk.max_loss_per_trade'] = 5
    elif name == 'invalid_leverage_100_hidden_by_dotted_5':
        raw['risk']['max_leverage'] = 100
        raw['risk.max_leverage'] = 5
    elif name == 'invalid_bool_hidden_by_dotted_bool':
        raw['live']['enabled'] = 'false'
        raw['live.enabled'] = False
    elif name == 'prohibited_live_true_hidden_by_dotted_false':
        raw['live']['enabled'] = True
        raw['live.enabled'] = False
    elif name == 'nested_strategy_dotted_leaf_collision':
        raw['strategies'] = {
            'trend_breakout': {'enabled': True, 'stop_distance_pct': 0.8},
            'trend_breakout.stop_distance_pct': 2,
        }
    elif name == 'dotted_section_collision':
        raw['strategies'] = {'trend_breakout': {'enabled': True, 'min_trend_pct': 0.5}}
        raw['strategies.trend_breakout'] = {'min_trend_pct': 1.0}
    else:
        raise AssertionError(name)
    return raw


@pytest.mark.parametrize('name', [
    'risk_budget_1_hidden_by_dotted_5',
    'invalid_leverage_100_hidden_by_dotted_5',
    'invalid_bool_hidden_by_dotted_bool',
    'prohibited_live_true_hidden_by_dotted_false',
    'nested_strategy_dotted_leaf_collision',
    'dotted_section_collision',
])
def test_review_semantic_collision_must_not_silently_override(name):
    raw = collision_case(name)
    with pytest.raises(ConfigurationError):
        normalize(raw)


def test_control_normal_nested_config():
    out, provenance = normalize(deepcopy(BASE))
    assert out['risk']['max_loss_per_trade'] == Decimal(1)
    assert out['risk']['max_leverage'] == 5
    assert out['live']['enabled'] is False
    assert provenance['risk.max_loss_per_trade'][1] is False


@pytest.mark.parametrize('field,value', [
    ('max_loss_per_trade', 0),
    ('max_loss_per_trade', -1),
    ('max_leverage', 100),
    ('max_margin_ratio', 0.3),
])
def test_control_invalid_risk_without_mask_is_rejected(field, value):
    raw = deepcopy(BASE)
    raw['risk'][field] = value
    with pytest.raises(ConfigurationError):
        normalize(raw)


@pytest.mark.parametrize('value', [True, 'false'])
def test_control_bad_live_value_without_mask_is_rejected(value):
    raw = deepcopy(BASE)
    raw['live']['enabled'] = value
    with pytest.raises(ConfigurationError):
        normalize(raw)


def test_control_documented_equivalent_alias_preserved():
    raw = deepcopy(BASE)
    raw['strategies'] = {'panic_rebound': {
        'enabled': True, 'drop_threshold_pct': -2, 'drop_pct': 2}}
    out, _ = normalize(raw)
    assert out['strategies']['panic_rebound']['drop_threshold_pct'] == Decimal(-2)


def test_control_documented_conflicting_alias_is_rejected():
    raw = deepcopy(BASE)
    raw['strategies'] = {'panic_rebound': {
        'enabled': True, 'drop_threshold_pct': -2, 'drop_pct': 3}}
    with pytest.raises(ConfigurationError):
        normalize(raw)


def test_control_ordinary_duplicate_yaml_key_is_rejected():
    with pytest.raises(ConfigurationError):
        parse_text('risk:\n  max_loss_per_trade: 1\n  max_loss_per_trade: 5\n')


def test_control_unknown_field_is_rejected():
    raw = deepcopy(BASE)
    raw['risk']['nonexistent_field'] = 5
    with pytest.raises(ConfigurationError):
        normalize(raw)


def test_control_normal_input_is_not_mutated():
    raw = deepcopy(BASE)
    old = deepcopy(raw)
    normalize(raw)
    assert raw == old
