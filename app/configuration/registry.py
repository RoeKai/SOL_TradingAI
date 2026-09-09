"""Reviewed field catalogue. Main defaults mirror declarations, not runtime IO."""

from dataclasses import dataclass
from decimal import Decimal
import re

from .encoding import flatten
from .models import ConfigurationError, decimal_input


@dataclass(frozen=True)
class Spec:
    default: object
    unit: str
    kind: str = 'decimal'
    low: str | None = None
    high: str | None = None
    inclusive_low: bool = True
    choices: tuple = ()
    authority: str = 'app/config.py'
    required: bool = False

    @property
    def constraint(self):
        return f'{self.kind}; lower={self.low}; lower_inclusive={self.inclusive_low}; upper={self.high}; choices={self.choices}'

    def validate(self, value):
        if self.kind == 'boolean':
            if type(value) is not bool: raise ConfigurationError('YAML boolean required')
        elif self.kind == 'integer':
            if type(value) is not int: raise ConfigurationError('Integer required; no bool/string/float coercion')
        elif self.kind == 'text':
            if type(value) is not str or len(value) > 4096: raise ConfigurationError('Bounded string required')
        elif self.kind == 'symbols':
            if type(value) is not list or len(value) != 3 or set(value) != {'SOLUSDT','BTCUSDT','ETHUSDT'}:
                raise ConfigurationError('Exactly three distinct configured market symbols required')
        else:
            value = decimal_input(value)
        if self.choices and value not in self.choices: raise ConfigurationError('Value not in explicit supported enum')
        if self.low is not None and (value < Decimal(self.low) if self.inclusive_low else value <= Decimal(self.low)):
            raise ConfigurationError('Value below allowed range')
        if self.high is not None and value > Decimal(self.high): raise ConfigurationError('Value above allowed range')
        return value


def _number(default, unit, low='0', high=None, *, required=False, inclusive=False):
    return Spec(default, unit, low=low, high=high, inclusive_low=inclusive, required=required)


def _count(default, low='1', high=None, *, required=False):
    return Spec(default, 'count', 'integer', low, high, required=required)


def main_specs():
    # These are configuration declarations, NEVER a confirmed account snapshot.
    s = {
        'instance_id': Spec('sol-ai-local','instance','text',required=True),
        'dry_run': Spec(True,'boolean','boolean',required=True),
        'timezone': Spec('Asia/Kuala_Lumpur','timezone','text',choices=('Asia/Kuala_Lumpur','UTC')),
        'symbols': Spec(('SOLUSDT','BTCUSDT','ETHUSDT'),'symbol_set','symbols'),
        'trade_symbol': Spec('SOLUSDT','symbol','text',choices=('SOLUSDT',)),
        'paper.initial_balance': _number('500','USDT'),
        'live.enabled': Spec(False,'boolean','boolean',required=True),
        'live.confirmation': Spec('','text','text'),
        'live.dedicated_account_confirmed': Spec(False,'boolean','boolean'),
        'live.max_account_equity_usdt': _number('500','USDT'),
        'live.bridge_url': Spec('http://127.0.0.1:8766','url','text'),
        'live.reconcile_interval_seconds': _number('10','seconds','5','60',inclusive=True),
        'live.order_timeout_seconds': _number('8','seconds','0','30'),
        'risk.max_loss_per_trade': _number('5','USDT_loss_budget',required=True),
        'risk.daily_loss_limit': _number('20','USDT_net_cash_day_loss',required=True),
        'risk.max_trades_per_day': _count(3,required=True),
        'risk.max_consecutive_losses': _count(2,required=True),
        'risk.max_positions': _count(1,'1','1',required=True),
        'risk.max_margin_ratio': _number('.2','equity_fraction','0','.2',required=True),
        'risk.max_leverage': _count(5,'1','5',required=True),
        'risk.btc_crash_pct': _number('-.8','percent_points','-100','0'),
        'risk.taker_fee_rate': _number('.0005','fee_rate','0','.01',inclusive=True),
        'risk.slippage_bps': _number('10','bps','0','100',inclusive=True),
        'execution.leverage': _count(5,'1','5'),
        'execution.entry_order_type': Spec('MARKET','order_type','text',choices=('MARKET','LIMIT')),
        'execution.limit_expiry_seconds': _number('30','seconds','1','300',inclusive=True),
        'data.stale_after_seconds': _number('15','seconds','2','120',inclusive=True),
        'data.warmup_candles': _count(120,'20','240'),
        'data.ws_base_url': Spec('wss://fstream.binance.com','url','text',choices=('wss://fstream.binance.com',)),
        'data.rest_base_url': Spec('https://fapi.binance.com','url','text',choices=('https://fapi.binance.com',)),
        'runtime.scoring_interval_seconds': _number('.5','seconds','.1','5',inclusive=True),
        'runtime.heartbeat_seconds': _number('1','seconds','.2','5',inclusive=True),
        'runtime.max_event_loop_lag_seconds': _number('5','seconds','1','15',inclusive=True),
        'dashboard.host': Spec('127.0.0.1','host','text'),
        'dashboard.port': _count(8765,'1024','65535'),
        'dashboard.require_auth': Spec(True,'boolean','boolean'),
        'dashboard.title': Spec('SOL AI 独立交易台','text','text'),
        'dashboard.root_path': Spec('','path','text'),
        'alerts.enabled': Spec(False,'boolean','boolean'),
        'review.daily_hour': _count(0,'0','23'),
        'review.daily_minute': _count(5,'0','59'),
    }
    # Strategy fallbacks include runtime's explicit order_type inheritance.
    names = ('panic_rebound','trend_breakout','pullback_entry','fake_breakout_reverse')
    for name in names:
        defaults = {
            'enabled': Spec(True,'boolean','boolean'),
            'order_type': Spec('MARKET','order_type','text',choices=('MARKET','LIMIT')),
            'cooldown_seconds': _number('300','seconds'),
            'observation_seconds': _number('300','seconds'),
            'min_score': _number('60','score_points','0','100'),
            'max_stop_distance_pct': _number('5','percent_points'),
            'btc_crash_pct': _number('-.8','percent_points','-100','0'),
        }
        extras = {
            'panic_rebound': {'drop_threshold_pct':_number('-2','percent_points','-100','0'),
                'rebound_fraction':_number('.25','fraction','0','1'),'stop_buffer_pct':_number('.15','percent_points')},
            'trend_breakout': {'min_trend_pct':_number('.5','percent_points'),'breakout_buffer_pct':_number('.05','percent_points'),
                'min_volume_ratio':_number('1.2','volume_multiple'),'min_buy_pressure':_number('.55','fraction','0','1'),
                'stop_distance_pct':_number('.8','percent_points')},
            'pullback_entry': {'min_trend_pct':_number('.8','percent_points'),'pullback_pct':_number('.3','percent_points'),
                'bounce_pct':_number('.2','percent_points')},
            'fake_breakout_reverse': {'breakout_buffer_pct':_number('.05','percent_points')},
        }
        defaults.update(extras[name])
        for key, spec in defaults.items():
            s['strategies.'+name+'.'+key] = Spec(spec.default,spec.unit,spec.kind,spec.low,spec.high,
                spec.inclusive_low,spec.choices,'app/strategies/engine.py + app/runtime.py')
    for key in ('risk.max_leverage','execution.leverage'): s[key] = Spec(**{**vars(s[key]),'unit':'leverage_multiple'})
    return s


