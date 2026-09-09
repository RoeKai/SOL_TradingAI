"""Strict synthetic-only inputs; no credentials, environmental switches or IO."""
from decimal import Decimal
from typing import Literal

from pydantic import Field, StrictBool, model_validator
from app.setups.models import Record, Text
from app.exits.models import Positive, Amount
from app.admission.models import AccountHardLimits


class OfflineError(ValueError):
    pass


class RunSettings(Record):
    application: Literal['sol-offline-paper/8a'] = 'sol-offline-paper/8a'
    schema_version: Literal[1] = 1
    mode: Literal['synthetic_offline'] = 'synthetic_offline'
    live_allowed: Literal[False] = False
    instance_id: Text
    initial_balance: Positive
    initial_time: int = Field(strict=True, ge=0)
    allow_fixtures: StrictBool = False
    leverage: int = Field(default=5, strict=True, ge=1, le=5)
    limits: AccountHardLimits

    @model_validator(mode='before')
    @classmethod
    def safety_types(cls,value):
        if isinstance(value,dict):
            if 'live_allowed' in value and type(value['live_allowed']) is not bool:
                raise ValueError('live_allowed is a strict boolean, never numeric')
            if 'schema_version' in value and type(value['schema_version']) is not int:
                raise ValueError('schema_version is a strict integer')
        return value

    @model_validator(mode='after')
    def explicit_limits(self):
        if any(getattr(self.limits,k) is None for k in type(self.limits).model_fields):
            raise ValueError('All independent simulated account hard limits are required')
        return self


class Quote(Record):
    event_id: Text
    at: int = Field(strict=True, ge=0)
    bid: Positive | None = None
    ask: Positive | None = None

    @model_validator(mode='after')
    def spread(self):
        if self.bid is not None and self.ask is not None and self.bid>self.ask:
            raise ValueError('Crossed synthetic quote')
        return self


class FixtureEntry(Record):
    """Explicit reconstruction of a synthetic opening leg, never an approval."""
    origin: Literal['FIXTURE_EXISTING_POSITION'] = 'FIXTURE_EXISTING_POSITION'
    fixture_id: Text
    side: Literal['LONG','SHORT']
    quantity: Positive
    reference_price: Positive
    initial_stop: Positive
    risk_budget: Positive
    expires_at: int = Field(strict=True, ge=0)
    expected_revision: int = Field(strict=True, ge=0)
    bundle_digest: Text

    @model_validator(mode='after')
    def stop_side(self):
        if (self.side=='LONG' and self.initial_stop>=self.reference_price or
            self.side=='SHORT' and self.initial_stop<=self.reference_price):
            raise ValueError('Fixture initial stop must be on the loss side')
        return self


def amount(value):
    if isinstance(value,bool) or not isinstance(value,(str,int,Decimal)):
        raise OfflineError('Explicit decimal amount required')
    value=Decimal(value)
    if not value.is_finite() or value<0:
        raise OfflineError('Nonnegative finite amount required')
    return value
