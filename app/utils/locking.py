import fcntl
import os
from pathlib import Path
from app.utils.paths import ModulePaths


class ProcessLock:
    def __init__(self, path: Path, *, paths: ModulePaths):
        path = paths.require(path, f'trades/{paths.mode}/runtime.lock')
        paths.directory(f'trades/{paths.mode}')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, 'a+')
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another SOL runtime owns this state directory; refusing a second writer')

    def close(self):
        if not self.file.closed:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
