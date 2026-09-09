"""Synthetic reproductions of the Stage 7 review; not the unavailable review attachment.

Exercise real YAML compilation and the offline CLI file-reading boundary. No
trading runtime, account client, order adapter or external network is started.
"""

from copy import deepcopy
from decimal import Decimal
import json
import shutil
import subprocess
import sys

import pytest
import yaml

from app.configuration.compiler import compile_bundle, verify_bundle
from app.configuration.encoding import flatten, parse_text, text_hash
from app.configuration.models import ConfigurationError
from app.configuration.registry import Spec, prepare_main
from test_config_bundle import Dumper, FILES, ROOT, texts


CASES = (
    'conflicting-risk', 'invalid-boolean', 'invalid-string', 'invalid-zero',
    'hidden-live-enabled', 'hidden-leverage', 'nested-dotted-key',
    'dotted-section', 'hidden-invalid-alias',
)


def collision_sources(case, reverse=False):
    sources = texts()
    raw = parse_text(sources['main_text'])
    if case == 'conflicting-risk':
        raw['risk.max_loss_per_trade'] = 4
    elif case in ('invalid-boolean', 'invalid-string', 'invalid-zero'):
        raw['risk']['max_loss_per_trade'] = {
            'invalid-boolean': True, 'invalid-string': 'not-an-amount', 'invalid-zero': 0,
        }[case]
        raw['risk.max_loss_per_trade'] = 5
    elif case == 'hidden-live-enabled':
        raw['live']['enabled'] = True
        raw['live.enabled'] = False
    elif case == 'hidden-leverage':
        raw['risk']['max_leverage'] = 99
        raw['risk.max_leverage'] = 5
    elif case == 'nested-dotted-key':
        raw['strategies']['panic_rebound']['drop_threshold_pct'] = 'invalid-original'
        raw['strategies']['panic_rebound.drop_threshold_pct'] = -2
    elif case == 'dotted-section':
        raw['strategies']['panic_rebound']['drop_threshold_pct'] = 'invalid-original'
        raw['strategies.panic_rebound'] = {'drop_threshold_pct': -2}
    elif case == 'hidden-invalid-alias':
        raw['strategies']['panic_rebound']['drop_pct'] = 'invalid-original'
        raw['strategies.panic_rebound.drop_pct'] = 2
    else:
        raise AssertionError(case)
    if reverse:
        raw = dict(reversed(tuple(raw.items())))
    sources['main_text'] = yaml.dump(raw, Dumper=Dumper, sort_keys=False)
    return sources


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('reverse', (False, True))
def test_compile_collision_never_produces_usable_bundle(case, reverse):
    sources = collision_sources(case, reverse)
    before = deepcopy(sources)
    result = compile_bundle(**sources)
    assert result.parsing == 'FAIL'
    assert result.consistency == 'NOT_EVALUATED'
    assert result.bundle is None
    assert sources == before
    assert any(i.reason_code == 'CONFIG_INPUT_INVALID' and i.source == 'main' for i in result.issues)


