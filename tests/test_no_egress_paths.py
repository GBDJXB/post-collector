"""红线守卫：仅 DESIGN §4.5/§6.3 批准的固定打标适配器可导入 httpx。

除精确文件/模块组合外继续禁止任何网络客户端及服务商 SDK；端口层无第三方依赖。
判据是 AST 导入图，不是注释或关键词。实际闸门/审计/计费预算由网关测试验证。
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
    allowed = {"httpx"} if path == SRC / "adapters" / "deepseek_model.py" else set()
    offenders = sorted((_imported_roots(tree) & FORBIDDEN_MODULES) - allowed)
    assert not offenders, (
        f"{path.relative_to(SRC.parent.parent)} 导入了未批准的出网模块：{offenders}\n"
        "只允许固定打标适配器导入 httpx（DESIGN §4.5/§6.3）；其他出网路径须先获批准。"
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
    """唯一批准的网络运行时依赖是固定打标适配器使用的 httpx。"""
    data = _pyproject()
    project = data.get("project", {})
    assert isinstance(project, dict)
    runtime = _dependency_names([str(item) for item in project.get("dependencies", [])])
    offenders = sorted((runtime & FORBIDDEN_MODULES) - {"httpx"})
    assert not offenders, f"运行时依赖里出现了出网库：{offenders}"


def test_fixed_model_adapter_has_runtime_httpx() -> None:
    """uv sync --no-dev 也必须能加载批准的打标适配器，不依赖 dev 组。"""
    data = _pyproject()
    project = data.get("project", {})
    assert isinstance(project, dict)
    runtime = _dependency_names([str(item) for item in project.get("dependencies", [])])
    assert "httpx" in runtime, "固定打标适配器需要运行时 httpx"
