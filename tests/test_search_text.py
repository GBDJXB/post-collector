"""检索分词（DESIGN §5.1）：写侧与读侧共用同一套规则。

为什么这份测试值得写：FTS5 **没有中文分词器**，而 `trigram` 索引在查询串少于 3 个
字符时**命中 0 行且不报错**。也就是说，分词规则错一点，表现不是报错，而是"搜不到"——
最安静的一类故障。所以这里把规则逐条钉死：

* 写侧产**单字 + 双字**（单字让 1 字查询可命中，双字让 2 字查询可命中）；
* 拉丁/数字/下划线按整词，并折叠大小写（`blue_hair` 是一个词，不是两个）；
* 标点、空白、emoji 是分隔符（emoji 不承诺可搜）；
* 读侧切出来的词元必须都在写侧的词元集合里——否则索引里有、查询却找不到。
"""

from __future__ import annotations

import pytest

from pc.domain import (
    FILE_NAME_LIMIT,
    ITEM_TYPE_LABELS,
    ITEM_TYPES,
    POST_SIGNATURE_LIMIT,
    SEARCH_EXTRA_TEXT_LIMIT,
    filename_from_ref,
    post_composition_label,
    post_is_mixed,
    post_matches_type,
    post_signature,
    post_type_mask,
    search_extra_value,
    search_tokens,
    tokenize_document,
    tokenize_query,
)


class TestDocumentTokens:
    def test_chinese_gets_unigrams_and_bigrams(self) -> None:
        """三字 CJK：3 个单字 + 2 个双字。"""
        assert tokenize_document("蓝发精") == ("蓝", "发", "精", "蓝发", "发精")

    def test_single_character(self) -> None:
        assert tokenize_document("雨") == ("雨",)

    def test_two_characters_gives_both_scales(self) -> None:
        assert tokenize_document("蓝发") == ("蓝", "发", "蓝发")

    def test_long_chinese_run(self) -> None:
        tokens = tokenize_document("蓝发精灵在雨里")
        assert tokens[:7] == ("蓝", "发", "精", "灵", "在", "雨", "里")
        assert "雨里" in tokens
        assert "里在" not in tokens, "双字按顺序取，不跨边界乱配"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("アカ", ("ア", "カ", "アカ")),
            ("한글", ("한", "글", "한글")),
            ("サクラ", ("サ", "ク", "ラ", "サク", "クラ")),
        ],
    )
    def test_kana_and_hangul_are_cjk(self, text: str, expected: tuple[str, ...]) -> None:
        """假名与谚文按"字"切（它们是 CJK，不靠空格分词）。"""
        assert tokenize_document(text) == expected

    def test_latin_words_are_kept_whole_and_lowercased(self) -> None:
        assert tokenize_document("Sunset PORTRAIT") == ("sunset", "portrait")

    def test_underscore_is_a_word_character(self) -> None:
        """标签名形如 ``blue_hair``：拆成两个词会让"按标签名搜"的命中范围变味。"""
        assert tokenize_document("blue_hair") == ("blue_hair",)

    def test_punctuation_splits(self) -> None:
        assert tokenize_document("sunset,portrait/sketch") == ("sunset", "portrait", "sketch")

    def test_mixed_scripts(self) -> None:
        assert tokenize_document("蓝发hair") == ("蓝", "发", "蓝发", "hair")

    def test_full_width_is_normalized(self) -> None:
        """NFKC 之后全角字母与数字就是普通字符，不应该出现"全角搜不到半角"。"""
        assert tokenize_document("ＡＢＣ１２３") == ("abc123",)

    def test_emoji_and_spaces_are_separators(self) -> None:
        assert tokenize_document("🐱猫 🐶狗") == ("猫", "狗")

    @pytest.mark.parametrize("text", ["", "   ", "\n\t", "，。！", "🐱"])
    def test_nothing_to_index(self, text: str) -> None:
        assert tokenize_document(text) == ()

    def test_is_deterministic(self) -> None:
        text = "蓝发 精灵 sunset blue_hair"
        assert tokenize_document(text) == tokenize_document(text)


class TestQueryTokens:
    def test_query_tokens_are_a_subset_of_document_tokens(self) -> None:
        """读侧切出来的每个词元，写侧都必须产出过——这是"索引里有、查询找得到"的前提。"""
        for text in ("雨", "蓝发", "蓝发精", "sunset", "blue_hair", "蓝发hair"):
            assert set(tokenize_query(text)) <= set(tokenize_document(text)), text

    def test_two_char_query_includes_the_bigram(self) -> None:
        assert "蓝发" in tokenize_query("蓝发")

    def test_three_char_query_still_tokenizes(self) -> None:
        """≥3 字走 trigram；这里只要求它不崩、且与写侧一致（读侧逻辑不参与那条路径）。"""
        assert tokenize_query("蓝发精") == tokenize_document("蓝发精")


class TestDerivedColumnHelpers:
    def test_search_tokens_joins_with_spaces(self) -> None:
        assert search_tokens("蓝发精灵", None, None, "mine local") == (
            "蓝 发 精 灵 蓝发 发精 精灵 mine local"
        )

    def test_search_tokens_none_when_nothing_to_index(self) -> None:
        """四列都空 → ``None``：与"分词结果为空"区分开，重算命令才不会反复认为它没算过。"""
        assert search_tokens(None, None, None, None) is None
        assert search_tokens("", "   ", None, "") is None

    def test_search_extra_joins_present_parts(self) -> None:
        assert search_extra_value("mine", "local", None, "blue_hair") == "mine local blue_hair"

    def test_search_extra_ignores_blanks(self) -> None:
        assert search_extra_value("  ", "", None, "  ") == ""

    def test_search_extra_caps_free_text(self) -> None:
        """自由文本（文件名、备注）有上限：它们是用户可控的，索引不该被一个超长备注撑大。"""
        long_note = "备" + "注" * 900
        composed = search_extra_value(None, "mine", long_note)
        assert composed.startswith("mine ")
        assert len(composed.split()[-1]) == SEARCH_EXTRA_TEXT_LIMIT

    def test_search_tokens_covers_search_extra(self) -> None:
        """``text_search`` 涵盖与 ``content_fts`` 相同的四列。

        否则"2 字的标签名/来源名"在词级索引里无处可查，1~2 字查询会静默落空。
        """
        tokens = search_tokens(None, None, None, search_extra_value("mine", "local"))
        assert tokens is not None
        assert "mine" in tokens.split()


