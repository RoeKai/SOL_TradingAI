"""Explicit public-only download entry. No replay/Broker/application import."""
import argparse
import json
import os
from pathlib import Path
from .client import OfficialClient, preflight, archive_url, parse_checksum, API_ROOT, WARMUP_MS, END_MS, MAX_ZIP_BYTES
from app.utils.paths import check_owned, read_text_nofollow
from app.historical_replay.data import inspect_archive, funding_records, immutable_json, seal, verify_seal, sha_file, START, END, WARMUP


def schedule_complete(records):
    # Completeness is by settlement slot, NOT a rewrite of original timestamps.
    # Official fundingTime has millisecond offsets. Retain every exact time.
    return (len(records)==93 and [r['event_time_ms']//28800000 for r in records]
            == list(range(START//28800000,END//28800000)))


def finish(root,client,files):
    url=API_ROOT+'/fapi/v1/fundingRate?symbol=SOLUSDT&startTime='+str(WARMUP_MS)+'&endTime='+str(END_MS-1)+'&limit=1000'
    received=client.get(url,max_bytes=1024**2);payload=json.loads(received['body']);records=list(funding_records(payload))
    if len(payload)==1000: raise ValueError('FUNDING_PAGINATION_REQUIRED')
    if not schedule_complete(records): raise ValueError('FUNDING_SLOT_COVERAGE_INCOMPLETE_OR_UNSUPPORTED')
    with (root/'SOLUSDT-funding.json').open('xb') as stream: stream.write(received['body'])
    files=[*files,dict(file='SOLUSDT-funding.json',kind='fundingRate',symbol='SOLUSDT',
        official_checksum='NOT_PUBLISHED_FOR_REST_JSON',record_count=len(records),
        **{k:v for k,v in received.items() if k!='body'})]
    manifest=seal(dict(version='historical-dataset/v1',status='COMPLETE',market='BINANCE_USD_M_LINEAR_PERPETUAL',
        symbols=['SOLUSDT','BTCUSDT','ETHUSDT'],evaluation_range_ms=[START,END],warmup_range_ms=[WARMUP,START],
        files=files,gaps=[],revisions='funding-slot-completeness/v2; original failure record retained; no event timestamps rewritten',
        funding_schedule='one observed event per eight-hour slot; actual millisecond times retained; not an ex-ante funding schedule oracle',
        historical_rules='NOT_PROVIDED; execution requires separately declared rule assumptions'))
    immutable_json(root/'dataset.json',manifest)
    return manifest


def finalize(directory):
    root=check_owned(directory)
    failure=verify_seal(json.loads(read_text_nofollow(root/'failure.json')))
    files=failure['completed_files']
    if len(files)!=6: raise ValueError('ONLY_COMPLETE_VERIFIED_ARCHIVE_SET_CAN_FINALIZE')
    expected={archive_url(s,p) for s in ('SOLUSDT','BTCUSDT','ETHUSDT') for p in ('2026-07-31','2026-08')}
    if {f['url'] for f in files}!=expected: raise ValueError('ARCHIVE_SET_MISMATCH')
    for f in files:
        if Path(f['file']).name!=f['file'] or sha_file(root/f['file'])!=f['official_sha256'] or f['sha256']!=f['official_sha256']:
            raise ValueError('VERIFIED_ARCHIVE_CHANGED')
    return finish(root,OfficialClient(),files)


def download(directory):
    root=check_owned(directory)
    root.mkdir(mode=0o700,parents=False,exist_ok=False)
    client=OfficialClient();check=preflight(client)
    immutable_json(root/'preflight.json',check)
    if not check['all_sources_available']: return check
    files=[]
    try:
        for item in check['checks']:
            if 'symbol' not in item: continue
            url=archive_url(item['symbol'],item['period']);name=url.rsplit('/',1)[1]
            partial=root/(name+'.partial')
            with partial.open('xb+') as stream:
                received=client.get(url,max_bytes=MAX_ZIP_BYTES,sink=stream)
                stream.flush();os.fsync(stream.fileno())
            if received['sha256']!=item['official_sha256']: raise ValueError('OFFICIAL_CHECKSUM_MISMATCH: '+name)
            details=inspect_archive(partial,item['symbol'],item['period'])
            if details['sequence_gaps']: raise ValueError('ARCHIVE_SEQUENCE_GAPS: '+name)
            partial.rename(root/name)
            files.append(dict(file=name,symbol=item['symbol'],period=item['period'],kind='aggTrades',
                official_sha256=item['official_sha256'],checksum_url=item['url'],
                **{k:v for k,v in received.items() if k!='body'},**details))
            print(json.dumps({'verified_file':name,'bytes':received['bytes'],'records':details['record_count']}),flush=True)
        return finish(root,client,files)
    except Exception as error:
        failure=seal(dict(version='historical-download-failure/v1',usable_dataset=False,
            error=type(error).__name__+': '+str(error),completed_files=files))
        immutable_json(root/'failure.json',failure)
        return failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preflight','download','finalize'])
    parser.add_argument('--directory',type=Path)
    args=parser.parse_args()
    if args.command!='preflight' and args.directory is None: parser.error('--directory is required')
    report = preflight(OfficialClient()) if args.command=='preflight' else download(args.directory) if args.command=='download' else finalize(args.directory)
    print(json.dumps(report, indent=2))
    return 0 if report.get('all_sources_available') or report.get('status')=='COMPLETE' else 2


if __name__ == '__main__':
    raise SystemExit(main())
