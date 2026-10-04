"""CLI 标签流程与 JSON 管道的完整输出约定。"""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from pc import cli
from pc.adapters import sqlite as db
from pc.presentation import _display_width, render_result
from pc.services import tagging


@pytest.fixture
def workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PC_DATA_ROOT", str(tmp_path / "data"))
    path = str(tmp_path / "data" / "pc.sqlite3")
    assert cli.main(["db", "apply", "--path", path]) == 0
    folder = tmp_path / "input"
    folder.mkdir()
    (folder / "sample.txt").write_text("示例正文", encoding="utf-8")
    assert cli.main(["upload", str(folder / "sample.txt"), "--path", path]) == 0
    capsys.readouterr()
    return path


def call(path, capsys, *args):
    assert cli.main([*args, "--path", path]) == 0
    return capsys.readouterr()


def test_tag_cli_and_pipeline(workspace, monkeypatch, capsys):
    path = workspace
    items = json.loads(call(path, capsys, "item", "list", "--json").out)
    message = str(items["items"][0]["message_id"])
    long_name = "人工选择的完整名称" * 8
    call(path, capsys, "tag", "add", "--message", message, "--tag", long_name)
    call(path, capsys, "tag", "alias", "add", "--tag", long_name, "--alias", "独特别名")
    raw = call(path, capsys, "tag", "list", "--json")
    assert not raw.err
    payload = json.loads(raw.out)
    assert payload["tags"][0]["name"] == long_name
    direct = call(path, capsys, "tag", "list").out
    assert long_name not in direct
    monkeypatch.setattr("sys.stdin", io.StringIO(raw.out))
    assert cli.main(["render", "table"]) == 0
    assert capsys.readouterr().out == direct
    found = call(path, capsys, "search", "独特别名", "--json")
    assert json.loads(found.out)["hits"][0]["message_id"] == int(message)
    direct_search = call(path, capsys, "search", "独特别名").out
    # 使用相同结果，避免两次查询的耗时字段影响慢查询提示。
    monkeypatch.setattr("sys.stdin", io.StringIO(found.out))
    assert cli.main(["render", "table"]) == 0
    assert capsys.readouterr().out == direct_search
    call(path, capsys, "tag", "remove", "--message", message, "--tag", "独特别名")
    assert not json.loads(call(path, capsys, "search", "独特别名", "--json").out)["hits"]
    shown = json.loads(call(path, capsys, "tag", "show", "--message", message, "--json").out)
    assert shown["assignments"][0]["revoked_at"] is not None
    assert (
        json.loads(call(path, capsys, "tag", "alias", "list", "--tag", long_name, "--json").out)[
            "aliases"
        ][0]["alias"]
        == "独特别名"
    )
    call(path, capsys, "tag", "alias", "remove", "--alias", "独特别名")


@pytest.mark.parametrize("width", [12, 20, 40, 80, 120])
def test_narrow_table_width(width, capsys):
    payload = tagging.result(
        "tags",
        "tags",
        [
            {
                "tag_id": 123456,
                "name": "很长的中文名称" * 20,
                "namespace": "general",
                "status": "active",
                "usage_count": 120,
                "aliases": ["别名"],
            }
        ],
    )
    render_result(payload, width=width)
    lines = capsys.readouterr().out.splitlines()
    assert all(_display_width(line) <= width for line in lines[:3])
    assert len({_display_width(line) for line in lines[:3]}) == 1
    assert payload["tags"][0]["name"] == "很长的中文名称" * 20


@pytest.mark.parametrize(
    "raw",
    [
        "broken",
        "[]",
        "{}",
        '{"result_type":"unknown","result_version":1}',
        '{"result_type":"tags","result_version":2}',
        '{"result_type":"tags","result_version":1,"tags":[1]}',
    ],
)
def test_invalid_pipeline_input(raw, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert cli.main(["render", "table"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "渲染失败" in output.err


def test_json_warning_goes_to_stderr(workspace, monkeypatch, capsys):
    from pc import config

    def unavailable(*_):
        raise config.ConfigError("注册表不可用")

    monkeypatch.setattr(config, "load_registry", unavailable)
    output = call(workspace, capsys, "search", "--json")
    assert json.loads(output.out)["result_type"] == "search"
    assert "注册表不可用" in output.err


def test_empty_results_and_missing_database(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "empty.sqlite3")
    assert cli.main(["db", "apply", "--path", path]) == 0
    capsys.readouterr()
    raw = call(path, capsys, "tag", "list", "--json").out
    assert json.loads(raw)["tags"] == []
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert cli.main(["render", "table"]) == 0
    assert "没有记录" in capsys.readouterr().out
    assert cli.main(["tag", "list", "--json", "--path", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err
    # 渲染命令独立于库，甚至在没有项目库的情况下也能完成。
    with db.open_db(path) as conn:
        assert conn.execute("SELECT count(*) FROM tag").fetchone()[0] == 0


def test_native_utf8_pipe_and_bom(workspace, capsys):
    """真实子进程使用字节管道，中文标签完整保留。"""
    name = "中文管道名称"
    call(workspace, capsys, "tag", "add", "--message", "1", "--tag", name)
    producer = subprocess.run(
        [sys.executable, "-m", "pc.cli", "tag", "list", "--json", "--path", workspace],
        capture_output=True,
        check=True,
    )
    for raw in (producer.stdout, b"\xef\xbb\xbf" + producer.stdout):
        consumer = subprocess.run(
            [sys.executable, "-m", "pc.cli", "render", "table"],
            input=raw,
            capture_output=True,
            check=True,
        )
        assert name in consumer.stdout.decode("utf-8")
        assert not consumer.stderr


def test_json_ascii_transport_preserves_values():
    payload = {
        "正文": "壁纸😀\n原文",
        "parts": [{"id": 1, "enabled": True, "rank": 0.5, "empty": None}],
    }
    serialized = cli.cfg_dump_json(payload)
    assert serialized.isascii()
    assert json.loads(serialized) == payload
    assert "壁纸" in cli.cfg_dump_json(payload, ensure_ascii=False)


@pytest.mark.parametrize("query", ["tags", "search"])
def test_windows_powershell_default_ascii_pipeline(workspace, capsys, query):
    """真实 Windows PowerShell 5.1 默认 ASCII 管道，保留中文与 emoji。"""
    import os
    from pathlib import Path

    if os.name != "nt":
        pytest.skip("Windows PowerShell 默认管道回归")
    powershell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    name = "中文壁纸😀"
    call(workspace, capsys, "tag", "add", "--message", "1", "--tag", name)

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    producer = [sys.executable, "-m", "pc.cli"]
    producer += ["tag", "list"] if query == "tags" else ["search"]
    producer += ["--json", "--path", workspace]
    # 命令文本和 JSON 传输为 ASCII，业务数据包含中文及 emoji。
    command = (
        "if ($OutputEncoding.WebName -ne 'us-ascii') { throw 'Expected default ASCII' }; "
        + "& "
        + " ".join(map(quote, producer))
        + " | & "
        + " ".join(map(quote, [sys.executable, "-m", "pc.cli", "render", "table"]))
    )
    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        check=True,
    )
    assert name in completed.stdout.decode("utf-8")
    assert not completed.stderr

    direct = subprocess.run(producer, capture_output=True, check=True)
    assert direct.stdout.isascii()
    assert name in json.dumps(json.loads(direct.stdout), ensure_ascii=False)
