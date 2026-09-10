"""Dedicated SQLite boundary. No adoption, reset, migrations or parent fallback."""
from contextlib import contextmanager
from decimal import Context, localcontext
import hashlib
import json
import os
import re
import sqlite3

from app.utils.paths import ModulePaths, check_owned
from app.configuration.compiler import verify_bundle, main_values
from app.configuration.models import ConfigBundle
from .models import OfflineError, RunSettings

APPLICATION_ID=1397705784
TABLES=('identity','account','configurations','requests','reservations','positions','fills',
        'inbox','outbox','broker_commands','broker_orders','broker_fills','broker_events','quarantine')
# Explicit 8B schema only; the 8A table set/identity remains unchanged.
ADMITTED_TABLES=('paper_providers','paper_inputs','paper_candidates','paper_approvals','paper_consumptions')


def encode(value):
    if hasattr(value,'model_dump'): value=value.model_dump(mode='json')
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def get(db,table,key):
    if table not in (*TABLES,*ADMITTED_TABLES): raise OfflineError('Unknown table')
    row=db.execute('SELECT payload FROM '+table+' WHERE id=?',(key,)).fetchone()
    return None if row is None else json.loads(row[0])


def put(db,table,key,value):
    if table not in (*TABLES,*ADMITTED_TABLES): raise OfflineError('Unknown table')
    db.execute('INSERT INTO '+table+'(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
               (key,encode(value)))


def rows(db,table):
    if table not in (*TABLES,*ADMITTED_TABLES): raise OfflineError('Unknown table')
    return [(r[0],json.loads(r[1])) for r in db.execute('SELECT id,payload FROM '+table+' ORDER BY rowid')]


class Store:
    """BEGIN IMMEDIATE is the cross-process execution lock; no lock-only-in-RAM."""
    settings_model=RunSettings
    application_id=APPLICATION_ID
    schema_version=1
    table_names=TABLES
    directory='offline-runs'
    def __init__(self,workspace,run_id,instance_id):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{2,63}',run_id): raise OfflineError('Invalid run ID')
        self.paths=ModulePaths(workspace)
        self.root=self.paths.file(self.directory+'/'+run_id)
        self.path=self.paths.file(self.directory+'/'+run_id+'/ledger.sqlite3')
        self.run_id=run_id;self.instance_id=instance_id;self.failed=False

    @classmethod
    def create(cls,workspace,run_id,settings,bundle):
        settings=cls.settings_model.model_validate(settings.model_dump())
        checked=verify_bundle(bundle)
        if checked.parsing!='PASS' or checked.consistency!='PASS' or settings.instance_id!=bundle.manifest.instance_id:
            raise OfflineError('Configuration/instance mismatch')
        values=main_values(bundle);risk=values['risk']
        for setting,key in (('max_loss_per_trade_usdt','max_loss_per_trade'),('daily_loss_limit_usdt','daily_loss_limit'),
            ('max_trades_per_day','max_trades_per_day'),('max_consecutive_losses','max_consecutive_losses'),
            ('max_positions','max_positions'),('max_leverage','max_leverage'),('max_margin_ratio','max_margin_ratio')):
            from decimal import Decimal
            if Decimal(str(getattr(settings.limits,setting)))>Decimal(str(risk[key])):
                raise OfflineError('Simulated account limit exceeds frozen configuration: '+setting)
        if settings.leverage>settings.limits.max_leverage:
            raise OfflineError('Leverage exceeds account limit')
        if settings.initial_balance!=Decimal(str(values['paper']['initial_balance'])):
            raise OfflineError('Explicit initial balance/configuration conflict')
        store=cls(workspace,run_id,settings.instance_id)
        if store.root.exists(): raise OfflineError('Run already exists; never reinitialize balance')
        store.paths.directory(store.directory+'/'+run_id)
        descriptor=os.open(store.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.close(descriptor)
        db=sqlite3.connect(store.path,isolation_level=None)
        try:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            db.execute('PRAGMA application_id='+str(cls.application_id));db.execute('PRAGMA user_version='+str(cls.schema_version))
            for table in cls.table_names: db.execute('CREATE TABLE '+table+' (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            put(db,'identity','identity',dict(settings=settings.model_dump(mode='json'),run_id=run_id,bundle_digest=bundle.bundle_digest))
            put(db,'configurations',bundle.bundle_digest,bundle)
            put(db,'account','account',dict(version=0,now=settings.initial_time,cash=str(settings.initial_balance),
                paused=False,reconciliation_clear=True,quote=None,quarantined=False,recovery_count=0))
            db.commit()
        except BaseException:
            db.rollback();raise  # Preserve incomplete file as evidence; never adopt it next time.
        finally: db.close()
        return store

    def connect(self):
        if self.failed: raise OfflineError('STORE_FAILED_REOPEN_AND_RECOVER_REQUIRED')
        for suffix in ('','-wal','-shm','-journal'): check_owned(str(self.path)+suffix)
        if not self.path.is_file(): raise OfflineError('Explicit initialization required')
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=5)
        try:
            if db.execute('PRAGMA application_id').fetchone()[0]!=self.application_id or db.execute('PRAGMA user_version').fetchone()[0]!=self.schema_version:
                raise OfflineError('Unknown database/version; no automatic schema creation')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables!=set(self.table_names): raise OfflineError('Database schema mismatch')
            identity=get(db,'identity','identity')
            settings=self.settings_model.model_validate(identity['settings'])
            if identity['run_id']!=self.run_id or settings.instance_id!=self.instance_id:
                raise OfflineError('Foreign database identity')
            db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA foreign_keys=ON')
            return db
        except BaseException:
            db.close();raise

    @contextmanager
    def transaction(self):
        try: db=self.connect()
        except sqlite3.Error:
            self.failed=True;raise
        try:
            db.execute('BEGIN IMMEDIATE')
            with localcontext(Context(prec=50)):
                yield db
            db.commit()
        except BaseException as error:
            db.rollback()
            if isinstance(error,sqlite3.Error): self.failed=True
            raise
        finally: db.close()

    def settings(self,db):
        return self.settings_model.model_validate(get(db,'identity','identity')['settings'])

    def bundle(self,db):
        key=get(db,'identity','identity')['bundle_digest']
        bundle=ConfigBundle.model_validate(get(db,'configurations',key))
        if bundle.bundle_digest!=key or verify_bundle(bundle).consistency!='PASS': raise OfflineError('Saved bundle invalid')
        return bundle

    def quarantine(self,reason):
        with self.transaction() as db:
            account=get(db,'account','account');account['quarantined']=True;account['reconciliation_clear']=False
            account['version']+=1;put(db,'account','account',account)
            put(db,'quarantine',digest({'reason':reason,'version':account['version']}),{'reason':reason,'at':account['now']})
