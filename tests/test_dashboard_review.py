import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.alerts import AlertManager
from app.alerts.redaction import redact
from app.dashboard import create_app
from app.review import DailyReview


class Audit:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append({"event": event, **fields})


class Runtime:
    def __init__(self):
        self.audit = Audit()
        self.started = False
        self.paused = False
        self.halted = True

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    def snapshot(self):
        return {"dry_run": True, "risk": {"halted": self.halted}, "api_key": "never-return"}

    async def pause(self):
        self.paused = True

    async def resume(self):
        return False  # Loss/UNKNOWN state cannot be overridden by dashboard.

    def generate_report(self, day):
        return f"# Review {day or 'today'}"


def config(auth=True, host="127.0.0.1", root_path=""):
    return SimpleNamespace(dashboard=SimpleNamespace(require_auth=auth, host=host, root_path=root_path, title="SOL AI"))


TOKEN = "test-dashboard-token-at-least-32-chars"


def test_auth_health_and_sensitive_redaction():
    runtime = Runtime()
    with TestClient(create_app(runtime, config(), TOKEN)) as client:
        assert runtime.started
        assert client.get("/health").json() == {"service": "sol-ai-trading-system", "status": "alive"}
        assert client.get("/api/snapshot").status_code == 401
        assert client.get("/docs").status_code == 404
        result = client.get("/api/snapshot", headers={"Authorization": f"Bearer {TOKEN}"})
        assert result.status_code == 200
        assert result.json()["api_key"] == "[REDACTED]"
        assert "never-return" not in result.text
        assert result.headers["Cache-Control"] == "no-store"
    assert not runtime.started


def test_session_cookie_and_csrf_pause_cannot_clear_halt():
    runtime = Runtime()
    with TestClient(create_app(runtime, config(), TOKEN)) as client:
        login = client.post("/login", json={"token": TOKEN}, headers={"Origin": "http://testserver"})
        assert login.status_code == 200
        assert "httponly" in login.headers["set-cookie"].lower()
        assert "samesite=strict" in login.headers["set-cookie"].lower()
        assert TOKEN not in login.headers["set-cookie"]
        assert client.get("/").status_code == 200
        assert client.post("/api/pause").status_code == 403
        assert client.post("/api/pause", headers={"Origin": "https://evil.example"}).status_code == 403
        assert not runtime.paused
        assert client.post("/api/pause", headers={"Origin": "http://testserver"}).status_code == 200
        assert runtime.paused
        assert client.post("/api/resume", headers={"Origin": "http://testserver"}).json()["ok"] is False
        assert runtime.halted
        assert client.post("/logout", headers={"Origin": "http://testserver"}).status_code == 200
        assert client.get("/api/snapshot").status_code == 401


def test_config_rejects_public_no_auth_and_short_token():
    with pytest.raises(ValueError):
        create_app(Runtime(), config(False, "0.0.0.0"))
    with pytest.raises(ValueError):
        create_app(Runtime(), config(), "short")
    with TestClient(create_app(Runtime(), config(False))) as client:
        assert client.get("/api/snapshot").status_code == 200


def test_login_rate_limit_and_origin():
    with TestClient(create_app(Runtime(), config(), TOKEN)) as client:
        assert client.post("/login", json={"token": TOKEN}).status_code == 403
        for _ in range(5):
            assert client.post("/login", json={"token": "bad"}, headers={"Origin": "http://testserver"}).status_code == 401
        assert client.post("/login", json={"token": TOKEN}, headers={"Origin": "http://testserver"}).status_code == 429


def test_login_bounds_streamed_body_and_rejects_bad_json():
    with TestClient(create_app(Runtime(), config(), TOKEN)) as client:
        headers = {"Origin": "http://testserver", "Content-Type": "application/json"}
        assert client.post("/login", content=iter([b"x" * 2048, b"x" * 4096]), headers=headers).status_code == 413
        assert client.post("/login", content=b"not-json", headers=headers).status_code == 400


def test_report_validation_assets_and_no_live_toggle():
    with TestClient(create_app(Runtime(), config(), TOKEN)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert client.get("/api/report?day=2026-09-09", headers=headers).text == "# Review 2026-09-09"
        assert client.get("/api/report?day=../../.env", headers=headers).status_code == 422
        assert client.post("/api/live", headers=headers).status_code == 404
        assert client.get("/assets/dashboard.js").status_code == 200
        assert "innerHTML" not in client.get("/assets/dashboard.js").text


def test_daily_review_timezone_net_metrics_and_atomic_write(tmp_path):
    review = DailyReview(tmp_path)
    trades = [
        {"strategy": "panic", "net_pnl": 5, "fees": 9, "opened_at": "2026-09-08T16:00:00Z", "closed_at": "2026-09-08T16:05:00Z"},
        {"strategy": "panic", "net_pnl": -2, "opened_at": "2026-09-08T16:05:00Z", "closed_at": "2026-09-08T16:15:00Z"},
        {"strategy": "panic", "net_pnl": -1, "opened_at": "2026-09-08T16:15:00Z", "closed_at": "2026-09-08T16:30:00Z"},
        {"strategy": "outside", "net_pnl": 100, "closed_at": "2026-09-08T15:59:00Z"},
    ]
    result = review.generate(trades, "2026-09-09")
    assert "完成交易：3 笔" in result
    assert "净盈亏：+2.0000 USDT" in result
    assert "33.3%" in result and "3.0000" in result and "10.0 分钟" in result
    assert "outside" not in result
    path = review.write(trades, "2026-09-09")
    assert path.read_text() == result
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError):
        review.write(trades, "../../unsafe")


