"""Public market data; never imports or calls an order endpoint."""

from .service import DataService

__all__ = ["DataService"]
