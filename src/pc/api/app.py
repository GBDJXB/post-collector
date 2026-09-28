"""FastAPI 应用工厂（DESIGN §8.1、§10）。

骨架阶段只提供两个只读端点：
    GET /healthz        存活探针，供 docker-compose 健康检查与人工排查使用
    GET /api/v1/meta    把「当前处于什么模式」暴露成**可观察状态**，而不只是写在文档里

启动方式：
    uv run uvicorn pc.api.app:create_app --factory --port 8080

实现阶段接入端口层（pc.ports）后，检索、详情、tag 编辑、手动分类台与提报审查台
都挂在本应用上；路由按资源拆分（items / tags / inbox / settings / storage），
但业务规则一律写在适配器与服务层，路由只做参数校验与鉴权。
"""

from __future__ import annotations

from fastapi import FastAPI, Response

from pc import __version__
from pc.domain import AiInvolvement, EgressState, SettingsSource

#: 骨架阶段生效的默认档位（实现阶段改为从配置快照读取：config/app.yaml 的 defaults）
_DEFAULT_AI_INVOLVEMENT = AiInvolvement.LOCAL_ONLY


def create_app() -> FastAPI:
    """构造应用实例。使用工厂形式，便于测试与多进程部署。"""
    app = FastAPI(
        title="post-collector",
        version=__version__,
        description=(
            "面向个人授权来源的内容采集、分类与检索系统。"
            "当前为骨架阶段：仅提供健康检查与元信息，业务功能尚未实现。"
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.get("/healthz", tags=["meta"], summary="存活探针")
    def healthz() -> dict[str, str]:
        """返回服务存活与版本；不校验数据库，避免把依赖故障误报成进程死亡。"""
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/meta", tags=["meta"], summary="运行模式元信息")
    def meta() -> dict[str, object]:
        """暴露当前运行模式。

        ``egress_enabled`` 恒为 ``False``：骨架与 MVP 阶段**不存在任何 AI 外发
        代码路径**（DESIGN §8.1）。把它做成接口返回值，是为了让「默认不外发」
        成为一个可被探测、可被测试断言的事实，而不是一句只写在文档里的承诺。
        """
        return {
            "name": "post-collector",
            "version": __version__,
            "stage": "skeleton",
            "ai_involvement": str(_DEFAULT_AI_INVOLVEMENT),
            "ai_involvement_source": str(SettingsSource.DEFAULT),
            "egress_enabled": False,
            "egress_state_default": str(EgressState.LOCAL_ONLY),
            "notes": [
                "骨架阶段：端口接口已声明，业务实现未接入",
                "无 AI 外发代码路径，内容不会离开本机",
                "详见 DESIGN.md §6.3（外发网关，阶段 2）与 §8.1（MVP 明确不做外发）",
            ],
        }

    @app.get("/", tags=["meta"], summary="索引页")
    def index() -> dict[str, object]:
        """根路径的可读入口。

        骨架阶段没有 Web 界面，直接访问根路径原本会 404——第一次部署的人很容易据此
        判断「服务坏了」。这里返回一个最小索引：版本、当前阶段、可用端点，
        以及「不外发」这个事实。
        """
        return {
            "name": "post-collector",
            "version": __version__,
            "stage": "skeleton",
            "endpoints": {
                "health": "/healthz",
                "meta": "/api/v1/meta",
                "docs": "/docs",
                "openapi": "/openapi.json",
            },
            "egress_enabled": False,
            "hint": "骨架阶段没有 Web 界面；功能范围见 README.md，设计见 DESIGN.md",
        }

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        """浏览器会自动请求，返回 204 以免日志里一直刷 404。"""
        return Response(status_code=204)

    return app


__all__ = ["create_app"]
