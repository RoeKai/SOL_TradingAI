"""Explicit frozen 8D file only, read-only SQLite, no Store or runtime construction."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from app.configuration.models import ConfigBundle
from app.configuration.compiler import verify_bundle
from app.offline_paper.storage import digest
from app.historical_replay.data import verify_seal
from app.historical_replay.models import ExecutionModel


@contextmanager
def frozen_source(path):
    path=Path(path).absolute()
    if path.name!='ledger.sqlite3' or path.parent.parent.name!='quantified-runs':
        raise ValueError('EXPLICIT_8D_FROZEN_SOURCE_REQUIRED')
    if any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file():
        raise ValueError('REGULAR_FROZEN_SOURCE_REQUIRED')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        if (db.execute('PRAGMA application_id').fetchone()[0],db.execute('PRAGMA user_version').fetchone()[0])!=(1397705787,4):
            raise ValueError('FROZEN_8D_IDENTITY_REQUIRED')
        # Belt and suspenders: reject SQL writes even if a future caller errs.
        denied={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ATTACH,sqlite3.SQLITE_DETACH}
        db.set_authorizer(lambda action,*args:sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
        db.execute('BEGIN')
        run=verify_seal(json.loads(db.execute("SELECT payload FROM history_meta WHERE id='run'").fetchone()[0]))
        identity=json.loads(db.execute("SELECT payload FROM identity WHERE id='identity'").fetchone()[0])
        bundle=ConfigBundle.model_validate_json(db.execute('SELECT payload FROM configurations WHERE id=?',(identity['bundle_digest'],)).fetchone()[0])
        if (verify_bundle(bundle).consistency!='PASS' or bundle.bundle_digest!=run['config_digest'] or
            run['instance_id']!=identity['settings']['instance_id'] or run['live_allowed'] is not False):
            raise ValueError('FROZEN_BINDING_MISMATCH')
        yield db,run,bundle,ExecutionModel.model_validate(run['execution_model'])
    finally:
        db.close()


def bodies(db):
    for row in db.execute('SELECT payload FROM history_approvals ORDER BY rowid'):
        envelope=json.loads(row[0]);body=envelope['body']
        if digest(body)!=envelope['body_digest']:raise ValueError('FROZEN_APPROVAL_BODY_CORRUPT')
        # This checksum binds historical evidence, NOT permission or authenticity.
        yield body
