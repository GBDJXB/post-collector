"""本机 Web 应用；临时 root 会话保护业务接口。"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from pc import __version__
from pc.adapters import sqlite as db
from pc.adapters import view_files
from pc.adapters.safe_files import UnsafeLocalPathError
from pc.config import IngestLimits, JobSettings, SecurityPolicy
from pc.domain import SourceNotAuthorizedError
from pc.services import desktop_view, search, tagging, viewing, web_search

ID = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Targets(Input):
    message_ids: list[ID] = Field(default_factory=list, max_length=500)
    content_ids: list[ID] = Field(default_factory=list, max_length=500)


class TagInput(Targets):
    names: list[str] = Field(min_length=1, max_length=100)
    namespace: str = "general"


class ReviewInput(Input):
    ids: list[ID] = Field(min_length=1, max_length=500)
    expected_runs: dict[int, dict[str, Any]] = Field(default_factory=dict)


class AliasInput(Input):
    alias: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=255)


class ViewInput(Input):
    message_id: ID | None = None
    content_id: ID | None = None
    copy_requested: bool = Field(default=False, alias="copy")


class AuthInput(Input):
    token: str = Field(min_length=1, max_length=128)


class SearchInput(Input):
    q: str = Field(default="", max_length=4096)
    source: str | None = None
    item_types: list[
        Literal["text", "image", "mixed", "video", "audio", "file", "link", "sticker"]
    ] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, max_length=100)
    since: str | None = None
    until: str | None = None
    time_field: Literal["ingest", "sent"] = "ingest"
    nsfw_max_level: int = Field(default=0, ge=0, le=4)
    include_duplicates: bool = False
    sort: Literal["time", "relevance"] | None = None
    limit: int | None = Field(default=None, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2048)
    page: int = Field(default=1, ge=1)


@dataclass
class Runtime:
    """启动器注入的路径与配置，不接受浏览器修改。"""

    database: Path
    cache_root: Path
    media_limit: int
    registry: Any = None
    yaml_defaults: dict[str, Any] = field(default_factory=dict)
    port: int = 8080
    paths: Any = None
    limits: IngestLimits = field(default_factory=IngestLimits)
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    jobs: JobSettings = field(default_factory=JobSettings)


class Sessions:
    """一次性引导与可撤销会话；时间使用单调时钟。"""

    def __init__(self, on_revoke=lambda _ident: None):
        self.on_revoke = on_revoke
        self.bootstrap = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + 600
        self.entries: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def exchange(self, token: str) -> tuple[str, str]:
        with self.lock:
            now = time.monotonic()
            if (
                not self.bootstrap
                or now >= self.deadline
                or not secrets.compare_digest(token.encode("utf-8"), self.bootstrap.encode("ascii"))
            ):
                raise HTTPException(401, "授权链接无效或已过期，请重启 pc serve 获取新链接")
            self.bootstrap = ""
            ident, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.entries[ident] = {"created": now, "seen": now, "csrf": csrf}
            return ident, csrf

    def check(self, ident: str, *, touch=True) -> str:
        with self.lock:
            now = time.monotonic()
            session = self.entries.get(ident)
            valid = session is not None and now - session["created"] < 28800 and now - session["seen"] < 1800
            if valid:
                if touch:
                    session["seen"] = now
                return session["csrf"]
            self.entries.pop(ident, None)
        # 不持有会话锁调用模型清理，避免会话锁与模型锁顺序反转。
        self.on_revoke(ident)
        raise HTTPException(401, "本机会话已失效，请重启 pc serve 获取授权链接")

    def valid(self, ident: str) -> bool:
        try:
            self.check(ident, touch=False)
        except HTTPException:
            return False
        return True

    def revoke(self, ident: str):
        with self.lock:
            self.entries.pop(ident, None)
        self.on_revoke(ident)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """工厂直接启动保留探针；业务页面通过 pc serve 注入本机运行时。"""
    from pc.services.accounts import identity
    from pc.services.model_access import ModelAccess
    from pc.services.model_gateway import ModelGateway

    access = ModelAccess(registry=runtime.registry if runtime else None, yaml_defaults=runtime.yaml_defaults if runtime else None, max_image_pixels=runtime.limits.max_image_pixels if runtime else 100_000_000)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            # Uvicorn 正常关闭（含 Ctrl+C）时主动解除凭据引用；不依赖 GC。
            access.close()

    app = FastAPI(title="post-collector", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    sessions = Sessions(on_revoke=access.invalidate_session)
    gateway = ModelGateway(access, session_valid=sessions.valid)
    app.state.sessions = sessions
    app.state.model_access = access
    app.state.model_gateway = gateway
    origin = f"http://127.0.0.1:{runtime.port if runtime else 8080}"

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        try:
            local = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            )
        except ValueError:
            local = False
        if not local or request.headers.get("host") != origin.removeprefix("http://"):
            return JSONResponse({"detail": "仅接受当前地址的本机请求"}, status_code=403)
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin") != origin
        ):
            return JSONResponse({"detail": "写操作必须来自本页面"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def authorized(request: Request) -> str:
        csrf = sessions.check(request.cookies.get("pc_session", ""))
        if request.method != "GET" and not secrets.compare_digest(
            csrf.encode("ascii"), request.headers.get("x-csrf-token", "").encode("utf-8")
        ):
            raise HTTPException(403, "操作凭据无效，请刷新页面")
        if runtime is None:
            raise HTTPException(503, "请使用 pc serve 启动业务页面")
        return csrf

    protected = [Depends(authorized)]

    @app.exception_handler(UnsafeLocalPathError)
    @app.exception_handler(ValueError)
    @app.exception_handler(search.SearchError)
    async def invalid_request(request: Request, exc: Exception):
        return JSONResponse(
            {"detail": str(exc), "copied": getattr(exc, "copied", False)}, status_code=400
        )

    @app.exception_handler(SourceNotAuthorizedError)
    async def source_denied(request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    @app.exception_handler(db.DatabaseError)
    async def unavailable(request: Request, exc: Exception):
        logging.getLogger(__name__).error("归档访问失败", exc_info=exc)
        return JSONResponse({"detail": "数据库或归档文件不可用，请检查服务终端"}, status_code=503)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/meta")
    def meta(request: Request):
        enabled = False
        session = request.cookies.get("pc_session", "")
        if runtime is not None and sessions.valid(session):
            with connect() as conn:
                enabled = access.status(conn, session)["enabled"]
        return {
            "name": "post-collector",
            "version": __version__,
            "stage": "local-web",
            "egress_enabled": enabled,
            "authentication": "temporary-local-root",
        }

    @app.get("/docs", include_in_schema=False)
    def docs():
        return RedirectResponse("/openapi.json")

    assets = Path(__file__).resolve().parent.parent / "web"
    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/model", include_in_schema=False)
    @app.get("/compositions", include_in_schema=False)
    @app.get("/add", include_in_schema=False)
    @app.get("/manage", include_in_schema=False)
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(assets / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.post("/api/v1/session")
    def login(body: AuthInput, response: Response):
        if runtime is None:
            raise HTTPException(503, "请使用 pc serve 启动业务页面")
        ident, csrf = sessions.exchange(body.token)
        response.set_cookie("pc_session", ident, httponly=True, samesite="strict", path="/")
        return {**identity(), "csrf": csrf}

    @app.get("/api/v1/session", dependencies=protected)
    def session(request: Request):
        return {**identity(), "csrf": sessions.check(request.cookies.get("pc_session", ""))}

    @app.get("/identity", dependencies=protected)
    def identity_json():
        return identity()

    @app.post("/api/v1/logout", dependencies=protected)
    def logout(request: Request, response: Response):
        sessions.revoke(request.cookies.get("pc_session", ""))
        response.delete_cookie("pc_session", path="/")
        return {"ok": True}

    def connect():
        assert runtime is not None
        return db.open_db(runtime.database, expect_exists=True)

    upload_lock = threading.Lock()

    @app.post("/api/v1/uploads", dependencies=protected)
    async def upload(request: Request):
        from pc.api.upload_form import parse
        from pc.services import uploads

        assert runtime is not None
        form = await parse(request, media_limit=runtime.media_limit)
        if form.get("create_composition") not in (None, "true", "false"):
            await form.close()
            raise HTTPException(422, "是否创建组合须为 true 或 false")

        def execute():
            paths = runtime.paths or db.resolve_paths(env={"PC_DATA_ROOT": str(runtime.cache_root.parent)})
            limits = runtime.limits
            # 工厂测试也沿用显式媒体限额。
            from dataclasses import replace

            limits = replace(limits, max_media_bytes=runtime.media_limit)
            with upload_lock, connect() as conn:
                return uploads.submit(
                    conn, paths=paths, limits=limits, security=runtime.security, jobs=runtime.jobs,
                    text=form.get("text", ""), note=form.get("note", ""), images=form.getlist("images"),
                    parts=form.get("parts"), create_composition=form.get("create_composition") == "true",
                    composition_name=form.get("composition_name", ""),
                    composition_description=form.get("composition_description", ""),
                    composition_tags=form.get("composition_tags", ""),
                )

        try:
            return await run_in_threadpool(execute)
        finally:
            await form.close()

    @app.get("/api/v1/search", dependencies=protected)
    def find(request: Request):
        if request.query_params.get("result_version") == "2":
            kind = request.query_params.get("kind", "all")
            values_v2: dict[str, Any] = dict(request.query_params)
            values_v2.pop("result_version", None)
            values_v2.pop("kind", None)
            for key in ("item_types", "statuses", "tags"):
                values_v2[key] = request.query_params.getlist(key)
            try:
                params = SearchInput.model_validate(values_v2).model_dump()
            except ValidationError as exc:
                raise HTTPException(422, "检索参数无效") from exc
            assert runtime is not None
            with connect() as conn:
                if params["limit"] is None:
                    params["limit"] = search.default_page_size(
                        conn, registry=runtime.registry, yaml_defaults=runtime.yaml_defaults,
                    )
                with db.transaction(conn, immediate=False):
                    return web_search.run_web_search(conn, kind=kind, **params)
        values: dict[str, Any] = dict(request.query_params)
        for key in ("item_types", "statuses", "tags"):
            values[key] = request.query_params.getlist(key)
        try:
            params = SearchInput.model_validate(values).model_dump()
        except ValidationError as exc:
            raise HTTPException(422, "检索参数无效") from exc
        for key in ("item_types", "statuses", "tags"):
            params[key] = tuple(params[key])
        assert runtime is not None
        with connect() as conn:
            return search.run_search(
                conn,
                search.SearchQuery(**params),
                registry=runtime.registry,
                yaml_defaults=runtime.yaml_defaults,
            )

    @app.get("/api/v1/detail", dependencies=protected)
    def detail(message_id: int | None = None, content_id: int | None = None):
        with connect() as conn:
            return viewing.detail(conn, message_id=message_id, content_id=content_id)

    @app.get("/api/v1/media/{content_id}/{media_id}", dependencies=protected)
    def media(content_id: int, media_id: int):
        assert runtime is not None
        with connect() as conn:
            result = viewing.detail(conn, message_id=None, content_id=content_id)
            part = result["parts"][0]
            matched = [m for m in part["media"] if m["media_id"] == media_id]
            if not matched:
                raise HTTPException(404, "内容项没有此媒体")
            part["media"] = matched[:1]
            images = view_files.read_images(conn, result, limit=runtime.media_limit)
            current = viewing.detail(conn, message_id=None, content_id=content_id)
            if matched[0] not in current["parts"][0]["media"]:
                raise HTTPException(409, "媒体在准备期间发生变化")
            return Response(images[0][1], media_type=matched[0]["mime_detected"])

    @app.get("/api/v1/tags", dependencies=protected)
    def tags():
        with connect() as conn:
            return tagging.list_tags(conn)

    @app.get("/api/v1/assignments", dependencies=protected)
    def assignments(
        message_ids: Annotated[list[int] | None, Query()] = None,
        content_ids: Annotated[list[int] | None, Query()] = None,
    ):
        targets = Targets(message_ids=message_ids or [], content_ids=content_ids or [])
        with connect() as conn:
            return tagging.show_tags(conn, **targets.model_dump())

    @app.get("/api/v1/aliases", dependencies=protected)
    def aliases(name: str):
        with connect() as conn:
            return tagging.list_aliases(conn, name)

    @app.post("/api/v1/tags/{operation}", dependencies=protected)
    def write_tags(operation: Literal["add", "remove"], body: TagInput):
        with connect() as conn:
            targets = {"message_ids": body.message_ids, "content_ids": body.content_ids}
            if operation == "add":
                return tagging.apply_classification(
                    conn, tagging.manual_result(body.names, body.namespace), **targets
                )
            return tagging.remove_tags(conn, body.names, **targets)

    @app.post("/api/v1/suggestions/{operation}", dependencies=protected)
    def review(operation: Literal["confirm", "reject"], body: ReviewInput):
        with connect() as conn:
            return tagging.review_suggestions(conn, body.ids, confirm=operation == "confirm", expected_runs=body.expected_runs, model_access=access)

    @app.post("/api/v1/aliases/{operation}", dependencies=protected)
    def write_alias(operation: Literal["add", "remove"], body: AliasInput):
        if operation == "add" and not body.name:
            raise HTTPException(422, "添加别名需要规范标签名")
        with connect() as conn:
            return tagging.change_alias(
                conn, body.alias, name=body.name if operation == "add" else None
            )

    action_lock = threading.Lock()

    @app.post("/api/v1/actions/{action}", dependencies=protected)
    def action(action: Literal["open", "reveal", "copy"], body: ViewInput):
        assert runtime is not None
        with action_lock, connect() as conn:
            result = desktop_view.execute(
                conn,
                **body.model_dump(by_alias=True),
                action=action,
                cache_root=runtime.cache_root,
                limit=runtime.media_limit,
            )
        return {"action": action, "copied": result["copied"], "ok": True}

    from pc.api.model_routes import register

    register(app, connect=connect, protected=protected, access=access, gateway=gateway, max_text_chars=runtime.limits.max_text_chars if runtime else 100000)
    return app
