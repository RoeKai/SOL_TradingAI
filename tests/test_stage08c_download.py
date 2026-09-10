"""Offline HTTP transport mocks. No test fetches a real market endpoint."""
import io
import hashlib
import httpx
import pytest
from app.historical_download.client import OfficialClient, validate_url, archive_url, DownloadError, parse_checksum, START_MS, END_MS
from app.historical_download.cli import schedule_complete


@pytest.mark.parametrize('url',[
    'http://data.binance.vision/x','https://evil.example/data','https://fapi.binance.com/fapi/v1/order',
    'https://fapi.binance.com/fapi/v2/account','https://data.binance.vision/../secrets',
    'https://key:secret@data.binance.vision/x','https://data.binance.vision:444/x',
    archive_url('SOLUSDT','2026-08')+'?key=x',archive_url('SOLUSDT','2026-08')+'#x',
    'https://fapi.binance.com/fapi/v1/fundingRate?symbol=SOLUSDT&startTime=0&endTime=1&limit=1',
    'https://fapi.binance.com/fapi/v1/fundingRate?symbol=SOLUSDT&symbol=BTCUSDT&startTime=1785542400000&endTime=1785542400001&limit=1',
])
def test_exact_endpoint_allowlist(url):
    with pytest.raises(ValueError): validate_url(url)


@pytest.mark.parametrize('status',[301,302,303,307,308])
def test_redirect_cannot_expand_authority(status):
    seen=[]
    def handler(req):
        seen.append(req.url);return httpx.Response(status,headers={'Location':'https://evil.example/'})
    client=OfficialClient(transport=httpx.MockTransport(handler),pause=lambda _:None)
    with pytest.raises(DownloadError,match='HOST'): client.get(archive_url('SOLUSDT','2026-08'))
    assert len(seen)==1


def test_no_auth_cookies_or_proxy_inheritance(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://secret@127.0.0.1:1')
    monkeypatch.setenv('BINANCE_API_KEY','DO_NOT_SEND')
    seen=[]
    def handler(req):
        seen.append(req)
        if len(seen)==1: return httpx.Response(302,headers={'Location':archive_url('BTCUSDT','2026-08'),'Set-Cookie':'account=x'})
        return httpx.Response(200,content=b'ok')
    result=OfficialClient(transport=httpx.MockTransport(handler),pause=lambda _:None).get(archive_url('SOLUSDT','2026-08'))
    assert result['body']==b'ok' and len(seen)==2
    for req in seen:
        assert req.method=='GET' and not any(k in req.headers for k in ('cookie','authorization','x-mbx-apikey'))


def test_bounded_retry_and_body():
    seen=[]
    def handler(req): seen.append(req);return httpx.Response(429)
    with pytest.raises(DownloadError): OfficialClient(transport=httpx.MockTransport(handler),pause=lambda _:None).get(archive_url('SOLUSDT','2026-08'))
    assert len(seen)==3
    with pytest.raises(DownloadError,match='LARGE'):
        OfficialClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=b'x'*100)),pause=lambda _:None).get(archive_url('SOLUSDT','2026-08'),max_bytes=10)


def test_stream_checksum_and_mismatch_format():
    body=b'test public data';sink=io.BytesIO()
    value=OfficialClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=body)),pause=lambda _:None).get(archive_url('SOLUSDT','2026-08'),sink=sink)
    sha=hashlib.sha256(body).hexdigest()
    assert value['sha256']==sha and sink.getvalue()==body
    assert parse_checksum((sha+'  file.zip\n').encode(),'file.zip')==sha
    with pytest.raises(DownloadError): parse_checksum((sha+'  wrong.zip').encode(),'file.zip')


def test_funding_slots_do_not_round_actual_times():
    records=[{'event_time_ms':at+26} for at in range(START_MS,END_MS,28800000)]
    assert schedule_complete(records) and records[0]['event_time_ms']==START_MS+26
    assert not schedule_complete(records[:-1])
