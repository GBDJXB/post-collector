"""只挂载到本机应用的模型总闸与逐次同意；远程应用不导入此模块。"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from pc.services import compositions, tagging

ID = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Key(Input):
    key: str


class Access(Input):
    enabled: StrictBool
    confirmed: StrictBool = False


class Target(Input):
    content_id: ID | None = None
    composition_id: ID | None = None


class Send(Input):
    preview_id: str = Field(max_length=128)
    confirmed: StrictBool = False


class Review(Input):
    ids: list[ID] = Field(min_length=1, max_length=500)
    expected_runs: dict[int, dict] = Field(default_factory=dict)


class CreateComposition(Input):
    name: str = Field(min_length=1, max_length=255)
    description: str = ''
    content_ids: list[ID] = Field(min_length=1, max_length=500)


class CompositionTags(Input):
    names: list[str] = Field(min_length=1, max_length=100)
    namespace: str = 'general'


def register(app, *, connect, protected, access, gateway, max_text_chars=100000):
    @app.exception_handler(RequestValidationError)
    async def invalid_body(_request, _exc):
        # FastAPI 默认详情会包含输入值；密钥和授权令牌不能回显。
        return JSONResponse({'detail': '参数无效，请检查输入'}, status_code=422)

    @app.get('/api/v1/model/status', dependencies=protected)
    def status(request: Request):
        with connect() as conn:
            return access.status(conn, request.cookies.get('pc_session', ''))

    @app.post('/api/v1/model/key/set', dependencies=protected)
    def set_key(request: Request, body: Key):
        access.set_key(request.cookies.get('pc_session', ''), body.key)
        return status(request)

    @app.post('/api/v1/model/key/delete', dependencies=protected)
    def delete_key(request: Request):
        access.delete_key(request.cookies.get('pc_session', ''))
        return status(request)

    @app.post('/api/v1/model/access', dependencies=protected)
    def set_access(request: Request, body: Access):
        with connect() as conn:
            return access.set_access(conn, request.cookies.get('pc_session', ''), body.enabled, confirmed=body.confirmed)

    @app.post('/api/v1/model/preview', dependencies=protected)
    def preview(request: Request, body: Target):
        with connect() as conn:
            return gateway.preview(conn, request.cookies.get('pc_session', ''), **body.model_dump())

    @app.post('/api/v1/model/send', dependencies=protected)
    def send(request: Request, body: Send):
        with connect() as conn:
            return gateway.send(conn, request.cookies.get('pc_session', ''), body.preview_id, confirmed=body.confirmed)

    @app.post('/api/v1/compositions', dependencies=protected)
    def create_composition(body: CreateComposition):
        with connect() as conn:
            return compositions.write(conn, 'create', name=body.name, description=body.description, contents=body.content_ids, max_text_chars=max_text_chars)

    @app.post('/api/v1/compositions/{composition_id}/tags/{operation}', dependencies=protected)
    def change_composition_tags(composition_id: int, operation: Literal['add', 'remove'], body: CompositionTags):
        with connect() as conn:
            return compositions.tag_change(conn, composition_id, body.names, remove=operation == 'remove', namespace=body.namespace)

    @app.get('/api/v1/compositions', dependencies=protected)
    def list_compositions(tags: Annotated[list[str] | None, Query()] = None, match_member_tags: bool = False, limit: int = 50, offset: int = 0):
        with connect() as conn:
            return compositions.list_compositions(conn, names=tags or (), match_member_tags=match_member_tags, limit=limit, offset=offset)

    @app.get('/api/v1/compositions/{composition_id}', dependencies=protected)
    def composition(composition_id: int):
        with connect() as conn:
            return compositions.show(conn, composition_id)

    @app.get('/api/v1/compositions/{composition_id}/assignments', dependencies=protected)
    def assignments(composition_id: int):
        with connect() as conn:
            return compositions.show_tags(conn, composition_id)

    @app.post('/api/v1/compositions/{composition_id}/suggestions/{operation}', dependencies=protected)
    def review(composition_id: int, operation: Literal['confirm', 'reject'], body: Review):
        with connect() as conn:
            # 明确指定组合，禁止用另一个组合的关联编号混淆目标。
            return tagging.review_suggestions(conn, body.ids, confirm=operation == 'confirm', composition=True, expected_composition_id=composition_id, expected_runs=body.expected_runs, model_access=access)
