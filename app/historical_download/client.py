"""The only 8C network boundary: bounded anonymous GET to exact public sources."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, parse_qs

import httpx

SYMBOLS = ('SOLUSDT', 'BTCUSDT', 'ETHUSDT')
START_MS = 1785542400000  # 2026-08-01T00:00:00Z
END_MS = 1788220800000    # 2026-09-01T00:00:00Z (exclusive)
WARMUP_MS = START_MS - 300000
ARCHIVE_ROOT = 'https://data.binance.vision'
API_ROOT = 'https://fapi.binance.com'
MAX_ZIP_BYTES = 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 8 * 1024 * 1024 * 1024


class DownloadError(ValueError):
    pass


def archive_url(symbol, period, *, checksum=False):
    if symbol not in SYMBOLS or period not in ('2026-08', '2026-07-31'):
        raise DownloadError('UNAUTHORIZED_SYMBOL_OR_PERIOD')
    cadence = 'monthly' if period == '2026-08' else 'daily'
    return (ARCHIVE_ROOT + '/data/futures/um/' + cadence + '/aggTrades/' + symbol + '/' +
            symbol + '-aggTrades-' + period + '.zip' + ('.CHECKSUM' if checksum else ''))


def validate_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or p.fragment:
        raise DownloadError('HTTPS_ANONYMOUS_ONLY')
    if p.hostname == 'data.binance.vision':
        allowed = {archive_url(s, period, checksum=c) for s in SYMBOLS
                   for period in ('2026-08', '2026-07-31') for c in (False, True)}
        if url not in allowed: raise DownloadError('ARCHIVE_PATH_NOT_ALLOWLISTED')
    elif p.hostname == 'fapi.binance.com':
        q = parse_qs(p.query, keep_blank_values=True, strict_parsing=True)
        required = {'symbol', 'startTime', 'endTime', 'limit'}
        if p.path == '/fapi/v1/markPriceKlines': required.add('interval')
        elif p.path != '/fapi/v1/fundingRate': raise DownloadError('PUBLIC_ENDPOINT_NOT_ALLOWLISTED')
        if set(q) != required or any(len(v) != 1 for v in q.values()):
            raise DownloadError('QUERY_KEYS_INVALID')
        q = {k: v[0] for k, v in q.items()}
        if q['symbol'] not in SYMBOLS: raise DownloadError('SYMBOL_NOT_ALLOWLISTED')
        if not all(re.fullmatch(r'[0-9]+', q[k]) for k in ('startTime', 'endTime', 'limit')):
            raise DownloadError('INTEGER_RANGE_REQUIRED')
        if not WARMUP_MS <= int(q['startTime']) <= int(q['endTime']) < END_MS:
            raise DownloadError('TIME_RANGE_NOT_ALLOWLISTED')
        if not 1 <= int(q['limit']) <= 1000: raise DownloadError('RESPONSE_LIMIT_INVALID')
        if 'interval' in q and (q['interval'] != '1m' or int(q['endTime']) - int(q['startTime']) > 60000000):
            raise DownloadError('MARK_KLINE_WINDOW_INVALID')
    else:
        raise DownloadError('HOST_NOT_ALLOWLISTED')
    return url


class OfficialClient:
    """No caller headers/auth/cookies/proxy environment. Redirects revalidated."""
    def __init__(self, *, transport=None, pause=time.sleep):
        self.transport = transport
        self.pause = pause
        self.last_started = 0.0

    def get(self, url, *, max_bytes=4096, sink=None):
        if not 0 < max_bytes <= MAX_ZIP_BYTES: raise DownloadError('DOWNLOAD_LIMIT_INVALID')
        original = validate_url(url)
        deadline=time.monotonic()+1800
        for attempt in range(3):
            current = original
            try:
                for redirect in range(3):
                    self.pause(max(0, 0.3 - (time.monotonic() - self.last_started)))
                    self.last_started = time.monotonic()
                    # A fresh client for each hop prevents response Set-Cookie
                    # from becoming a credential on a later request.
                    with httpx.Client(transport=self.transport, trust_env=False,
                                      follow_redirects=False, timeout=20.0) as client:
                        with client.stream('GET', current, headers={'Accept-Encoding': 'identity',
                                'User-Agent': 'SOL-TradingAI-8C-official-archive/1'}) as response:
                            if response.status_code in (301, 302, 303, 307, 308):
                                if redirect == 2: raise DownloadError('REDIRECT_LIMIT')
                                current = validate_url(str(response.url.join(response.headers.get('location', ''))))
                                continue
                            if response.status_code in (429, 500, 502, 503, 504):
                                raise DownloadError('RETRYABLE_HTTP_' + str(response.status_code))
                            if response.status_code != 200:
                                raise DownloadError('HTTP_' + str(response.status_code) + ': ' + current)
                            length = response.headers.get('content-length')
                            if length and int(length) > max_bytes: raise DownloadError('DOWNLOAD_TOO_LARGE')
                            chunks = [] if sink is None else None
                            if sink is not None: sink.seek(0); sink.truncate(0)
                            total = 0; sha = hashlib.sha256()
                            for chunk in response.iter_bytes(1024 * 256):
                                if time.monotonic()>deadline: raise DownloadError('TOTAL_DOWNLOAD_DEADLINE')
                                total += len(chunk)
                                if total > max_bytes: raise DownloadError('DOWNLOAD_TOO_LARGE')
                                sha.update(chunk)
                                if sink is None: chunks.append(chunk)
                                else: sink.write(chunk)
                            if length and total != int(length): raise DownloadError('INCOMPLETE_DOWNLOAD')
                            return dict(url=current, bytes=total, sha256=sha.hexdigest(),
                                downloaded_at=datetime.now(timezone.utc).isoformat(),
                                body=b''.join(chunks) if sink is None else None)
                raise DownloadError('REDIRECT_LIMIT')
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, DownloadError) as error:
                retry = isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)) or str(error).startswith('RETRYABLE_HTTP_')
                if not retry or attempt == 2: raise DownloadError(str(error)) from error
                self.pause(2 ** attempt)


def parse_checksum(data, expected_name):
    match = re.fullmatch(r'([a-fA-F0-9]{64}) [ *]' + re.escape(expected_name) + r'\s*', data.decode('ascii'))
    if not match: raise DownloadError('OFFICIAL_CHECKSUM_FORMAT_INVALID')
    return match[1].lower()


def preflight(client):
    checks = []
    for symbol in SYMBOLS:
        for period in ('2026-07-31', '2026-08'):
            url = archive_url(symbol, period, checksum=True)
            try:
                result = client.get(url)
                checksum = parse_checksum(result.pop('body'), url.rsplit('/', 1)[1][:-9])
                checks.append(dict(symbol=symbol, period=period, status='CHECKSUM_AVAILABLE',
                    official_sha256=checksum, **result))
            except (DownloadError, UnicodeError) as error:
                checks.append(dict(symbol=symbol, period=period, url=url, status='UNAVAILABLE', error=str(error)))
    url = API_ROOT + '/fapi/v1/fundingRate?symbol=SOLUSDT&startTime=' + str(WARMUP_MS) + '&endTime=' + str(END_MS-1) + '&limit=1000'
    try:
        result = client.get(url, max_bytes=1024*1024)
        payload = json.loads(result.pop('body'))
        checks.append(dict(kind='fundingRate', status='RECORDS_AVAILABLE' if isinstance(payload,list) and payload else 'EMPTY_OR_INVALID',
            record_count=len(payload) if isinstance(payload,list) else None, **result))
    except (DownloadError, ValueError) as error:
        checks.append(dict(kind='fundingRate', url=url, status='UNAVAILABLE', error=str(error)))
    return dict(version='historical-preflight/v1', scope='OFFICIAL_PUBLIC_DOWNLOAD_ONLY',
        checked_at=datetime.now(timezone.utc).isoformat(), evaluation_start_ms=START_MS, evaluation_end_ms=END_MS,
        warmup_start_ms=WARMUP_MS, checks=checks, usable_dataset=False,
        all_sources_available=all(c['status'] in ('CHECKSUM_AVAILABLE','RECORDS_AVAILABLE') for c in checks))
