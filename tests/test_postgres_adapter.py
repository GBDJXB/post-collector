"""适配器层单元测试：不碰数据库，任何时候都必须通过。

覆盖三块最容易出错的纯函数：连接串脱敏、``.env`` 解析、来源优先级。
外加一条「迁移文件与清单一致」的结构检查——它能在不连库的情况下发现
「往迁移里加了表却忘了更新 ``EXPECTED_TABLES``」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pc.adapters import postgres as pg

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestMaskDsn:
    def test_masks_url_password(self) -> None:
        assert (
            pg.mask_dsn("postgresql://pc:s3cret@localhost:5432/pc")
            == "postgresql://pc:***@localhost:5432/pc"
        )

    def test_keeps_host_port_and_database(self) -> None:
        masked = pg.mask_dsn("postgresql://user:pw@db.example.com:6000/mydb")
        assert masked == "postgresql://user:***@db.example.com:6000/mydb"

    def test_masks_keyword_form(self) -> None:
        masked = pg.mask_dsn("host=localhost password=s3cret dbname=pc")
        assert "s3cret" not in masked
        assert "password=***" in masked

    def test_without_password_is_unchanged(self) -> None:
        assert pg.mask_dsn("postgresql://localhost:5432/pc") == "postgresql://localhost:5432/pc"

    def test_never_leaks_password_on_unparsable_input(self) -> None:
        masked = pg.mask_dsn("postgresql://pc:s3cret@host:not-a-port/pc")
        assert "s3cret" not in masked


class TestReadEnvFile:
    def test_parses_values_and_skips_noise(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text(
            "# 注释行\n"
            "\n"
            "POSTGRES_USER=pc\n"
            'QUOTED="a b"\n'
            "SINGLE='x'\n"
            "export EXPORTED=1\n"
            "NO_EQUALS_SIGN\n",
            encoding="utf-8",
        )
        assert pg.read_env_file(env) == {
            "POSTGRES_USER": "pc",
            "QUOTED": "a b",
            "SINGLE": "x",
            "EXPORTED": "1",
        }

    def test_handles_crlf(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_bytes(b"A=1\r\nB=2\r\n")
        assert pg.read_env_file(env) == {"A": "1", "B": "2"}

    def test_missing_file_and_none(self, tmp_path: Path) -> None:
        assert pg.read_env_file(tmp_path / "nope.env") == {}
        assert pg.read_env_file(None) == {}


class TestResolveDsn:
    def test_explicit_wins(self, tmp_path: Path) -> None:
        resolved = pg.resolve_dsn(
            "postgresql://x@explicit/db",
            env={"PC__DB__DSN": "postgresql://x@env/db"},
            env_file=tmp_path / "none",
            app_config=tmp_path / "none",
        )
        assert resolved.source == "explicit"
        assert resolved.dsn.endswith("@explicit/db")

    def test_env_container_dsn_wins_over_test_dsn(self, tmp_path: Path) -> None:
        resolved = pg.resolve_dsn(
            env={
                "PC__DB__DSN": "postgresql://x@db:5432/pc",
                "PC_TEST_DSN": "postgresql://x@localhost:5432/pc",
            },
            env_file=tmp_path / "none",
            app_config=tmp_path / "none",
        )
        assert resolved.source == "env:PC__DB__DSN"
        assert "@db:5432" in resolved.dsn

    def test_env_test_dsn_used_on_host(self, tmp_path: Path) -> None:
        resolved = pg.resolve_dsn(
            env={"PC_TEST_DSN": "postgresql://x@localhost:5432/pc"},
            env_file=tmp_path / "none",
            app_config=tmp_path / "none",
        )
        assert resolved.source == "env:PC_TEST_DSN"

    def test_env_file_test_dsn_used(self, tmp_path: Path) -> None:
        env = tmp_path / ".env"
        env.write_text("PC_TEST_DSN=postgresql://x@localhost:5432/pc\n", encoding="utf-8")
        resolved = pg.resolve_dsn(env={}, env_file=env, app_config=tmp_path / "none")
        assert resolved.source == "env-file:PC_TEST_DSN"

    def test_env_file_container_dsn_is_ignored(self, tmp_path: Path) -> None:
        """``.env`` 里的 PC__DB__DSN 指向容器服务名 db，宿主机侧解析不了，必须忽略。"""
        env = tmp_path / ".env"
        env.write_text("PC__DB__DSN=postgresql://x@db:5432/pc\n", encoding="utf-8")
        resolved = pg.resolve_dsn(env={}, env_file=env, app_config=tmp_path / "none")
        assert resolved.source == "default"
        assert resolved.dsn == pg.DEFAULT_DSN

    def test_app_config_used_when_no_env(self, tmp_path: Path) -> None:
        pytest.importorskip("yaml")
        app_config = tmp_path / "app.yaml"
        app_config.write_text(
            "db:\n  dsn: postgresql://x@from-yaml:5432/pc\n", encoding="utf-8"
        )
        resolved = pg.resolve_dsn(env={}, env_file=tmp_path / "none", app_config=app_config)
        assert resolved.source.endswith(":db.dsn")
        assert "@from-yaml:5432" in resolved.dsn

    def test_falls_back_to_builtin_default(self, tmp_path: Path) -> None:
        resolved = pg.resolve_dsn(env={}, env_file=tmp_path / "none", app_config=tmp_path / "none")
        assert resolved.source == "default"
        assert resolved.dsn == pg.DEFAULT_DSN


class TestMigrationChecksum:
    def test_comment_edits_do_not_change_fingerprint(self) -> None:
        """文件头是拿来追加修订记录的，注释变更不该被报成「文件被改过」。"""
        plain = "CREATE TABLE t (x int);\n"
        annotated = "-- 2026-09-28 修订记录\nCREATE TABLE t (x int);\n\n"
        assert pg.migration_checksum(plain) == pg.migration_checksum(annotated)

    def test_trailing_comment_counts_as_content(self) -> None:
        """保守取向：行尾注释算内容，宁可多报一次漂移，也不漏掉真正的 DDL 改动。"""
        assert pg.migration_checksum("CREATE TABLE t (x int);") != pg.migration_checksum(
            "CREATE TABLE t (x int); -- 行尾说明"
        )

    def test_ddl_change_changes_fingerprint(self) -> None:
        assert pg.migration_checksum("CREATE TABLE t (x int);") != pg.migration_checksum(
            "CREATE TABLE t (x bigint);"
        )


class TestTableInventory:
    def test_matches_tables_declared_in_migration(self) -> None:
        """清单与迁移文件必须同步；只改一边时这条测试会红。"""
        sql = (REPO_ROOT / pg.DEFAULT_MIGRATION_FILE).read_text(encoding="utf-8")
        declared = set(re.findall(r"(?m)^CREATE TABLE (?:IF NOT EXISTS )?([a-z_][a-z0-9_]*)", sql))
        assert declared == set(pg.EXPECTED_TABLES)

    def test_expected_shape(self) -> None:
        assert len(pg.EXPECTED_TABLES) == 32
        assert "schema_migration" in pg.EXPECTED_TABLES

    def test_deferred_tables_are_disjoint(self) -> None:
        assert {"content_embedding", "ai_provider_policy", "egress_log"} == set(pg.DEFERRED_TABLES)
        assert not (pg.EXPECTED_TABLES & pg.DEFERRED_TABLES)

    def test_reserved_word_table_name_not_reintroduced(self) -> None:
        """authorization 是 PostgreSQL 保留字，首次真实执行时已改名为 source_authorization。"""
        assert "authorization" not in pg.EXPECTED_TABLES
        assert "source_authorization" in pg.EXPECTED_TABLES
