"""Versioned, configurable thresholds; pure parsing of explicitly supplied YAML text."""

from typing import Literal

import yaml
from pydantic import Field, StrictBool, model_validator

from app.setups.models import Record, Text, Positive, Score
from app.setups.scorecard_models import DIMENSIONS
from .models import Amount, PositiveAmount, PositiveInt, Ratio


class DimensionFloor(Record):
    dimension: Literal['direction_confidence', 'entry_quality', 'stop_loss_quality',
        'take_profit_quality', 'rr_quality', 'position_quality', 'execution_clarity']
    minimum: Score


class OpportunityTier(Record):
    name: Literal['S', 'A', 'B', 'C']
    minimum_total: Score
    risk_fraction: Ratio
    max_notional_equity_ratio: PositiveAmount
    minimum_net_rr: PositiveAmount


class MarketRule(Record):
    regime: Literal['trend', 'range', 'high_volatility', 'unknown']
    allowed: StrictBool
    risk_fraction: Ratio
    minimum_net_rr: PositiveAmount


class AdmissionPolicy(Record):
    policy_version: Text = 'paper-risk-admission/v1'
    mode: Literal['paper_only'] = 'paper_only'
    enabled: StrictBool = True
    allowed_symbols: tuple[Text, ...] = ('SOLUSDT',)
    allowed_exchanges: tuple[Text, ...] = ('BINANCE_USDT_M',)
    allowed_risk_sources: tuple[Text, ...] = ('paper-ledger-snapshot/v1',)
    allowed_evidence_verifiers: tuple[Text, ...] = ('paper-structure-review/v1',)
    required_data: tuple[Text, ...] = ('market_price', 'structure', 'volatility', 'btc_reference', 'eth_reference')
    max_market_age_seconds: Positive = 15
    max_structure_age_seconds: Positive = 300
    max_account_age_seconds: Positive = 5
    max_exchange_age_seconds: Positive = 3600
    max_cost_age_seconds: Positive = 300
    max_scorecard_age_seconds: Positive = 30
    decision_ttl_seconds: Positive = 5
    max_holding_assumption_seconds: Positive = 86400
    minimum_data_coverage: Ratio = '0.8'
    minimum_score_coverage: Ratio = '1'
    minimum_total_score: Score = 60
    minimum_net_rr: PositiveAmount = '1.5'
    max_loss_per_trade_usdt: PositiveAmount = '5'
    daily_loss_limit_usdt: PositiveAmount = '20'
    max_trades_per_day: PositiveInt = 3
    max_consecutive_losses: PositiveInt = 2
    max_positions: PositiveInt = 1
    max_leverage: PositiveInt = 5
    max_margin_ratio: Ratio = '0.2'
    max_margin_usdt: Amount = '100'
    max_position_notional_usdt: Amount = '500'
    max_position_quantity: Amount = '100'
    max_risk_fraction_of_equity: Ratio = '0.01'
    minimum_risk_budget_usdt: Amount = '0.5'
    btc_crash_3m_pct: float = Field(default=-0.8, strict=True, allow_inf_nan=False, le=0)
    max_volatility_pct: Positive = 3
    max_spread_bps: Positive = 20
    max_entry_deviation_bps: Positive = 50
    minimum_entry_fee_rate: Ratio = '0.0005'
    minimum_exit_fee_rate: Ratio = '0.0005'
    minimum_entry_slippage_bps: Amount = '10'
    minimum_exit_slippage_bps: Amount = '10'
    maximum_entry_slippage_bps: Amount = '30'
    maximum_exit_slippage_bps: Amount = '30'
    dimension_floors: tuple[DimensionFloor, ...] = Field(default_factory=lambda: tuple(
        DimensionFloor(dimension=d, minimum=s) for d,s in zip(DIMENSIONS, (55,65,80,75,50,50,80))))
    tiers: tuple[OpportunityTier, ...] = Field(default_factory=lambda: (
        OpportunityTier(name='S',minimum_total=85,risk_fraction='1',max_notional_equity_ratio='1',minimum_net_rr='2'),
        OpportunityTier(name='A',minimum_total=75,risk_fraction='0.75',max_notional_equity_ratio='0.75',minimum_net_rr='2'),
        OpportunityTier(name='B',minimum_total=65,risk_fraction='0.5',max_notional_equity_ratio='0.5',minimum_net_rr='1.5'),
        OpportunityTier(name='C',minimum_total=55,risk_fraction='0.25',max_notional_equity_ratio='0.25',minimum_net_rr='1.5')))
    markets: tuple[MarketRule, ...] = Field(default_factory=lambda: (
        MarketRule(regime='trend',allowed=True,risk_fraction='1',minimum_net_rr='2'),
        MarketRule(regime='range',allowed=True,risk_fraction='0.5',minimum_net_rr='1.5'),
        MarketRule(regime='high_volatility',allowed=False,risk_fraction='0.25',minimum_net_rr='2'),
        MarketRule(regime='unknown',allowed=False,risk_fraction='0',minimum_net_rr='2')))

    @model_validator(mode='after')
    def internally_ordered_policy(self):
        for name in ('allowed_symbols','allowed_exchanges','allowed_risk_sources','allowed_evidence_verifiers','required_data'):
            items=getattr(self,name)
            if not items or len(items)!=len(set(items)):
                raise ValueError('Policy lists must be nonempty and unique: '+name)
        if {x.dimension for x in self.dimension_floors}!=set(DIMENSIONS) or len(self.dimension_floors)!=7:
            raise ValueError('Exactly seven primitive dimension floors are required')
        if tuple(t.name for t in self.tiers)!=('S','A','B','C'):
            raise ValueError('Tiers must contain S/A/B/C in descending order')
        for high,low in zip(self.tiers,self.tiers[1:]):
            if (high.minimum_total<=low.minimum_total or high.risk_fraction<low.risk_fraction
                or high.max_notional_equity_ratio<low.max_notional_equity_ratio
                or high.minimum_net_rr<low.minimum_net_rr):
                raise ValueError('Higher tiers cannot reduce RR floors or have disordered limits')
        if {m.regime for m in self.markets}!={'trend','range','high_volatility','unknown'} or len(self.markets)!=4:
            raise ValueError('Exactly four explicit market rules are required')
        if self.minimum_entry_slippage_bps>self.maximum_entry_slippage_bps or self.minimum_exit_slippage_bps>self.maximum_exit_slippage_bps:
            raise ValueError('Cost bounds are inverted')
        if self.minimum_risk_budget_usdt>self.max_loss_per_trade_usdt:
            raise ValueError('Minimum risk budget exceeds single-trade ceiling')
        return self


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys=[self.construct_object(key,deep=deep) for key,_ in node.value]
        if len(set(keys))!=len(keys):
            raise ValueError('Duplicate YAML policy key')
        return super().construct_mapping(node,deep=deep)


def parse_admission_policy(text: str) -> AdmissionPolicy:
    """Accept text only, never a path; no environment expansion or file reading."""
    if type(text) is not str:
        raise TypeError('Pass explicit YAML text, not a configuration path or runtime object')
    if any(isinstance(t,(yaml.tokens.AliasToken,yaml.tokens.AnchorToken)) for t in yaml.scan(text)):
        raise ValueError('YAML aliases/anchors are not accepted in admission policy')
    data=yaml.load(text,Loader=_UniqueSafeLoader)
    if type(data) is not dict:
        raise ValueError('Admission policy must be an explicit mapping')
    return AdmissionPolicy.model_validate(data)
