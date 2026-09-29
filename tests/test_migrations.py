"""迁移器（多文件）的契约测试。

`migrations/README.md` 说"按序号追加"，而适配器必须真的做到：按序应用、每个文件
一个事务、只应用未记录的版本、逐版本检出结构指纹漂移。这组测试钉的就是这些承诺——
它们是**任何未来 schema 改动的前提**，写坏一条就等于把后续迁移的路堵死。
"""

from __future__ import annotations

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
