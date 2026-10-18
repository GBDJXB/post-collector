"""表格排版：按**显示宽度**对齐，中文算两格。

这组测试守的是一件很具体的事：**开发者盯着看的表格不能错位**。用 ``len()`` 排版时
一个汉字只算一格、屏幕上却占两格，于是中文表头后面所有列整体左移——每一行的偏移还不
一样（取决于那一列有没有中文），越看越难受。

判据也取得很直接：**每一行的显示宽度必须严格相等**。补空格到定宽之后，这条就等价于
"每一列的起点都对齐"，而且比按列切分字符串更好断言。
"""

from __future__ import annotations

from typing import Any

import pytest

from pc import cli


def _width(text: str) -> int:
    return cli._display_width(text)


def test_cli_binary_size_uses_iec_units() -> None:
    assert cli._human_size(1024) == "1.0 KiB"
    assert cli._human_size(1024**2) == "1.0 MiB"
    assert cli._human_size(1024**4) == "1024.0 GiB"


class TestDisplayWidth:
    def test_empty_is_zero(self) -> None:
        assert _width("") == 0

    def test_ascii_is_one_per_character(self) -> None:
        assert _width("abc") == 3
        assert _width("id") == 2

    @pytest.mark.parametrize(
        "text",
        [
            "中文",  # 汉字（W）
            "アカ",  # 片假名（W）
            "한글",  # 谚文（W）
            "（）",  # 全角括号（F）
            "ＡＢ",  # 全角字母（F）
            "\u3000\u3000",  # 表意空格（F）
            "、。",  # 中文标点（W）
            "✨🐱",  # emoji（W）
        ],
    )
    def test_wide_characters_are_two_cells(self, text: str) -> None:
        assert _width(text) == 2 * len(text)

    @pytest.mark.parametrize("char", ["…", "·", "—", "“", "”", "→"])
    def test_ambiguous_width_counts_as_one(self, char: str) -> None:
        """歧义宽度按 1 格算——与 wcwidth/rich 的默认一致。

        这条把选择钉住：如果哪天有人改成 2，截断标记 ``…`` 会让每个被截断的单元格多算
        一格，整行又开始错位。要改就得连同 ELLIPSIS 一起改。
        """
        assert _width(char) == 1

    def test_combining_mark_adds_nothing(self) -> None:
        assert _width("e\u0301") == 1, "组合重音不额外占格"
        assert _width("中\u0301") == 2

    @pytest.mark.parametrize("char", ["\u200d", "\u200b", "\ufe0f"])
    def test_zero_width_characters(self, char: str) -> None:
        assert _width(char) == 0, "零宽连接符 / 零宽空格 / 变体选择符都不占格"

    def test_mixed_text(self) -> None:
        assert _width("id=1 内容") == len("id=1 ") + 4


class TestTruncateDisplay:
    def test_short_text_is_returned_unchanged(self) -> None:
        assert cli._truncate_display("一只猫", 10) == "一只猫"

    def test_exact_fit_is_unchanged(self) -> None:
        assert cli._truncate_display("abcde", 5) == "abcde"

    def test_long_ascii_is_cut_to_the_limit(self) -> None:
        result = cli._truncate_display("x" * 100, 10)
        assert _width(result) == 10
        assert result.endswith(cli.ELLIPSIS)

    def test_long_chinese_never_splits_a_wide_character(self) -> None:
        """宽度是逐字符累加的：放不下就整字符停下，绝不会切出半个汉字。"""
        result = cli._truncate_display("中" * 50, 9)
        assert _width(result) == 9
        assert result.endswith(cli.ELLIPSIS)
        kept = result[: -len(cli.ELLIPSIS)]
        assert set(kept) == {"中"}, "kept 里不该出现半个字符（例如替换字符）"
        assert kept == "中" * (len(kept))

    def test_odd_limit_with_wide_characters(self) -> None:
        """奇数上限 + 宽字符：结果不会超过上限，也不会为了凑满而切开字符。"""
        for limit in range(2, 12):
            result = cli._truncate_display("中文内容" * 5, limit)
            assert _width(result) <= limit

    def test_tiny_limit_degrades_to_the_marker(self) -> None:
        assert cli._truncate_display("中文", 2) == cli.ELLIPSIS
        assert _width(cli._truncate_display("中文", 2)) == 1

    def test_limit_below_two_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="至少要有 2 格"):
            cli._truncate_display("x", 1)


