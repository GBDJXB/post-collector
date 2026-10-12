"""SQLite 适配器单元测试：不碰真实数据库文件的服务依赖，任何时候都必须通过。

覆盖从 PostgreSQL 版沿用下来的纯函数（``.env`` 解析、迁移指纹），
以及 SQLite 版新增的部分：数据库**文件路径**的来源优先级、平台默认目录、
迁移语句切分、版本门槛、FTS5 影子表识别、时间/备份命名。
外加「迁移文件与清单一致」的结构检查——它能在不连库的情况下发现
「往迁移里加了表却忘了更新 ``EXPECTED_TABLES``」。

已随引擎更换而删除的用例（概念不复存在）：
``mask_dsn`` 的 5 条（不再有连接串，也就没有密码要遮）与
``resolve_dsn`` 的 6 条优先级用例（被下面的 ``TestResolveDbPath`` 取代）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pc.adapters import sqlite as db

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 迁移目录，用来核对 ``EXPECTED_TABLES``（必须扫**全部**版本，而不只是 0001）
MIGRATIONS_DIR = REPO_ROOT / db.DEFAULT_MIGRATIONS_DIR


def _read_migration() -> str:
    """把迁移目录里所有 ``.sql`` 按序拼起来，供"声明的表集合"这类断言使用。"""
    return "\n".join(
        migration.path.read_text(encoding="utf-8")
        for migration in db.discover_migrations(MIGRATIONS_DIR)
    )


class TestReadEnvFile:
    def test_parses_values_and_strips_quotes(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "# 注释行\n"
            "\n"
            "PC_DATA_ROOT=/srv/pc\n"
            'QUOTED="a b"\n'
            "SINGLE='x'\n"
            "export EXPORTED=1\n",
            encoding="utf-8",
        )
        assert db.read_env_file(env) == {
            "PC_DATA_ROOT": "/srv/pc",
            "QUOTED": "a b",
            "SINGLE": "x",
            "EXPORTED": "1",
        }

    def test_handles_crlf(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_bytes(b"A=1\r\nB=2\r\n")
        assert db.read_env_file(env) == {"A": "1", "B": "2"}

    def test_missing_file_and_none(self, tmp_path: Path) -> None:
        assert db.read_env_file(tmp_path / "nope.env") == {}
        assert db.read_env_file(None) == {}

    def test_ignores_noise_lines(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "# 整行注释\n"
            "NOT_A_PAIR\n"
            "=VALUE_WITHOUT_KEY\n"
            "SPACED = trimmed \n",
            encoding="utf-8",
        )
        assert db.read_env_file(env) == {"SPACED": "trimmed"}


class TestResolveDbPath:
    """路径来源优先级：explicit > 环境变量 > .env > app.yaml > 平台默认。

    全部通过 ``env={...}`` 注入，绝不读真实环境变量；``.env`` 与 ``app.yaml``
    都在 ``tmp_path`` 里现造，不依赖仓库里的真文件。
    """

    def test_explicit_wins_over_environment_and_files(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            f"PC__DB__PATH={tmp_path / 'from_env_file.sqlite3'}\n", encoding="utf-8"
        )
        resolved = db.resolve_db_path(
            tmp_path / "explicit.sqlite3",
            env={"PC__DB__PATH": str(tmp_path / "from_env.sqlite3")},
            env_file=env_file,
            app_config=tmp_path / "none",
        )
        assert resolved.source == "explicit"
        assert resolved.path == tmp_path / "explicit.sqlite3"

    def test_env_db_path_is_a_file_and_beats_data_root(self, tmp_path: Path) -> None:
        target = tmp_path / "custom.sqlite3"
        resolved = db.resolve_db_path(
            env={"PC__DB__PATH": str(target), "PC_DATA_ROOT": str(tmp_path / "data")},
            env_file=tmp_path / "none",
            app_config=tmp_path / "none",
        )
        assert resolved.source == "env:PC__DB__PATH"
        assert resolved.path == target

    def test_env_data_root_gets_the_fixed_filename(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        resolved = db.resolve_db_path(
            env={"PC_DATA_ROOT": str(root)},
            env_file=tmp_path / "none",
            app_config=tmp_path / "none",
        )
        assert resolved.source == "env:PC_DATA_ROOT"
        assert resolved.path == root / db.DB_FILENAME
        assert resolved.path.name == "pc.sqlite3"

    def test_env_file_data_root_is_honoured(self, tmp_path: Path) -> None:
        root = tmp_path / "from_dot_env"
        env_file = tmp_path / ".env"
        env_file.write_text(f"PC_DATA_ROOT={root}\n", encoding="utf-8")
        resolved = db.resolve_db_path(
            env={}, env_file=env_file, app_config=tmp_path / "none"
        )
        assert resolved.source == "env-file:PC_DATA_ROOT"
        assert resolved.path == root / db.DB_FILENAME

    def test_app_config_db_path_is_used(self, tmp_path: Path) -> None:
        pytest.importorskip("yaml")
        target = tmp_path / "from_yaml.sqlite3"
        app_config = tmp_path / "app.yaml"
        app_config.write_text(f"db:\n  path: {target.as_posix()}\n", encoding="utf-8")
        resolved = db.resolve_db_path(
            env={}, env_file=tmp_path / "none", app_config=app_config
        )
        assert resolved.source == f"{app_config.as_posix()}:db.path"
        assert resolved.path == target

    def test_app_config_db_data_root_is_used(self, tmp_path: Path) -> None:
        pytest.importorskip("yaml")
        root = tmp_path / "yaml_data"
        app_config = tmp_path / "app.yaml"
        app_config.write_text(f"db:\n  data_root: {root.as_posix()}\n", encoding="utf-8")
        resolved = db.resolve_db_path(
            env={}, env_file=tmp_path / "none", app_config=app_config
        )
        assert resolved.source == f"{app_config.as_posix()}:db.data_root"
        assert resolved.path == root / db.DB_FILENAME

    def test_environment_beats_the_config_file(self, tmp_path: Path) -> None:
        pytest.importorskip("yaml")
        app_config = tmp_path / "app.yaml"
        app_config.write_text(
            f"db:\n  path: {(tmp_path / 'from_yaml.sqlite3').as_posix()}\n", encoding="utf-8"
        )
        env_root = tmp_path / "env_data"
        resolved = db.resolve_db_path(
            env={"PC_DATA_ROOT": str(env_root)},
            env_file=tmp_path / "none",
            app_config=app_config,
        )
        assert resolved.source == "env:PC_DATA_ROOT"
        assert resolved.path == env_root / db.DB_FILENAME

    def test_environment_layer_beats_the_env_file_layer(self, tmp_path: Path) -> None:
        """优先级是「层高于层」：环境变量里什么都没命中时，才轮到 ``.env``。

        这条曾经钉着一个真实缺陷：适配器把进程环境与 ``.env`` 按同一层逐个键去查，
        于是 ``.env`` 里的 ``PC__DB__PATH`` 压过了进程环境的 ``PC_DATA_ROOT``。
        现在 lookup 拆成两层依次查，本用例是它的回归钉子。
        """
        env_file = tmp_path / ".env"
        env_file.write_text(
            f"PC__DB__PATH={tmp_path / 'from_env_file.sqlite3'}\n", encoding="utf-8"
        )
        env_root = tmp_path / "env_data"
        resolved = db.resolve_db_path(
            env={"PC_DATA_ROOT": str(env_root)},
            env_file=env_file,
            app_config=tmp_path / "none",
        )
        assert resolved.source == "env:PC_DATA_ROOT"
        assert resolved.path == env_root / db.DB_FILENAME

    def test_falls_back_to_the_platform_default(self, tmp_path: Path) -> None:
        resolved = db.resolve_db_path(
            env={}, env_file=tmp_path / "none", app_config=tmp_path / "none"
        )
        assert resolved.source == "default"
        assert resolved.path == db.default_data_root(env={}) / db.DB_FILENAME
        assert resolved.path.name == "pc.sqlite3"


class TestDefaultDataRoot:
    def test_windows_uses_localappdata(self, tmp_path: Path) -> None:
        base = tmp_path / "LocalAppData"
        assert db.default_data_root(
            "win32", env={"LOCALAPPDATA": str(base)}, home=tmp_path / "home"
        ) == (base / "post-collector")

    def test_windows_falls_back_to_home(self, tmp_path: Path) -> None:
        assert db.default_data_root("win32", env={}, home=tmp_path / "home") == (
            tmp_path / "home" / "AppData" / "Local" / "post-collector"
        )

    def test_macos_uses_application_support(self, tmp_path: Path) -> None:
        assert db.default_data_root("darwin", env={}, home=tmp_path / "home") == (
            tmp_path / "home" / "Library" / "Application Support" / "post-collector"
        )

    def test_linux_prefers_xdg_data_home(self, tmp_path: Path) -> None:
        base = tmp_path / "xdg"
        assert db.default_data_root(
            "linux", env={"XDG_DATA_HOME": str(base)}, home=tmp_path / "home"
        ) == (base / "post-collector")

    def test_linux_falls_back_to_local_share(self, tmp_path: Path) -> None:
        assert db.default_data_root("linux", env={}, home=tmp_path / "home") == (
            tmp_path / "home" / ".local" / "share" / "post-collector"
        )


class TestMigrationChecksum:
    def test_comment_edits_do_not_change_fingerprint(self) -> None:
        """文件头是拿来追加修订记录的，注释变更不该被报成「文件被改过」。"""
        plain = "CREATE TABLE t (x int);\n"
        annotated = "-- 2026-09-28 修订记录\nCREATE TABLE t (x int);\n\n"
        assert db.migration_checksum(plain) == db.migration_checksum(annotated)

    def test_trailing_comment_counts_as_content(self) -> None:
        """保守取向：行尾注释算内容，宁可多报一次漂移，也不漏掉真正的 DDL 改动。"""
        assert db.migration_checksum("CREATE TABLE t (x int);") != db.migration_checksum(
            "CREATE TABLE t (x int); -- 行尾说明"
        )

    def test_ddl_change_changes_fingerprint(self) -> None:
        assert db.migration_checksum("CREATE TABLE t (x int);") != db.migration_checksum(
            "CREATE TABLE t (x bigint);"
        )


class TestSplitStatements:
    def test_semicolon_inside_a_string_literal_is_kept(self) -> None:
        """朴素 ``split(';')`` 会在这里切坏；必须靠 ``complete_statement`` 判断。"""
        sql = "INSERT INTO t (x) VALUES ('a;b');\nINSERT INTO t (x) VALUES ('c');\n"
        statements = db.split_statements(sql)
        assert len(statements) == 2
        assert "'a;b'" in statements[0]

    def test_comment_line_does_not_split(self) -> None:
        sql = "-- 表说明; 注释里的分号不算语句结束\nCREATE TABLE t (x TEXT);\n"
        statements = db.split_statements(sql)
        assert len(statements) == 1
        assert statements[0].endswith("CREATE TABLE t (x TEXT);")

    def test_half_statement_raises(self) -> None:
        """最后一条缺少分号：宁可报错，也不猜着执行半截语句。"""
        with pytest.raises(db.MigrationError, match="分号"):
            db.split_statements("CREATE TABLE t (x int);\nINSERT INTO t (x) VALUES (1)\n")

    def test_pragma_is_rejected(self) -> None:
        """PRAGMA 由连接基线负责，写进迁移文件会在事务里报错。"""
        with pytest.raises(db.MigrationError, match="PRAGMA"):
            db.split_statements("PRAGMA journal_mode = WAL;\n")


class TestVersionGate:
    def test_below_the_gate_is_rejected_with_human_readable_advice(self) -> None:
        assert db.MIN_SQLITE_VERSION == (3, 40)
        assert db.sqlite_version_ok((3, 39)) is False
        with pytest.raises(db.DatabaseUnavailableError, match="单文件包"):
            db.ensure_version_gate((3, 39))

    def test_at_or_above_the_gate_passes(self) -> None:
        assert db.sqlite_version_ok((3, 40)) is True
        assert db.sqlite_version_ok((3, 50, 4)) is True
        # 达到门槛时静默通过，不抛异常
        assert db.ensure_version_gate((3, 40)) is None
        assert db.ensure_version_gate((3, 50, 4)) is None


class TestFtsShadowTables:
    def test_shadow_tables_are_recognised(self) -> None:
        assert db.is_fts_shadow_table("content_fts_data") is True
        assert db.is_fts_shadow_table("content_word_idx") is True

    def test_virtual_tables_and_business_tables_are_not_shadow(self) -> None:
        assert db.is_fts_shadow_table("content_fts") is False
        assert db.is_fts_shadow_table("content_item") is False


class TestNames:
    def test_now_iso_shape(self) -> None:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", db.now_iso())

    def test_backup_filename_shape(self) -> None:
        assert db.backup_filename(datetime(2026, 1, 1, tzinfo=UTC)) == (
            "pc-20260101T000000Z.sqlite3"
        )
        assert re.fullmatch(r"pc-\d{8}T\d{6}Z\.sqlite3", db.backup_filename())


class TestTableInventory:
    def test_matches_tables_declared_in_migration(self) -> None:
        """清单与迁移文件必须同步；只改一边时这条测试会红。

        **重建用的临时表**（以 ``_rebuild`` 结尾）不算 —— SQLite 改不了 CHECK，只能
        建临时表、拷数据、删旧表、改名。约定写在 ``migrations/README.md``：临时表一律
        以 ``_rebuild`` 结尾，且必须在同一个文件里改名成正式表。改名漏了的话，
        ``test_schema_integration`` 里"库里不该有 _rebuild 表"那条会红。
        """
        declared = {
            name
            for name in re.findall(
                r"(?m)^CREATE TABLE (?:IF NOT EXISTS )?([a-z_][a-z0-9_]*)", _read_migration()
            )
            if not name.endswith("_rebuild")
        }
        assert declared == set(db.EXPECTED_TABLES)

    def test_expected_shape(self) -> None:
        assert len(db.EXPECTED_TABLES) == 39
        assert "schema_migration" in db.EXPECTED_TABLES
        # 相对 PostgreSQL 版新增的两张表（DESIGN §3.2、§3.8）
        assert "content_media" in db.EXPECTED_TABLES
        assert "hash_tombstone" in db.EXPECTED_TABLES
        # 0007 新增的派生表：一帖一行，检索的结果单位（DESIGN §4.4、§5.1）
        assert "post" in db.EXPECTED_TABLES

    def test_deferred_tables_are_disjoint(self) -> None:
        assert {"content_embedding", "ai_provider_policy"} == set(
            db.DEFERRED_TABLES
        )
        assert not (db.EXPECTED_TABLES & db.DEFERRED_TABLES)

    def test_reserved_word_renames_stay_in_place(self) -> None:
        """三处保留字改名必须仍在：``user`` / ``before``+``after`` / ``authorization``。"""
        assert "app_user" in db.EXPECTED_TABLES
        assert "user" not in db.EXPECTED_TABLES
        assert "source_authorization" in db.EXPECTED_TABLES
        assert "authorization" not in db.EXPECTED_TABLES
        # before / after 是列名而不是表名：audit_log 与 setting_change 各有一对
        sql = _read_migration()
        assert sql.count("before_state") >= 2
        assert sql.count("after_state") >= 2
        assert re.search(r"(?m)^\s*(before|after)\s+(TEXT|INTEGER|JSONB)", sql) is None
