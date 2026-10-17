"""项目根目录的定位：``pc db apply`` 不该依赖当前工作目录。

这条曾经是缺陷：只有 ``doctor`` / ``config`` 走了「按 pyproject.toml + DESIGN.md 向上
查找」，``db`` 子命令用的是「当前目录 + 相对路径」，于是从仓库外运行会报
「找不到迁移目录」；更糟的是 ``config/app.yaml`` 与 ``.env`` 会被**静默跳过**——
配置分层少了两层却不报错，那比报错难查得多。

这组测试钉住三件事：从任何目录都能建库、``--root``/``--dir`` 能显式接管、
以及「跑到仓库外」时配置文件仍然被读到。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pc import cli

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _fake_repo(root: Path, *, data_root: Path | None = None) -> Path:
    """造一个最小的"仓库"：pyproject.toml + DESIGN.md + config/app.yaml + 一个迁移。"""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "migrations" / "sqlite").mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'fake'\n", encoding="utf-8")
    (root / "DESIGN.md").write_text("# fake\n", encoding="utf-8")
    db_section = f"db:\n  data_root: {data_root.as_posix()}\n" if data_root else "db:\n  data_root: ''\n"
    (root / "config" / "app.yaml").write_text(
        f"schema_version: 1\ndefaults: {{}}\n{db_section}", encoding="utf-8"
    )
    (root / "migrations" / "sqlite" / "0001_probe.sql").write_text(
        "CREATE TABLE probe (a INTEGER PRIMARY KEY);\n", encoding="utf-8"
    )
    return root


class TestWorksFromAnyDirectory:
    def test_db_apply_from_a_foreign_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """仓库外运行也能建库：迁移目录应当从项目根（由 __file__ 找到）解析。"""
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "pc.sqlite3"
        code, out = _run(["db", "apply", "--path", str(target)], capsys)
        assert code == 0, out
        assert target.is_file(), out
        assert "已应用" in out
        conn = sqlite3.connect(target)
        try:
            assert conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 0
        finally:
            conn.close()

    def test_reports_which_migration_directory_was_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code, out = _run(["db", "apply", "--path", str(tmp_path / "pc.sqlite3")], capsys)
        assert code == 0, out
        assert "[迁移]" in out, "要能看出它到底用了哪个迁移目录，否则排错只能靠猜"
        assert "migrations" in out

    def test_db_status_from_a_foreign_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "pc.sqlite3"
        _run(["db", "apply", "--path", str(target)], capsys)
        code, out = _run(["db", "status", "--path", str(target)], capsys)
        assert code == 0, out
        assert "结构就绪" in out


class TestExplicitOverrides:
    def test_explicit_dir_takes_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path / "repo")
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "pc.sqlite3"
        code, out = _run(
            ["db", "apply", "--path", str(target), "--dir", str(repo / "migrations" / "sqlite")],
            capsys,
        )
        assert code == 0, out
        conn = sqlite3.connect(target)
        try:
            names = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            conn.close()
        assert "probe" in names, "显式 --dir 必须真的被用上"
        assert "content_item" not in names

    def test_explicit_root_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path / "repo")
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "pc.sqlite3"
        code, out = _run(["db", "apply", "--root", str(repo), "--path", str(target)], capsys)
        assert code == 0, out
        assert str(repo / "migrations" / "sqlite") in out

    def test_root_makes_the_app_config_layer_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """跑到仓库外时 ``config/app.yaml`` 仍然要被读到——它决定库文件放哪。

        修好之前这里会静默退回平台默认目录：命令照样成功，库却建在别处。
        """
        data_root = tmp_path / "from-yaml"
        repo = _fake_repo(tmp_path / "repo", data_root=data_root)
        monkeypatch.chdir(tmp_path)
        code, out = _run(["db", "apply", "--root", str(repo)], capsys)
        assert code == 0, out
        assert (data_root / "pc.sqlite3").is_file(), out
        assert "app.yaml:db.data_root" in out, "来源要能被解释，否则无法判断它读没读到配置"


class TestBackupDestination:
    def test_default_backup_uses_configured_backup_root_not_database_parent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path / "repo")
        database = tmp_path / "database-disk" / "pc.sqlite3"
        backup_root = tmp_path / "backup-disk"
        monkeypatch.setenv("PC_DATA_ROOT", str(tmp_path / "data-root"))
        monkeypatch.setenv("PC__PATHS__BACKUP_ROOT", str(backup_root))
        code, out = _run(["db", "apply", "--root", str(repo), "--path", str(database)], capsys)
        assert code == 0, out

        code, out = _run(["db", "backup", "--root", str(repo), "--path", str(database)], capsys)
        assert code == 0, out
        snapshots = list(backup_root.glob("pc-*.sqlite3"))
        assert len(snapshots) == 1
        assert not (database.parent / "backup").exists()
        conn = sqlite3.connect(snapshots[0])
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='probe'").fetchone()[0] == "probe"
        finally:
            conn.close()

    def test_explicit_backup_target_still_wins_and_refuses_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path / "repo")
        database = tmp_path / "database-disk" / "pc.sqlite3"
        backup_root = tmp_path / "backup-disk"
        destination = tmp_path / "chosen-disk" / "snapshot.sqlite3"
        monkeypatch.setenv("PC_DATA_ROOT", str(tmp_path / "data-root"))
        monkeypatch.setenv("PC__PATHS__BACKUP_ROOT", str(backup_root))
        code, out = _run(["db", "apply", "--root", str(repo), "--path", str(database)], capsys)
        assert code == 0, out

        args = ["db", "backup", "--root", str(repo), "--path", str(database), "--to", str(destination)]
        code, out = _run(args, capsys)
        assert code == 0, out
        assert destination.is_file() and not backup_root.exists()
        original = destination.read_bytes()
        code, out = _run(args, capsys)
        assert code == 2 and "已存在" in out
        assert destination.read_bytes() == original


class TestDoctorRequiredFiles:
    def test_doctor_accepts_repo_with_required_files_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _fake_repo(tmp_path / "repo", data_root=tmp_path / "state")
        for relative in (
            "AGENTS.md",
            "README.md",
            ".env.example",
            ".gitignore",
            "config/settings-registry.yaml",
            "migrations/sqlite/0001_init.sql",
        ):
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# fixture\n", encoding="utf-8")

        code, out = _run(["doctor", "--root", str(repo)], capsys)
        assert code == 0, out
        assert "仓库文件不完整" not in out

        (repo / "README.md").unlink()
        code, out = _run(["doctor", "--root", str(repo)], capsys)
        assert code == 2, out
        assert "✗ README.md" in out


class TestErrorsAreActionable:
    def test_missing_migrations_dir_lists_the_ways_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code, out = _run(
            ["db", "apply", "--path", str(tmp_path / "pc.sqlite3"), "--dir", str(tmp_path / "nope")],
            capsys,
        )
        assert code == 2
        assert "找不到迁移目录" in out
        assert "--dir" in out and "--root" in out and "仓库根目录" in out

    def test_message_mentions_the_distribution_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``uv tool install`` 装的包没带迁移文件，报错要说清这件事而不是让人猜。"""
        monkeypatch.chdir(tmp_path)
        _code, out = _run(
            ["db", "apply", "--path", str(tmp_path / "pc.sqlite3"), "--dir", str(tmp_path / "nope")],
            capsys,
        )
        assert "uv tool install" in out