class TestPadDisplay:
    def test_pads_by_display_width(self) -> None:
        assert cli._pad_display("中文", 6) == "中文  "
        assert _width(cli._pad_display("中文", 6)) == 6

    def test_over_wide_text_is_left_alone(self) -> None:
        assert cli._pad_display("中文中文", 4) == "中文中文"

    def test_exact_width_gets_no_padding(self) -> None:
        assert cli._pad_display("abc", 3) == "abc"


def _lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.strip()]


class TestPrintTable:
    def test_every_line_has_the_same_display_width(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """核心不变式：中文表头 + 中文内容 + 长文本，行行等宽。"""
        cli._print_table(
            ("id", "类型", "状态", "来源", "内容", "备注"),
            [
                (1, "image", "quarantined", "manual", "媒体 dfbc8a7da8c6…（8.8 MB）", "电脑壁纸"),
                (2, "text", "active", "我的素材", "一只猫在窗台上晒太阳，很长很长的一段中文", "某个群里的"),
            ],
            max_widths={"内容": 12, "备注": 8, "来源": 10},
        )
        lines = capsys.readouterr().out.splitlines()
        widths = {_width(line) for line in lines}
        assert len(widths) == 1, f"每行显示宽度必须相等，实际是 {sorted(widths)}：\n" + "\n".join(lines)

    def test_columns_never_exceed_their_cap(self, capsys: pytest.CaptureFixture[str]) -> None:
        long_text = "中" * 200
        cli._print_table(
            ("id", "内容"),
            [(1, long_text)],
            max_widths={"id": 4, "内容": 10},
        )
        lines = _lines(capsys.readouterr().out)
        for line in lines:
            assert _width(line) <= 2 + 4 + 2 + 10, line
        assert cli.ELLIPSIS in lines[-1], "超长内容要被截断"

    def test_total_width_is_bounded(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_table(
            cli.ITEM_LIST_HEADERS,
            [(1, 1, "image", "quarantined", "中" * 50, "中" * 200, "suspicious", 3, "中" * 50, "2026-01-01T00:00:00.000Z")],
            max_widths=cli.ITEM_LIST_MAX_WIDTHS,
        )
        bound = 2 + sum(cli.ITEM_LIST_MAX_WIDTHS.values()) + 2 * (len(cli.ITEM_LIST_HEADERS) - 1)
        for line in capsys.readouterr().out.splitlines():
            assert _width(line) <= bound, line

    def test_without_caps_the_column_grows_to_fit(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_table(("a", "b"), [("x" * 20, "y")])
        lines = _lines(capsys.readouterr().out)
        assert _width(lines[0]) == _width(lines[-1])
        assert "x" * 20 in lines[-1]

    def test_none_becomes_empty_and_newlines_are_flattened(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli._print_table(("a", "b"), [(None, "第一行\n第二行\t带制表符")], max_widths={"a": 4, "b": 30})
        lines = _lines(capsys.readouterr().out)
        assert len(lines) == 3, "换行必须被拍平，否则一张表会被撕成两半"
        assert "第一行 第二行 带制表符" in lines[-1]

    def test_no_rows_prints_header_and_separator_only(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli._print_table(cli.SOURCE_LIST_HEADERS, [], max_widths=cli.SOURCE_LIST_MAX_WIDTHS)
        lines = _lines(capsys.readouterr().out)
        assert len(lines) == 2
        assert _width(lines[0]) == _width(lines[1])

    def test_short_rows_are_padded_and_long_rows_are_ignored(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli._print_table(("a", "b"), [("1",), ("2", "3", "多余的一格")], max_widths={"a": 4, "b": 4})
        lines = _lines(capsys.readouterr().out)
        assert len(lines) == 4, "行比表头短不该崩，长出来的那格直接忽略"
        assert {_width(line) for line in lines} == {_width(lines[0])}

    def test_caps_are_matched_by_column_name_not_position(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """上限按列名匹配：换了顺序也不会把上限套到别的列上。"""
        cli._print_table(("短", "很长的列"), [("x" * 30, "中" * 30)], max_widths={"短": 4, "很长的列": 6})
        lines = _lines(capsys.readouterr().out)
        assert _width(lines[-1]) == 2 + 4 + 2 + 6


class TestDeclarations:
    def test_item_list_caps_cover_every_column(self) -> None:
        assert set(cli.ITEM_LIST_MAX_WIDTHS) == set(cli.ITEM_LIST_HEADERS), (
            "新增一列就必须给它一个宽度上限，否则那一列会由最长的单元格决定"
        )

    def test_source_list_caps_cover_every_column(self) -> None:
        assert set(cli.SOURCE_LIST_MAX_WIDTHS) == set(cli.SOURCE_LIST_HEADERS)

    def test_search_caps_cover_every_column(self) -> None:
        assert set(cli.SEARCH_MAX_WIDTHS) == set(cli.SEARCH_HEADERS)

    @pytest.mark.parametrize(
        "limits",
        [cli.ITEM_LIST_MAX_WIDTHS, cli.SOURCE_LIST_MAX_WIDTHS, cli.SEARCH_MAX_WIDTHS],
    )
    def test_every_cap_is_usable(self, limits: dict[str, int]) -> None:
        for header, limit in limits.items():
            assert limit >= 2, f"{header} 的上限太小，连截断标记都放不下"

    def test_key_values_are_never_truncated(self, capsys: pytest.CaptureFixture[str]) -> None:
        """上限必须容得下**现有取值**，否则表格会悄悄吃掉有用的信息。

        这里用真实取值：状态最长 `quarantined`、授权方式最长 `manual_export`、
        授权原因最长 `授权已撤销或已过期`、时间戳是固定 24 格的 ISO-8601 UTC 毫秒。
        """
        stamp = "2026-09-30T23:49:54.845Z"
        cli._print_table(
            cli.ITEM_LIST_HEADERS,
            [(1, 1, "image", "quarantined", "manual", "媒体 dfbc8a7da8c6…（8.8 MB）", "suspicious", 1, "某个群里的", stamp)],
            max_widths=cli.ITEM_LIST_MAX_WIDTHS,
        )
        out = capsys.readouterr().out
        for needle in ("quarantined", "suspicious", stamp, "某个群里的"):
            assert needle in out, f"{needle} 被截断了——上限调小了"

        capsys.readouterr()
        cli._print_table(
            cli.SOURCE_LIST_HEADERS,
            [(1, "manual", "local", "manual", 1, "启用", "授权已撤销或已过期", "manual_export、root_self", "-")],
            max_widths=cli.SOURCE_LIST_MAX_WIDTHS,
        )
        out = capsys.readouterr().out
        for needle in ("授权已撤销或已过期", "manual_export、root_self"):
            assert needle in out, f"{needle} 被截断了——上限调小了"


class TestRealCommands:
    """端到端：真实的 `pc item list` / `pc source list` 输出必须行行等宽。"""

    @staticmethod
    def _bootstrap(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
        data_root = tmp_path / "data"
        junk = tmp_path / "下载目录"
        junk.mkdir(parents=True)
        monkeypatch.setenv("PC_DATA_ROOT", str(data_root))
        (junk / "一张很长的中文名字的图片.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"p" * 64
        )
        return {"db": str(data_root / "pc.sqlite3"), "junk": str(junk)}

    def test_item_list_lines_are_all_the_same_display_width(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = self._bootstrap(tmp_path, monkeypatch)
        code = cli.main(["db", "apply", "--path", workspace["db"]])
        assert code == 0
        code = cli.main(
            [
                "upload",
                workspace["junk"],
                "--note",
                "从某个群里存下来的，具体哪个群已经想不起来了",
                "--path",
                workspace["db"],
            ]
        )
        assert code == 0
        capsys.readouterr()

        assert cli.main(["item", "list", "--path", workspace["db"]]) == 0
        out_lines = [
            line for line in capsys.readouterr().out.splitlines() if line.startswith("  ")
        ]
        # 前 3 行是表格（表头、分隔线、1 行数据），最后一行是"共 N 个内容项"的汇总
        table_lines = out_lines[:3]
        widths = {_width(line) for line in table_lines}
        assert len(widths) == 1, f"行宽不一致：{sorted(widths)}\n" + "\n".join(table_lines)
        assert cli.ELLIPSIS in "".join(table_lines), "长中文备注应当被截断"

    def test_source_list_lines_are_all_the_same_display_width(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = self._bootstrap(tmp_path, monkeypatch)
        assert cli.main(["db", "apply", "--path", workspace["db"]]) == 0
        assert (
            cli.main(
                [
                    "source",
                    "add",
                    "一个名字特别长的中文来源",
                    "--kind",
                    "export",
                    "--path",
                    workspace["db"],
                ]
            )
            == 0
        )
        capsys.readouterr()

        assert cli.main(["source", "list", "--path", workspace["db"]]) == 0
        out_lines = [
            line for line in capsys.readouterr().out.splitlines() if line.startswith("  ")
        ]
        table_lines = out_lines[:2 + 1]  # 表头 + 分隔线 + 1 个来源
        widths = {_width(line) for line in table_lines}
        assert len(widths) == 1, f"行宽不一致：{sorted(widths)}\n" + "\n".join(table_lines)

    def test_search_lines_are_all_the_same_display_width(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """检索结果表也要等宽：命中片段里有中文与方括号，最容易把对齐搞坏。"""
        workspace = self._bootstrap(tmp_path, monkeypatch)
        assert cli.main(["db", "apply", "--path", workspace["db"]]) == 0
        text_file = tmp_path / "素材" / "一句话.txt"
        text_file.parent.mkdir(parents=True, exist_ok=True)
        text_file.write_text("蓝发精灵站在雨里", encoding="utf-8")
        assert cli.main(["upload", str(text_file), "--path", workspace["db"]]) == 0
        capsys.readouterr()

        assert cli.main(["search", "雨", "--path", workspace["db"]]) == 0
        out_lines = [
            line for line in capsys.readouterr().out.splitlines() if line.startswith("  ")
        ]
        table_lines = out_lines[:2 + 1]  # 表头 + 分隔线 + 1 条命中
        widths = {_width(line) for line in table_lines}
        assert len(widths) == 1, f"行宽不一致：{sorted(widths)}\n" + "\n".join(table_lines)
        assert "[雨]" in "".join(table_lines), "命中片段要标出命中的位置"

    def test_json_output_is_never_truncated(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """列宽上限只作用于**表格**；机器可读那条路必须给出完整值。"""
        import json

        workspace = self._bootstrap(tmp_path, monkeypatch)
        assert cli.main(["db", "apply", "--path", workspace["db"]]) == 0
        long_note = "从某个群里存下来的，具体哪个群已经想不起来了，只记得是半夜刷到的"
        assert (
            cli.main(
                [
                    "upload",
                    workspace["junk"],
                    "--note",
                    long_note,
                    "--path",
                    workspace["db"],
                ]
            )
            == 0
        )
        capsys.readouterr()

        assert cli.main(["item", "list", "--json", "--path", workspace["db"]]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert {item["origin_note"] for item in payload["items"]} == {long_note}
        assert long_note not in str(
            payload
        ).replace(long_note, ""), "完整备注必须原样出现在 JSON 里"