def prepare_main(raw):
    specs = main_specs()
    aliases_allowed={ 'strategies.'+n+'.btc_max_drop_pct' for n in
        ('panic_rebound','trend_breakout','pullback_entry','fake_breakout_reverse')}
    aliases_allowed.add('strategies.panic_rebound.drop_pct')
    allowed=set(specs)|aliases_allowed
    def keys(value,prefix=''):
        if not isinstance(value,dict): return
        for k,v in value.items():
            path=prefix+'.'+k if prefix else k
            if path not in allowed and not any(x.startswith(path+'.') for x in allowed):
                raise ConfigurationError('UNKNOWN_FIELD: '+path)
            if path not in allowed and not isinstance(v,dict):
                raise ConfigurationError('Configuration section must be a mapping: '+path)
            if path in allowed and isinstance(v,dict):
                raise ConfigurationError('Scalar/collection field cannot be an empty mapping: '+path)
            keys(v,path)
    keys(raw)
    explicit = dict(flatten(raw))
    # symbols are one semantic collection, not three overridable scalar slots.
    for k in list(explicit):
        if k.startswith('symbols.'): del explicit[k]
    if 'symbols' in raw: explicit['symbols'] = raw['symbols']
    aliases = {}
    for name in ('panic_rebound','trend_breakout','pullback_entry','fake_breakout_reverse'):
        base = 'strategies.'+name+'.'
        aliases[base+'btc_max_drop_pct'] = base+'btc_crash_pct'
    aliases['strategies.panic_rebound.drop_pct'] = 'strategies.panic_rebound.drop_threshold_pct'
    origins = {}
    for alias, target in aliases.items():
        if alias not in explicit: continue
        value = decimal_input(explicit.pop(alias))
        if value <= 0 or value >= 100: raise ConfigurationError('Legacy magnitude alias must be in (0,100)')
        if target in explicit and decimal_input(explicit[target]) != -value:
            raise ConfigurationError('SEMANTIC_ALIAS_CONFLICT: '+alias+' vs '+target)
        if target not in explicit: explicit[target] = -value
        origins[target] = target+' + '+alias if target in dict(flatten(raw)) else alias
    unknown = set(explicit)-set(specs)
    if unknown: raise ConfigurationError('UNKNOWN_FIELD: '+','.join(sorted(unknown)))
    result, provenance = {}, {}
    for path, spec in specs.items():
        if spec.required and path not in explicit: raise ConfigurationError('SAFETY_FIELD_MISSING: '+path)
        value = explicit.get(path, spec.default)
        if path.startswith('strategies.') and path.endswith('.enabled') and path not in explicit and 'strategies' in raw:
            value = path.split('.')[1]=='panic_rebound'
        if spec.kind == 'symbols' and path not in explicit: value = list(value)
        source = origins.get(path,path)
        if path.startswith('strategies.') and path.endswith('.order_type') and path not in explicit:
            value = explicit.get('execution.entry_order_type','MARKET')
            source = 'execution.entry_order_type'
        value = spec.validate(value)
        if path.endswith('btc_crash_pct') or path.endswith('drop_threshold_pct'):
            if value >= 0: raise ConfigurationError('Negative crash/drop threshold required')
        nodes = result
        parts = path.split('.')
        for part in parts[:-1]: nodes = nodes.setdefault(part,{})
        nodes[parts[-1]] = value
        provenance[path] = (source,path not in explicit,spec)
    if not re.fullmatch(r'[a-z][a-z0-9_-]{2,47}',result['instance_id']): raise ConfigurationError('Invalid instance_id')
    if (result['dry_run'] is not True or result['live']['enabled'] is not False
        or result['live']['dedicated_account_confirmed'] is not False or result['live']['confirmation'] != ''):
        raise ConfigurationError('LIVE_HARD_DISABLED')
    if result['live']['bridge_url'] not in ('http://127.0.0.1:8766','http://localhost:8766','http://[::1]:8766'):
        raise ConfigurationError('Only the declared loopback bridge endpoint is supported; no connection is made')
    if not result['dashboard']['require_auth'] and result['dashboard']['host'] not in ('127.0.0.1','localhost','::1'):
        raise ConfigurationError('Nonloopback dashboard requires authentication')
    return result, provenance


