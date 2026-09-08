import json
import sqlite3
import os
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from app.models import Position
from app.utils.paths import ModulePaths, IsolationError
from app.utils.locking import ProcessLock
from urllib.parse import quote

PENDING = ('PREPARED', 'SUBMITTING', 'NEW', 'PARTIALLY_FILLED', 'UNKNOWN')


class PortfolioManager:
    """One-writer WAL ledger; order identity and cumulative application survive restart."""
    def __init__(self, path: str | Path, initial_balance=500, timezone='Asia/Kuala_Lumpur', mode='paper',
                 *, state_root: Path, instance_id: str):
        self.paths = ModulePaths(state_root, instance_id, mode)
        path = self.paths.require(path, f'trades/{mode}/ledger.sqlite3')
        self.paths.validate_sqlite_files()
        self.instance_id = instance_id
        self.timezone = ZoneInfo(timezone)
        # Inspect immutable existing metadata BEFORE mkdir, WAL, schema, locks or any write.
        # In particular, never attach/create tables in an unknown or foreign ledger.
        existed = path.exists()
        if existed:
            self._verify_existing(path)
        self.paths.directory(f'trades/{mode}')
        self._lock = ProcessLock(self.paths.state_file('runtime.lock'), paths=self.paths)
        try:
            self.paths.validate_sqlite_files()
            if existed:
                self._verify_existing(path)
            else:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self._initialize(initial_balance, mode, existed)
        except BaseException:
            if hasattr(self, 'db'):
                self.db.close()
            self._lock.close()
            raise

    def _verify_existing(self, path):
        # immutable=1 cannot see uncheckpointed WAL identity changes. Never open
        # that ambiguous state read/write in this isolation-only release. Keep it
        # intact for an offline recovery audit; do not delete/checkpoint to bypass.
        wal = self.paths.file(f'trades/{self.paths.mode}/ledger.sqlite3-wal')
        if wal.exists() and wal.stat().st_size:
            raise IsolationError('WAL_RECOVERY_REQUIRES_OFFLINE_VALIDATION')
        probe = None
        try:
            probe = sqlite3.connect(f'file:{quote(str(path), safe="/")}?mode=ro&immutable=1', uri=True)
            # Module identity is checkpointed at creation; missing/corrupt identity fails closed.
            if probe.execute('PRAGMA application_id').fetchone()[0] != 0x534F4C31:
                raise IsolationError('FOREIGN_LEDGER_REFUSED')
            meta = {k: json.loads(v) for k, v in probe.execute('SELECT key,value FROM meta')}
            if (meta.get('isolation_identity') != self.paths.identity or meta.get('mode') != self.paths.mode
                    or meta.get('instance_id') != self.instance_id):
                raise IsolationError('LEDGER_MODE_OR_INSTANCE_MISMATCH')
        except (sqlite3.Error, ValueError, TypeError) as exc:
            if isinstance(exc, IsolationError):
                raise
            raise IsolationError('UNVERIFIED_LEDGER_REFUSED') from None
        finally:
            if probe is not None:
                probe.close()

    def _initialize(self, initial_balance, mode, existed):
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        if existed:
            if self.get_meta('isolation_identity') != self.paths.identity or self.get_meta('mode') != mode:
                raise IsolationError('LEDGER_IDENTITY_MISMATCH')
            return
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(client_id TEXT PRIMARY KEY,status TEXT NOT NULL,
          kind TEXT NOT NULL,position_id TEXT NOT NULL,payload TEXT NOT NULL,created_at REAL NOT NULL,
          applied_qty REAL NOT NULL DEFAULT 0,applied_fee REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS cash_events(id TEXT PRIMARY KEY,amount REAL NOT NULL,at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY,data TEXT NOT NULL,closed_at REAL NOT NULL);
        ''')
        self.db.execute('PRAGMA application_id=1397705777')
        self.set_meta('isolation_identity', self.paths.identity)
        self.set_meta('instance_id', self.instance_id)
        self.set_meta('mode', mode)
        self.set_meta('initial_balance', initial_balance)
        self.db.execute('PRAGMA wal_checkpoint(FULL)')

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        if key in ('isolation_identity', 'instance_id', 'mode'):
            expected = {'isolation_identity': self.paths.identity, 'instance_id': self.instance_id,
                        'mode': self.paths.mode}[key]
            if value != expected:
                raise IsolationError('IMMUTABLE_INSTANCE_IDENTITY')
        self.db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, json.dumps(value, allow_nan=False)))

    def positions(self):
        return [Position(**json.loads(r['data'])) for r in self.db.execute('SELECT data FROM positions')]

    def save_position(self, p: Position):
        self.db.execute('INSERT INTO positions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                        (p.id, json.dumps(p.to_dict(), allow_nan=False)))

    def position(self, position_id):
        row = self.db.execute('SELECT data FROM positions WHERE id=?', (position_id,)).fetchone()
        return Position(**json.loads(row[0])) if row else None

    def order(self, client_id):
        row = self.db.execute('SELECT * FROM orders WHERE client_id=?', (client_id,)).fetchone()
        if row:
            result = dict(row)
            result['payload'] = json.loads(result['payload'])
            return result

    def prepare(self, client_id, kind, position_id, payload, now):
        try:
            self.db.execute('INSERT INTO orders(client_id,status,kind,position_id,payload,created_at) VALUES (?,?,?,?,?,?)',
                            (client_id, 'PREPARED', kind, position_id, json.dumps(payload, allow_nan=False), now))
            return True
        except sqlite3.IntegrityError:
            return False

    def update_order(self, client_id, status, applied_qty=None, applied_fee=None):
        self.db.execute('UPDATE orders SET status=?, applied_qty=coalesce(?,applied_qty), applied_fee=coalesce(?,applied_fee) WHERE client_id=?',
                        (status, applied_qty, applied_fee, client_id))

    def pending(self):
        q = ','.join('?' for _ in PENDING)
        return [self.order(r[0]) for r in self.db.execute(f'SELECT client_id FROM orders WHERE status IN ({q})', PENDING)]

    def add_cash(self, event_id, amount, now):
        self.db.execute('INSERT OR IGNORE INTO cash_events VALUES (?,?,?)', (event_id, amount, now))

    @property
    def balance(self):
        return self.get_meta('initial_balance', 500) + self.db.execute('SELECT coalesce(sum(amount),0) FROM cash_events').fetchone()[0]

    def all_trades(self, limit=1000):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM trades ORDER BY closed_at DESC LIMIT ?', (limit,))]

    def trades_for_day(self, day):
        start = datetime.fromisoformat(day).replace(tzinfo=self.timezone)
        from datetime import timedelta
        end = start + timedelta(days=1)
        return [json.loads(r[0]) for r in self.db.execute(
            'SELECT data FROM trades WHERE closed_at>=? AND closed_at<? ORDER BY closed_at',
            (start.timestamp(), end.timestamp()))]

    def recent_orders(self, limit=50):
        # No raw credential-bearing order payload is exposed by the dashboard.
        return [dict(r) for r in self.db.execute(
            'SELECT client_id,status,kind,position_id,created_at,applied_qty,applied_fee FROM orders '
            'ORDER BY created_at DESC LIMIT ?', (min(200, max(1, limit)),))]

    def record_equity(self, now, prices):
        day = self.day(now)
        key = 'equity:' + day
        metrics = self.metrics(now, prices)
        state = self.get_meta(key, {'peak': metrics['equity'], 'max_drawdown': 0, 'samples': 0})
        state['peak'] = max(state['peak'], metrics['equity'])
        state['max_drawdown'] = max(state['max_drawdown'], state['peak'] - metrics['equity'])
        state['samples'] += 1
        state['last_equity'], state['at'] = metrics['equity'], now
        self.set_meta(key, state)
        return state

    def closed(self, p: Position, price, now, reason):
        row = {**p.to_dict(), 'entry_time': p.opened_at, 'exit_time': now, 'closed_at': now,
               'exit_price': price, 'net_pnl': p.realized_pnl - p.fees,
               'pnl': p.realized_pnl - p.fees, 'holding_seconds': max(0, now-p.opened_at), 'reason': reason,
               'mode': self.get_meta('mode')}
        self.db.execute('INSERT OR IGNORE INTO trades VALUES (?,?,?)', (p.id, json.dumps(row, allow_nan=False), now))
        self.db.execute('DELETE FROM positions WHERE id=?', (p.id,))

    def day(self, timestamp):
        return datetime.fromtimestamp(timestamp, self.timezone).date().isoformat()

    def metrics(self, now, prices=None):
        prices = prices or {}
        positions = self.positions()
        unrealized = sum(p.unrealized(prices.get(p.symbol, p.entry_price)) for p in positions)
        day = self.day(now)
        daily = sum(r['amount'] for r in self.db.execute('SELECT amount,at FROM cash_events') if self.day(r['at']) == day)
        # Persisted entry intents count even while pending/failed: retry cannot evade the daily budget.
        count = sum(1 for r in self.db.execute("SELECT created_at FROM orders WHERE kind='ENTRY'") if self.day(r[0]) == day)
        losses = 0
        for trade in self.all_trades():
            if trade['net_pnl'] < 0:
                losses += 1
            else:
                break
        margin = sum(p.entry_price*p.quantity/p.leverage for p in positions)
        return {'balance': self.balance, 'equity': self.balance+unrealized, 'unrealized_pnl': unrealized,
                'realized_pnl': self.balance-self.get_meta('initial_balance', 500), 'today_pnl': daily,
                'margin_used': margin, 'today_trades': count, 'consecutive_losses': losses,
                'positions_count': len(positions), 'pending_count': len(self.pending()), 'day': day}

    def close(self):
        try:
            self.db.close()
        finally:
            self._lock.close()
