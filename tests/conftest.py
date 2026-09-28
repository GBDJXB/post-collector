"""pytest 公共夹具与集成测试开关。

``integration`` marker 已在 pyproject.toml 里声明：需要真实 PostgreSQL 的测试
**默认跳过**，只有能拿到连接串时才运行，取值顺序：

1. 环境变量 ``PC_TEST_DSN``（宿主机侧，见 ``.env.example``）
2. 环境变量 ``PC__DB__DSN``（容器内即此路径）
3. ``.env`` 里的 ``PC_TEST_DSN``

集成测试一律在**临时库**里跑（``pc_test_<随机>``）：建库 -> 应用迁移 -> 断言 -> 删库。
DSN 指向的那个库自始至终只被用来执行 ``CREATE DATABASE`` / ``DROP DATABASE``，
不会被写入任何东西。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from pc.adapters import postgres as pg


def integration_dsn() -> str | None:
    """集成测试用的连接串；没有则返回 ``None``（测试将跳过）。"""
    for name in ("PC_TEST_DSN", "PC__DB__DSN"):
        value = os.environ.get(name)
        if value:
            return value
    return pg.read_env_file().get("PC_TEST_DSN") or None


def dsn_for_database(dsn: str, database: str) -> str:
    """替换连接串里的库名，其余部分（主机、端口、凭据）原样保留。"""
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


@pytest.fixture(scope="module")
def scratch_db() -> Iterator[Any]:
    """建一个临时库并连上去；结束时无论断言成败都删掉。"""
    dsn = integration_dsn()
    if not dsn:
        pytest.skip("未提供集成测试连接串（设置 PC_TEST_DSN，或写进 .env 的 PC_TEST_DSN）")

    name = f"pc_test_{uuid4().hex[:8]}"
    try:
        # CREATE DATABASE 不能在事务里执行，因此管理连接用 autocommit
        admin = pg.connect(dsn, autocommit=True, connect_timeout=5)
    except pg.DatabaseError as exc:
        pytest.fail(f"提供了连接串但连不上数据库，集成测试无法进行：{exc}", pytrace=False)

    try:
        admin.execute(f'CREATE DATABASE "{name}"')
    except Exception as exc:  # 建库失败原因很多，统一转成人话
        admin.close()
        pytest.fail(f"无法创建临时库 {name}：{exc}", pytrace=False)

    conn = pg.connect(dsn_for_database(dsn, name))
    try:
        yield conn
    finally:
        conn.close()
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            admin.close()


@pytest.fixture(scope="module")
def applied_schema(scratch_db: Any) -> Any:
    """在临时库里应用 0001，返回已应用迁移的连接。"""
    result = pg.apply_migration(scratch_db, pg.DEFAULT_MIGRATION_FILE)
    assert result["applied"] is True, f"临时库应处于干净状态，实际结果：{result}"
    return scratch_db
