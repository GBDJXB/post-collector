"""文本规范化：只做无损变换，且必须确定性。

``normalize_text`` 的结果会进 ``content_hash``（文本去重的分组键）与检索文本，
所以它有两个硬要求：**无损**（不改写内容）与**幂等**（同一段文本任何时候都得到同一个
结果）。任何一处偏差都会让"同一份内容"在去重与检索里分裂成两份。
"""

from __future__ import annotations

import pytest

from pc.domain import normalize_text


class TestBasicShape:
    def test_strips_and_collapses_whitespace(self) -> None:
        assert normalize_text("  a \t b \n\n c  ") == "a b c"

    def test_empty_and_whitespace_only(self) -> None:
        assert normalize_text("") == ""
        assert normalize_text("   \t\n ") == ""

    def test_keeps_cjk_intact(self) -> None:
        assert normalize_text("一只很凶的猫") == "一只很凶的猫"

    def test_does_not_fold_case(self) -> None:
        """刻意不折叠大小写：中文场景下折叠会让 URL 与代码片段变形。"""
        assert normalize_text("HTTP GET /API") == "HTTP GET /API"

    def test_newlines_become_spaces(self) -> None:
        assert normalize_text("第一行\n第二行") == "第一行 第二行"


class TestNfkc:
    def test_fullwidth_digits_become_ascii(self) -> None:
        assert normalize_text("１２３") == "123"

    def test_fullwidth_latin_becomes_ascii(self) -> None:
        assert normalize_text("ＡＢＣ") == "ABC"

    def test_halfwidth_katakana_becomes_fullwidth(self) -> None:
        assert normalize_text("ｶﾀｶﾅ") == "カタカナ"

    def test_compatibility_ideograph_is_expanded(self) -> None:
        # NFKC 会把「㍿」这类兼容字符展开成常规写法，这正是"文本相同"该有的判据
        assert normalize_text("㍿") == "株式会社"


class TestInvisibleCharacters:
    def test_nbsp_is_treated_as_whitespace(self) -> None:
        """不间断空格是最常见的隐形字符，折叠它比留着它更安全。"""
        assert normalize_text("a\u00a0b") == "a b"

    def test_zero_width_space_is_kept(self) -> None:
        """零宽空格不是空白（str.isspace() 为假），保留它——假装看不见它才是错的。"""
        assert normalize_text("a\u200bb") == "a\u200bb"


class TestIdempotence:
    @pytest.mark.parametrize(
        "text",
        ["  多  空白  ", "１２３ ＡＢＣ", "ｶﾀｶﾅ\nｶﾀｶﾅ", "㍿ と ㈱", "", "a\u00a0b"],
    )
    def test_normalizing_twice_changes_nothing(self, text: str) -> None:
        once = normalize_text(text)
        assert normalize_text(once) == once
