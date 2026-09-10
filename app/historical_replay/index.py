"""Verified local CSV streaming and seekable restart index, never a fetcher."""
import hashlib
import heapq
import json
import zipfile
from pathlib import Path
from app.utils.paths import check_owned, read_text_nofollow
from .data import load_manifest, immutable_json, seal, verify_seal, sha_file, HistoricalError, SYMBOLS, START, END, WARMUP, MAX_EXPANDED


def build_index(root):
    root=check_owned(root);dataset=load_manifest(root);target=check_owned(root/'stream-v1')
    if target.exists(): raise HistoricalError('INDEX_EXISTS_NEVER_OVERWRITE')
    target.mkdir();files=[]
    for source in dataset['files']:
        if not source['file'].endswith('.zip'): continue
        name=source['file'][:-4]+'.csv';out=target/name
        records=0;bytes_written=0;h=hashlib.sha256();first=last=None
        with zipfile.ZipFile(root/source['file']) as archive:
            members=archive.infolist()
            if len(members)!=1 or members[0].filename!=name or members[0].file_size>MAX_EXPANDED:
                raise HistoricalError('INDEX_ARCHIVE_MEMBER_INVALID')
            with archive.open(members[0]) as stream,out.open('xb') as dest:
                for line in stream:
                    if len(line)>4096: raise HistoricalError('CSV_LINE_BOUND_EXCEEDED')
                    cols=line.rstrip(b'\r\n').split(b',')
                    if cols[0]==b'agg_trade_id': continue
                    if len(cols)!=7: raise HistoricalError('CSV_SCHEMA_INVALID')
                    at=int(cols[5])
                    if not WARMUP<=at<END: continue
                    dest.write(line);h.update(line);bytes_written+=len(line);records+=1
                    first=at if first is None else first;last=at
        if records!=source['selected_record_count']:
            raise HistoricalError('INDEX_SELECTED_COUNT_MISMATCH')
        files.append(dict(file=name,source_file=source['file'],source_digest=source['sha256'],symbol=source['symbol'],
            sha256=h.hexdigest(),bytes=bytes_written,records=records,first_ms=first,last_ms=last,downloaded_at=source['downloaded_at']))
    if len(files)!=6: raise HistoricalError('INDEX_MISSING_REQUIRED_ARCHIVES')
    value=seal(dict(version='seekable-historical-csv/v1',dataset_digest=dataset['content_digest'],files=files))
    immutable_json(target/'index.json',value)
    return value


def verify_index(root,dataset):
    root=check_owned(root);idx=verify_seal(json.loads(read_text_nofollow(root/'stream-v1/index.json')))
    if idx['version']!='seekable-historical-csv/v1' or idx['dataset_digest']!=dataset['content_digest']:
        raise HistoricalError('INDEX_BINDING_CHANGED')
    if len(idx['files'])!=6: raise HistoricalError('INDEX_MISSING_REQUIRED_ARCHIVES')
    for f in idx['files']:
        if Path(f['file']).name!=f['file'] or sha_file(root/'stream-v1'/f['file'])!=f['sha256']:
            raise HistoricalError('INDEX_CONTENT_CHANGED')
        original=next((x for x in dataset['files'] if x['file']==f['source_file']),None)
        if original is None or original['sha256']!=f['source_digest']: raise HistoricalError('INDEX_SOURCE_CHANGED')
    return idx


class TradeStream:
    def __init__(self,root,metadata,delay_ms,offset=0):
        self.meta=metadata;self.stream=check_owned(Path(root)/'stream-v1'/metadata['file']).open('rb')
        self.stream.seek(offset);self.delay=delay_ms

    def next(self):
        line=self.stream.readline(4097)
        if not line: return None
        if len(line)>4096: raise HistoricalError('CSV_LINE_BOUND_EXCEEDED')
        c=line.rstrip(b'\r\n').split(b',')
        if len(c)!=7: raise HistoricalError('CSV_SCHEMA_INVALID')
        seq=int(c[0]);at=int(c[5]);m=self.meta
        return dict(kind='TRADE',symbol=m['symbol'],event_id=m['symbol']+':agg:'+str(seq),raw_sequence=seq,
            event_time_ms=at,available_at_ms=at+self.delay,price=c[1].decode('ascii'),quantity=c[2].decode('ascii'),
            source_file=m['source_file'],source_digest=m['source_digest'],downloaded_at=m['downloaded_at'],
            stream_file=m['file'],next_offset=self.stream.tell())

    def close(self): self.stream.close()


def merged_events(root,index,model,cursor):
    """One lookahead per source, next byte offset commits ONLY when consumed."""
    streams=[];heap=[]
    try:
        for i,m in enumerate(index['files']):
            stream=TradeStream(root,m,model.observation_delay_ms,cursor['by_file'].get(m['file'],0));streams.append(stream)
            e=stream.next()
            if e is not None: heapq.heappush(heap,(e['available_at_ms'],e['symbol'],e['raw_sequence'],i,e))
        while heap:
            _,_,_,i,e=heapq.heappop(heap);yield e
            new=streams[i].next()
            if new is not None: heapq.heappush(heap,(new['available_at_ms'],new['symbol'],new['raw_sequence'],i,new))
    finally:
        for stream in streams: stream.close()