def test_review_numeric_time_empty_and_unknown():
    timestamp = datetime(2026, 9, 9, tzinfo=timezone.utc).timestamp()
    report = DailyReview().generate([{"strategy": "trend", "net_pnl": 2, "closed_at": timestamp, "holding_seconds": 120}], "2026-09-09")
    assert "完成交易：1 笔" in report and "2.0 分钟" in report
    assert "无已平仓交易" in DailyReview().generate([], "2026-09-09")


def test_telegram_success_redaction_and_no_disabled_network():
    requests = []
    async def scenario():
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            audit = Audit()
            manager = AlertManager(enabled=True, bot_token="123456789:very-private-token", chat_id="-100123", client=client, audit=audit)
            assert await manager.send("entry", {"symbol": "SOLUSDT", "api_key": "secret-value"})
            assert b"secret-value" not in requests[0].content
            assert "123456789:very-private-token" not in str(audit.events)
            disabled = AlertManager(enabled=False, client=client)
            assert await disabled.send("entry", {}) is False
    asyncio.run(scenario())
    assert len(requests) == 1


def test_telegram_retry_limit_drops_long_retry_and_queue_is_bounded():
    async def scenario():
        audit = Audit()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"parameters": {"retry_after": 60}}))) as client:
            manager = AlertManager(enabled=True, bot_token="secret", chat_id="chat", audit=audit, client=client, queue_size=1)
            assert await manager.send("api_error", {}) is False
            manager.notify("entry", {})
            manager.notify("close", {})
            assert any(item["event"] == "telegram_queue_full" for item in audit.events)
            assert any(item["event"] == "telegram_retry_deferred" for item in audit.events)
    asyncio.run(scenario())


def test_redaction_handles_nested_url_and_exception_text():
    result = redact({"nested": [{"authorization": "Bearer secret"}], "error": "url?signature=abc&token=xyz", "message": "literal-secret"}, ("literal-secret",))
    assert "secret" not in str(result)
    assert "abc" not in str(result) and "xyz" not in str(result)


def test_dashboard_tokens_and_sessions_are_instance_local(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "old-copy-dashboard-token-sentinel")
    first = create_app(Runtime(), config(), TOKEN, manage_lifespan=False)
    second_token = "second-sol-instance-token-at-least32chars"
    second = create_app(Runtime(), config(), second_token, manage_lifespan=False)
    with TestClient(first) as one, TestClient(second) as two:
        assert one.get("/api/snapshot", headers={"Authorization": "Bearer old-copy-dashboard-token-sentinel"}).status_code == 401
        assert two.get("/api/snapshot", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401
        assert one.post("/login", json={"token": TOKEN}, headers={"Origin": "http://testserver"}).status_code == 200
        session = one.cookies.get("sol_ai_session")
        assert two.get("/api/snapshot", headers={"Cookie": f"sol_ai_session={session}"}).status_code == 401
        assert two.get("/api/snapshot", headers={"Authorization": f"Bearer {second_token}"}).status_code == 200


def test_sol_route_cookie_is_not_shared_with_original_root():
    with TestClient(create_app(Runtime(), config(root_path="/sol-ai"), TOKEN, manage_lifespan=False),
                    base_url="https://testserver") as client:
        login = client.post("/sol-ai/login", json={"token": TOKEN}, headers={"Origin": "https://testserver"})
        assert login.status_code == 200
        cookie = login.headers["set-cookie"].lower()
        assert "path=/sol-ai" in cookie and "secure" in cookie
        assert client.get("/sol-ai/api/snapshot").status_code == 200
        # The cookie is scoped away from the old site's root routes.
        assert "sol_ai_session" not in client.build_request("GET", "/api/snapshot").headers.get("cookie", "")
        assert client.get("/sol-ai/api/execute-old-order").status_code == 404
        assert client.post("/sol-ai/api/execute-old-order").status_code == 404


@pytest.mark.parametrize("prefix", ["https://old-copy.invalid", "//old-copy.invalid", "/api", "/../copy"])
def test_dashboard_prefix_cannot_redirect_browser_to_old_backend(prefix):
    with pytest.raises(ValueError, match="dedicated /sol-ai"):
        create_app(Runtime(), config(root_path=prefix), TOKEN, manage_lifespan=False)


def test_telegram_client_never_inherits_host_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://old-copy-proxy.invalid:3002")
    monkeypatch.setenv("HTTPS_PROXY", "http://old-copy-proxy.invalid:3002")
    created = []
    original_client = httpx.AsyncClient

    def factory(**kwargs):
        created.append(kwargs)
        return original_client(**kwargs, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})))

    monkeypatch.setattr("app.alerts.telegram.httpx.AsyncClient", factory)

    async def scenario():
        manager = AlertManager(enabled=True, bot_token="FAKE_NEW_MODULE_TOKEN", chat_id="FAKE_CHAT")
        assert await manager.send("entry", {"symbol": "SOLUSDT"})
        await manager.stop()

    asyncio.run(scenario())
    assert created[0]["trust_env"] is False
