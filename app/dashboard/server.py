"""Authenticated administration surface for this backend only."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
import hmac
import html
import inspect
import json
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.alerts.redaction import redact

ASSETS = Path(__file__).parent / "assets"
SESSION_SECONDS = 3600


def _field(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def create_app(runtime: Any, config: Any, dashboard_token: str = "", manage_lifespan: bool = True) -> FastAPI:
    dashboard = _field(config, "dashboard", config)
    require_auth = _field(dashboard, "require_auth", True)
    root_path = str(_field(dashboard, "root_path", "")).rstrip("/")
    if root_path not in {"", "/sol-ai"}:
        raise ValueError("Dashboard isolation requires standalone root or the dedicated /sol-ai prefix")
    title = str(_field(dashboard, "title", "SOL AI 交易控制台"))
    host = str(_field(dashboard, "host", "127.0.0.1"))
    if not require_auth and host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Unauthenticated dashboard is only allowed on a loopback host")
    if require_auth and len(dashboard_token) < 24:
        raise ValueError("Set SOL_DASHBOARD_TOKEN to at least 24 random characters in this module's .env")
    sessions: dict[str, float] = {}
    failures: dict[str, list[float]] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage_lifespan:
            await _resolve(runtime.start())
        try:
            yield
        finally:
            if manage_lifespan:
                await _resolve(runtime.stop())

    app = FastAPI(title=title, root_path=root_path, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    class SecurityHeaders(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any):
            response = await call_next(request)
            response.headers.update({
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                           "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                                           "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            })
            return response

    app.add_middleware(SecurityHeaders)
    app.mount("/assets", StaticFiles(directory=str(ASSETS)), name="assets")

    def log(event: str, **fields: Any) -> None:
        audit = getattr(runtime, "audit", None)
        if audit is not None:
            audit.emit(event, **redact(fields, (dashboard_token,)))

    def authenticated(request: Request) -> str:
        if not require_auth:
            return "loopback"
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer ") and hmac.compare_digest(authorization[7:], dashboard_token):
            return "bearer"
        session = request.cookies.get("sol_ai_session", "")
        if session and sessions.get(session, 0) > time.monotonic():
            return "session"
        raise HTTPException(status_code=401, detail="Authentication required")

    def same_origin(request: Request, *, must_exist: bool = False) -> None:
        origin = request.headers.get("origin")
        if not origin:
            if must_exist:
                raise HTTPException(status_code=403, detail="Origin header required")
            return
        parsed = urlsplit(origin)
        request_origin = urlsplit(str(request.url))
        if (parsed.scheme, parsed.netloc) != (request_origin.scheme, request_origin.netloc):
            raise HTTPException(status_code=403, detail="Cross-origin write forbidden")

    def write_authority(request: Request) -> str:
        auth = authenticated(request)
        same_origin(request, must_exist=(auth == "session"))
        return auth

    def page(filename: str) -> HTMLResponse:
        template = (ASSETS / filename).read_text(encoding="utf-8")
        return HTMLResponse(template.replace("__BASE__", html.escape(root_path, quote=True))
                            .replace("__TITLE__", html.escape(title)))

    @app.get("/health")
    async def health():
        # Liveness only: no balances, configuration, credentials, or trading-state claims.
        return {"service": "sol-ai-trading-system", "status": "alive"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return page("login.html")

    @app.post("/login")
    async def login(request: Request):
        same_origin(request, must_exist=True)
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        failures[ip] = [item for item in failures.get(ip, []) if item > now - 60]
        if len(failures[ip]) >= 5:
            raise HTTPException(status_code=429, detail="Too many attempts; wait one minute")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(status_code=413, detail="Request too large")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON") from None
        token = payload.get("token", "") if isinstance(payload, dict) else ""
        if require_auth and (not isinstance(token, str) or not hmac.compare_digest(token, dashboard_token)):
            failures[ip].append(now)
            log("dashboard_login_rejected")
            raise HTTPException(status_code=401, detail="Invalid dashboard token")
        failures.pop(ip, None)
        # Keep memory bounded; sessions expire after one hour and never contain keys.
        for expired in [key for key, expiry in sessions.items() if expiry <= now]:
            sessions.pop(expired, None)
        if len(sessions) >= 100:
            sessions.pop(next(iter(sessions)))
        if len(failures) > 1000:
            failures.clear()
        session = secrets.token_urlsafe(32)
        sessions[session] = now + SESSION_SECONDS
        response = JSONResponse({"ok": True})
        response.set_cookie("sol_ai_session", session, max_age=SESSION_SECONDS,
                            httponly=True, secure=request.url.scheme == "https",
                            samesite="strict", path=root_path or "/")
        log("dashboard_login_success")
        return response

    @app.post("/logout", dependencies=[Depends(write_authority)])
    async def logout(request: Request):
        sessions.pop(request.cookies.get("sol_ai_session", ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("sol_ai_session", path=root_path or "/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        try:
            authenticated(request)
        except HTTPException:
            return RedirectResponse(f"{root_path}/login", status_code=303)
        return page("index.html")

    @app.get("/api/snapshot", dependencies=[Depends(authenticated)])
    async def snapshot():
        return redact(await _resolve(runtime.snapshot()), (dashboard_token,))

    @app.post("/api/pause", dependencies=[Depends(write_authority)])
    async def pause():
        result = await _resolve(runtime.pause())
        log("dashboard_pause_requested")
        return {"ok": True, "result": redact(result)}

    @app.post("/api/resume", dependencies=[Depends(write_authority)])
    async def resume():
        # Runtime remains the sole authority: this cannot change dry_run, clear
        # daily loss/consecutive-loss counters, or resolve UNKNOWN orders.
        result = await _resolve(runtime.resume())
        log("dashboard_resume_requested")
        return {"ok": result is not False, "result": redact(result)}

    @app.get("/api/report", response_class=PlainTextResponse, dependencies=[Depends(authenticated)])
    async def report(day: str | None = None):
        if day is not None:
            try:
                date.fromisoformat(day)
            except ValueError:
                raise HTTPException(status_code=422, detail="Expected date YYYY-MM-DD") from None
        result = await _resolve(runtime.generate_report(day))
        if isinstance(result, Path):
            result = result.read_text(encoding="utf-8")
        return PlainTextResponse(str(redact(result, (dashboard_token,))), media_type="text/markdown")

    return app
