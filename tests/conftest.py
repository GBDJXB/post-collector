"""pytest 公共夹具。

数据库测试**不再依赖任何外部服务**：每个测试模块用自己的临时 SQLite 文件，
跑完随临时目录一起销毁。因此这些测试不需要 ``integration`` marker，也不会因为
「Docker 没开」而跳过或报错——这是从 PostgreSQL 换到 SQLite 之后最直接的一处收益。

``integration`` marker 仍然保留在 ``pyproject.toml`` 里，留给将来真正需要外部
服务的测试（平台接口、模型下载、对象存储等）。

夹具分层的用意：

* :func:`scratch_db` 给的是**尚未应用迁移**的空库，用来验证「干净库」路径；
* :func:`applied_schema` 在它之上应用 0001，供绝大多数结构与设置层测试使用。
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
    """在临时库里应用 0001，返回已应用迁移的连接。"""
    result = db.apply_migration(scratch_db, db.DEFAULT_MIGRATION_FILE)
    assert result["applied"] is True, f"临时库应处于干净状态，实际结果：{result}"
    return scratch_db