def run_file_cli(tmp_path, sources, *args):
    # An isolated source-only fixture. Keep real argparse, readers, parser and
    # compiler; copying this package does not import/start its trade modules.
    root = tmp_path.resolve()
    shutil.copytree(ROOT / 'app', root / 'app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(ROOT / 'isolation-policy.json', root / 'isolation-policy.json')
    for role, name in FILES.items():
        (root / name).write_text(sources[role + '_text'], encoding='utf8')
    return subprocess.run(
        [sys.executable, '-m', 'app.configuration.check', '--json', '--parameters', *args],
        cwd=root, text=True, capture_output=True, timeout=20,
    )


@pytest.mark.parametrize('case', CASES)
def test_real_offline_cli_rejects_collision_without_bundle(tmp_path, case):
    run = run_file_cli(tmp_path, collision_sources(case))
    assert run.returncode == 2, (run.returncode, run.stderr)
    output = json.loads(run.stdout)
    assert output['bundle'] is None
    result = output['validation']
    assert result['config_parsing'] == 'FAIL'
    assert result['config_consistency'] == 'NOT_EVALUATED'
    assert result['execution'] == 'NOT_INTEGRATED'
    assert result['execution_authority'] == 'none' and result['live_allowed'] is False


@pytest.mark.parametrize('case', CASES)
def test_rejection_precedes_defaulting_and_scalar_validation(monkeypatch, case):
    sources = collision_sources(case)
    def no_main_defaults_or_validation(*args, **kwargs):
        raise AssertionError('Ambiguous raw YAML must fail before effective values/defaults are processed')
    monkeypatch.setattr(Spec, 'validate', no_main_defaults_or_validation)
    result = compile_bundle(**sources)
    assert result.parsing == 'FAIL' and result.bundle is None
    assert 'LITERAL_DOTTED_KEY_FORBIDDEN' in result.issues[0].suggestion


@pytest.mark.parametrize('case', CASES)
@pytest.mark.parametrize('entry', ('prepare_main', 'flatten'))
def test_explicit_mapping_callers_cannot_bypass_key_grammar(case, entry):
    # Deliberately not using the production parser: test its mapping-level defence.
    raw = yaml.safe_load(collision_sources(case)['main_text'])
    before = deepcopy(raw)
    with pytest.raises(ConfigurationError, match='LITERAL_DOTTED_KEY_FORBIDDEN'):
        if entry == 'prepare_main':
            prepare_main(raw)
        else:
            dict(flatten(raw))
    assert raw == before


@pytest.mark.parametrize('text', (
    'risk: {max_loss_per_trade: 5}\nrisk.max_loss_per_trade: 5\n',
    'risk.max_loss_per_trade: 5\n',
    'strategies: {panic_rebound: {drop_pct: 2}, panic_rebound.drop_pct: 2}\n',
    'tiers: [{minimum.total: 85}]\n',
    '"risk\\u002emax_loss_per_trade": 5\n',
    "'risk.max_loss_per_trade': 5\n",
))
def test_nested_only_grammar_rejects_equal_standalone_quoted_and_sequence_keys(text):
    with pytest.raises(ConfigurationError, match='LITERAL_DOTTED_KEY_FORBIDDEN'):
        parse_text(text)


@pytest.mark.parametrize('role', tuple(FILES))
def test_shared_yaml_entry_rejects_literal_paths_in_every_source(role):
    sources = texts()
    sources[role + '_text'] += '\nundefined.path: synthetic-private-value-must-not-be-echoed\n'
    result = compile_bundle(**sources)
    assert result.parsing == 'FAIL' and result.bundle is None
    error = next(i for i in result.issues if i.source == role)
    assert error.reason_code == 'CONFIG_INPUT_INVALID'
    assert 'LITERAL_DOTTED_KEY_FORBIDDEN' in error.suggestion
    assert 'synthetic-private-value' not in result.model_dump_json()


ALIASES = (
    ('panic_rebound', 'drop_pct', 'drop_threshold_pct', Decimal('2')),
    *((name, 'btc_max_drop_pct', 'btc_crash_pct', Decimal('.8')) for name in (
        'panic_rebound', 'trend_breakout', 'pullback_entry', 'fake_breakout_reverse',
    )),
)


def alias_sources(name, alias, canonical, magnitude, mode):
    sources = texts()
    raw = parse_text(sources['main_text'])
    strategy = raw['strategies'][name]
    # Explicit decimal strings avoid the generic test dumper emitting !!float
    # for an integral Decimal. Tags remain intentionally forbidden in production.
    strategy[alias] = str(magnitude)
    if mode == 'alias-only':
        strategy.pop(canonical, None)
    else:
        strategy[canonical] = str(-magnitude if mode == 'equal' else -(magnitude + 1))
    sources['main_text'] = yaml.dump(raw, Dumper=Dumper, sort_keys=False)
    return sources


@pytest.mark.parametrize('name,alias,canonical,magnitude', ALIASES)
@pytest.mark.parametrize('mode', ('alias-only', 'equal', 'conflict'))
def test_formal_nested_aliases_keep_conversion_conflicts_and_provenance(name, alias, canonical, magnitude, mode):
    sources = alias_sources(name, alias, canonical, magnitude, mode)
    result = compile_bundle(**sources)
    if mode == 'conflict':
        assert result.parsing == 'FAIL' and result.bundle is None
        assert 'SEMANTIC_ALIAS_CONFLICT' in result.issues[0].suggestion
        return
    assert result.parsing == result.consistency == 'PASS'
    assert verify_bundle(result.bundle) == result
    base = 'strategies.' + name + '.'
    item = next(p for p in result.bundle.parameters if p.path == 'main.' + base + canonical)
    expected_origin = base + canonical + ' + ' + base + alias if mode == 'equal' else base + alias
    assert item.source_path == expected_origin
    assert item.default_applied is False
    assert Decimal(json.loads(item.value_json)) == -magnitude
    assert item.source_digest == text_hash(sources['main_text'])
    assert item.unit == 'percent_points'


@pytest.mark.parametrize('name,alias,canonical,magnitude', ALIASES)
@pytest.mark.parametrize('mode', ('alias-only', 'equal'))
def test_file_cli_keeps_all_supported_nested_aliases(tmp_path, name, alias, canonical, magnitude, mode):
    sources = alias_sources(name, alias, canonical, magnitude, mode)
    run = run_file_cli(tmp_path, sources)
    assert run.returncode == 0, (run.returncode, run.stderr)
    output = json.loads(run.stdout)
    assert output['validation']['config_consistency'] == 'PASS'
    assert output['bundle'] == compile_bundle(**sources).bundle.model_dump(mode='json')
    assert output['validation']['execution_authority'] == 'none'
    assert output['validation']['live_allowed'] is False


def test_normal_template_has_identical_bundle_effective_values_and_origins(tmp_path):
    sources = texts()
    result = compile_bundle(**sources)
    assert result.parsing == result.consistency == 'PASS'
    assert result.bundle.bundle_digest == '88bfb9aff742aa99cefd8c8aa2a96ce1eadd8973ffaca1e6e033bc8d27c5c982'
    assert len(result.bundle.parameters) == 235
    assert verify_bundle(result.bundle) == result
    run = run_file_cli(tmp_path, sources)
    assert run.returncode == 0, (run.returncode, run.stderr)
    assert json.loads(run.stdout)['bundle'] == result.bundle.model_dump(mode='json')


def test_dots_in_values_and_generated_paths_are_not_raw_dotted_keys():
    raw = parse_text('outer:\n  fields: [{value: "a.b"}, {value: "https://example.invalid/1.2"}]\n')
    assert dict(flatten(raw)) == {
        'outer.fields.0.value': 'a.b', 'outer.fields.1.value': 'https://example.invalid/1.2',
    }
    assert dict(flatten({'value': '1.2'}, prefix='generated.path')) == {'generated.path.value': '1.2'}


@pytest.mark.parametrize('raw', ({'': {'risk': 5}, 'risk': 4}, {1: 'bad'}, {'<<': 'bad'}))
def test_non_field_segments_cannot_produce_ambiguous_flat_paths(raw):
    with pytest.raises(ConfigurationError):
        dict(flatten(raw))
