"""迁移器（多文件）的契约测试。

`migrations/README.md` 说"按序号追加"，而适配器必须真的做到：按序应用、每个文件
一个事务、只应用未记录的版本、逐版本检出结构指纹漂移。这组测试钉的就是这些承诺——
它们是**任何未来 schema 改动的前提**，写坏一条就等于把后续迁移的路堵死。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pc.adapters import sqlite as db


def _write(directory: Path, version: int, slug: str, sql: str) -> Path:
    path = directory / f"{version:04d}_{slug}.sql"
    path.write_text(sql, encoding="utf-8")
    return path


def _connect(tmp_path: Path) -> Any:
    return db.connect(tmp_path / "migrate.sqlite3")


# ---------------------------------------------------------------------------
# 发现与校验
# ---------------------------------------------------------------------------


def test_discovers_in_version_order(tmp_path: Path) -> None:
    _write(tmp_path, 2, "b", "CREATE TABLE probe_two (a INTEGER);")
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    files = db.discover_migrations(tmp_path)
    assert [f.version for f in files] == [1, 2]
    assert [f.name for f in files] == ["0001_a.sql", "0002_b.sql"]


def test_missing_directory_is_a_human_error(tmp_path: Path) -> None:
    with pytest.raises(db.MigrationError, match="找不到迁移目录"):
        db.discover_migrations(tmp_path / "nope")


def test_empty_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(db.MigrationError, match=r"没有任何 \.sql 文件"):
        db.discover_migrations(tmp_path)


def test_version_gap_is_rejected(tmp_path: Path) -> None:
    """断号必须报错：断号会让「哪些已应用」变得不可回答。"""
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    _write(tmp_path, 3, "c", "CREATE TABLE probe_three (a INTEGER);")
    with pytest.raises(db.MigrationError, match="必须从 1 连续"):
        db.discover_migrations(tmp_path)


def test_duplicate_version_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    _write(tmp_path, 1, "b", "CREATE TABLE probe_other (a INTEGER);")
    with pytest.raises(db.MigrationError, match="出现了两个文件"):
        db.discover_migrations(tmp_path)


def test_bad_filename_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "init.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(db.MigrationError, match="文件名不合规"):
        db.discover_migrations(tmp_path)


def test_non_sql_files_are_ignored(tmp_path: Path) -> None:
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    (tmp_path / "notes.txt").write_text("不是迁移", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"\x00")
    assert [f.version for f in db.discover_migrations(tmp_path)] == [1]


def test_latest_version_comes_from_the_directory(tmp_path: Path) -> None:
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    _write(tmp_path, 2, "b", "CREATE TABLE probe_two (a INTEGER);")
    assert db.latest_migration_version(tmp_path) == 2


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------


def test_applies_all_versions_in_order(tmp_path: Path) -> None:
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    _write(tmp_path, 2, "b", "CREATE TABLE probe_two (a INTEGER);")
    conn = _connect(tmp_path)
    try:
        result = db.apply_migration(conn, tmp_path)
        assert result["applied"] == [1, 2]
        assert result["statements"] == 2
        assert db.applied_versions(conn) == {1, 2}
        assert db.recorded_checksum(conn, 2) == db.migration_checksum(
            (tmp_path / "0002_b.sql").read_text(encoding="utf-8")
        )
        assert db.migration_state(conn, db.discover_migrations(tmp_path)) is (
            db.MigrationState.APPLIED
        )
    finally:
        conn.close()


def test_only_missing_versions_are_applied(tmp_path: Path) -> None:
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    conn = _connect(tmp_path)
    try:
        assert db.apply_migration(conn, tmp_path)["applied"] == [1]

        _write(tmp_path, 2, "b", "CREATE TABLE probe_two (a INTEGER);")
        # 应用了 1 但还缺 2：这是**正常**的升级路径，不是脏库
        assert db.migration_state(conn, db.discover_migrations(tmp_path)) is (
            db.MigrationState.PENDING
        )
        second = db.apply_migration(conn, tmp_path)
        assert second["applied"] == [2]
        assert second["skipped"] == [1]
        assert db.applied_versions(conn) == {1, 2}

        third = db.apply_migration(conn, tmp_path)
        assert third["applied"] == []
        assert third["skipped"] == [1, 2]
    finally:
        conn.close()


def test_failure_in_one_file_keeps_the_earlier_ones(tmp_path: Path) -> None:
    """每个文件一个事务：第 2 个文件炸了，第 1 个仍然有效。"""
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    _write(tmp_path, 2, "b", "CREATE TABLE probe_two (a INTEGER);\n这行不是 SQL;\n")
    conn = _connect(tmp_path)
    try:
        with pytest.raises(db.MigrationError) as excinfo:
            db.apply_migration(conn, tmp_path)
        assert "整体回滚" in str(excinfo.value)
        assert "之前成功的版本仍然有效" in str(excinfo.value)
        assert db.applied_versions(conn) == {1}, "第 1 个版本不该被第 2 个的失败连累"
        assert "probe_one" in db.public_tables(conn)
        assert "probe_two" not in db.public_tables(conn), "失败的文件必须整体回滚"
    finally:
        conn.close()


def test_dirty_database_is_refused_without_guessing(tmp_path: Path) -> None:
    """有业务表却一条记账都没有 = 脏库：报错，不猜、不修。"""
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    conn = _connect(tmp_path)
    try:
        conn.execute("CREATE TABLE source (source_id INTEGER PRIMARY KEY)")
        assert db.migration_state(conn, db.discover_migrations(tmp_path)) is (
            db.MigrationState.PARTIAL
        )
        with pytest.raises(db.SchemaMismatchError, match="半成品状态"):
            db.apply_migration(conn, tmp_path)
    finally:
        conn.close()


def test_drift_is_reported_per_version(tmp_path: Path) -> None:
    """两个版本各自比对指纹：改其一，只报那一个。"""
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    _write(tmp_path, 2, "b", "CREATE TABLE probe_two (a INTEGER);")
    conn = _connect(tmp_path)
    try:
        db.apply_migration(conn, tmp_path)
        _write(tmp_path, 2, "b", "CREATE TABLE probe_two (a INTEGER, b INTEGER);")
        result = db.apply_migration(conn, tmp_path)
        assert result["applied"] == []
        assert result["skipped"] == [1, 2]
        assert result["drifted"] == [2], f"只有被改过的版本该报漂移：{result}"
    finally:
        conn.close()


def test_checksum_is_backfilled_when_missing(tmp_path: Path) -> None:
    """记账行存在但 checksum 为空（老库或人工插入）时补记，而不是报漂移。"""
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    conn = _connect(tmp_path)
    try:
        db.apply_migration(conn, tmp_path)
        conn.execute("UPDATE schema_migration SET checksum = NULL WHERE version = 1")
        result = db.apply_migration(conn, tmp_path)
        assert result["checksum_backfilled"] == [1]
        assert result["drifted"] == []
        assert db.recorded_checksum(conn, 1) == db.migration_checksum(
            (tmp_path / "0001_a.sql").read_text(encoding="utf-8")
        )
    finally:
        conn.close()


def test_unknown_version_in_database_is_visible(tmp_path: Path) -> None:
    """库里的版本比迁移目录新（程序比库旧）必须能被看出来，而不是当作一致。"""
    _write(tmp_path, 1, "a", "CREATE TABLE probe_one (a INTEGER);")
    conn = _connect(tmp_path)
    try:
        db.apply_migration(conn, tmp_path)
        conn.execute(
            "INSERT INTO schema_migration (version, name, checksum) VALUES (9, '0009_future.sql', 'x')"
        )
        status = db.database_status(conn, tmp_path)
        assert status["migrations_unknown"] == (9,)
        assert status["ok"] is False, "库里出现未知版本时不能声称结构就绪"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 指令注释与表重建
#
# SQLite 改不了 CHECK 约束，只能重建表；而重建必须在关闭外键的情况下进行。这组测试
# 钉住那套机制：指令被正确识别（打错字必须报错）、重建后数据与外键都还在、真出现
# 外键违例时整体回滚、以及"没有指令就真的会失败"——最后一条证明指令不是装饰。
# ---------------------------------------------------------------------------

_PARENT_DDL = """
CREATE TABLE parent (
    parent_id INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    CONSTRAINT parent_kind_chk CHECK (kind IN ('one', 'two'))
) STRICT;
"""

_CHILD_DDL = """
CREATE TABLE child (
    child_id  INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL REFERENCES parent (parent_id)
) STRICT;
"""

_PARENT_INDEX = "CREATE INDEX parent_name_idx ON parent (name);"


def _rebuild_sql(*, copy_where: str = "", directive: bool = True) -> str:
    """一段"放宽 parent.kind 枚举"的重建迁移。"""
    header = "-- pc:foreign_keys=off\n" if directive else ""
    return (
        header
        + """
