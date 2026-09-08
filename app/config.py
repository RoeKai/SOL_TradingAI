from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from app.utils.paths import ModulePaths, IsolationError, read_text_nofollow

ROOT = Path(__file__).resolve().parent.parent


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class PaperConfig(StrictModel):
    initial_balance: float = Field(default=500, gt=0)


class LiveConfig(StrictModel):
    enabled: StrictBool = False
    confirmation: str = ''
    dedicated_account_confirmed: StrictBool = False
    max_account_equity_usdt: float = Field(default=500, gt=0)
    bridge_url: str = 'http://127.0.0.1:8766'
    reconcile_interval_seconds: float = Field(default=10, ge=5, le=60)
    order_timeout_seconds: float = Field(default=8, gt=0, le=30)


class RiskConfig(StrictModel):
    max_loss_per_trade: float = Field(default=5, gt=0)
    daily_loss_limit: float = Field(default=20, gt=0)
    max_trades_per_day: int = Field(default=3, ge=1)
    max_consecutive_losses: int = Field(default=2, ge=1)
    max_positions: int = Field(default=1, ge=1, le=1)
    max_margin_ratio: float = Field(default=.2, gt=0, le=.2)
    max_leverage: int = Field(default=5, ge=1, le=5)
    btc_crash_pct: float = Field(default=-.8, lt=0)
    taker_fee_rate: float = Field(default=.0005, ge=0, le=.01)
    slippage_bps: float = Field(default=10, ge=0, le=100)


class ExecutionConfig(StrictModel):
    leverage: int = Field(default=5, ge=1, le=5)
    entry_order_type: Literal['MARKET', 'LIMIT'] = 'MARKET'
    limit_expiry_seconds: float = Field(default=30, ge=1, le=300)


class DataConfig(StrictModel):
    stale_after_seconds: float = Field(default=15, ge=2, le=120)
    warmup_candles: int = Field(default=120, ge=20, le=240)
    ws_base_url: str = 'wss://fstream.binance.com'
    rest_base_url: str = 'https://fapi.binance.com'


class RuntimeConfig(StrictModel):
    scoring_interval_seconds: float = Field(default=.5, ge=.1, le=5)
    heartbeat_seconds: float = Field(default=1, ge=.2, le=5)
    max_event_loop_lag_seconds: float = Field(default=5, ge=1, le=15)


class DashboardConfig(StrictModel):
    host: str = '127.0.0.1'
    port: int = Field(default=8765, ge=1024, le=65535)
    require_auth: StrictBool = True
    title: str = 'SOL AI 独立交易台'
    root_path: str = ''


class AlertsConfig(StrictModel):
    enabled: StrictBool = False


class ReviewConfig(StrictModel):
    daily_hour: int = Field(default=0, ge=0, le=23)
    daily_minute: int = Field(default=5, ge=0, le=59)


class Config(StrictModel):
    instance_id: str = Field(default='sol-ai-local', pattern=r'^[a-z][a-z0-9_-]{2,47}$')
    dry_run: StrictBool = True
    timezone: str = 'Asia/Kuala_Lumpur'
    symbols: list[str] = ['SOLUSDT', 'BTCUSDT', 'ETHUSDT']
    trade_symbol: Literal['SOLUSDT'] = 'SOLUSDT'
    paper: PaperConfig = Field(default_factory=PaperConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    strategies: dict = Field(default_factory=lambda: {name: {'enabled': True} for name in
        ('panic_rebound', 'trend_breakout', 'pullback_entry', 'fake_breakout_reverse')})
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)

    @model_validator(mode='after')
    def safety_contract(self):
        ZoneInfo(self.timezone)
        if set(self.symbols) != {'SOLUSDT', 'BTCUSDT', 'ETHUSDT'}:
            raise ValueError('V1 requires exactly SOLUSDT/BTCUSDT/ETHUSDT; only SOL may trade')
        if self.execution.leverage > self.risk.max_leverage:
            raise ValueError('Execution leverage exceeds risk cap')
        if not self.dashboard.require_auth and self.dashboard.host not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('Authentication is mandatory on non-loopback interfaces')
        if not self.dry_run and not (self.live.enabled and self.live.dedicated_account_confirmed
                                    and self.live.confirmation == 'ENABLE_SMALL_CAPITAL_LIVE'):
            raise ValueError('Live requires dry_run=false, enabled, dedicated account confirmation and exact confirmation phrase in config.yaml')
        url = urlparse(self.live.bridge_url)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.username:
            raise ValueError('Execution bridge must be local to the independent backend')
        if self.data.ws_base_url != 'wss://fstream.binance.com' or self.data.rest_base_url != 'https://fapi.binance.com':
            raise ValueError('Only official Binance USDT-M market endpoints are allowed')
        return self


def load_config(path: str | Path = ROOT / 'config.yaml', *, module_root: Path = ROOT) -> Config:
    paths = ModulePaths(module_root)
    path = paths.require(path, 'config.yaml')
    try:
        raw = yaml.safe_load(read_text_nofollow(path))
    except yaml.YAMLError:
        raise IsolationError('CONFIG_PARSE_REFUSED') from None
    if not isinstance(raw, dict):
        raise ValueError('config.yaml must be a mapping')
    # Bool strings must not silently enable real trading.
    if 'dry_run' not in raw or not isinstance(raw['dry_run'], bool):
        raise ValueError('dry_run must be a YAML boolean')
    return Config.model_validate(raw)


def load_secrets(path: str | Path = ROOT / '.env', *, module_root: Path = ROOT,
                 paper_controls_only: bool = False) -> dict[str, str]:
    """No upward .env search, inherited copy-account credentials, or YAML secrets."""
    allowed = {'BINANCE_API_KEY', 'BINANCE_API_SECRET', 'SOL_BRIDGE_TOKEN',
               'SOL_DASHBOARD_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'}
    paths = ModulePaths(module_root)
    path = paths.require(path, '.env')
    # parse an explicit stream; never invoke find_dotenv, os.environ or interpolation.
    from io import StringIO
    content = read_text_nofollow(path, secret=True) if path.exists() else ''
    if '${' in content:
        raise IsolationError('ENV_INTERPOLATION_REFUSED')
    values = dotenv_values(stream=StringIO(content), interpolate=False)
    if any(key not in allowed for key in values):
        raise IsolationError('ENV_KEY_NOT_ALLOWED')
    selected = {'SOL_DASHBOARD_TOKEN', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'} if paper_controls_only else allowed
    return {key: str(values.get(key) or '') for key in selected}
