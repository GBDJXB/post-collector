"""schema 结构红线测试：在临时 SQLite 库里应用全部迁移，断言结构与两条红线。

存储引擎从 PostgreSQL 换成 SQLite 之后，本文件不再需要任何外部服务，因此也
不再打 ``integration`` marker（见 ``tests/conftest.py`` 的说明）。探测手法从
``pg_catalog`` / ``information_schema`` 换成了 ``sqlite_master`` 与 ``PRAGMA``，
**断言的语义一条都没有弱化**：外发表缺席、匿名提报没有任何通往 content_item 的
通路、外发默认值仍是 local_only。

这些断言是 DESIGN §9 阶段 1 验收里可以**在数据模型层守住**的部分。本文件是项目的
结构红线看守：为了让测试变绿而放宽这里任何一条断言，都等于把红线撤掉。
"""

from __future__ import annotations

from typing import Any

from pc.adapters import sqlite as db

#: 0001 建出的基础表数量。这里刻意写死数字：表清单一旦变化，必须有人来本文件
#: 明确改一次，而不是让「多出来一张表」悄悄通过 inventory 断言。
EXPECTED_TABLE_COUNT = 34

#: 迁移记账里记录的文件名（``Path.name``，含扩展名）。
#: 形如 ``{version: name}``：新增迁移时在这里加一行，同时也是「有人确认过」的痕迹。
EXPECTED_MIGRATION_NAMES = {
    1: "0001_init.sql",
    2: "0002_content_hash_not_unique.sql",
    3: "0003_message_media_refs.sql",
}


def _object_sql(conn: Any, name: str, kind: str) -> str:
    """取 ``sqlite_master`` 里某个对象的建表 / 建索引语句，空白归一化后返回。

    ``sqlite_master.sql`` 保留建表时的原始换行，做子串断言前必须归一化，
    否则「同一句 CHECK 换了个行」就会被误报成约束消失。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
    ).fetchone()
    assert row is not None and row[0], f"sqlite_master 里没有 {kind} {name}"
    return " ".join(str(row[0]).split())


def _index_sqls(conn: Any, table: str) -> dict[str, str]:
    """某张表上的索引：索引名 → 归一化的索引定义（跳过 sql 为 NULL 的自动索引）。"""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table,)
    ).fetchall()
    return {str(r[0]): " ".join(str(r[1]).split()) for r in rows if r[1]}


def _columns(conn: Any, table: str) -> list[str]:
    """某张表的列名，按 ``PRAGMA table_info`` 的声明顺序。"""
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]


def _primary_key(conn: Any, table: str) -> list[str]:
    """某张表主键的列名，按主键内的顺序（``table_info`` 的 pk 序号）。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(r[1]) for r in sorted(rows, key=lambda r: r[5]) if r[5]]


def test_table_inventory_matches_expected(applied_schema: Any) -> None:
    actual = db.public_tables(applied_schema)
    missing = sorted(set(db.EXPECTED_TABLES) - actual)
    unexpected = sorted(actual - set(db.EXPECTED_TABLES))
    assert actual == set(db.EXPECTED_TABLES), f"缺表：{missing}；多表：{unexpected}"
    assert len(actual) == EXPECTED_TABLE_COUNT, (
        f"基础表应恰好 {EXPECTED_TABLE_COUNT} 张，实际 {len(actual)} 张：{sorted(actual)}"
    )


def test_deferred_tables_absent(applied_schema: Any) -> None:
    """content_embedding / ai_provider_policy / egress_log 属阶段 2，出现即触碰红线。

    这是「无外发」红线的结构层表现：阶段 1 不得引入任何第三方 AI 调用的代码路径，
    连承载外发记账的表都不该存在（DESIGN §8.1 的 MVP 明确不做外发）。
    """
    present = sorted(db.DEFERRED_TABLES & db.public_tables(applied_schema))
    assert not present, f"阶段 2 才引入的表不该存在：{present}"


