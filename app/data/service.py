"""Bounded Binance USD-M market subscriptions, with independent routed sessions.

Official migration reference (checked 2026-09-09):
https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice

Market: ticker / aggTrade / 1m kline. Public: full top-20 snapshots,
not a diff-book stream, so no unbounded REST order-book reconstruction.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets


async def _call(callback: Callable | None, *args: Any) -> None:
    if callback is not None:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result


class DataService:
    def __init__(
        self,
        symbols: list[str],
        on_event: Callable,
        *,
        audit: Callable | None = None,
        alert: Callable | None = None,
        ws_base_url: str = "wss://fstream.binance.com",
        rest_base_url: str = "https://fapi.binance.com",
        warmup_candles: int = 120,
        reconnect_max_seconds: float = 60,
    ) -> None:
        self.symbols = tuple(dict.fromkeys(s.upper() for s in symbols))
        if not self.symbols or any(not s.isalnum() or not s.endswith("USDT") for s in self.symbols):
            raise ValueError("Only explicit USDT symbols are supported")
        if ws_base_url != 'wss://fstream.binance.com' or rest_base_url != 'https://fapi.binance.com':
            raise ValueError('Only fixed official public market hosts are allowed')
        self.on_event, self.audit, self.alert = on_event, audit, alert
        self.ws_base_url = ws_base_url.rstrip("/")
        self.rest_base_url = rest_base_url.rstrip("/")
        self.warmup_candles = min(500, max(20, int(warmup_candles)))
        self.reconnect_max_seconds = min(120.0, max(1.0, reconnect_max_seconds))
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._sockets: dict[str, Any] = {}
        self._last_warmup = 0.0
        self.receipts: dict[str, int] = {}
        self._status = {
            route: {"connected": False, "last_event_at": None, "reconnects": 0, "error": None}
            for route in ("market", "public")
        }

    def stream_urls(self) -> dict[str, str]:
        symbols = [s.lower() for s in self.symbols]
        market = [f"{s}@{stream}" for s in symbols for stream in ("ticker", "kline_1m", "aggTrade")]
        public = [f"{s}@depth20@500ms" for s in symbols]
        return {
            "market": f"{self.ws_base_url}/market/stream?streams={'/'.join(market)}",
            "public": f"{self.ws_base_url}/public/stream?streams={'/'.join(public)}",
        }

    def status(self) -> dict[str, Any]:
        return {key: dict(value) for key, value in self._status.items()}

    def reconnect_delay(self, attempts: int) -> float:
        ceiling = min(self.reconnect_max_seconds, 2 ** min(max(0, attempts), 7))
        return random.uniform(ceiling * 0.5, ceiling)

    async def _public_get(self, client, path, params=None):
        # This capability has no arbitrary method/path, credentials, signatures,
        # redirects or inherited proxies; no private read or order route exists.
        if self.rest_base_url != 'https://fapi.binance.com' or path not in ('/fapi/v1/klines', '/fapi/v1/exchangeInfo'):
            raise ValueError('PRIVATE_OR_UNKNOWN_ENDPOINT_REFUSED')
        if path == '/fapi/v1/exchangeInfo' and params:
            raise ValueError('UNEXPECTED_PUBLIC_PARAMETERS')
        if path == '/fapi/v1/klines' and (not isinstance(params, dict)
                or set(params) != {'symbol', 'interval', 'limit'} or params['symbol'] not in self.symbols
                or params['interval'] != '1m' or type(params['limit']) is not int or not 20 <= params['limit'] <= 500):
            raise ValueError('UNEXPECTED_PUBLIC_PARAMETERS')
        key = 'GET ' + path
        self.receipts[key] = self.receipts.get(key, 0) + 1
        await _call(self.audit, 'public_request', {'method': 'GET', 'host': 'fapi.binance.com', 'path': path})
        response = await client.get(self.rest_base_url + path, params=params)
        response.raise_for_status()
        return response.json()

    async def exchange_rules(self):
        """One startup public metadata fetch, never private account discovery."""
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                payload = await self._public_get(client, '/fapi/v1/exchangeInfo')
            row = next(s for s in payload['symbols'] if s['symbol'] == 'SOLUSDT')
            if row.get('status') != 'TRADING' or row.get('contractType') != 'PERPETUAL' or row.get('quoteAsset') != 'USDT':
                raise ValueError('SOL_USDT_PERPETUAL_NOT_TRADING')
            await _call(self.on_event, {'e': 'exchange_rules', 's': 'SOLUSDT', 'rules': row})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await _call(self.audit, 'market_rules_error', {'error': str(exc)})
            await _call(self.alert, 'api_error', 'Public contract rules unavailable; new paper entries disabled')

    async def warmup(self) -> None:
        """One bounded REST bootstrap, then at most one backfill per 60 seconds.

        The normal stream has no REST market polling. Backfill is called only
        at startup and after a market WebSocket disconnection; failures leave
        the indicator engine unready rather than manufacturing missing bars.
        """
        now = time.monotonic()
        if self._last_warmup and now - self._last_warmup < 60:
            return
        self._last_warmup = now
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            for symbol in self.symbols:
                if self._stop.is_set():
                    return
                try:
                    rows = await self._public_get(client, '/fapi/v1/klines',
                        {"symbol": symbol, "interval": "1m", "limit": self.warmup_candles})
                    if not isinstance(rows, list):
                        raise ValueError("Unexpected kline response")
                    now_ms = int(time.time() * 1000)
                    for row in rows:
                        # REST's current unfinished candle cannot impersonate a live event.
                        if int(row[6]) >= now_ms:
                            continue
                        event = {
                            "e": "kline", "s": symbol, "E": int(row[6]), "_warmup": True,
                            "k": {"t": row[0], "T": row[6], "s": symbol, "i": "1m",
                                  "o": row[1], "h": row[2], "l": row[3], "c": row[4],
                                  "v": row[5], "x": True},
                        }
                        await _call(self.audit, "market_data", event)
                        await _call(self.on_event, event)
                    await _call(self.audit, "market_bootstrap", {"symbol": symbol, "rows": len(rows)})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await _call(self.audit, "market_bootstrap_error", {"symbol": symbol, "error": str(exc)})
                    await _call(self.alert, "api_error", f"Market bootstrap failed for {symbol}: {exc}")

    async def handle_message(self, raw: str | bytes, route: str) -> None:
        try:
            decoded = json.loads(raw)
            event = decoded.get("data", decoded)
            if not isinstance(event, dict):
                raise ValueError("WebSocket event is not an object")
            if event.get("s") not in self.symbols:
                return
            if str(event.get("st", "1")) != "1":
                return  # Do not mix COIN-M after UM/CM migration.
            if event.get("e") not in {"kline", "aggTrade", "24hrTicker", "depthUpdate"}:
                return
            await _call(self.audit, "market_data", event)
            await _call(self.on_event, event)
            self._status[route]["last_event_at"] = time.time()
        except (TypeError, ValueError, KeyError) as exc:
            await _call(self.audit, "market_payload_error", {"route": route, "error": str(exc)})

    async def _consume(self, route: str, url: str) -> None:
        if self.ws_base_url != 'wss://fstream.binance.com' or route not in ('market', 'public') or url != self.stream_urls()[route]:
            raise ValueError('PRIVATE_OR_UNKNOWN_WEBSOCKET_REFUSED')
        attempts = 0
        while not self._stop.is_set():
            connected_at = time.monotonic()
            try:
                key = 'WS /' + route
                self.receipts[key] = self.receipts.get(key, 0) + 1
                await _call(self.audit, 'public_request', {'method': 'WS', 'host': 'fstream.binance.com', 'path': '/' + route})
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5,
                    open_timeout=15, max_size=256_000, max_queue=64, proxy=None,
                ) as socket:
                    self._sockets[route] = socket
                    self._status[route].update(connected=True, error=None)
                    await _call(self.audit, "websocket_connected", {"route": route})
                    while not self._stop.is_set():
                        # A connected socket that receives nothing is not a healthy feed.
                        raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        await self.handle_message(raw, route)
                if not self._stop.is_set():
                    raise ConnectionError("WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._status[route].update(connected=False, error=str(exc))
                if self._stop.is_set():
                    break
                self._status[route]["reconnects"] += 1
                await _call(self.on_event, {"e": "feed_disconnected", "route": route, "E": int(time.time() * 1000)})
                await _call(self.audit, "websocket_disconnected", {"route": route, "error": str(exc)})
                await _call(self.alert, "websocket_disconnect", f"Binance {route} stream disconnected: {exc}")
                if time.monotonic() - connected_at > 60:
                    attempts = 0
                delay = self.reconnect_delay(attempts)
                attempts += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                if route == "market" and not self._stop.is_set():
                    await self.warmup()
            finally:
                self._sockets.pop(route, None)
                self._status[route]["connected"] = False

    async def run(self) -> None:
        if self._tasks:
            raise RuntimeError("DataService is already running")
        await self.exchange_rules()
        await self.warmup()
        self._tasks = [asyncio.create_task(self._consume(route, url), name=f"market-{route}")
                       for route, url in self.stream_urls().items()]
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._stop.set()
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for socket in list(self._sockets.values()):
            await socket.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
