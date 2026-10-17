"""pytest 公共夹具。

数据库测试**不再依赖任何外部服务**：每个测试模块用自己的临时 SQLite 文件，
跑完随临时目录一起销毁。因此这些测试不需要 ``integration`` marker，
也不依赖外部数据库服务。

``integration`` marker 仍然保留在 ``pyproject.toml`` 里，留给将来真正需要外部
服务的测试（平台接口、模型下载、对象存储等）。

夹具分层的用意：

* :func:`scratch_db` 给的是**尚未应用迁移**的空库，用来验证「干净库」路径；
* :func:`applied_schema` 在它之上应用全部迁移，供绝大多数结构与设置层测试使用；
* :func:`fresh_db` 是**每个测试一份**的已迁移库，给服务层测试用——服务层会写数据，
  共用一个模块级连接会让测试之间通过数据库互相影响，那种耦合最难查。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pc.adapters import sqlite as db


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """本模块专属的临时数据库文件路径（尚未创建）。"""
    return tmp_path_factory.mktemp("pc_db") / "pc_test.sqlite3"


@pytest.fixture(scope="module")
def scratch_db(db_path: Path) -> Iterator[Any]:
    """打开一个临时数据库并执行 PRAGMA 基线；**不应用迁移**。"""
    conn = db.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def applied_schema(scratch_db: Any) -> Any:
    """在临时库里应用全部迁移，返回已应用迁移的连接。"""
    result = db.apply_migration(scratch_db)
    assert result["applied"] == sorted(db.applied_versions(scratch_db)), (
        f"临时库应处于干净状态并应用全部版本，实际结果：{result}"
    )
    assert result["drifted"] == [], f"干净库不该有漂移：{result}"
    return scratch_db


@pytest.fixture
def fresh_db(tmp_path: Path) -> Iterator[Any]:
    """每个测试一份、已应用全部迁移的连接（服务层测试用）。"""
    conn = db.connect(tmp_path / "fresh.sqlite3")
    try:
        db.apply_migration(conn)
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def isolate_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 ``PC_DATA_ROOT`` / ``PC__PATHS__*`` 从环境里摘掉。

    CI 与开发机上可能残留部署变量（例如 ``PC__PATHS__MEDIA_ROOT=/data/media``）。
    它们是**最高优先级**，留着会让"媒体落到哪里"的断言在不同机器上给出不同结果——
    测试必须只依赖自己显式传进去的东西。
    """
    for name in (
        "PC_DATA_ROOT",
        "PC__DB__PATH",
        "PC__PATHS__MEDIA_ROOT",
        "PC__PATHS__THUMB_ROOT",
        "PC__PATHS__COLD_ROOT",
        "PC__PATHS__QUARANTINE_ROOT",
        "PC__PATHS__BACKUP_ROOT",
        "PC__PATHS__MODELS_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