CREATE TABLE parent_rebuild (
    parent_id INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    CONSTRAINT parent_kind_chk CHECK (kind IN ('one', 'two', 'three'))
) STRICT;
INSERT INTO parent_rebuild SELECT * FROM parent COPY_WHERE;
DROP TABLE parent;
ALTER TABLE parent_rebuild RENAME TO parent;
"""
        + _PARENT_INDEX
        + "\n"
    ).replace("COPY_WHERE", copy_where)


def _seed(tmp_path: Path) -> Any:
    """建一个"老 schema + 一行父 + 一行子"的库，返回连接。"""
    migrations = tmp_path / "migrations"
    migrations.mkdir(exist_ok=True)
    _write(migrations, 1, "init", f"{_PARENT_DDL}{_CHILD_DDL}{_PARENT_INDEX}")
    conn = _connect(tmp_path)
    db.apply_migration(conn, migrations)
    conn.execute("INSERT INTO parent (name, kind) VALUES ('a', 'one')")
    conn.execute("INSERT INTO child (parent_id) VALUES (1)")
    return conn


class TestDirectiveParsing:
    def test_off_is_recognized(self) -> None:
        assert db.migration_directives("-- pc:foreign_keys=off\nSELECT 1;") == {
            "foreign_keys": "off"
        }

    def test_spaces_around_equals_are_tolerated(self) -> None:
        assert db.migration_directives("--   pc:foreign_keys = off  \nSELECT 1;") == {
            "foreign_keys": "off"
        }

    def test_typo_in_the_name_is_rejected(self) -> None:
        """打错一个字母必须报错：静默失效会让重建在开着外键时跑，报的错与原因无关。"""
        with pytest.raises(db.MigrationError, match="无法识别的指令注释"):
            db.migration_directives("-- pc:foreign_key=off\nSELECT 1;")

    def test_unknown_value_is_rejected(self) -> None:
        with pytest.raises(db.MigrationError, match="取值不认识"):
            db.migration_directives("-- pc:foreign_keys=on\nSELECT 1;")

    def test_directive_does_not_affect_the_checksum(self) -> None:
        """指令是注释：往文件里加它不该让结构指纹漂移。"""
        body = "CREATE TABLE probe (a INTEGER);"
        assert db.migration_checksum(f"-- pc:foreign_keys=off\n{body}") == db.migration_checksum(body)

    def test_no_directive_means_no_directive(self) -> None:
        assert db.migration_directives("-- 普通注释\nSELECT 1;") == {}


class TestTableRebuild:
    def test_rebuild_keeps_rows_and_widens_the_check(self, tmp_path: Path) -> None:
        conn = _seed(tmp_path)
        try:
            _write(tmp_path / "migrations", 2, "rebuild", _rebuild_sql())
            result = db.apply_migration(conn, tmp_path / "migrations")
            assert result["applied"] == [2]

            assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 1
            assert tuple(conn.execute("SELECT name, kind FROM parent").fetchone()) == ("a", "one")
            # 新取值现在被接受，旧取值当然还接受
            conn.execute("INSERT INTO parent (name, kind) VALUES ('b', 'three')")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO parent (name, kind) VALUES ('c', 'four')")
        finally:
            conn.close()

    def test_rebuild_keeps_the_child_foreign_key_working(self, tmp_path: Path) -> None:
        """重建父表之后，子表的外键必须仍然指向它——否则约束就静默失效了。"""
        conn = _seed(tmp_path)
        try:
            _write(tmp_path / "migrations", 2, "rebuild", _rebuild_sql())
            db.apply_migration(conn, tmp_path / "migrations")
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO child (parent_id) VALUES (999)")
        finally:
            conn.close()

    def test_rebuild_restores_the_indexes(self, tmp_path: Path) -> None:
        conn = _seed(tmp_path)
        try:
            _write(tmp_path / "migrations", 2, "rebuild", _rebuild_sql())
            db.apply_migration(conn, tmp_path / "migrations")
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'parent'"
                )
            }
            assert "parent_name_idx" in names, "重建时漏了索引——它跟着旧表一起被删掉了"
        finally:
            conn.close()

    def test_foreign_keys_is_restored_afterwards(self, tmp_path: Path) -> None:
        conn = _seed(tmp_path)
        try:
            _write(tmp_path / "migrations", 2, "rebuild", _rebuild_sql())
            db.apply_migration(conn, tmp_path / "migrations")
            assert db.pragma_value(conn, "foreign_keys") == "1", "迁移结束后外键必须恢复为 ON"
        finally:
            conn.close()

    def test_rebuild_without_the_directive_fails(self, tmp_path: Path) -> None:
        """没有指令就真的会失败——这条证明指令不是装饰，也解释了它为什么存在。"""
        conn = _seed(tmp_path)
        try:
            _write(tmp_path / "migrations", 2, "rebuild", _rebuild_sql(directive=False))
            with pytest.raises(db.MigrationError):
                db.apply_migration(conn, tmp_path / "migrations")
            # 失败要干净：旧表原样、版本没记账、外键仍是开的
            assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 1
            assert db.applied_versions(conn) == {1}
            assert db.pragma_value(conn, "foreign_keys") == "1"
        finally:
            conn.close()

    def test_foreign_key_violation_rolls_the_whole_migration_back(self, tmp_path: Path) -> None:
        """重建把父行丢了 → 子行变成孤儿 → 提交前的 foreign_key_check 必须拦下并回滚。"""
        conn = _seed(tmp_path)
        try:
            # 只拷 parent_id > 1 的行，于是 child 里那一行成了孤儿
            _write(tmp_path / "migrations", 2, "rebuild", _rebuild_sql(copy_where="WHERE parent_id > 1"))
            with pytest.raises(db.MigrationError, match="外键违例"):
                db.apply_migration(conn, tmp_path / "migrations")
            assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 1, (
                "整体回滚之后旧表必须原样还在"
            )
            assert conn.execute("SELECT count(*) FROM child").fetchone()[0] == 1
            assert db.applied_versions(conn) == {1}
            assert db.pragma_value(conn, "foreign_keys") == "1"
        finally:
            conn.close()

    def test_rebuild_failure_does_not_hide_behind_the_restore(self, tmp_path: Path) -> None:
        """重建失败时，报出来的必须是**重建的错误**，而不是恢复 PRAGMA 时顺带发现的错。

        这条防的是：`finally` 里的恢复动作把原始异常顶掉，排查方向被带偏。
        """
        conn = _seed(tmp_path)
        try:
            broken = _rebuild_sql() + "THIS IS NOT SQL;\n"
            _write(tmp_path / "migrations", 2, "rebuild", broken)
            with pytest.raises(db.MigrationError) as excinfo:
                db.apply_migration(conn, tmp_path / "migrations")
            assert "THIS IS NOT SQL" in str(excinfo.value) or "near" in str(excinfo.value)
            assert "无法把 foreign_keys" not in str(excinfo.value)
        finally:
            conn.close()


def test_set_foreign_keys_refuses_inside_a_transaction(tmp_path: Path) -> None:
    """``PRAGMA foreign_keys`` 在事务里是空操作——必须被检出，而不是假装成功。"""
    conn = _connect(tmp_path)
    try:
        with (
            pytest.raises(db.MigrationError, match="无法把 foreign_keys 切到 OFF"),
            db.transaction(conn),
        ):
            db.set_foreign_keys(conn, False)
    finally:
        conn.close()
