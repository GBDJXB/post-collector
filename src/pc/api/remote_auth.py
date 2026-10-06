"""独立远程认证入口；只返回身份说明，不挂载归档业务。"""

from __future__ import annotations

import html
import ipaddress
import secrets
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import URL, QueryParams

from pc import __version__
from pc.adapters import sqlite as db
from pc.services import accounts

COOKIE = "__Host-pc_remote_session"
LOGIN_COOKIE = "__Host-pc_remote_login"


def public_origin(value: str) -> str:
    try:
        parsed = URL(value)
        port = parsed.port
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or value != f"https://{parsed.netloc}" or any(c.isspace() for c in value)
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
    except ValueError:
        raise ValueError("--public-origin 须为 HTTPS origin，且不含凭据、路径、查询或 fragment") from None
    return value


class AuthState:
    """有界短期状态；此实例仅属于远程应用。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.forms: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.attempts: dict[str, deque] = {}
        self.total: deque = deque()
        self.clock = time.monotonic

    def _prune(self):
        now = self.clock()
        self.forms = {k: v for k, v in self.forms.items() if now - v["created"] < 300}
        self.sessions = {k: v for k, v in self.sessions.items()
                         if now - v["created"] < 28800 and now - v["seen"] < 1800}
        self.attempts = {k: v for k, v in self.attempts.items() if v and now - v[-1] < 60}

    def form(self):
        with self.lock:
            self._prune()
            if len(self.forms) >= 512:
                raise HTTPException(429, "登录请求过多，请稍后再试")
            ident, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.forms[ident] = {"created": self.clock(), "csrf": csrf}
            return ident, csrf

    def consume_form(self, ident: str, csrf: str):
        with self.lock:
            self._prune()
            form = self.forms.get(ident)
            if form is None or not secrets.compare_digest(form["csrf"].encode(), csrf.encode()):
                raise HTTPException(403, "登录凭据无效，请重新打开登录页")
            self.forms.pop(ident)

    def throttle(self, name: str):
        with self.lock:
            self._prune()
            now = self.clock()
            while self.total and now - self.total[0] >= 60:
                self.total.popleft()
            if len(self.total) >= 30:
                raise HTTPException(429, "登录请求过多，请稍后再试", headers={"Retry-After": "60"})
            self.total.append(now)
            attempts = self.attempts.setdefault(name, deque())
            while attempts and now - attempts[0] >= 60:
                attempts.popleft()
            if len(attempts) >= 5:
                raise HTTPException(429, "登录请求过多，请稍后再试", headers={"Retry-After": "60"})
            attempts.append(now)

    def issue(self, row):
        with self.lock:
            self._prune()
            if len(self.sessions) >= 1024:
                raise HTTPException(429, "会话数量达到上限，请稍后再试")
            ident, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            now = self.clock()
            self.sessions[ident] = {"created": now, "seen": now, "csrf": csrf,
                                    "user_id": row["user_id"], "version": accounts.fingerprint(row)}
            return ident, csrf

    def check(self, ident: str):
        with self.lock:
            self._prune()
            session = self.sessions.get(ident)
            if session is None:
                raise HTTPException(401, "会话已失效，请重新登录")
            session["seen"] = self.clock()
            return dict(session)

    def revoke(self, ident: str):
        with self.lock:
            self.sessions.pop(ident, None)


def create_remote_app(database: Path, origin: str) -> FastAPI:
    origin = public_origin(origin)
    host = URL(origin).netloc
    app = FastAPI(title="post-collector 登录", docs_url=None, redoc_url=None, openapi_url=None)
    state = AuthState()
    app.state.auth = state

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        try:
            local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local or request.headers.get("host") != host:
            response = JSONResponse({"detail": "请求入口无效"}, status_code=403)
        elif request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") != origin:
            response = JSONResponse({"detail": "请求必须来自登录页面"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        })
        return response

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(db.DatabaseError)
    @app.exception_handler(OSError)
    async def unavailable(request: Request, exc: Exception):
        return JSONResponse({"detail": "账号数据库不可用，请检查服务终端"}, status_code=503)

    def set_cookie(response, name, value, max_age):
        response.set_cookie(name, value, max_age=max_age, secure=True, httponly=True,
                            samesite="strict", path="/")

    def current(request: Request):
        ident = request.cookies.get(COOKIE, "")
        session = state.check(ident)
        with db.open_db(database, expect_exists=True) as conn:
            row = accounts.active_session_user(conn, session["user_id"], session["version"])
        if row is None:
            state.revoke(ident)
            raise HTTPException(401, "会话已失效，请重新登录")
        return row, session

    def payload(row, csrf):
        return {**accounts.identity(user_id=row["user_id"], name=row["username"], remote=True), "csrf": csrf}

    @app.get("/", response_class=HTMLResponse)
    def form(request: Request):
        # 同一浏览器刷新表单时替换旧凭据，避免占满短期状态。
        with state.lock:
            state.forms.pop(request.cookies.get(LOGIN_COOKIE, ""), None)
        ident, csrf = state.form()
        response = HTMLResponse(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>post-collector 登录</title><h1>受邀用户登录</h1>'
            '<form action="/api/v1/session" method="post">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
            '<label>用户名 <input name="username" maxlength="64" autocomplete="username" required></label><br>'
            '<label>密码 <input type="password" name="password" maxlength="128" autocomplete="current-password" required></label><br>'
            '<button type="submit">登录并查看身份 JSON</button></form>'
            '<p>当前入口仅提供登录与权限说明。</p></html>'
        )
        set_cookie(response, LOGIN_COOKIE, ident, 300)
        return response

    @app.post("/api/v1/session")
    async def login(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/x-www-form-urlencoded":
            raise HTTPException(415, "登录请求须为表单")
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > 8192:
                raise HTTPException(413, "登录请求超过 8 KiB")
            chunks.extend(chunk)
        try:
            encoded = chunks.decode("utf-8")
            parts = encoded.split("&")
            if len(parts) != 3 or any("=" not in part for part in parts):
                raise ValueError
            values = QueryParams(encoded)
            if set(values) != {"username", "password", "csrf"} or any(len(values.getlist(key)) != 1 for key in values):
                raise ValueError
            name, password, csrf = (values[key] for key in ("username", "password", "csrf"))
            if len(name) > 64 or len(password) > 128 or len(csrf) > 128:
                raise ValueError
        except (ValueError, UnicodeError):
            raise HTTPException(400, "登录表单无效") from None
        state.consume_form(request.cookies.get(LOGIN_COOKIE, ""), csrf)
        state.throttle(name.strip().lower())

        def verify():
            with db.open_db(database, expect_exists=True) as conn:
                return accounts.authenticate(conn, name, password)

        row = await run_in_threadpool(verify)
        if row is None:
            raise HTTPException(401, "用户名或密码无效")
        state.revoke(request.cookies.get(COOKIE, ""))
        ident, csrf = state.issue(row)
        response = JSONResponse(payload(row, csrf))
        set_cookie(response, COOKIE, ident, 28800)
        response.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/v1/session")
    def session(request: Request):
        row, value = current(request)
        return payload(row, value["csrf"])

    @app.post("/api/v1/logout")
    def logout(request: Request):
        row, session = current(request)
        if not secrets.compare_digest(session["csrf"].encode(), request.headers.get("x-csrf-token", "").encode()):
            raise HTTPException(403, "操作凭据无效")
        with db.open_db(database, expect_exists=True) as conn, db.transaction(conn):
            accounts.audit(conn, "auth.logout", row["user_id"])
        state.revoke(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__}

    return app