# Exact semantic mappings: never infer monetary units from similar field names.
POLICY_UNITS = {
    'max_loss_per_trade_usdt':'USDT_loss_budget','daily_loss_limit_usdt':'USDT_cumulative_losing_outcomes',
    'max_trades_per_day':'count','max_consecutive_losses':'count','max_positions':'count','max_leverage':'leverage_multiple',
    'max_margin_ratio':'equity_fraction','max_margin_usdt':'USDT_margin','max_position_notional_usdt':'USDT_notional',
    'max_position_quantity':'base_quantity','max_risk_fraction_of_equity':'equity_fraction','minimum_risk_budget_usdt':'USDT_loss_budget',
    'btc_crash_3m_pct':'percent_points','max_volatility_pct':'percent_points','max_spread_bps':'bps','max_entry_deviation_bps':'bps',
    'minimum_entry_fee_rate':'fee_rate','minimum_exit_fee_rate':'fee_rate','minimum_entry_slippage_bps':'bps',
    'minimum_exit_slippage_bps':'bps','maximum_entry_slippage_bps':'bps','maximum_exit_slippage_bps':'bps',
    'minimum_data_coverage':'fraction','minimum_score_coverage':'fraction','minimum_total_score':'score_points',
    'minimum_net_rr':'net_R_multiple','minimum':'score_points','minimum_total':'score_points','risk_fraction':'fraction',
    'max_notional_equity_ratio':'equity_multiple','tp1_r':'frozen_R_multiple','tp2_r':'frozen_R_multiple',
    'tp1_fraction':'original_quantity_fraction','tp2_fraction':'original_quantity_fraction','runner_fraction':'original_quantity_fraction',
    'expected_exit_fee_rate':'fee_rate','expected_exit_slippage_bps':'bps','runner_activation_r':'frozen_R_multiple',
    'runner_trail_r':'frozen_R_multiple','swing_buffer_r':'frozen_R_multiple','atr_multiple':'ATR_multiple',
    'max_known_zero_fill_failures':'count','max_control_attempts':'count',
}
TIME_FIELDS = frozenset(('max_market_age_seconds','max_structure_age_seconds','max_account_age_seconds',
    'max_exchange_age_seconds','max_cost_age_seconds','max_scorecard_age_seconds','decision_ttl_seconds',
    'max_holding_assumption_seconds','max_holding_seconds','market_max_age_seconds','evidence_max_age_seconds',
    'score_context_max_age_seconds'))


def field_unit(path, value):
    key = path.split('.')[-1]
    if key in TIME_FIELDS: return 'seconds'
    if key in POLICY_UNITS: return POLICY_UNITS[key]
    if type(value) is bool: return 'boolean'
    if isinstance(value,(str,list)) or value is None: return 'text_or_enum'
    raise ConfigurationError('UNIT_MAPPING_MISSING: '+path)
