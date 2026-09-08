"""Telegram notifications on an isolated, bounded best-effort channel.

Trading code should use ``notify`` (non-blocking), not wait for Telegram before
protecting/closing a position. No notification failure changes execution state.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .redaction import redact

_LABELS = {
    'position_opened': '模拟开仓', 'position_closed': '模拟平仓', 'position_reduced': '模拟减仓',
    'stop_triggered': '模拟止损触发',
    "entry": "开仓", "close": "平仓", "stop_loss": "止损",
    "circuit_breaker": "交易熔断", "api_error": "API 错误",
    "websocket_disconnect": "行情连接中断", "websocket_reconnected": "行情已恢复",
    "risk_rejected": "风控拒绝", "stop_failed": "保护单失败 / 紧急平仓",
}


class AlertManager:
    def __init__(self, *, enabled: bool = False, bot_token: str = "", chat_id: str = "",
                 audit: Any = None, client: httpx.AsyncClient | None = None,
                 queue_size: int = 100, max_attempts: int = 3) -> None:
        self.enabled = enabled
        self._token = bot_token
        self._chat_id = chat_id
        self._audit = audit
        self._client = client
        self._owns_client = client is None
        self._max_attempts = max(1, min(max_attempts, 3))
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=queue_size)
        self._worker: asyncio.Task[None] | None = None

    def _log(self, event: str, **fields: Any) -> None:
        if self._audit is not None:
            self._audit.emit(event, **redact(fields, (self._token, self._chat_id)))

    async def start(self) -> None:
        if self.enabled and self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="sol-telegram-alerts")

    def notify(self, event: str, payload: dict[str, Any]) -> None:
        """Never block the order / stop-loss path or grow memory without bound."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait((event, redact(payload, (self._token,))))
        except asyncio.QueueFull:
            self._log("telegram_queue_full", alert_event=event, dropped=True)

    async def _run(self) -> None:
        while True:
            event, payload = await self._queue.get()
            try:
                await self.send(event, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # notifications never crash the runtime
                self._log("telegram_worker_error", error_type=type(exc).__name__)
            finally:
                self._queue.task_done()

    async def send(self, event: str, payload: dict[str, Any]) -> bool:
        """Finite retries, no Markdown injection, and no secrets in error logs."""
        if not self.enabled:
            return False
        if not self._token or not self._chat_id:
            self._log("telegram_not_configured", alert_event=event)
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0, follow_redirects=False, trust_env=False)
        safe_payload = redact(payload, (self._token,))
        body = "\n".join(f"{key}: {value}" for key, value in safe_payload.items())
        message = redact(f"SOL AI · {_LABELS.get(event, event)}\n{body}", (self._token,))
        if len(message) > 4000:
            message = message[:3950] + "\n[详情请查看本地审计日志]"
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        for attempt in range(self._max_attempts):
            delay = min(2 ** attempt, 4)
            try:
                response = await self._client.post(url, json={
                    "chat_id": self._chat_id, "text": message,
                    "disable_web_page_preview": True,
                })
                try:
                    data = response.json()
                except (ValueError, json.JSONDecodeError):
                    data = {}
                if response.status_code == 200 and data.get("ok") is True:
                    self._log("telegram_sent", alert_event=event,
                              message_id=data.get("result", {}).get("message_id"))
                    return True
                code = response.status_code
                self._log("telegram_send_failed", alert_event=event, status=code, attempt=attempt + 1)
                if code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 1)
                    try:
                        delay = max(1, int(retry_after))
                    except (TypeError, ValueError):
                        delay = 1
                    # Never violate Telegram's requested wait; drop instead of
                    # keeping the worker blocked for a long retry interval.
                    if delay > 10:
                        self._log("telegram_retry_deferred", retry_after=delay, alert_event=event)
                        return False
                elif code < 500:
                    return False
            except (httpx.HTTPError, OSError) as exc:
                # str(exc) can contain a URL with the Bot Token. Do not log it.
                self._log("telegram_send_error", alert_event=event,
                          error_type=type(exc).__name__, attempt=attempt + 1)
            if attempt + 1 < self._max_attempts:
                await asyncio.sleep(delay)
        return False

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