def test_fts5_virtual_tables_are_installed_and_queryable(applied_schema: Any) -> None:
    """``pg_trgm`` 的位置由两个 FTS5 虚拟表顶上（DESIGN §5.1），两者都要在位且可查。"""
    assert db.fts_tables(applied_schema) == set(db.FTS_VIRTUAL_TABLES)
    for virtual in db.FTS_VIRTUAL_TABLES:
        # 空库上的 MATCH 也必须跑通：证明倒排表结构与分词器都装好了
        row = applied_schema.execute(
            f"SELECT count(*) FROM {virtual} WHERE {virtual} MATCH ?", ("精灵",)
        ).fetchone()
        assert row is not None and int(row[0]) == 0, f"{virtual} 上的 MATCH 查询异常：{row}"


def test_migration_bookkeeping_and_checksum(applied_schema: Any) -> None:
    rows = applied_schema.execute(
        "SELECT version, name, checksum FROM schema_migration ORDER BY version"
    ).fetchall()
    assert [int(row[0]) for row in rows] == sorted(EXPECTED_MIGRATION_NAMES), (
        f"记账的版本号与预期不符：实际 {[int(r[0]) for r in rows]}"
    )
    for version, name, checksum in rows:
        version = int(version)
        assert str(name) == EXPECTED_MIGRATION_NAMES[version], f"version {version} 的文件名不符"
        path = db.DEFAULT_MIGRATIONS_DIR / str(name)
        assert checksum == db.migration_checksum(path.read_text(encoding="utf-8")), (
            f"version {version} 的结构指纹与文件不一致"
        )


def test_red_line_constraints_present(applied_schema: Any) -> None:
    """红线与关键取值集合必须以命名 CHECK 出现在建表语句里。"""
    app_user_sql = _object_sql(applied_schema, "app_user", "table")
    assert "CONSTRAINT app_user_root_requires_secrets" in app_user_sql
    tag_sql = _object_sql(applied_schema, "tag", "table")
    assert "CONSTRAINT tag_merged_needs_target" in tag_sql

    assignment_sql = _object_sql(applied_schema, "tag_assignment", "table")
    assert "CONSTRAINT tag_assignment_auto_needs_model" in assignment_sql
    assert "model_version_id IS NOT NULL AND confidence IS NOT NULL" in assignment_sql, (
        "source='auto' 必须同时带 model_version_id 与 confidence（DESIGN §4.7.6）"
    )

    content_sql = _object_sql(applied_schema, "content_item", "table")
    egress_values = "egress_state IN ('local_only', 'pending', 'cleared', 'denied', 'expired')"
    assert egress_values in content_sql
    assert "nsfw_level BETWEEN 0 AND 4" in content_sql
    assert "status IN ('active', 'quarantined', 'archived', 'deleted')" in content_sql
    assert "submission_tier IN (0, 1, 2)" in content_sql


def test_content_hash_index_is_partial_but_not_unique(applied_schema: Any) -> None:
    """``content_hash`` 上是**部分**索引，但**不是唯一**索引。

    v3.1 的 0002 迁移刻意把唯一性去掉了：``content_item.message_id`` 是 NOT NULL，
    同一段文本出现在两条消息里必须能落两行；文本的精确重复交给 `dup_group_id` /
    `canonical_id` 归组（DESIGN §3.6、§3.8）。部分条件仍然要求排除已软删的行，
    这样"软删后可重新导入"依旧成立。
    """
    candidates = {
        name: sql
        for name, sql in _index_sqls(applied_schema, "content_item").items()
        if "content_hash" in sql
    }
    assert len(candidates) == 1, f"应恰好有一个 content_hash 索引，实际 {sorted(candidates)}"
    index_sql = next(iter(candidates.values()))
    assert not index_sql.startswith("CREATE UNIQUE INDEX"), (
        "content_hash 不能是唯一索引：同文本跨消息必须能共存（0002 迁移）"
    )
    assert "WHERE content_hash IS NOT NULL AND deleted_at IS NULL" in index_sql


def test_egress_state_defaults_to_local_only(applied_schema: Any) -> None:
    defaults = {str(r[1]): r[4] for r in applied_schema.execute("PRAGMA table_info(content_item)")}
    assert "egress_state" in defaults, "content_item.egress_state 列不存在"
    assert "local_only" in str(defaults["egress_state"]), (
        f"外发默认值必须是 local_only，实际 {defaults['egress_state']}"
    )


