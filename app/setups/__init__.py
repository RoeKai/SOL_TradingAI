"""Descriptive trade plans only; deliberately not imported by the trading runtime."""

from .models import TradeSetup
from .adapters import adapt_legacy_signal

__all__ = ['TradeSetup', 'adapt_legacy_signal']
