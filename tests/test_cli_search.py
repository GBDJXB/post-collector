"""端到端：``pc search`` —— 从命令行到检索结果。

检索最容易"看起来能用但搜不到东西"：一条 2 字中文查询命中 0 行**不会报错**，只是安静地
给你一个空列表。所以这里的测试从真实入口走完全程——建库 → ``pc upload`` 几个文件 →
``pc search`` 各种查询长度与过滤器——并断言**具体那一条**被找到，而不是"没抛异常"。

同时钉住几条面向使用者的承诺：

* 1 字、2 字、3 字中文都能搜到（设计点名的退化点）；
* 默认只看安全、有效的内容，放宽必须显式给参数；
* 老数据（分词之前落库的）搜不到时**要说清楚怎么办**，而不是只说"没有匹配"；
* ``--json`` 里有命中理由与翻页游标，``--timings`` 有各段耗时。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from pc import cli


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    data_root = tmp_path / "data"
    files = tmp_path / "files"
    files.mkdir(parents=True)
    monkeypatch.setenv("PC_DATA_ROOT", str(data_root))
    (files / "a.txt").write_text("蓝发精灵站在雨里", encoding="utf-8")
    (files / "b.txt").write_text("银发法师在雪夜读书", encoding="utf-8")
    (files / "c.txt").write_text("sunset portrait with blue hair", encoding="utf-8")
    return {"data_root": data_root, "files": files, "db_path": str(data_root / "pc.sqlite3")}


@pytest.fixture
def ready(workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """建库 + 上传三个文本文件（上传会顺手处理它们）。"""
    code, out = _run(["db", "apply", "--path", workspace["db_path"]], capsys)
    assert code == 0, out
    code, out = _run(
        [
            "upload",
            str(workspace["files"] / "a.txt"),
            str(workspace["files"] / "b.txt"),
            str(workspace["files"] / "c.txt"),
            "--path",
            workspace["db_path"],
        ],
        capsys,
    )
    assert code == 0, out
    assert "成功 3" in out, out
    return workspace


def _search(ready: dict[str, Any], capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    return _run(["search", *args, "--path", ready["db_path"]], capsys)


def _json(ready: dict[str, Any], capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    code, out = _search(ready, capsys, *args, "--json")
    assert code == 0, out
    return json.loads(out)


class TestQueryLengths:
    @pytest.mark.parametrize("term", ["雨", "蓝发", "蓝发精", "精灵", "站在"])
    def test_chinese_queries_of_every_length_find_it(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str], term: str
    ) -> None:
        result = _json(ready, capsys, term)
        texts = [hit["text"] for hit in result["hits"]]
        assert "蓝发精灵站在雨里" in texts, f"{term} 没搜到：{texts}"

    def test_table_shows_the_marked_snippet(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """表格里给的是命中片段（带 [] 标记），这是"为什么它出现在这里"的最直接答案。"""
        code, out = _search(ready, capsys, "雨")
        assert code == 0
        assert "[雨]" in out, out

    def test_latin_word_and_prefix(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        for term in ("sunset", "portra", "PORTRAIT"):
            code, out = _search(ready, capsys, term)
            assert code == 0
            assert "sunset" in out, f"{term} 没搜到：{out}"

    def test_source_name_is_searchable(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """来源名进了 search_extra，所以它本身也是个关键词。"""
        result = _json(ready, capsys, "manual")
        assert result["hits"], "按来源名搜不到内容"

    def test_multiple_terms_are_anded(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _json(ready, capsys, "蓝发", "雨")["hits"], "两个都在 a.txt 里，应当命中"
        assert not _json(ready, capsys, "蓝发", "雪夜")["hits"], "两个词分属不同文件，不该命中"

    def test_time_sort_keeps_keywords(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--sort time` 只换排法：结果集仍然是关键词命中的那些，不能变成整库列表。"""
        result = _json(ready, capsys, "雨", "--sort", "time")
        assert result["sort"] == "time"
        assert [hit["text"] for hit in result["hits"]] == ["蓝发精灵站在雨里"]

    def test_empty_result_is_explained(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _search(ready, capsys, "绝对不存在的词")
        assert code == 0
        assert "没有匹配的内容" in out
        assert "词级索引" in out, "空结果要给出可操作的提示，而不是只说没有"


class TestFilters:
    def test_plain_list_without_keywords(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _search(ready, capsys)
        assert code == 0
        assert "共 3 条命中" in out, out

    def test_source_filter(self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        result = _json(ready, capsys, "--source", "manual")
        assert len(result["hits"]) == 3
        assert result["filters_applied"]["source_id"] == result["hits"][0]["source_id"]

    def test_unknown_source_reports_known_ones(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _search(ready, capsys, "--source", "nope")
        assert code == 2
        assert "找不到来源" in out and "manual" in out

    def test_type_filter(self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        assert len(_json(ready, capsys, "--type", "text")["hits"]) == 3
        assert _json(ready, capsys, "--type", "image")["hits"] == []

    def test_time_filter(self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        assert len(_json(ready, capsys, "--since", "2000-01-01")["hits"]) == 3
        assert _json(ready, capsys, "--until", "2000-01-01")["hits"] == []

    def test_bad_time_is_rejected(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _search(ready, capsys, "--since", "上周")
        assert code == 2
        assert "时间格式" in out

    def test_unknown_tag_suggests(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _search(ready, capsys, "--tag", "rain")
        assert code == 2
        assert "找不到标签" in out

    def test_page_size_is_capped(self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
        code, out = _search(ready, capsys, "--limit", "500")
        assert code == 2
        assert "50" in out


class TestVisibility:
    def test_quarantined_and_nsfw_hidden_by_default(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = sqlite3.connect(ready["db_path"])
        try:
            db.execute(
                "UPDATE content_item SET status = 'quarantined' WHERE text_content LIKE '%雪夜%'"
            )
            db.execute("UPDATE content_item SET nsfw_level = 3 WHERE text_content LIKE '%sunset%'")
            db.commit()
        finally:
            db.close()

        visible = _json(ready, capsys)["hits"]
        assert len(visible) == 1, "隔离与受限内容默认不该出现"
        assert "雨里" in visible[0]["text"]

        assert len(_json(ready, capsys, "--status", "quarantined")["hits"]) == 1
        assert len(_json(ready, capsys, "--nsfw-level", "3")["hits"]) == 2


class TestStaleHint:
    def test_old_rows_are_reported_and_fixable(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """模拟"分词之前落库的老行"：清空 text_search，再搜 2 字词。"""
        db = sqlite3.connect(ready["db_path"])
        try:
            db.execute("UPDATE content_item SET text_search = NULL")
            db.execute("INSERT INTO content_word(content_word) VALUES('rebuild')")
            db.commit()
        finally:
            db.close()

        code, out = _search(ready, capsys, "蓝发")
        assert code == 0
        assert "没有匹配的内容" in out
        assert "还没有分词结果" in out and "recompute-derived" in out

        code, out = _run(
            ["db", "reindex", "--recompute-derived", "--path", ready["db_path"]], capsys
        )
        assert code == 0, out
        assert "派生列" in out

        texts = [hit["text"] for hit in _json(ready, capsys, "蓝发")["hits"]]
        assert "蓝发精灵站在雨里" in texts, "重算之后老内容应当能搜到"


class TestOutput:
    def test_json_carries_the_reason_and_cursor(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = _json(ready, capsys, "雨", "--limit", "1")
        hit = result["hits"][0]
        assert hit["matched_terms"] == ["雨"]
        assert "[雨]" in hit["snippet"], "命中片段要标出命中的位置"
        assert "text_content" in hit["matched_fields"]
        assert result["total"] is None, "关键词检索不给精确总数"
        assert result["candidates"] is not None

    def test_cursor_walks_to_the_next_page(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = _json(ready, capsys, "--limit", "2")
        assert first["has_more"] is True
        assert first["next_cursor"]
        second = _json(ready, capsys, "--limit", "2", "--cursor", first["next_cursor"])
        first_ids = {hit["content_id"] for hit in first["hits"]}
        second_ids = {hit["content_id"] for hit in second["hits"]}
        assert not (first_ids & second_ids), "第二页不该重复第一页的内容"

    def test_broken_cursor_is_rejected(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _search(ready, capsys, "--cursor", "不是游标")
        assert code == 2
        assert "游标" in out

    def test_timings_are_printed(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _search(ready, capsys, "雨", "--timings")
        assert code == 0
        assert "耗时：" in out and "candidates=" in out and "total=" in out

    def test_table_lines_have_equal_display_width(
        self, ready: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """表格按显示宽度对齐：同一张表的每一行显示宽度必须相等（上一切片的约定）。"""
        code, out = _search(ready, capsys, "雨")
        assert code == 0
        table = [
            line
            for line in out.splitlines()
            if line.strip() and not line.startswith("  候选") and not line.startswith("  耗时")
        ]
        assert len(table) >= 3, out
        widths = {cli._display_width(line) for line in table}
        assert len(widths) == 1, f"行宽不一致：{widths}\n{out}"


class TestWithoutDatabase:
    def test_no_database_is_a_hint_not_an_error(
        self, workspace: dict[str, Any], capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out = _run(["search", "雨", "--path", workspace["db_path"]], capsys)
        assert code == 0
        assert "还没有数据库文件" in out
