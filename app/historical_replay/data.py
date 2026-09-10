"""Bounded archive validation and streaming; no network or account imports."""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

from app.utils.paths import check_owned, read_text_nofollow

PARSER = 'binance-um-aggtrades-ms/v1'
SYMBOLS = ('SOLUSDT', 'BTCUSDT', 'ETHUSDT')
START = 1785542400000
END = 1788220800000
WARMUP = START - 300000
MAX_EXPANDED = 8 * 1024**3


class HistoricalError(ValueError):
    pass


def sha_file(path):
    check_owned(path)
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''): h.update(block)
    return h.hexdigest()


def content_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def seal(value):
    return dict(value, content_digest=content_digest(value))


def verify_seal(value):
    body = {k: v for k, v in value.items() if k != 'content_digest'}
    if value.get('content_digest') != content_digest(body): raise HistoricalError('MANIFEST_CONTENT_CHANGED')
    return value


def immutable_json(path, value):
    path = check_owned(path)
    with path.open('x', encoding='utf8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def archive_rows(path, *, expected_csv, symbol, start_ms, end_ms):
    """Read only the named single CSV member; never extract an arbitrary path."""
    check_owned(path)
    csv.field_size_limit(4096)
    with zipfile.ZipFile(path) as archive:
        info_list = archive.infolist()
        if len(info_list) != 1: raise HistoricalError('ZIP_MEMBER_COUNT_INVALID')
        info = info_list[0]
        if (info.filename != expected_csv or '/' in info.filename or '\\' in info.filename or
            info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
            raise HistoricalError('ZIP_UNSAFE_MEMBER')
        if info.file_size > MAX_EXPANDED or info.file_size > max(1, info.compress_size) * 200:
            raise HistoricalError('ZIP_EXPANSION_LIMIT')
        with archive.open(info) as raw, io.TextIOWrapper(raw, encoding='utf-8', newline='') as stream:
            reader = csv.reader(stream)
            previous = None
            for line, row in enumerate(reader, 1):
                if line == 1 and row and row[0] in ('agg_trade_id', 'Aggregate tradeId'):
                    if row not in (['agg_trade_id','price','quantity','first_trade_id','last_trade_id','transact_time','is_buyer_maker'],
                                    ['Aggregate tradeId','Price','Quantity','First tradeId','Last tradeId','Timestamp','Was the buyer the maker']):
                        raise HistoricalError('ARCHIVE_HEADER_UNKNOWN')
                    continue
                if len(row) != 7: raise HistoricalError('ARCHIVE_COLUMN_COUNT_INVALID')
                aid, first, last, at = (int(row[i]) for i in (0,3,4,5))
                price, qty = Decimal(row[1]), Decimal(row[2])
                if not price.is_finite() or not qty.is_finite() or price <= 0 or qty <= 0:
                    raise HistoricalError('ARCHIVE_NONPOSITIVE_OR_NONFINITE')
                if not start_ms <= at < end_ms: raise HistoricalError('ARCHIVE_RANGE_OR_MILLISECOND_UNIT_INVALID')
                if aid < 0 or first < 0 or first > last or row[6].lower() not in ('true','false'):
                    raise HistoricalError('ARCHIVE_SEQUENCE_INVALID')
                event = dict(kind='TRADE', provenance='OFFICIAL_HISTORICAL_OBSERVATION', symbol=symbol,
                    event_id=symbol+':agg:'+str(aid), raw_sequence=aid, event_time_ms=at,
                    price=str(price), quantity=str(qty), first_trade_id=first, last_trade_id=last,
                    buyer_is_maker=row[6].lower()=='true')
                if previous is not None:
                    if aid == previous['raw_sequence']:
                        if event != previous: raise HistoricalError('DUPLICATE_ID_CONTENT_CONFLICT')
                        event = dict(event, duplicate=True)
                    elif aid < previous['raw_sequence'] or at < previous['event_time_ms']:
                        raise HistoricalError('ARCHIVE_TIME_OR_ID_BACKWARDS')
                    elif aid != previous['raw_sequence'] + 1:
                        event = dict(event, sequence_gap=aid-previous['raw_sequence']-1)
                previous = {k:v for k,v in event.items() if k not in ('duplicate','sequence_gap')}
                yield event


def archive_period(period):
    start = datetime.strptime(period, '%Y-%m' if len(period)==7 else '%Y-%m-%d').replace(tzinfo=timezone.utc)
    seconds = 31*86400 if period=='2026-08' else 86400
    return int(start.timestamp()*1000), int(start.timestamp()*1000)+seconds*1000


def inspect_archive(path, symbol, period):
    first = last = None; count = duplicates = gaps = selected = 0
    begin, end = archive_period(period)
    name = symbol+'-aggTrades-'+period+'.csv'
    for event in archive_rows(path, expected_csv=name, symbol=symbol, start_ms=begin, end_ms=end):
        duplicates += bool(event.get('duplicate')); gaps += event.get('sequence_gap',0)
        if event.get('duplicate'): continue
        count += 1; first = event if first is None else first; last = event
        selected += WARMUP <= event['event_time_ms'] < END
    if not count: raise HistoricalError('EMPTY_ARCHIVE')
    return dict(parser_version=PARSER, time_unit='milliseconds_utc', record_count=count,
        selected_record_count=selected, duplicate_records=duplicates, sequence_gaps=gaps,
        first_event=first, last_event=last)


def funding_records(payload):
    if not isinstance(payload,list) or not payload: raise HistoricalError('HISTORICAL_FUNDING_MISSING')
    previous = -1
    for row in payload:
        if set(row) - {'symbol','fundingTime','fundingRate','markPrice','rateType'}:
            raise HistoricalError('FUNDING_SCHEMA_UNKNOWN')
        at = row.get('fundingTime');rate = Decimal(row['fundingRate']);mark = Decimal(row['markPrice'])
        if row.get('symbol') != 'SOLUSDT' or type(at) is not int or not WARMUP <= at < END:
            raise HistoricalError('FUNDING_RANGE_OR_SYMBOL_INVALID')
        if not rate.is_finite() or not mark.is_finite() or mark<=0 or abs(rate)>1:
            raise HistoricalError('FUNDING_RATE_OR_MARK_INVALID')
        if at<=previous or row.get('rateType','Regular')!='Regular': raise HistoricalError('FUNDING_ORDER_OR_TYPE_UNSUPPORTED')
        previous = at
        yield dict(kind='FUNDING', provenance='OFFICIAL_HISTORICAL_OBSERVATION', symbol='SOLUSDT',
            event_id='SOLUSDT:funding:'+str(at), event_time_ms=at, rate=str(rate), mark_price=str(mark),
            price_basis='official_fundingRate_response.markPrice')


def load_manifest(root):
    root = check_owned(root)
    value = verify_seal(json.loads(read_text_nofollow(root/'dataset.json')))
    if value['version']!='historical-dataset/v1' or value['status']!='COMPLETE':
        raise HistoricalError('DATASET_NOT_COMPLETE')
    if value['evaluation_range_ms'] != [START,END] or value['warmup_range_ms'] != [WARMUP,START]:
        raise HistoricalError('EXPERIMENT_RANGE_CHANGED')
    expected={s+'-aggTrades-'+period+'.zip' for s in SYMBOLS for period in ('2026-07-31','2026-08')}|{'SOLUSDT-funding.json'}
    if value['market']!='BINANCE_USD_M_LINEAR_PERPETUAL' or {f['file'] for f in value['files']}!=expected or len(value['files'])!=7:
        raise HistoricalError('DATASET_REQUIRED_FILES_MISSING_OR_WRONG_MARKET')
    for item in value['files']:
        if Path(item['file']).name != item['file']: raise HistoricalError('DATASET_PATH_INVALID')
        if item['file'].endswith('.zip') and (item.get('official_sha256')!=item['sha256'] or item.get('parser_version')!=PARSER or item.get('sequence_gaps')!=0):
            raise HistoricalError('DATASET_OFFICIAL_CHECKSUM_OR_SEQUENCE_INVALID')
        if sha_file(root/item['file']) != item['sha256']: raise HistoricalError('DATASET_FILE_HASH_CHANGED')
    return value
