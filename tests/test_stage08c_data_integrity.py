"""Synthetic damaged archives; no third-party or live data request."""
import hashlib
import json
import zipfile
import pytest
from app.historical_replay.data import archive_rows,load_manifest,seal,START,END,WARMUP,HistoricalError
from app.historical_download import cli as downloader


@pytest.mark.parametrize('problem',['bad_price','backwards','duplicate_conflict','gap'])
def test_archive_integrity_not_silently_repaired(tmp_path,problem):
    first=f'1,100,1,1,1,{START},true\n'
    second={'bad_price':f'2,NaN,1,2,2,{START+1},true\n',
        'backwards':f'2,100,1,2,2,{START-1},true\n',
        'duplicate_conflict':f'1,101,1,1,1,{START},true\n',
        'gap':f'3,100,1,3,3,{START+1},true\n'}[problem]
    path=tmp_path/'sample.zip'
    with zipfile.ZipFile(path,'w') as z:z.writestr('sample.csv',first+second)
    def read():return list(archive_rows(path,expected_csv='sample.csv',symbol='SOLUSDT',start_ms=WARMUP,end_ms=END))
    if problem=='gap':assert read()[1]['sequence_gap']==1
    else:
        with pytest.raises(HistoricalError):read()


def test_exact_duplicate_is_marked_without_mutating_facts(tmp_path):
    line=f'1,100,1,1,1,{START},true\n';path=tmp_path/'sample.zip'
    with zipfile.ZipFile(path,'w') as z:z.writestr('sample.csv',line*2)
    rows=list(archive_rows(path,expected_csv='sample.csv',symbol='SOLUSDT',start_ms=START,end_ms=END))
    assert rows[1].pop('duplicate') is True and rows[0]==rows[1]


def test_missing_shard_is_not_a_complete_dataset(tmp_path):
    value=seal(dict(version='historical-dataset/v1',status='COMPLETE',market='BINANCE_USD_M_LINEAR_PERPETUAL',
        evaluation_range_ms=[START,END],warmup_range_ms=[WARMUP,START],files=[]))
    (tmp_path/'dataset.json').write_text(json.dumps(value))
    with pytest.raises(HistoricalError,match='REQUIRED_FILES'):load_manifest(tmp_path)


def test_checksum_failure_preserves_failure_without_usable_manifest(tmp_path,monkeypatch):
    # Only downloader transport is replaced, never Admission/score/risk.
    class Client:
        def get(self,url,**kwargs):
            kwargs['sink'].write(b'damaged archive')
            return dict(sha256=hashlib.sha256(b'damaged archive').hexdigest(),bytes=15)
    monkeypatch.setattr(downloader,'OfficialClient',Client)
    monkeypatch.setattr(downloader,'preflight',lambda _:dict(all_sources_available=True,checks=[
        dict(symbol='SOLUSDT',period='2026-08',official_sha256='0'*64,url='OFFICIAL_TEST_CHECKSUM')]))
    target=tmp_path/'download';value=downloader.download(target)
    assert value['usable_dataset'] is False and 'CHECKSUM_MISMATCH' in value['error']
    assert (target/'failure.json').exists() and not (target/'dataset.json').exists()
    assert (target/'SOLUSDT-aggTrades-2026-08.zip.partial').exists()
