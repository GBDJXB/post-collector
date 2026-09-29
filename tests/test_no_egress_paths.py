"""红线守卫：``src/pc`` 下不存在任何出网代码路径。

DESIGN §9 阶段 1 验收⑦ 要求「全库 grep 不存在任何外发 AI 调用的代码路径」。
把它写成**测试**而不是写在文档里，是因为文档会被忽略、测试不会：这条红线一旦破，
"内容不会离开本机"就从事实退化成一句承诺。

判据是导入图，不是关键字：``import socket`` 与 ``from urllib import request`` 都会被
``ast`` 抓到，而注释与文档里提到 ``httpx``（比如说明"将来换 httpx2"）不会误报。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "pc"

#: 一律禁止的顶层模块名。判据：**它们的存在就等于"能出网"**。
#: ``fastapi`` / ``uvicorn`` 不在名单里：它们是服务端框架，监听本机端口是本项目的
#: 功能之一，不是外发；而且它们是否用套接字是实现细节，不由我们的代码决定。
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        # HTTP / 网络客户端
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
        "urllib",
        "http",
        "socket",
        "ssl",
        "websockets",
        "websocket",
        "telnetlib",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "xmlrpc",
        "asyncio",  # 允许 asyncio 就意味着允许任意网络协程，阶段 1 不需要它
        # AI 服务商 SDK
        "openai",
        "anthropic",
        "google",
        "cohere",
        "mistralai",
        "litellm",
        "transformers",
        "huggingface_hub",
        "ollama",
        # 云与远程访问
        "boto3",
        "botocore",
        "googleapiclient",
        "paramiko",
        "fabric",
        "minio",
    }
)


def _source_files() -> list[Path]:
    return sorted(path for path in SRC.rglob("*.py") if path.is_file())


def _imported_roots(tree: ast.AST) -> set[str]:
    """收集一个模块里所有 import 的顶层模块名。"""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif (
            isinstance(node, ast.ImportFrom)
            # ``from . import x`` / ``from .mod import y`` 是包内相对导入
            and node.level == 0
            and node.module is not None
        ):
            roots.add(node.module.split(".")[0])
    return roots


def test_there_are_source_files_to_check() -> None:
    """反空转：如果路径写错，上面那条测试会"全绿"，这条会红。"""
    files = _source_files()
    assert len(files) > 8, f"只找到 {len(files)} 个源文件，路径可能不对：{SRC}"


@pytest.mark.parametrize("path", _source_files(), ids=lambda path: path.name)
def test_no_egress_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = sorted(_imported_roots(tree) & FORBIDDEN_MODULES)
    assert not offenders, (
        f"{path.relative_to(SRC.parent.parent)} 导入了出网模块：{offenders}\n"
        "  阶段 1 不允许任何外发代码路径（DESIGN §9 验收⑦、§6.3）。\n"
        "  如果这确实是必需的（例如阶段 2 的外发网关），先改 DESIGN 与 AGENTS，"
        "并把调用收敛到单一内部函数 + egress_log，再动这份名单。"
    )


def test_ports_layer_stays_dependency_free() -> None:
    """端口层只允许标准库与领域层：它必须能在没有第三方依赖时被导入。"""
    allowed_third_party: set[str] = set()
    for path in (SRC / "ports").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots = _imported_roots(tree)
        external = {
            root
            for root in roots
            if root not in {"pc", "typing", "collections", "dataclasses", "enum", "__future__"}
        }
        assert not (external - allowed_third_party), (
            f"{path.name} 引入了端口层不该有的依赖：{sorted(external)}"
        )


def _dependency_names(entries: list[str]) -> set[str]:
    """从 PEP 508 依赖串里取出规范化的发行包名（``httpx>=0.27`` → ``httpx``）。"""
    import re

    names: set[str] = set()
    for entry in entries:
        token = re.split(r"[<>=!~\[;( ]", entry.strip(), maxsplit=1)[0]
        if token:
            names.add(token.lower().replace("-", "_"))
    return names


def _pyproject() -> dict[str, object]:
    import tomllib

    path = SRC.parents[1] / "pyproject.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_pyproject_runtime_dependencies_are_clean() -> None:
    """**运行时**依赖里不该出现出网库：装了它，迟早会有人用。"""
    data = _pyproject()
    project = data.get("project", {})
    assert isinstance(project, dict)
    runtime = _dependency_names([str(item) for item in project.get("dependencies", [])])
    offenders = sorted(runtime & FORBIDDEN_MODULES)
    assert not offenders, f"运行时依赖里出现了出网库：{offenders}"


def test_network_client_is_dev_only() -> None:
    """``httpx`` 是 FastAPI TestClient 的依赖，只允许出现在 dev 组。

    这条断言的意义在于：它把"它只是测试用的"变成**代码里的事实**。哪天有人把它挪进
    运行时依赖，这条测试会红，而不是等到某段代码开始发请求才被发现。
    """
    data = _pyproject()
    groups = data.get("dependency-groups", {})
    assert isinstance(groups, dict)
    dev = _dependency_names([str(item) for item in groups.get("dev", [])])
    assert "httpx" in dev, "TestClient 需要 httpx（dev 组）"

    project = data.get("project", {})
    assert isinstance(project, dict)
    runtime = _dependency_names([str(item) for item in project.get("dependencies", [])])
    assert "httpx" not in runtime, "httpx 只该在 dev 组：运行时不发请求"
