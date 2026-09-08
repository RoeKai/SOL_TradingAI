"""Last-line redaction for monitoring and external notification payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_PRIVATE_KEYS = re.compile(r"(?i)(secret|password|passphrase|api.?key|token|signature|authorization|cookie|credential)")
_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_QUERY_SECRET = re.compile(r"(?i)(signature|api_?key|token|secret|password)=([^\s&]+)")


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Recursively omit credentials; never serialize an exception's request object."""
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if _PRIVATE_KEYS.search(str(key)) else redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        result = _TOKEN.sub("[REDACTED]", value)
        result = _QUERY_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact(str(value), secrets)
