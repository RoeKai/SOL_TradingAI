"""Explicit deterministic exit configuration; no environment or file reads."""

from pydantic import Field, StrictBool, model_validator
import yaml

from app.setups.models import Record, Text
from .models import Amount, Positive, Ratio
from typing import Literal


class ExitPolicy(Record):
    version: Text = 'paper-exit-policy/v1'
    mode: Literal['paper_only'] = 'paper_only'
    tp1_r: Positive = '1'
    tp2_r: Positive = '2'
    tp1_fraction: Ratio = '0.3'
    tp2_fraction: Ratio = '0.4'
    runner_fraction: Ratio = '0.3'
    break_even_mode: Literal['entry_price', 'cost_covered'] = 'cost_covered'
    expected_exit_fee_rate: Ratio = '0.0005'
    expected_exit_slippage_bps: Amount = '10'
    runner_strategy: Literal['fixed_r', 'swing', 'atr'] = 'fixed_r'
    runner_activation_r: Positive = '3'
    runner_trail_r: Positive = '1'
    swing_buffer_r: Amount = '0'
    atr_multiple: Positive = '2'
    max_holding_seconds: Positive = '3600'
    trend_exit_enabled: StrictBool = True
    market_max_age_seconds: Positive = '5'
    evidence_max_age_seconds: Positive = '30'
    max_known_zero_fill_failures: int = Field(default=3,strict=True,gt=0)

    @model_validator(mode='after')
    def ordered(self):
        if self.tp1_r >= self.tp2_r or self.runner_activation_r < self.tp2_r:
            raise ValueError('Exit R levels must be ordered')
        if self.tp1_fraction+self.tp2_fraction+self.runner_fraction != 1:
            raise ValueError('Original quantity fractions must sum exactly to one')
        if min(self.tp1_fraction,self.tp2_fraction,self.runner_fraction) <= 0:
            raise ValueError('Three nonzero exit allocations are required')
        if self.expected_exit_fee_rate >= 1 or self.expected_exit_slippage_bps >= 10000:
            raise ValueError('Invalid cost-cover assumptions')
        return self


class _Loader(yaml.SafeLoader):
    def construct_mapping(self,node,deep=False):
        self.flatten_mapping(node)
        keys=[self.construct_object(k,deep=deep) for k,_ in node.value]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate exit policy key')
        return super().construct_mapping(node,deep=deep)


def parse_exit_policy(text: str) -> ExitPolicy:
    if type(text) is not str:
        raise TypeError('Explicit YAML text required')
    if any(isinstance(t,(yaml.tokens.AnchorToken,yaml.tokens.AliasToken)) for t in yaml.scan(text)):
        raise ValueError('No YAML aliases/anchors')
    value=yaml.load(text,Loader=_Loader)
    if type(value) is not dict:
        raise ValueError('Exit policy must be a mapping')
    return ExitPolicy.model_validate(value)
