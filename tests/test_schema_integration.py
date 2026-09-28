"""schema 集成测试：在临时库里应用 0001，断言结构与两条红线。

需要真实 PostgreSQL，未提供连接串时整文件跳过（见 ``tests/conftest.py``）。
这些断言是 DESIGN §9 阶段 1 验收里可以**在数据模型层守住**的部分：
外发表缺席、匿名提报没有任何通往 content_item 的通路、外发默认值仍是 local_only。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pc.adapters import postgres as pg

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / pg.DEFAULT_MIGRATION_FILE


def _constraint_defs(conn: Any, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'c'",
        (table,),
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _fk_targets(conn: Any, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT confrelid::regclass::text FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'f'",
        (table,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def test_table_inventory_matches_expected(applied_schema: Any) -> None:
    actual = pg.public_tables(applied_schema)
    missing = sorted(set(pg.EXPECTED_TABLES) - actual)
    unexpected = sorted(actual - set(pg.EXPECTED_TABLES))
    assert actual == set(pg.EXPECTED_TABLES), f"缺表：{missing}；多表：{unexpected}"


def test_deferred_tables_absent(applied_schema: Any) -> None:
    """content_embedding / ai_provider_policy / egress_log 属阶段 2，出现即触碰红线。"""
    present = sorted(pg.DEFERRED_TABLES & pg.public_tables(applied_schema))
    assert not present, f"阶段 2 才引入的表不该存在：{present}"


def test_pg_trgm_installed(applied_schema: Any) -> None:
    row = applied_schema.execute(
        "SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'"
    ).fetchone()
    assert row is not None and row[0] == 1


def test_migration_bookkeeping_and_checksum(applied_schema: Any) -> None:
    rows = applied_schema.execute("SELECT version, name, checksum FROM schema_migration").fetchall()
    assert len(rows) == 1, f"记账表应只有一条记录，实际 {len(rows)} 条"
    version, name, checksum = rows[0]
    assert version == pg.MIGRATION_VERSION
    assert name == "0001_init"
    assert checksum == pg.migration_checksum(MIGRATION.read_text(encoding="utf-8"))


def test_red_line_constraints_present(applied_schema: Any) -> None:
    assert "app_user_root_requires_secrets" in _constraint_defs(applied_schema, "app_user")
    assert "tag_merged_needs_target" in _constraint_defs(applied_schema, "tag")

    tag_assignment_defs = _constraint_defs(applied_schema, "tag_assignment").values()
    assert any(
        "model_version_id IS NOT NULL" in d and "confidence IS NOT NULL" in d
        for d in tag_assignment_defs
    ), "source='auto' 必须同时带 model_version_id 与 confidence（DESIGN §4.7.6）"


def test_content_hash_index_is_partial(applied_schema: Any) -> None:
    """软删后可重新导入同一内容：唯一索引必须排除 deleted_at 非空的行。"""
    rows = applied_schema.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'content_item' AND indexdef LIKE '%content_hash%'"
    ).fetchall()
    assert len(rows) == 1, f"应恰好有一个 content_hash 索引，实际 {len(rows)} 个"
    indexdef = str(rows[0][0])
    assert indexdef.startswith("CREATE UNIQUE INDEX")
    assert "deleted_at IS NULL" in indexdef


def test_egress_state_defaults_to_local_only(applied_schema: Any) -> None:
    row = applied_schema.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'content_item' AND column_name = 'egress_state'"
    ).fetchone()
    assert row is not None, "content_item.egress_state 列不存在"
    assert "local_only" in str(row[0]), f"外发默认值必须是 local_only，实际 {row[0]}"


def test_inbox_item_has_no_path_to_content_item(applied_schema: Any) -> None:
    """L2 匿名提报在审查通过前零副作用——结构上就不该有通往 content_item 的通路。

    允许的出边只有两条：promoted_message_id → message（审查通过后的溯源指针）
    与 reviewed_by → app_user（谁审的）。
    """
    columns = {
        str(r[0])
        for r in applied_schema.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'inbox_item'"
        ).fetchall()
    }
    leaked = sorted(c for c in columns if "content" in c)
    assert not leaked, f"inbox_item 不该有指向内容项的列：{leaked}"

    targets = _fk_targets(applied_schema, "inbox_item")
    assert "content_item" not in targets, f"inbox_item 存在通往 content_item 的外键：{targets}"
    assert targets <= {"message", "app_user"}, f"inbox_item 出现预期外的外键：{targets}"


def test_apply_migration_is_idempotent(applied_schema: Any) -> None:
    result = pg.apply_migration(applied_schema, MIGRATION)
    assert result["applied"] is False
    assert result["reason"] == "already_applied"


def test_database_status_reports_ok(applied_schema: Any) -> None:
    status = pg.database_status(applied_schema, MIGRATION)
    assert status["ok"] is True, f"status 未通过：{status}"
    assert status["missing_tables"] == ()
    assert status["deferred_present"] == ()
    assert status["unexpected_tables"] == ()
    assert status["checksum_drift"] is False
    assert status["migration_version"] == pg.MIGRATION_VERSION