class TestPostComposition:
    """帖子的构成口径（DESIGN §5.1）：``--type`` 说的是"返回什么内容"。"""

    def test_mask_uses_item_types_order(self) -> None:
        assert post_type_mask(["text"]) == 1
        assert post_type_mask(["image"]) == 2
        assert post_type_mask(["file"]) == 64
        assert post_type_mask(["text", "image"]) == 3
        assert post_type_mask(["image", "image"]) == 2, "同一类型出现两次仍只有一位"

    def test_mixed_needs_more_than_one_bit(self) -> None:
        assert post_is_mixed(post_type_mask(["image", "text"]))
        assert not post_is_mixed(post_type_mask(["image"]))
        assert not post_is_mixed(post_type_mask(["image", "image"])), "两张图不是跨类型内容"

    @pytest.mark.parametrize(
        ("parts", "selected", "include_mixed", "expected"),
        [
            # 纯内容：只看该类型
            (["image"], ["image"], False, True),
            (["image"], ["text"], False, False),
            # 跨类型内容：默认不算；--include-mixed 且含所选类型才算
            (["image", "text"], ["image"], False, False),
            (["image", "text"], ["image"], True, True),
            (["image", "text"], ["text"], True, True),
            (["video", "text"], ["image"], True, False),
            # 多个 --type 是并集
            (["image"], ["image", "text"], False, True),
            (["image", "text"], ["image", "text"], True, True),
        ],
    )
    def test_type_predicate_truth_table(
        self, parts: list[str], selected: list[str], include_mixed: bool, expected: bool
    ) -> None:
        mask = post_type_mask(parts)
        assert (
            post_matches_type(mask, post_type_mask(selected), include_mixed=include_mixed)
            is expected
        )

    def test_signature_preserves_order_and_duplicates(self) -> None:
        forward = post_signature([("text", "aaa"), ("media", "bbb"), ("media", "ccc")])
        backward = post_signature([("media", "ccc"), ("media", "bbb"), ("text", "aaa")])
        assert forward != backward
        assert forward.startswith("v2:")
        assert post_signature([("media", "bbb"), ("media", "bbb")]) != post_signature([("media", "bbb")])

    def test_signature_is_none_when_it_cannot_be_computed(self) -> None:
        assert post_signature([]) is None
        assert post_signature([("text", None), ("media", None)]) is None

    def test_signature_gives_up_when_too_long(self) -> None:
        """超长留空 = 不折叠：宁可多显示一条，也绝不误折叠两份不同的内容。"""
        huge = [("media", f"{index:064x}") for index in range(200)]
        joined = "|".join(sorted(f"media:{digest}" for _kind, digest in huge))
        assert len(joined) > POST_SIGNATURE_LIMIT
        assert post_signature(huge) is None

    @pytest.mark.parametrize(
        ("item_types", "expected"),
        [
            (["text"], "文本"),
            (["image", "image"], "图片x2"),
            (["image", "text"], "图片+文本"),
            (["text", "image", "image"], "文本+图片x2"),
            (["text", "image", "text"], "文本+图片+文本"),
            (["weird"], "weird"),
        ],
    )
    def test_composition_label(self, item_types: list[str], expected: str) -> None:
        assert post_composition_label(item_types) == expected


class TestFilenameFromRef:
    """从平台侧引用里取文件名：只留 basename，且不做任何猜测。"""

    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            (r"D:\素材\壁纸.jpg", "壁纸.jpg"),
            ("/home/u/pics/a b.png", "a b.png"),
            ("https://cdn.example.com/x/y/z.webp?v=1#frag", "z.webp"),
            ("s3://bucket/key.png", "key.png"),
            ("a.jpg", "a.jpg"),
            (r"..\..\etc\passwd", "passwd"),
            ("bad\x01name.jpg", "badname.jpg"),
        ],
    )
    def test_extracts_basename(self, ref: str, expected: str) -> None:
        assert filename_from_ref(ref) == expected

    @pytest.mark.parametrize("ref", ["", "   ", "/", "\\", "https://example.com/", "https://example.com"])
    def test_no_name_is_none(self, ref: str) -> None:
        """没有名字就返回 None：站点的根地址取出来是主机名，不该冒充文件名。"""
        assert filename_from_ref(ref) is None

    def test_long_name_is_capped(self) -> None:
        assert len(filename_from_ref("长" * 400 + ".jpg") or "") == FILE_NAME_LIMIT


class TestItemTypeLabels:
    def test_labels_cover_every_type(self) -> None:
        assert set(ITEM_TYPES) == set(ITEM_TYPE_LABELS), "类型清单与中文标签必须一一对应"

    def test_types_match_the_schema_check(self) -> None:
        """取值清单与 0001 的 CHECK 逐字一致——CLI 的 --type 选项也用它。"""
        assert set(ITEM_TYPES) == {"text", "image", "video", "audio", "link", "sticker", "file"}
