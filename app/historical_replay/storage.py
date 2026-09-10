"""8C-only table access. The original 8A/8B SQL allowlist is NOT expanded."""
from contextlib import contextmanager
import json
import threading
from app.offline_paper.storage import Store, TABLES, encode
from .models import HistoricalSettings, ExecutionModel
from .data import verify_seal, HistoricalError

TABLE_NAMES=('history_meta','history_cursor','history_samples','history_levels',
    'history_candidates','history_approvals','history_consumptions','history_funding',
    'history_equity','history_signals','history_execution','history_batches')


def check(db,table):
    if table not in TABLE_NAMES: raise HistoricalError('UNKNOWN_HISTORICAL_TABLE')
    if db.execute('PRAGMA application_id').fetchone()[0]!=1397705786 or db.execute('PRAGMA user_version').fetchone()[0]!=3:
        raise HistoricalError('HISTORICAL_TABLES_REQUIRE_8C_INSTANCE')


def hget(db,table,key):
    check(db,table)
    row=db.execute('SELECT payload FROM '+table+' WHERE id=?',(key,)).fetchone()
    return None if row is None else json.loads(row[0])


def hput(db,table,key,value):
    check(db,table)
    db.execute('INSERT INTO '+table+'(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(key,encode(value)))


def hrows(db,table):
    check(db,table)
    return [(r[0],json.loads(r[1])) for r in db.execute('SELECT id,payload FROM '+table+' ORDER BY rowid')]


class HistoricalStore(Store):
    settings_model=HistoricalSettings
    application_id=1397705786
    schema_version=3
    table_names=(*TABLES,*TABLE_NAMES)
    directory='historical-runs'

    def __init__(self,*args):
        super().__init__(*args)
        self._local=threading.local()

    @contextmanager
    def transaction(self):
        nested=getattr(self._local,'db',None)
        if nested is not None:
            yield nested
            return
        with super().transaction() as db:
            self._local.db=db
            try: yield db
            finally: self._local.db=None

    @classmethod
    def initialize(cls,workspace,run_id,settings,bundle,dataset,run):
        verify_seal(dataset);verify_seal(run)
        if dataset['status']!='COMPLETE' or run['dataset_digest']!=dataset['content_digest']:
            raise HistoricalError('DATASET_BINDING_INVALID')
        if run['config_digest']!=bundle.bundle_digest or run['instance_id']!=settings.instance_id:
            raise HistoricalError('RUN_SCOPE_INVALID')
        ExecutionModel.model_validate(run['execution_model'])
        store=super().create(workspace,run_id,settings,bundle)
        with store.transaction() as db:
            hput(db,'history_meta','dataset',dataset);hput(db,'history_meta','run',run)
            hput(db,'history_meta','funding',dict(net_cash='0',count=0))
            hput(db,'history_meta','statistics',dict(categories={},days={},candidates=0))
            hput(db,'history_cursor','cursor',dict(version='market-cursor/v1',dataset_digest=dataset['content_digest'],
                run_digest=run['content_digest'],event_count=0,by_file={},at_ms=run['warmup_range_ms'][0],
                window=[],last={},volume={},prefix='0'*64,finished=False))
        return store

    def run(self,db):
        value=verify_seal(hget(db,'history_meta','run'))
        if value['version']!='historical-run/v1': raise HistoricalError('UNKNOWN_RUN_VERSION')
        return value

    def model(self,db):
        return ExecutionModel.model_validate(self.run(db)['execution_model'])
