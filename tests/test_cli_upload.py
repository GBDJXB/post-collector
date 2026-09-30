"""端到端：``pc upload`` —— root 手工上传这条路。

这是"我随手把几个文件丢进库"的入口，所以测试盯的是**摩擦与误伤**，而不是字段拼写：

* 一条命令就够（来源自动建，不需要先 ``pc source add``，不需要任何凭证）；
* 重传同一份内容**不会**变成两条（身份按内容哈希）；
* ``--note`` 写了要看得见（不然它就是个只写不读的字段）；
* 空文件被跳过并报告，而不是产生垃圾行或静默消失；
* ``--dry-run`` 什么都不写；
* ``--no-process`` 之后 ``pc work`` 能把活干完。

跨层次：CLI 参数 → 来源自动登记 → 连接器身份策略 → 摄入 → 队列 → worker → 存储 →
数据库 → 展示。单元测试各自都对、接起来错的情形，只有这里能抓到。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pc import cli

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"j" * 64
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"p" * 64


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """隔离的工作区：数据根、要上传的文件、库文件路径都在 tmp 下。"""
    data_root = tmp_path / "data"
    junk = tmp_path / "downloads"
    junk.mkdir(parents=True)
    monkeypatch.setenv("PC_DATA_ROOT", str(data_root))
    (junk / "cat.jpg").write_bytes(JPEG)
    (junk / "cat.txt").write_text("一只猫", encoding="utf-8")
    (junk / "meme.png").write_bytes(PNG)
    return {
        "data_root": data_root,
        "junk": junk,
        "db_path": str(data_root / "pc.sqlite3"),
    }


def _bootstrap(workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(["db", "apply", "--path", workspace["db_path"]], capsys)
    assert code == 0, out


def _db(workspace: dict[str, Any]) -> sqlite3.Connection:
    conn = sqlite3.connect(workspace["db_path"])
    conn.row_factory = sqlite3.Row
    return conn


def _count(workspace: dict[str, Any], table: str) -> int:
    conn = _db(workspace)
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _upload(
    workspace: dict[str, Any], capsys: pytest.CaptureFixture[str], *paths: str, **extra: Any
) -> tuple[int, str]:
    """拼出 ``pc upload`` 的 argv：值为 True 的键变成 ``--key``，``no_xxx`` 变成 ``--no-xxx``。

    开关型参数（``--process/--no-process``）需要能表达"关掉"，所以约定 ``no_process=True``
    表示 ``--no-process``；而 ``process=False`` 只是"不加这个参数"。
    """
    argv = ["upload", *paths, "--path", workspace["db_path"]]
    for key, value in extra.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value not in (False, None):
            argv.extend([flag, str(value)])
    return _run(argv, capsys)


class TestFirstUpload:
    def test_one_command_is_enough(self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        """不先 `pc source add`、不给任何凭证，直接上传就该成功。"""
        _bootstrap(workspace, capsys)
        code, out = _upload(
            workspace, capsys, str(workspace["junk"] / "cat.jpg"), note="某个群里的"
        )
        assert code == 0, out
        assert "本次新建" in out, "第一次运行要说明它顺手建了来源"
        assert "需要凭证：否" in out
        assert _count(workspace, "source") == 1
        assert _count(workspace, "source_authorization") == 1
        assert _count(workspace, "content_item") == 2, "图 + 侧车文本 = 两个内容项"

        conn = _db(workspace)
        try:
            source = conn.execute("SELECT name, kind, trust_level FROM source").fetchone()
            authz = conn.execute("SELECT mode FROM source_authorization").fetchone()
        finally:
            conn.close()
        assert (source["name"], source["kind"], source["trust_level"]) == ("manual", "manual", 1)
        assert authz["mode"] == "root_self"

    def test_note_is_visible_in_item_list(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """备注不是只写不读的字段：`pc item list` 要能看见它。"""
        _bootstrap(workspace, capsys)
        _upload(workspace, capsys, str(workspace["junk"] / "cat.jpg"), note="某个群里的")
        code, out = _run(["item", "list", "--path", workspace["db_path"]], capsys)
        assert code == 0, out
        assert "备注" in out and "某个群里的" in out

    def test_note_is_optional(self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        _bootstrap(workspace, capsys)
        code, out = _upload(workspace, capsys, str(workspace["junk"] / "meme.png"))
        assert code == 0, out
        conn = _db(workspace)
        try:
            note = conn.execute("SELECT origin_note FROM message").fetchone()[0]
        finally:
            conn.close()
        assert note is None

    def test_json_output_carries_the_note(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        _bootstrap(workspace, capsys)
        _upload(workspace, capsys, str(workspace["junk"] / "cat.jpg"), note="某个群里的")
        code, out = _run(["item", "list", "--json", "--path", workspace["db_path"]], capsys)
        assert code == 0, out
        payload = json.loads(out)
        assert {item["origin_note"] for item in payload["items"]} == {"某个群里的"}

    def test_multiple_files_in_one_invocation(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _upload(
            workspace,
            capsys,
            str(workspace["junk"] / "cat.jpg"),
            str(workspace["junk"] / "meme.png"),
            note="一次丢两个",
        )
        assert code == 0, out
        assert _count(workspace, "message") == 2, "一个文件一条消息"
        assert _count(workspace, "media") == 2


class TestScanScope:
    def test_uploading_one_file_does_not_rescan_previous_ones(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """本次扫描范围就是**这次给的路径**，与"来源记住过什么"是两件事。

        曾经写错：连接器拿合并后的 roots 去扫，于是第二次上传一个文件会把以前传过的
        路径全部重扫一遍——报告里冒出"重复 N 条"，而 N 还不是你这次给的文件数。
        """
        _bootstrap(workspace, capsys)
        _upload(workspace, capsys, str(workspace["junk"] / "cat.jpg"))
        code, out = _upload(workspace, capsys, str(workspace["junk"] / "meme.png"))
        assert code == 0, out
        assert "新消息 1 条，重复（已存在）0 条" in out, (
            "第二次只上传了 1 个文件，报告里就该只有 1 条"
        )

    def test_duplicate_count_does_not_accumulate(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        path = str(workspace["junk"] / "cat.jpg")
        _upload(workspace, capsys, path)
        for _ in range(2):
            _code, out = _upload(workspace, capsys, path)
            assert "重复（已存在）1 条" in out, "重复数不该随着上传次数累加"


class TestIdempotency:
    def test_reuploading_the_same_file_is_a_duplicate(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        _upload(workspace, capsys, str(workspace["junk"] / "cat.jpg"))
        before = (_count(workspace, "message"), _count(workspace, "media"))

        code, out = _upload(workspace, capsys, str(workspace["junk"] / "cat.jpg"))
        assert code == 0, out
        assert "新消息 0 条" in out and "重复（已存在）1 条" in out
        assert (_count(workspace, "message"), _count(workspace, "media")) == before

    def test_a_renamed_copy_is_still_the_same_content(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """改名、换目录都不该让它变成第二条——这是"按内容定身份"的全部意义。"""
        _bootstrap(workspace, capsys)
        _upload(workspace, capsys, str(workspace["junk"] / "meme.png"))
        elsewhere = workspace["junk"] / "somewhere-else"
        elsewhere.mkdir()
        (elsewhere / "a-totally-different-name.png").write_bytes(PNG)

        code, out = _upload(workspace, capsys, str(elsewhere / "a-totally-different-name.png"))
        assert code == 0, out
        assert _count(workspace, "message") == 1
        assert _count(workspace, "media") == 1, "同一份字节只占一行 media"

    def test_same_image_with_a_different_caption_is_a_new_entry(        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        _upload(workspace, capsys, str(workspace["junk"] / "cat.jpg"))
        first = _count(workspace, "message")

        other = workspace["junk"] / "again"
        other.mkdir()
        (other / "cat.jpg").write_bytes(JPEG)
        (other / "cat.txt").write_text("换个配文", encoding="utf-8")
        code, out = _upload(workspace, capsys, str(other / "cat.jpg"))
        assert code == 0, out
        assert _count(workspace, "message") == first + 1, "同图不同配文是两个条目"
        assert _count(workspace, "media") == 1, "但字节只存一份"


class TestSkipsAndDryRun:
    def test_empty_file_is_skipped_and_reported(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        empty = workspace["junk"] / "empty.txt"
        empty.write_bytes(b"")
        code, out = _upload(workspace, capsys, str(empty))
        assert code == 0, out
        assert "跳过 1 个文件" in out
        assert _count(workspace, "message") == 0

    def test_dry_run_writes_no_content(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """dry-run 不写消息、不入队。

        来源行是**配置**不是内容：还没有「manual」来源时它会先被登记好（否则连"会发现
        哪些文件"都算不出来）。这一点在输出里说清楚了，不想有任何写入就先 `pc source add`。
        """
        _bootstrap(workspace, capsys)
        code, out = _upload(
            workspace, capsys, str(workspace["junk"] / "cat.jpg"), dry_run=True, note="不该落库"
        )
        assert code == 0, out
        assert "dry-run" in out
        assert _count(workspace, "message") == 0
        assert _count(workspace, "job") == 0
        assert _count(workspace, "content_item") == 0

    def test_missing_path_is_an_error(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _upload(workspace, capsys, str(workspace["junk"] / "nope.jpg"))
        assert code == 2
        assert "找不到这些路径" in out


class TestProcessing:
    def test_processes_by_default(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """默认立刻处理：媒体引用是文件路径，用户上传完可能马上删掉原文件。"""
        _bootstrap(workspace, capsys)
        code, out = _upload(workspace, capsys, str(workspace["junk"] / "meme.png"))
        assert code == 0, out
        assert "处理 1 条" in out and "成功 1" in out
        assert _count(workspace, "job") == 1

    def test_no_process_leaves_the_queue_for_pc_work(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _upload(
            workspace, capsys, str(workspace["junk"] / "meme.png"), no_process=True
        )
        assert code == 0, out
        assert "接着处理" in out
        assert _count(workspace, "content_item") == 0

        code, out = _run(["work", "--path", workspace["db_path"]], capsys)
        assert code == 0, out
        assert _count(workspace, "content_item") == 1

    def test_upload_and_import_keep_one_media_row(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """两条通道跑同一份字节：两条消息、一行 media（多对多仍然成立）。"""
        _bootstrap(workspace, capsys)
        _upload(workspace, capsys, str(workspace["junk"] / "meme.png"))

        # 导出包放在**另一个**目录：一个来源的 roots 就是它扫描的范围，
        # 拿同一个目录当两个来源的 roots 会让"几条消息"变成一道算术题
        exported = tmp_path / "exported"
        exported.mkdir()
        (exported / "from-platform.png").write_bytes(PNG)

        code, out = _run(
            [
                "source",
                "add",
                "exported",
                "--kind",
                "export",
                "--roots",
                str(exported),
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        assert code == 0, out
        code, out = _run(
            [
                "import",
                str(exported),
                "--source",
                "exported",
                "--process",
                "--path",
                workspace["db_path"],
            ],
            capsys,
        )
        assert code == 0, out
        assert _count(workspace, "message") == 2, "两个来源各一条消息"
        assert _count(workspace, "media") == 1, "字节相同就只占一行 media"
        assert _count(workspace, "content_media") == 2


class TestSourceHandling:
    def test_explicit_manual_source_is_reused(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _run(
            ["source", "add", "memes", "--kind", "manual", "--path", workspace["db_path"]],
            capsys,
        )
        assert code == 0, out
        assert "不需要凭证" in out, "kind=manual 时要说清它不需要凭证"

        _upload(workspace, capsys, str(workspace["junk"] / "cat.jpg"), source="memes")
        conn = _db(workspace)
        try:
            name = conn.execute("SELECT name FROM source").fetchone()[0]
            mode = conn.execute("SELECT mode FROM source_authorization").fetchone()[0]
        finally:
            conn.close()
        assert name == "memes"
        assert mode == "root_self", "kind=manual 的默认授权方式"

    def test_upload_into_an_export_source_is_refused(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """拿 export 来源来上传会让身份策略从"内容"变成"路径"，必须挡住而不是默默换行为。"""
        _bootstrap(workspace, capsys)
        _run(
            ["source", "add", "exported", "--kind", "export", "--path", workspace["db_path"]],
            capsys,
        )
        code, out = _upload(
            workspace, capsys, str(workspace["junk"] / "cat.jpg"), source="exported"
        )
        assert code == 2
        assert "不是 manual" in out
        assert _count(workspace, "message") == 0

    def test_unknown_source_is_an_error(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _bootstrap(workspace, capsys)
        code, out = _upload(
            workspace, capsys, str(workspace["junk"] / "cat.jpg"), source="nope"
        )
        assert code == 2
        assert "找不到来源" in out

    def test_roots_persist_so_the_worker_can_fetch_later(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """worker 在另一个时刻取字节，它只读 source.config 的 roots。"""
        import json

        _bootstrap(workspace, capsys)
        _upload(
            workspace,
            capsys,
            str(workspace["junk"] / "meme.png"),
            no_process=True,
        )
        conn = _db(workspace)
        try:
            config = conn.execute("SELECT config FROM source").fetchone()[0]
        finally:
            conn.close()
        roots = json.loads(config)["roots"]
        assert roots == [str(workspace["junk"] / "meme.png")], (
            "存的是用户给的那个文件本身，不是它所在的整个目录——后者会把目录里别的文件"
            "也变成这个来源的一部分"
        )
