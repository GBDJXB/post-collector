"""端到端：``pc db apply`` → ``pc source add`` → ``pc import --process`` → ``pc item list``。

这是本切片唯一一条**跨全部层次**的测试：CLI 参数 → 配置 → 连接器 → 摄入 → 队列 →
worker → 存储 → 数据库 → 展示。它慢一点（要建真库、写真文件），但它能抓到单元测试
抓不到的东西：命令接线错、路径解析错、两个模块对同一列的理解不一致。

顺带钉住三件用户能感知的承诺：**重复导入是幂等的**、``--dry-run`` 什么都不写、
隔离的字节不在主媒体目录里。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pc import cli

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"j" * 64
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"p" * 64
EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"e" * 32
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """一个隔离的工作区：数据根、样本目录、库文件路径，全部在 tmp 下。

    ``PC_DATA_ROOT`` 指向 tmp，因此媒体与隔离区也会落在 tmp 里——本测试不依赖
    （也不污染）开发者自己的数据目录。conftest 的自动夹具已经清掉了环境里可能残留的
    ``PC__PATHS__*``，否则它们（最高优先级）会把媒体引到别处。
    """
    data_root = tmp_path / "data"
    samples = tmp_path / "samples"
    samples.mkdir(parents=True)
    monkeypatch.setenv("PC_DATA_ROOT", str(data_root))
    (samples / "cat.jpg").write_bytes(JPEG)
    (samples / "cat.txt").write_text("一只猫", encoding="utf-8")
    (samples / "notes.md").write_text("笔记正文", encoding="utf-8")
    (samples / "copy.png").write_bytes(JPEG)  # 与 cat.jpg 同一份字节
    (samples / "tool.exe").write_bytes(EXE)
    (samples / "logo.svg").write_bytes(SVG)
    (samples / ".hidden.jpg").write_bytes(PNG)
    nested = samples / "sub"
    nested.mkdir()
    (nested / "bird.png").write_bytes(PNG)
    return {
        "data_root": data_root,
        "samples": samples,
        "db_path": str(data_root / "pc.sqlite3"),
    }


def _bootstrap(workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(["db", "apply", "--path", workspace["db_path"]], capsys)
    assert code == 0, out
    code, out = _run(
        [
            "source",
            "add",
            "my-files",
            "--kind",
            "export",
            "--roots",
            str(workspace["samples"]),
            "--path",
            workspace["db_path"],
        ],
        capsys,
    )
    assert code == 0, out


def _count(workspace: dict[str, Any], table: str) -> int:
    import sqlite3

    conn = sqlite3.connect(workspace["db_path"])
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


class TestHappyPath:
    def test_import_process_and_list(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(
            [
                "import",
                str(workspace["samples"]),
                "--source",
                "my-files",
                "--process",
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        assert code == 0, out
        assert "新消息 7 条" in out, out
        assert "死信 0" in out, out
        # 隐藏文件不算，其余 7 个文件各一条消息
        assert _count(workspace, "message") == 7
        # 5 个媒体项（含 2 个被隔离）+ 3 个文本项（cat.txt 与 cat.jpg 的侧车各 1，notes 1）
        assert _count(workspace, "content_item") == 8
        # 同一份字节出现在两个路径：内容项 2 个引用同一行 media
        assert _count(workspace, "content_media") == 5
        # 去重后只剩 4 份不同的字节：jpeg（cat.jpg + copy.png）、png、exe、svg
        assert _count(workspace, "media") == 4

        code, out = _run(["item", "list", "--path", workspace["db_path"]], capsys)
        assert code == 0, out
        assert "共 8 个内容项" in out
        assert "quarantined" in out, "被拒的类型必须显示为隔离"
        assert "一只猫" in out, "文本内容要能看见"

    def test_objects_land_under_the_data_root(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        _run(
            [
                "import",
                str(workspace["samples"]),
                "--source",
                "my-files",
                "--process",
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        media_files = [p for p in (workspace["data_root"] / "media").rglob("*") if p.is_file()]
        quarantine_files = [
            p for p in (workspace["data_root"] / "quarantine").rglob("*") if p.is_file()
        ]
        assert media_files, "媒体本体必须落在 <data_root>/media"
        assert quarantine_files, "被隔离的字节必须落在 <data_root>/quarantine"
        assert {path.name for path in media_files}.isdisjoint(
            {path.name for path in quarantine_files}
        )


class TestIdempotency:
    def test_second_import_changes_nothing(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        argv = [
            "import",
            str(workspace["samples"]),
            "--source",
            "my-files",
            "--process",
            "--path",
            workspace["db_path"],
        ]
        _run(argv, capsys)
        before = {table: _count(workspace, table) for table in ("message", "content_item", "media")}

        code, out = _run(argv, capsys)
        assert code == 0, out
        assert "新消息 0 条" in out and "重复（已存在）7 条" in out
        assert {table: _count(workspace, table) for table in before} == before
        # 超时恢复会把 running 重置为 queued；这里的任务是 succeeded，不该被重复处理
        assert _count(workspace, "job") == 7


class TestDryRun:
    def test_dry_run_writes_nothing(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(
            [
                "import",
                str(workspace["samples"]),
                "--source",
                "my-files",
                "--dry-run",
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        assert code == 0, out
        assert "dry-run" in out and "发现 7 个文件" in out
        assert _count(workspace, "message") == 0
        assert _count(workspace, "job") == 0
        # dry-run 也不该改动来源配置
        import sqlite3

        conn = sqlite3.connect(workspace["db_path"])
        try:
            config = conn.execute("SELECT config FROM source").fetchone()[0]
        finally:
            conn.close()
        assert json.loads(config)["roots"] == [str(workspace["samples"])]


class TestErrorPaths:
    def test_import_without_a_database_is_a_clear_error(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _run(
            [
                "import",
                str(workspace["samples"]),
                "--source",
                "my-files",
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        assert code == 2
        assert "先建库" in out

    def test_unknown_source_is_a_human_error(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(
            ["import", str(workspace["samples"]), "--source", "nope", "--path", workspace["db_path"]],
            capsys,
        )
        assert code == 2
        assert "找不到来源" in out

    def test_missing_path_is_reported(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(
            [
                "import",
                str(workspace["data_root"] / "not-there"),
                "--source",
                "my-files",
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        assert code == 2
        assert "找不到这些路径" in out

    def test_duplicate_source_name_is_reported(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(
            ["source", "add", "my-files", "--kind", "export", "--path", workspace["db_path"]],
            capsys,
        )
        assert code == 2
        assert "已经有一个叫" in out

    def test_import_from_a_push_only_source_is_rejected(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(
            [
                "source",
                "add",
                "planned",
                "--kind",
                "bot_api",
                "--roots",
                str(workspace["samples"]),
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        assert code == 0, out
        assert "实现还没落地" in out
        code, out = _run(
            ["import", str(workspace["samples"]), "--source", "planned", "--path", workspace["db_path"]],
            capsys,
        )
        assert code == 2
        assert "暂不支持这种来源类型" in out


class TestListings:
    def test_source_list_shows_authorization(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(["source", "list", "--path", workspace["db_path"]], capsys)
        assert code == 0
        assert "授权有效" in out and "manual_export" in out

    def test_source_list_json_is_machine_readable(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(["source", "list", "--json", "--path", workspace["db_path"]], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload[0]["name"] == "my-files"
        assert payload[0]["authorization"]["ok"] is True

    def test_item_list_json(self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        _bootstrap(workspace, capsys)
        _run(
            [
                "import",
                str(workspace["samples"]),
                "--source",
                "my-files",
                "--process",
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        code, out = _run(["item", "list", "--json", "--path", workspace["db_path"]], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["total"] == 8
        assert {item["item_type"] for item in payload["items"]} == {"text", "image", "file"}

    def test_item_list_survives_a_missing_database(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _run(["item", "list", "--path", workspace["db_path"]], capsys)
        assert code == 0, "只读命令在未建库时应当降级提示，而不是报错"
        assert "还没有数据库文件" in out

    def test_work_on_an_empty_queue_is_not_an_error(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(["work", "--path", workspace["db_path"]], capsys)
        assert code == 0
        assert "队列里没有可处理的任务" in out

    def test_work_once_processes_exactly_one(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        _run(
            ["import", str(workspace["samples"]), "--source", "my-files", "--path", workspace["db_path"]],
            capsys,
        )
        code, out = _run(["work", "--once", "--path", workspace["db_path"]], capsys)
        assert code == 0, out
        assert "处理 1 条" in out
        # 第一条任务对应的消息是 cat.jpg + cat.txt 侧车，因此是 2 个内容项
        assert _count(workspace, "content_item") == 2
        assert _count(workspace, "job") == 7, "只该处理一条，其余仍在排队"
