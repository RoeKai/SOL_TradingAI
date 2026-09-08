import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from collections import deque
from datetime import datetime, timezone
from threading import RLock
from app.utils.paths import ModulePaths


class AuditLog:
    def __init__(self, directory: str | Path, secrets=(), max_bytes=10_000_000, backups=5, *, paths: ModulePaths):
        directory = paths.require(directory, f'logs/{paths.mode}')
        # Check active and rotated targets before opening any file; reject symlink/hardlink sinks.
        for i in range(backups + 1):
            suffix = f'.{i}' if i else ''
            paths.file(f'logs/{paths.mode}/events.jsonl{suffix}')
        paths.directory(f'logs/{paths.mode}')
        self.paths = paths
        self.secrets = [str(s) for s in secrets if s and len(str(s)) >= 3]
        self.items = deque(maxlen=500)
        self.lock = RLock()
        self.logger = logging.Logger(f'sol-ai-{id(self)}', logging.INFO)
        handler = RotatingFileHandler(Path(directory) / 'events.jsonl', maxBytes=max_bytes,
                                      backupCount=backups, encoding='utf8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(handler)

    def redact(self, value):
        if isinstance(value, dict):
            return {k: ('[REDACTED]' if re.search(r'key|secret|token|signature|authorization', k, re.I)
                        else self.redact(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(x) for x in value]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        s = str(value)
        for secret in self.secrets:
            s = s.replace(secret, '[REDACTED]')
        s = re.sub(r'(signature|token|apiKey)=([^&\s]+)', r'\1=[REDACTED]', s, flags=re.I)
        return s

    def emit(self, event: str, **fields):
        with self.lock:
            row = self.redact({'timestamp': datetime.now(timezone.utc).isoformat(),
                               'event': event, 'account_id': self.paths.instance_id,
                               'mode': self.paths.mode, **fields})
            self.logger.info(json.dumps(row, ensure_ascii=False, allow_nan=False, default=str))
            if event != 'market_data':
                self.items.append(row)

    def recent(self, limit=100):
        with self.lock:
            return list(self.items)[-min(max(int(limit), 1), 500):]

    def close(self):
        for handler in self.logger.handlers:
            handler.close()
