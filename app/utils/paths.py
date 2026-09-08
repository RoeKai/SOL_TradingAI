"""Dedicated-module filesystem capability. Never search parent configuration/state."""
import json
import os
from pathlib import Path
import re
import stat

APPLICATION = 'sol-ai-trading-system'
POLICY = {'application': APPLICATION, 'policy_version': 1, 'live_runtime_allowed': False}


class IsolationError(ValueError):
    pass


def lexical_path(path):
    # Do not resolve() before checking: that would hide a symlink escape.
    raw = Path(path)
    if '..' in raw.parts:
        raise IsolationError('PATH_TRAVERSAL_REFUSED')
    return Path(os.path.abspath(raw))


def reject_links(path):
    path = lexical_path(path)
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise IsolationError('SYMLINK_REFUSED')
        if item == path and stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise IsolationError('HARDLINK_REFUSED')
    return path


def check_owned(path, *, secret=False):
    path = reject_links(path)
    if not path.exists():
        return path
    info = path.stat()
    if info.st_uid != os.geteuid():
        raise IsolationError('FOREIGN_OWNER_REFUSED')
    if info.st_mode & (0o077 if secret else 0o022):
        raise IsolationError('UNSAFE_FILE_PERMISSIONS')
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise IsolationError('SPECIAL_FILE_REFUSED')
    return path


def read_text_nofollow(path, *, secret=False):
    path = check_owned(path, secret=secret)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise IsolationError('UNSAFE_OPEN_FILE')
        if info.st_mode & (0o077 if secret else 0o022):
            raise IsolationError('UNSAFE_FILE_PERMISSIONS')
        with os.fdopen(fd, encoding='utf8') as stream:
            fd = -1
            return stream.read()
    finally:
        if fd >= 0:
            os.close(fd)


class ModulePaths:
    def __init__(self, root, instance_id='sol-ai-local', mode='paper'):
        self.root = check_owned(root)
        if not self.root.is_dir():
            raise IsolationError('MODULE_ROOT_MISSING')
        if not isinstance(instance_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{2,47}', instance_id):
            raise IsolationError('INVALID_INSTANCE_ID')
        if mode not in ('paper', 'live'):
            raise IsolationError('INVALID_STATE_MODE')
        self.instance_id, self.mode = instance_id, mode
        marker = self.file('isolation-policy.json')
        if json.loads(read_text_nofollow(marker)) != POLICY:
            raise IsolationError('ISOLATION_POLICY_MISMATCH')

    def file(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts:
            raise IsolationError('OUTSIDE_MODULE_ROOT')
        target = lexical_path(self.root / relative)
        if not target.is_relative_to(self.root):
            raise IsolationError('OUTSIDE_MODULE_ROOT')
        for node in (target, *target.parents):
            if node == self.root:
                break
            check_owned(node)
        return target

    def require(self, path, relative):
        expected = self.file(relative)
        if lexical_path(path) != expected:
            raise IsolationError('OUTSIDE_EXPECTED_STATE_PATH')
        return expected

    def directory(self, relative):
        target = self.file(relative)
        current = self.root
        for part in target.relative_to(self.root).parts:
            current = current / part
            check_owned(current)
            current.mkdir(mode=0o700, exist_ok=True)
            check_owned(current)
        return target

    @property
    def ledger(self):
        return self.file(f'trades/{self.mode}/ledger.sqlite3')

    @property
    def identity(self):
        return {'application': APPLICATION, 'schema_version': 1,
                'instance_id': self.instance_id, 'mode': self.mode}

    def state_file(self, name):
        if Path(name).name != name:
            raise IsolationError('INVALID_STATE_FILENAME')
        return self.file(f'trades/{self.mode}/{name}')

    def validate_sqlite_files(self):
        for suffix in ('', '-wal', '-shm', '-journal'):
            self.file(f'trades/{self.mode}/ledger.sqlite3{suffix}')


def assert_paper_runtime(config):
    """Public-market paper runtime has no production live capability to unlock."""
    if config.dry_run is not True or config.live.enabled is not False:
        raise IsolationError('LIVE_ISOLATION_NOT_ACCEPTED: this release only permits paper trading')
