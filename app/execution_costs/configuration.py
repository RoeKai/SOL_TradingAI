"""Explicit tracked templates only; no .env, parent search or host inheritance."""
import json
from app.utils.paths import ModulePaths, read_text_nofollow
from app.configuration.compiler import compile_bundle, main_values
from .models import QuantifiedSettings, QuantificationPolicy
from .evidence import VERIFIER
from .funding import RISK_SOURCE
from app.historical_replay.data import HistoricalError, seal, START, END, WARMUP

INSTANCE='quantified-paper-august-2026'


def configuration(workspace,model):
    paths=ModulePaths(workspace)
    # Explicit metadata-only new authority names. Risk thresholds are copied
    # unchanged, not relaxed; raw effective source texts enter ConfigBundle.
    main=read_text_nofollow(paths.file('examples/admitted-paper/main.yaml')).replace('admitted-paper-demo',INSTANCE)
    admission=read_text_nofollow(paths.file('admission.yaml'))
    for old,new in (('[paper-ledger-snapshot/v1]','['+RISK_SOURCE+']'),('[paper-structure-review/v1]','['+VERIFIER+']')):
        if admission.count(old)!=1: raise HistoricalError('TEMPLATE_AUTHORITY_SCHEMA_CHANGED')
        admission=admission.replace(old,new)
    exit_text=read_text_nofollow(paths.file('exit-policy.yaml'))
    marker='expected_exit_slippage_bps: 10'
    if exit_text.count(marker)!=1: raise HistoricalError('EXIT_COST_TEMPLATE_CHANGED')
    exit_text=exit_text.replace(marker,'expected_exit_slippage_bps: '+str(model.slippage_bps))
    main=main.replace('slippage_bps: 10','slippage_bps: '+str(model.slippage_bps))
    manifest=read_text_nofollow(paths.file('examples/admitted-paper/manifest.yaml')).replace('admitted-paper-demo',INSTANCE)
    compiled=compile_bundle(main_text=main,admission_text=admission,exit_text=exit_text,manifest_text=manifest)
    if compiled.bundle is None: raise HistoricalError(compiled.model_dump_json())
    b=compiled.bundle;r=main_values(b)['risk']
    settings=QuantifiedSettings(instance_id=INSTANCE,initial_balance='500',initial_time=WARMUP//1000,
        allow_fixtures=False,limits=dict(max_loss_per_trade_usdt=str(r['max_loss_per_trade']),daily_loss_limit_usdt=str(r['daily_loss_limit']),
            max_trades_per_day=r['max_trades_per_day'],max_consecutive_losses=r['max_consecutive_losses'],max_positions=r['max_positions'],
            max_leverage=r['max_leverage'],max_margin_ratio=str(r['max_margin_ratio']),max_margin_usdt='100',
            max_position_notional_usdt='500',max_position_quantity='100'))
    return b,settings


def run_manifest(*,experiment_id,code_commit,code_digest,dataset,bundle,settings,model,kind='baseline',end_ms=END):
    if len(code_commit)!=40 or any(c not in '0123456789abcdef' for c in code_commit): raise HistoricalError('FULL_CODE_COMMIT_REQUIRED')
    if kind not in ('engineering','baseline','stress','missing_restart'): raise HistoricalError('UNKNOWN_EXPERIMENT_KIND')
    if kind in ('baseline','stress') and end_ms!=END: raise HistoricalError('FORMAL_RANGE_MUST_BE_FULL_MONTH')
    if kind in ('engineering','missing_restart') and end_ms!=START+3600000:
        raise HistoricalError('ENGINEERING_RANGE_FIXED_FIRST_HOUR')
    return seal(dict(version='quantified-historical-run/v1',experiment_id=experiment_id,kind=kind,code_commit=code_commit,
        observation_provenance='SYNTHETIC_TEST_NOT_HISTORICAL' if dataset['market']=='SYNTHETIC_TEST_NOT_HISTORICAL' else 'OFFICIAL_HISTORICAL_OBSERVATIONS',
        code_content_digest=code_digest,dataset_digest=dataset['content_digest'],config_digest=bundle.bundle_digest,
        instance_id=settings.instance_id,settings=settings.model_dump(mode='json'),execution_model=model.model_dump(mode='json'),
        provider_version='historical-exact-reclaim-provider/v1',evidence_version='historical-prefix-review/v1',
        exit_plan_version='quantified-exit-plan/v1',scenario_version='quantity-consistent-scenarios/v1',
        price_contract_version='execution-price-layers/v1',quantification=QuantificationPolicy().model_dump(mode='json'),
        cost_function_version='quantity-funding-budget/v1',admission_version='quantity-consistent-admission/v1',
        warmup_range_ms=[WARMUP,START],evaluation_range_ms=[START,end_ms],
        warmup_basis='300 seconds structure window; exact 180s endpoints; completed 15s samples',
        funding_budget_formula='Explicit fixed 0 plus evaluated quantity * maximum visible path budget price * 0.001 * 2; no future-rate access',
        missing_data=None if kind!='missing_restart' else dict(symbol='BTCUSDT',start_ms=START+600000,end_ms=START+720000,
            restart_after_ms=START+900000,meaning='Explicitly drop historical observations; retain cursor/count; do not fill missing prices'),
        scope='REAL_HISTORICAL_OBSERVATIONS_ASSUMED_EXECUTION_RULES_SIMULATED_ACCOUNT',
        static_rr='linear-usdt-rr/v1 unchanged; combined spread and slippage conservative bps budget',
        thresholds='unchanged Stage 5 numerical risk/score/RR gates; explicit new price and quantity cost semantics',
        performance_selection='August development replay, NOT out-of-sample; fixed dates/parameters; no optimization',
        market_storage='streamed verified CSV, 5000-event transaction batches, indexed exact price levels',
        checkpoint_semantics='input cursor and all ledger/outbox/funding effects committed atomically',
        same_millisecond_quotes='Exact raw milliseconds/IDs retained; modelled quote creation uses the next representable logical UTC second for same-ms ordinal, not a historical receive timestamp',
        full_policy_expected_return=None,win_probability=None,live_allowed=False,realtime_connected=False))
