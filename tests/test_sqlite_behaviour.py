"""SQLite 行为契约测试：验证适配器与 schema 在新引擎上的**实测行为**。

这些测试守的是「换引擎时最容易悄悄变味」的东西：PRAGMA 基线、外键与 STRICT 是否
真的生效、迁移是否真的整篇单事务、FTS5 双索引的同步与退化点、备份可恢复性、
以及 DESIGN §3.8 的引用计数结构前提。

夹具分层：``conftest.py`` 的 ``applied_schema`` 是 module 作用域，多个测试共享同一个
库，写数据的测试会互相污染，因此本文件凡是写库的测试一律用函数级夹具
``behaviour_db`` 自建临时库（或直接 ``db.connect(tmp_path / ...)``）。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from pc.adapters import sqlite as db

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / db.DEFAULT_MIGRATIONS_DIR

#: 造数据用的固定 sha256（STRICT 表不校验长度，但列语义要求 64 个十六进制字符）
SHA256 = "a" * 64


@pytest.fixture
def behaviour_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """函数级干净库：已应用全部迁移，测试之间互不影响。"""
    conn = db.connect(tmp_path / "behaviour.sqlite3")
    try:
        db.apply_migration(conn, MIGRATIONS_DIR)
        yield conn
    finally:
        conn.close()


def _uuid() -> str:
    """应用层生成的 uuid（0001 里刻意没有数据库默认值）。"""
    return str(uuid.uuid4())


def _seed_source(conn: sqlite3.Connection) -> None:
    """建一条 source；kind 只能取 bot_api|webhook|rss|export|user_api，这里用 export。"""
    conn.execute(
        "INSERT INTO source (source_id, uuid, name, platform, kind) VALUES (1, ?, ?, ?, ?)",
        (_uuid(), "demo", "telegram", "export"),
    )


def _seed_message(conn: sqlite3.Connection, message_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO message (message_id, uuid, source_id, platform_msg_id) VALUES (?, ?, 1, ?)",
        (message_id, _uuid(), f"msg-{message_id}"),
    )


def _seed_content(
    conn: sqlite3.Connection,
    content_id: int = 1,
    *,
    message_id: int = 1,
    ordinal: int = 0,
    text_content: str | None = None,
    text_search: str | None = None,
) -> None:
    """建一条 text 内容项；text_search 是应用层分词结果，喂给 content_word。"""
    conn.execute(
        "INSERT INTO content_item "
        "(content_id, uuid, message_id, item_type, ordinal, text_content, text_search) "
        "VALUES (?, ?, ?, 'text', ?, ?, ?)",
        (content_id, _uuid(), message_id, ordinal, text_content, text_search),
    )


def _seed_media(conn: sqlite3.Connection, media_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO media (media_id, uuid, sha256, size_bytes, object_key) VALUES (?, ?, ?, ?, ?)",
        (media_id, _uuid(), SHA256, 1024, f"aa/bb/{SHA256}"),
    )


def _match_count(conn: sqlite3.Connection, virtual: str, query: str) -> int:
    """在某个 FTS5 虚拟表上跑一次 MATCH，返回命中行数。"""
    row = conn.execute(
        f"SELECT count(*) FROM {virtual} WHERE {virtual} MATCH ?", (query,)
    ).fetchone()
    assert row is not None, f"{virtual} 上的 MATCH 查询没有返回结果"
    return int(row[0])


def _scalar(conn: sqlite3.Connection, sql: str, *params: object) -> object:
    row = conn.execute(sql, params).fetchone()
    assert row is not None, f"查询没有返回行：{sql}"
    return row[0]


# ---------------------------------------------------------------------------
# 1. PRAGMA 基线
# ---------------------------------------------------------------------------


def test_pragma_baseline_is_installed(behaviour_db: sqlite3.Connection) -> None:
    """连接建立后 PRAGMA 基线必须就位（DESIGN §3.2）。

    这几条不能写进迁移文件：``journal_mode`` 在事务内无法切换，所以它们由
    ``pc.adapters.sqlite.connect()`` 负责，也就在这里被盯住。
    """
    assert db.pragma_value(behaviour_db, "journal_mode") == "wal"
    assert db.pragma_value(behaviour_db, "foreign_keys") == "1"
    assert db.pragma_value(behaviour_db, "busy_timeout") == str(db.BUSY_TIMEOUT_MS) == "5000"
    assert db.pragma_value(behaviour_db, "synchronous") is not None


# ---------------------------------------------------------------------------
# 2. 外键真的生效
# ---------------------------------------------------------------------------


def test_foreign_keys_are_enforced(behaviour_db: sqlite3.Connection) -> None:
    """孤儿行必须被拒——证明 ``foreign_keys=ON`` 没有漏（SQLite 默认是关的）。"""
    _seed_media(behaviour_db, media_id=1)  # 先让 media_id 合法，唯一违规点就是 content_id
    with pytest.raises(sqlite3.IntegrityError):
        behaviour_db.execute(
            "INSERT INTO content_media (content_id, media_id, role) VALUES (999, 1, 'primary')"
        )
    assert _scalar(behaviour_db, "SELECT count(*) FROM content_media") == 0


# ---------------------------------------------------------------------------
# 3. STRICT 真的生效
# ---------------------------------------------------------------------------


def test_strict_tables_reject_wrong_types(behaviour_db: sqlite3.Connection) -> None:
    """STRICT 表必须拦下类型不符的写入，而不是静默按类型亲和性存进去。"""
    with pytest.raises(sqlite3.IntegrityError):
        behaviour_db.execute(
            "INSERT INTO schema_migration (version, name) VALUES ('not-an-integer', 'probe')"
        )


# ---------------------------------------------------------------------------
# 4. 版本门槛
# ---------------------------------------------------------------------------


def test_version_gate_fails_closed_with_human_readable_hint() -> None:
    """SQLite 版本不足时 fail closed，并给出「升级 Python 或换单文件包」两条出路。"""
    assert db.MIN_SQLITE_VERSION == (3, 40)
    with pytest.raises(db.DatabaseError) as excinfo:
        db.ensure_version_gate((3, 39))
    assert "单文件包" in str(excinfo.value)

    db.ensure_version_gate((3, 50, 4))  # 达标，不抛


# ---------------------------------------------------------------------------
# 5. 整篇事务回滚（DDL 也在事务里）
# ---------------------------------------------------------------------------


def test_migration_rolls_back_ddl_atomically(tmp_path: Path) -> None:
    """单个文件失败只回滚它自己：``CREATE TABLE`` 也要跟着回滚。

    Python 3.12 起 DDL 不再隐式开启事务，因此这条断言是在守
    「适配器显式 BEGIN IMMEDIATE」这个实现选择，而不是在守 SQLite 的默认行为。
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    broken_sql = (
        "CREATE TABLE probe_table (a INTEGER);\n"
        "THIS IS NOT VALID SQL;\n"
    )
    (migrations / "0001_broken.sql").write_text(broken_sql, encoding="utf-8")
    # 反空转：确认切分出来的确实是两条语句——否则「库是空的」只是因为 CREATE 根本没跑
    assert len(db.split_statements(broken_sql)) == 2

    conn = db.connect(tmp_path / "rollback.sqlite3")
    try:
        with pytest.raises(db.MigrationError) as excinfo:
            db.apply_migration(conn, migrations)
        assert "整体回滚" in str(excinfo.value)
        # 记账表是运行器自己兜底建的（基础设施），不属于业务表；探针表必须消失
        assert db.public_tables(conn) == {"schema_migration"}, "DDL 没有回滚，留下了半成品表"
        assert db.applied_versions(conn) == set(), "失败的版本不该被记账"
        assert db.migration_state(conn, db.discover_migrations(migrations)) is (
            db.MigrationState.EMPTY
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. 语句切分
# ---------------------------------------------------------------------------


def test_split_statements_handles_literals_and_comments() -> None:
    """字符串字面量与注释里的分号都不能切坏语句。"""
    script = (
        "-- 头注：这里的分号; 不该把语句切坏\n"
        "INSERT INTO t (a) VALUES ('x;y');\n"
        "INSERT INTO t (a) VALUES ('-- 不是注释;');\n"
    )
    statements = db.split_statements(script)
    assert len(statements) == 2, f"应切成 2 条语句，实际 {len(statements)} 条：{statements}"
    assert "'x;y'" in statements[0], "字符串字面量里的分号被当成了语句边界"
    assert "'-- 不是注释;'" in statements[1], "字面量里的 -- 被当成了注释"
    assert all(statement.endswith(";") for statement in statements)

    # 文件末尾只有注释时，不算「半截语句」
    assert len(db.split_statements("SELECT 1;\n-- 尾注; 只是注释\n")) == 1
    # 真的半截语句必须报错，而不是被当成合法脚本放过去
    with pytest.raises(db.MigrationError):
        db.split_statements("SELECT 1")


# ---------------------------------------------------------------------------
# 7. checksum 漂移
# ---------------------------------------------------------------------------


def test_checksum_drift_is_reported_but_not_applied(tmp_path: Path) -> None:
    """结构指纹：改注释不漂移、改 DDL 必须漂移，且漂移只被报告、不被执行。"""
    real_sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")

    # 单元级：整行注释不参与指纹（迁移文件头就是用来追加修订记录的）
    assert db.migration_checksum(f"-- 修订记录：无结构变化\n{real_sql}") == db.migration_checksum(
        real_sql
    )
    ddl_changed = f"{real_sql}\nCREATE INDEX probe_idx ON content_item (lang);\n"
    assert db.migration_checksum(ddl_changed) != db.migration_checksum(real_sql)

    # 用一个只含 0001 的临时迁移目录，这样"改文件"不会碰到真实仓库里的其他版本
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    target = migrations / "0001_init.sql"
    target.write_text(real_sql, encoding="utf-8")

    conn = db.connect(tmp_path / "drift.sqlite3")
    try:
        applied = db.apply_migration(conn, migrations)
        assert applied["applied"] == [1]
        assert applied["drifted"] == []

        target.write_text(f"-- 修订记录：无结构变化\n{real_sql}", encoding="utf-8")
        result = db.apply_migration(conn, migrations)
        assert result["applied"] == []
        assert result["drifted"] == [], "整行注释的增删不该被报成漂移"

        target.write_text(
            f"{real_sql}\nCREATE INDEX probe_drift_idx ON content_item (lang);\n",
            encoding="utf-8",
        )
        result = db.apply_migration(conn, migrations)
        assert result["applied"] == []
        assert result["drifted"] == [1], "改过 DDL 的已应用版本必须被报成漂移"
        # 漂移要被看见，不该被抹掉（记账仍然是原文件的指纹），也不该顺带执行
        assert db.recorded_checksum(conn, 1) == db.migration_checksum(real_sql)
        assert _scalar(
            conn, "SELECT count(*) FROM sqlite_master WHERE name = 'probe_drift_idx'"
        ) == 0, "漂移的迁移文件不该被执行"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8. FTS5 同步
# ---------------------------------------------------------------------------


def test_word_index_follows_insert_update_delete(behaviour_db: sqlite3.Connection) -> None:
    """触发器维持 content_word 与 content_item 同步：INSERT / UPDATE / DELETE 三态。"""
    _seed_source(behaviour_db)
    _seed_message(behaviour_db)
    _seed_content(behaviour_db, text_content="蓝发精灵在雨里", text_search="蓝发 精灵 雨")

    assert _match_count(behaviour_db, "content_word", "精灵") == 1

    behaviour_db.execute(
        "UPDATE content_item SET text_search = ? WHERE content_id = 1", ("红发 少女 雪",)
    )
    assert _match_count(behaviour_db, "content_word", "精灵") == 0, "UPDATE 后旧词仍被命中"
    assert _match_count(behaviour_db, "content_word", "红发") == 1, "UPDATE 后新词没有进索引"

    behaviour_db.execute("DELETE FROM content_item WHERE content_id = 1")
    assert _match_count(behaviour_db, "content_word", "少女") == 0, "DELETE 后索引行没有清掉"


# ---------------------------------------------------------------------------
# 9. trigram 的 3 字门槛（重要实测行为）
# ---------------------------------------------------------------------------


def test_trigram_needs_three_characters(behaviour_db: sqlite3.Connection) -> None:
    """DESIGN §5.1 写明的退化点：``trigram`` 索引在查询串少于 3 个字符时命中不了。

    中文里 2 字查询极常见（"蓝发"），因此 **2 字查询必须走 content_word 词级索引**，
    由应用层分词保证"蓝发"是一个词。这里用实测命中数把这条退化钉死：

    * ``content_fts``（trigram）MATCH '蓝发' —— 2 字，命中 **0**；
    * ``content_word``（unicode61）MATCH '蓝发' —— 命中 **1**；
    * ``content_fts``（trigram）MATCH '蓝发精' —— 满 3 字，命中 **1**。

    查询构造必须先看查询长度与分词结果，再决定查哪个索引；只查 trigram 就会让
    所有 2 字中文查询静默返回空结果。
    """
    _seed_source(behaviour_db)
    _seed_message(behaviour_db)
    _seed_content(behaviour_db, text_content="蓝发精灵在雨里", text_search="蓝发 精灵 雨")

    assert _match_count(behaviour_db, "content_fts", "蓝发") == 0
    assert _match_count(behaviour_db, "content_word", "蓝发") == 1
    assert _match_count(behaviour_db, "content_fts", "蓝发精") == 1


# ---------------------------------------------------------------------------
# 10. 重建索引
# ---------------------------------------------------------------------------


def test_reindex_fts_keeps_rows_and_hits(behaviour_db: sqlite3.Connection) -> None:
    """索引是派生数据，全量重建后行数与命中结果都不该变（DESIGN §9.1）。"""
    _seed_source(behaviour_db)
    _seed_message(behaviour_db)
    _seed_content(behaviour_db, text_content="蓝发精灵在雨里", text_search="蓝发 精灵 雨")

    rows_before = int(_scalar(behaviour_db, "SELECT count(*) FROM content_word"))
    assert rows_before == 1

    result = db.reindex_fts(behaviour_db)
    assert result["before"]["content_word"] == rows_before
    assert result["after"]["content_word"] == rows_before
    assert _scalar(behaviour_db, "SELECT count(*) FROM content_word") == rows_before

    assert _match_count(behaviour_db, "content_word", "精灵") == 1
    assert _match_count(behaviour_db, "content_fts", "蓝发精") == 1


# ---------------------------------------------------------------------------
# 11. 备份可恢复
# ---------------------------------------------------------------------------


def test_backup_is_restorable_and_refuses_to_overwrite(
    behaviour_db: sqlite3.Connection, tmp_path: Path
) -> None:
    """``VACUUM INTO`` 快照必须可打开、自洽、行数一致，且拒绝覆盖已存在的目标。"""
    _seed_source(behaviour_db)
    _seed_message(behaviour_db)
    _seed_content(behaviour_db)
    behaviour_db.execute(
        "INSERT INTO setting (key, value, value_type, source) "
        "VALUES ('probe.key', '1', 'int', 'db')"
    )

    target = tmp_path / "backup" / "pc-backup.sqlite3"
    result = db.backup_to(behaviour_db, target)
    assert result["integrity"] == "ok"
    assert int(result["size_bytes"]) > 0
    assert target.is_file()

    restored = db.connect(target)
    try:
        assert db.integrity_check(restored) == "ok"
        assert db.public_tables(restored) == db.public_tables(behaviour_db)
        for table in ("content_item", "setting"):
            assert _scalar(restored, f"SELECT count(*) FROM {table}") == _scalar(
                behaviour_db, f"SELECT count(*) FROM {table}"
            ), f"{table} 的行数在备份里对不上"
    finally:
        restored.close()

    # 静默覆盖备份目标是丢数据的经典方式，必须报错
    with pytest.raises(db.DatabaseError):
        db.backup_to(behaviour_db, target)


# ---------------------------------------------------------------------------
# 12. §3.8 的结构前提与引用计数
# ---------------------------------------------------------------------------


def test_media_reference_count_and_hash_tombstone(behaviour_db: sqlite3.Connection) -> None:
    """DESIGN §3.8：删内容项时靠 ``content_media`` 的引用计数决定 media 行去留。

    这里只断言**结构前提成立**：同一 media 行被两条内容项引用，删掉其中一条引用后
    计数仍为 1。删除策略本身（计数归零才删 media）属于业务逻辑，不在本文件范围。
    """
    _seed_source(behaviour_db)
    _seed_message(behaviour_db)
    _seed_content(behaviour_db, content_id=1, ordinal=0, text_search="蓝发")
    _seed_content(behaviour_db, content_id=2, ordinal=1, text_search="红发")
    _seed_media(behaviour_db, media_id=1)
    for content_id in (1, 2):
        behaviour_db.execute(
            "INSERT INTO content_media (content_id, media_id, role) VALUES (?, 1, 'primary')",
            (content_id,),
        )
    assert _scalar(behaviour_db, "SELECT count(*) FROM content_media WHERE media_id = 1") == 2

    behaviour_db.execute("DELETE FROM content_media WHERE content_id = 1")
    assert _scalar(behaviour_db, "SELECT count(*) FROM content_media WHERE media_id = 1") == 1
    assert _scalar(behaviour_db, "SELECT count(*) FROM media WHERE media_id = 1") == 1, (
        "引用计数仍为 1 时 media 行不该消失"
    )

    # 哈希墓地：只凭 hash + alg 就能查到，表里没有任何可关联到内容的列
    deleted_at = "2026-01-01T00:00:00.000Z"
    behaviour_db.execute(
        "INSERT INTO hash_tombstone (hash, alg, deleted_at) VALUES (?, 'sha256', ?)",
        (SHA256, deleted_at),
    )
    assert _scalar(
        behaviour_db,
        "SELECT count(*) FROM hash_tombstone WHERE hash = ? AND alg = ?",
        SHA256,
        "sha256",
    ) == 1

    columns = {str(r[1]) for r in behaviour_db.execute("PRAGMA table_info(hash_tombstone)")}
    assert columns == {"hash", "alg", "deleted_at", "expires_at"}, (
        f"hash_tombstone 的列集合应恰好是这四列，实际 {sorted(columns)}"
    )
    forbidden = ("content", "media", "message", "source", "uuid", "object_key", "url")
    assert not any(token in column for column in columns for token in forbidden), (
        f"hash_tombstone 出现了可关联到内容的列：{sorted(columns)}"
    )