def test_inbox_item_has_no_path_to_content_item(applied_schema: Any) -> None:
    """L2 匿名提报在审查通过前零副作用——结构上就不该有通往 content_item 的通路。

    允许的出边只有两条：promoted_message_id → message（审查通过后的溯源指针）
    与 reviewed_by → app_user（谁审的）。
    """
    columns = _columns(applied_schema, "inbox_item")
    assert not any("content_id" in column for column in columns), (
        f"inbox_item 不该有指向内容项的列：{columns}"
    )
    leaked = sorted(column for column in columns if "content" in column)
    assert not leaked, f"inbox_item 不该有指向内容项的列：{leaked}"

    targets = {str(r[2]) for r in applied_schema.execute("PRAGMA foreign_key_list(inbox_item)")}
    assert "content_item" not in targets, f"inbox_item 存在通往 content_item 的外键：{targets}"
    assert targets <= {"message", "app_user"}, f"inbox_item 出现预期外的外键：{targets}"


def test_apply_migration_is_idempotent(applied_schema: Any) -> None:
    result = db.apply_migration(applied_schema)
    assert result["applied"] == [], f"已应用的库不该再应用任何版本：{result}"
    assert result["skipped"] == sorted(EXPECTED_MIGRATION_NAMES)
    assert result["drifted"] == []


def test_database_status_reports_ok(applied_schema: Any) -> None:
    status = db.database_status(applied_schema)
    assert status["ok"] is True, f"status 未通过：{status}"
    assert status["missing_tables"] == ()
    assert status["deferred_present"] == ()
    assert status["unexpected_tables"] == ()
    assert status["checksum_drift"] is False
    assert status["migrations_missing"] == ()
    assert status["migrations_unknown"] == ()
    assert status["migrations_applied"] == tuple(sorted(EXPECTED_MIGRATION_NAMES))
    assert status["migration_version"] == db.latest_migration_version()
    # PRAGMA 基线与完整性：体检结论必须同时覆盖这三项，否则「ok」没有意义
    assert str(status["journal_mode"]).lower() == "wal"
    assert status["foreign_keys"] is True
    assert status["integrity"] == "ok"


# ---------------------------------------------------------------------------
# 本次迁移新增的结构断言（DESIGN v3.1 已批准的设计）
# ---------------------------------------------------------------------------


def test_content_media_is_the_many_to_many_join_table(applied_schema: Any) -> None:
    """v3.1 修掉的多对多缺陷：从属关系落在 content_media，media 只留首次归属。"""
    assert _primary_key(applied_schema, "content_media") == ["content_id", "media_id", "role"]

    media_columns = _columns(applied_schema, "media")
    assert "first_seen_content_id" in media_columns, "media 应保留首次入库归属列"
    assert "content_id" not in media_columns, (
        "media 不该再有 content_id 列：一个媒体本体被多条内容项引用时，"
        "从属关系必须落在 content_media（DESIGN §3.2、§3.8）"
    )


def test_hash_tombstone_primary_key(applied_schema: Any) -> None:
    """哈希墓地以 ``(hash, alg)`` 为主键：同一哈希的不同算法可以各留一条。"""
    assert _primary_key(applied_schema, "hash_tombstone") == ["hash", "alg"]


def test_every_base_table_is_strict(applied_schema: Any) -> None:
    """34 张基础表必须**全部**是 STRICT。

    漏掉一张，那一张就退回「类型随缘」的旧 SQLite 行为：往 INTEGER 列写字符串
    不会报错，静默存进去，等到检索时才炸。FTS5 虚拟表与其影子表不在其列
    （``public_tables()`` 已把它们排除，虚拟表也不支持 STRICT）。
    """
    checked = sorted(db.public_tables(applied_schema))
    assert len(checked) == EXPECTED_TABLE_COUNT, (
        f"应检查 {EXPECTED_TABLE_COUNT} 张表，实际 {len(checked)}"
    )
    offenders = [
        table
        for table in checked
        if not _object_sql(applied_schema, table, "table").endswith("STRICT")
    ]
    assert not offenders, f"这些表不是 STRICT：{offenders}"
