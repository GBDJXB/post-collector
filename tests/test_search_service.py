"""检索服务与 FTS5 适配器：过滤、分页、可解释性、可见性。

这一层最要紧的不是"能搜到"，而是**搜不到的那些内容为什么搜不到**：

* 软删的、被隔离的、超过 NSFW 档位的、以及同一段文本的重复项，默认都不该出现——
  漏一条就是"删了还能搜到"，多一条就是"看不见自己刚存的东西"，两种都很难发现；
* 1~2 字的中文查询必须命中（trigram 在这里命中 0，是设计里点名的退化点）；
* 关键词检索**不给精确总数**（§5.6 M1b），但纯过滤列表要给；
* 组装必须是固定的三条 SQL——用语句计数把 §5.6 的 M2（禁止 N+1）钉住。
"""

from __future__ import annotations

from typing import Any

import pytest

from pc.adapters.fts_search import MAX_PAGE_SIZE, SqliteFtsSearch
from pc.domain import ITEM_TYPE_LABELS, search_extra_value, search_tokens
from pc.services import search as svc
from pc.services.search import SearchError, SearchQuery


def _seed_source(conn: Any, name: str, platform: str = "local") -> int:
    import uuid

    return int(
        conn.execute(
            "INSERT INTO source (uuid, name, platform, kind) VALUES (?, ?, ?, 'manual') "
            "RETURNING source_id",
            (str(uuid.uuid4()), name, platform),
        ).fetchone()[0]
    )


def _seed_item(
    conn: Any,
    *,
    source_id: int,
    text: str | None = None,
    source_label: str = "mine",
    platform: str = "local",
    first_seen_at: str = "2026-01-01T00:00:00.000Z",
    sent_at: str | None = None,
    original_name: str | None = None,
    status: str = "active",
    nsfw_level: int = 0,
    deleted_at: str | None = None,
    canonical_id: int | None = None,
    dup_group_id: int | None = None,
    item_type: str = "text",
    tags: tuple[str, ...] = (),
) -> int:
    """种一条内容项：派生列按真实规则算好，FTS 索引由触发器搬进去。"""
    import uuid

    message_id = int(
        conn.execute(
            "INSERT INTO message (uuid, source_id, platform_msg_id, text_raw, sent_at) "
            "VALUES (?, ?, ?, ?, ?) RETURNING message_id",
            (str(uuid.uuid4()), source_id, f"m-{uuid.uuid4().hex[:8]}", text, sent_at),
        ).fetchone()[0]
    )
    extra = search_extra_value(
        source_label, platform, item_type, ITEM_TYPE_LABELS.get(item_type), original_name
    )
    content_id = int(
        conn.execute(
            "INSERT INTO content_item ("
            "  uuid, message_id, item_type, ordinal, text_content, text_search, search_extra,"
            "  original_name, status, nsfw_level, first_seen_at, deleted_at, canonical_id,"
            "  dup_group_id"
            ") VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING content_id",
            (
                str(uuid.uuid4()),
                message_id,
                item_type,
                text,
                search_tokens(text, None, None, extra),
                extra,
                original_name,
                status,
                nsfw_level,
                first_seen_at,
                deleted_at,
                canonical_id,
                dup_group_id,
            ),
        ).fetchone()[0]
    )
    for name in tags:
        tag_id = int(
            conn.execute(
                "INSERT INTO tag (uuid, name, name_norm, namespace) "
                "VALUES (?, ?, ?, 'general') RETURNING tag_id",
                (str(uuid.uuid4()), name, name),
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO tag_assignment (content_id, tag_id, source) VALUES (?, ?, 'human')",
            (content_id, tag_id),
        )
    return content_id


def _seed_media(conn: Any, content_id: int, *, thumb_key: str = "aa/bb/thumb") -> None:
    import uuid

    media_id = int(
        conn.execute(
            "INSERT INTO media (uuid, sha256, size_bytes, object_key, thumb_key, width, height,"
            " mime_detected, extension) "
            "VALUES (?, ?, 100, ?, ?, 800, 600, 'image/jpeg', 'jpg') RETURNING media_id",
            (str(uuid.uuid4()), uuid.uuid4().hex + uuid.uuid4().hex, thumb_key, thumb_key),
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO content_media (content_id, media_id, role, ordinal) VALUES (?, ?, 'primary', 0)",
        (content_id, media_id),
    )


@pytest.fixture
def corpus(fresh_db: Any) -> dict[str, Any]:
    """一个小语料：正常项、重复项、软删、隔离、NSFW、媒体项、标签。"""
    source_a = _seed_source(fresh_db, "alpha")
    source_b = _seed_source(fresh_db, "beta")
    ids: dict[str, int] = {}
    ids["rain"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text="蓝发精灵在雨里",
        source_label="alpha",
        first_seen_at="2026-01-05T00:00:00.000Z",
    )
    ids["snow"] = _seed_item(
        fresh_db,
        source_id=source_b,
        text="银发法师在雪夜读书",
        source_label="beta",
        first_seen_at="2026-01-04T00:00:00.000Z",
        tags=("rain",),
    )
    ids["latin"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text="sunset portrait with blue hair",
        source_label="alpha",
        first_seen_at="2026-01-03T00:00:00.000Z",
        tags=("blue_hair",),
    )
    ids["dup"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text="蓝发精灵在雨里",
        source_label="alpha",
        first_seen_at="2026-01-02T00:00:00.000Z",
        canonical_id=ids["rain"],
        dup_group_id=ids["rain"],
    )
    ids["deleted"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text="蓝发精灵在雨里（已删）",
        source_label="alpha",
        first_seen_at="2026-01-01T00:00:00.000Z",
        deleted_at="2026-01-06T00:00:00.000Z",
    )
    ids["quarantined"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text="蓝发精灵在雨里（隔离）",
        source_label="alpha",
        first_seen_at="2026-01-01T01:00:00.000Z",
        status="quarantined",
    )
    ids["nsfw"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text="蓝发精灵在雨里（受限）",
        source_label="alpha",
        first_seen_at="2026-01-01T02:00:00.000Z",
        nsfw_level=2,
    )
    ids["image"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text=None,
        source_label="alpha",
        first_seen_at="2026-01-06T00:00:00.000Z",
        item_type="image",
    )
    _seed_media(fresh_db, ids["image"])
    # 第二条可见的"蓝发"内容：相关度翻页与"多命中"的断言需要 ≥2 条可见结果
    ids["rain2"] = _seed_item(
        fresh_db,
        source_id=_seed_source(fresh_db, "gamma"),
        text="蓝发精灵在雨后的城市",
        source_label="gamma",
        first_seen_at="2026-01-06T12:00:00.000Z",
    )
    ids["canonical_deleted"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text="另一段重复文本",
        source_label="alpha",
        first_seen_at="2025-12-31T00:00:00.000Z",
        deleted_at="2026-01-07T00:00:00.000Z",
    )
    ids["orphan"] = _seed_item(
        fresh_db,
        source_id=source_a,
        text="另一段重复文本",
        source_label="alpha",
        first_seen_at="2026-01-07T00:00:00.000Z",
        canonical_id=ids["canonical_deleted"],
        dup_group_id=ids["canonical_deleted"],
    )
    return {"ids": ids, "source_a": source_a, "source_b": source_b}


def _ids(result: dict[str, Any]) -> list[int]:
    return [hit["content_id"] for hit in result["hits"]]


class TestFilterPath:
    def test_lists_visible_items_newest_first(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=50))
        assert result["sort"] == "time"
        assert result["total"] == len(_ids(result)), "纯过滤列表给精确总数"
        stamps = [hit["first_seen_at"] for hit in result["hits"]]
        assert stamps == sorted(stamps, reverse=True), "按 first_seen_at 倒序"

    def test_excludes_deleted_quarantined_and_nsfw(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        visible = set(_ids(svc.run_search(fresh_db, SearchQuery(limit=50))))
        for key in ("deleted", "quarantined", "nsfw"):
            assert corpus["ids"][key] not in visible, key

    def test_folds_duplicate_text(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        visible = set(_ids(svc.run_search(fresh_db, SearchQuery(limit=50))))
        assert corpus["ids"]["rain"] in visible
        assert corpus["ids"]["dup"] not in visible, "同一段文本只留代表项"

    def test_include_duplicates_shows_them(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        visible = set(_ids(svc.run_search(fresh_db, SearchQuery(limit=50, include_duplicates=True))))
        assert corpus["ids"]["dup"] in visible

    def test_orphaned_duplicate_stays_visible(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """代表项自己被软删时，不能把整组都藏起来（否则内容凭空消失）。"""
        visible = set(_ids(svc.run_search(fresh_db, SearchQuery(limit=50))))
        assert corpus["ids"]["orphan"] in visible

    def test_status_can_be_widened(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        visible = set(
            _ids(svc.run_search(fresh_db, SearchQuery(limit=50, statuses=("quarantined",))))
        )
        assert visible == {corpus["ids"]["quarantined"]}

    def test_nsfw_level_can_be_widened(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        visible = set(_ids(svc.run_search(fresh_db, SearchQuery(limit=50, nsfw_max_level=2))))
        assert corpus["ids"]["nsfw"] in visible

    def test_filter_by_source(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=50, source="beta"))
        assert _ids(result) == [corpus["ids"]["snow"]]

    def test_filter_by_type(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=50, item_types=("image",)))
        assert _ids(result) == [corpus["ids"]["image"]]

    def test_filter_by_time_range(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(
            fresh_db, SearchQuery(limit=50, since="2026-01-04", until="2026-01-05")
        )
        assert set(_ids(result)) == {corpus["ids"]["rain"], corpus["ids"]["snow"]}

    def test_filter_by_tag(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=50, tags=("blue_hair",)))
        assert _ids(result) == [corpus["ids"]["latin"]]

    def test_multiple_tags_are_anded(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        assert _ids(svc.run_search(fresh_db, SearchQuery(limit=50, tags=("blue_hair", "rain")))) == []
        assert _ids(
            svc.run_search(fresh_db, SearchQuery(limit=50, tags=("rain",)))
        ) == [corpus["ids"]["snow"]]

    def test_two_tags_on_the_same_item_match(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """正例的 AND：两个标签都挂在同一条内容上时必须命中。

        这条是回归测试：标签过滤会用"最小集合当驱动表、其余 EXISTS"的写法，而驱动子查询
        在 FROM 里、EXISTS 在 WHERE 末尾——参数顺序一旦错位，FTS 不会报错，只会静默查不到。
        只测"两个不共现的标签查不到"是抓不到那个 bug 的。
        """
        content_id = corpus["ids"]["rain"]
        for name in ("夜雨", "蓝"):
            fresh_db.execute(
                "INSERT INTO tag (uuid, name, name_norm, namespace) VALUES (?, ?, ?, 'general')",
                (f"t-{name}", name, name),
            )
            tag_id = int(fresh_db.execute("SELECT max(tag_id) FROM tag").fetchone()[0])
            fresh_db.execute(
                "INSERT INTO tag_assignment (content_id, tag_id, source) VALUES (?, ?, 'human')",
                (content_id, tag_id),
            )
        result = svc.run_search(fresh_db, SearchQuery(limit=50, tags=("夜雨", "蓝")))
        assert _ids(result) == [content_id]

    def test_three_tags_intersection(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        content_id = corpus["ids"]["snow"]
        for name in ("雪", "夜", "读书"):
            fresh_db.execute(
                "INSERT INTO tag (uuid, name, name_norm, namespace) VALUES (?, ?, ?, 'general')",
                (f"t3-{name}", name, name),
            )
            tag_id = int(fresh_db.execute("SELECT max(tag_id) FROM tag").fetchone()[0])
            fresh_db.execute(
                "INSERT INTO tag_assignment (content_id, tag_id, source) VALUES (?, ?, 'human')",
                (content_id, tag_id),
            )
        result = svc.run_search(fresh_db, SearchQuery(limit=50, tags=("雪", "夜", "读书")))
        assert _ids(result) == [content_id]


class TestTotals:
    """总数的三种口径（§5.6 M1b）：精确 / 至少 N 条 / 约 N 条。"""

    def test_plain_list_is_exact(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=50))
        assert result["total_kind"] == "exact"

    def test_cap_turns_it_into_at_least(
        self, fresh_db: Any, corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """超过上限就不再数下去，并如实说"至少这么多"。"""
        from pc.adapters import fts_search

        monkeypatch.setattr(fts_search, "COUNT_CAP", 2)
        result = svc.run_search(fresh_db, SearchQuery(limit=50))
        assert result["total"] == 2
        assert result["total_kind"] == "at_least"
        # has_more 说的是"这一页之外还有没有"，与总数是不是被截断无关
        assert result["has_more"] is (len(result["hits"]) == 50)

    def test_tag_usage_gives_an_estimate(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """标签维度用 tag.usage_count（打标切片维护的计数器），代价接近零。"""
        tag_id = int(
            fresh_db.execute("SELECT tag_id FROM tag WHERE name = 'rain'").fetchone()[0]
        )
        fresh_db.execute("UPDATE tag SET usage_count = 7 WHERE tag_id = ?", (tag_id,))
        result = svc.run_search(fresh_db, SearchQuery(limit=50, tags=("rain",)))
        assert (result["total"], result["total_kind"]) == (7, "estimate")

    def test_unmaintained_counter_falls_back_to_counting(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        """计数器还是 0（打标切片之前）时不能报 0——退回真实计数，并标成精确。"""
        result = svc.run_search(fresh_db, SearchQuery(limit=50, tags=("rain",)))
        assert result["total_kind"] == "exact"
        assert result["total"] == 1

    def test_keyword_has_no_total(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50))
        assert result["total"] is None and result["total_kind"] is None


class TestTagPlanSelection:
    """标签集规模决定查询计划（驱动集 vs 时间索引），两种都要给出正确结果。"""

    def test_small_tag_set_drives_the_query(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        engine = SqliteFtsSearch(fresh_db)
        driver, suffix, driver_params, suffix_params = engine._tag_driver([1, 2], limit=50)
        assert driver is not None, "稀疏组合应当物化最小集合当驱动集"
        assert len(driver_params) == 1
        assert len(suffix_params) == len(suffix)

    def test_dense_tag_uses_the_time_index(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """常见标签：沿时间索引扫几十行就凑满一页，不该花几万次回表。"""
        fresh_db.execute("UPDATE tag SET usage_count = 100000 WHERE tag_id = 1")
        engine = SqliteFtsSearch(fresh_db)
        driver, suffix, driver_params, _suffix_params = engine._tag_driver([1], limit=50)
        assert driver is None and driver_params == [], "高密度应当让时间索引驱动"
        assert suffix

    def test_results_are_the_same_either_way(
        self, fresh_db: Any, corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两种计划必须给出同一批结果——计划只该影响速度，不该影响命中。"""
        from pc.adapters import fts_search

        default = _ids(svc.run_search(fresh_db, SearchQuery(limit=50, tags=("rain",))))
        monkeypatch.setattr(fts_search, "TAG_SCAN_LIMIT", 0)  # 强制走驱动集
        driven = _ids(svc.run_search(fresh_db, SearchQuery(limit=50, tags=("rain",))))
        assert default == driven

    def test_keyword_with_filters(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """首选交互路径：关键词 + 来源 + 时间范围。"""
        result = svc.run_search(
            fresh_db,
            SearchQuery(q="蓝发", limit=50, source="alpha", since="2026-01-05"),
        )
        assert _ids(result) == [corpus["ids"]["rain"]]

    def test_keyword_with_time_sort_keeps_the_keyword(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        """`--sort time` 只改排法，不能把关键词丢掉。

        早先的实现是"只有相关度排序才算关键词查询"，于是 `pc search 雨 --sort time` 退化成
        纯过滤列表，返回一堆不相干的内容且不报错——这条回归测试盯住它。
        """
        result = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50, sort="time"))
        assert result["sort"] == "time"
        assert set(_ids(result)) == {corpus["ids"]["rain"], corpus["ids"]["rain2"]}
        assert corpus["ids"]["snow"] not in _ids(result), "不含关键词的内容不该出现"

    def test_keyword_with_time_sort_pages_by_cursor(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        first = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=1, sort="time"))
        assert first["has_more"] is True and first["next_cursor"]
        second = svc.run_search(
            fresh_db, SearchQuery(q="蓝发", limit=1, sort="time", cursor=first["next_cursor"])
        )
        assert not (set(_ids(first)) & set(_ids(second)))
        assert _ids(first) == [corpus["ids"]["rain2"]], "按时间倒序：最新的是 rain2"


class TestKeywordPath:
    @pytest.mark.parametrize(
        ("query", "expected_key"),
        [
            ("雨", "rain"),  # 1 字：trigram 命中 0，必须走词级索引
            ("蓝发", "rain"),  # 2 字：同上（中文最常见的查询）
            ("蓝发精", "rain"),  # 3 字：trigram
            ("精灵", "rain"),
            ("sunset", "latin"),
            ("blue_hair", "latin"),
        ],
    )
    def test_matches_at_every_length(
        self, fresh_db: Any, corpus: dict[str, Any], query: str, expected_key: str
    ) -> None:
        result = svc.run_search(fresh_db, SearchQuery(q=query, limit=50))
        assert corpus["ids"][expected_key] in _ids(result), f"{query} 没命中"

    def test_two_characters_and_trigram_disagree_by_design(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        """设计点名的退化点：同一个 2 字查询，trigram 命中 0、词级索引命中 1。"""
        assert (
            fresh_db.execute(
                "SELECT count(*) FROM content_fts WHERE content_fts MATCH ?", ('"蓝发"',)
            ).fetchone()[0]
            == 0
        )
        assert (
            fresh_db.execute(
                "SELECT count(*) FROM content_word WHERE content_word MATCH ?", ('"蓝发"',)
            ).fetchone()[0]
            >= 1
        )

    def test_latin_prefix_matches(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """拉丁词走词级索引 + 前缀查询：只记得开头几个字母也能搜到（中文走子串，不需要前缀）。"""
        for query in ("portra", "portrait", "PORTRA"):
            assert corpus["ids"]["latin"] in _ids(
                svc.run_search(fresh_db, SearchQuery(q=query, limit=50))
            ), query

    def test_punctuation_only_query_is_not_an_error(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """纯标点/emoji 没有可搜词元：退回纯过滤路径，而不是让 FTS5 报语法错。"""
        result = svc.run_search(fresh_db, SearchQuery(q="*", limit=50))
        assert result["sort"] == "time"
        assert result["hits"], "当成没有关键词，正常列出内容"

    def test_multiple_terms_are_anded(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        both = svc.run_search(fresh_db, SearchQuery(q="蓝发 雨", limit=50))
        assert corpus["ids"]["rain"] in _ids(both)
        assert corpus["ids"]["latin"] not in _ids(both)

    def test_mixed_length_terms_use_both_indexes(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """长词取候选、短词当过滤器：混合长度查询不能因为"选错索引"漏掉。"""
        result = svc.run_search(fresh_db, SearchQuery(q="雨 精灵", limit=50))
        assert corpus["ids"]["rain"] in _ids(result)
        assert corpus["ids"]["latin"] not in _ids(result)

    def test_keyword_excludes_restricted_content(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50))
        assert corpus["ids"]["nsfw"] not in _ids(result)
        assert corpus["ids"]["deleted"] not in _ids(result)
        assert corpus["ids"]["quarantined"] not in _ids(result)

    def test_no_exact_total(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """§5.6 M1b：关键词检索不返回精确总数，只给候选数。"""
        result = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50))
        assert result["total"] is None
        assert result["candidates"] is not None

    def test_search_extra_is_searchable(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """来源名进了 search_extra，所以按来源名当关键词也能搜到。"""
        result = svc.run_search(fresh_db, SearchQuery(q="beta", limit=50))
        assert _ids(result) == [corpus["ids"]["snow"]]

    def test_syntax_characters_are_literal(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """用户输入里的 FTS5 语法字符只是普通文本，不能变成操作符或语法错误。"""
        for query in ("NEAR(", '"', "*", "^", "OR", "蓝发 OR 银发"):
            svc.run_search(fresh_db, SearchQuery(q=query, limit=50))  # 不抛异常即通过


class TestExplainability:
    def test_hit_explains_itself(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50))
        hit = next(item for item in result["hits"] if item["content_id"] == corpus["ids"]["rain"])
        assert hit["matched_terms"] == ["蓝发"]
        assert "text_content" in hit["matched_fields"]
        assert hit["snippet"] and "[蓝发]" in hit["snippet"]
        assert hit["filters_applied"]["statuses"] == ["active"]
        assert hit["filters_applied"]["nsfw_max_level"] == 0

    def test_tags_and_media_come_back(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=50, tags=("rain",)))
        hit = result["hits"][0]
        assert hit["tags"][0]["name"] == "rain"
        assert hit["tags"][0]["source"] == "human"

        image = svc.run_search(fresh_db, SearchQuery(limit=50, item_types=("image",)))["hits"][0]
        assert image["media"]["count"] == 1
        assert image["media"]["thumb_key"] == "aa/bb/thumb"
        assert image["media"]["width"] == 800

    def test_no_terms_means_no_snippet(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=1))
        assert result["hits"][0]["snippet"] is None
        assert result["hits"][0]["matched_terms"] == []


class TestPaging:
    def test_cursor_paging_walks_without_gaps_or_repeats(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        seen: list[int] = []
        cursor = None
        for _ in range(10):
            result = svc.run_search(fresh_db, SearchQuery(limit=2, cursor=cursor))
            seen += _ids(result)
            cursor = result["next_cursor"]
            if not result["has_more"]:
                break
        assert len(seen) == len(set(seen)), "翻页不能重复"
        assert set(seen) == set(
            _ids(svc.run_search(fresh_db, SearchQuery(limit=50)))
        ), "翻页不能漏"

    def test_relevance_page_is_inside_candidates(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        first = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=1, page=1))
        second = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=1, page=2))
        assert first["has_more"] is True
        assert second["page"] == 2
        assert set(_ids(first)) != set(_ids(second))

    def test_broken_cursor_is_rejected(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        with pytest.raises(SearchError, match="游标"):
            svc.run_search(fresh_db, SearchQuery(limit=2, cursor="这不是游标"))

    def test_page_size_is_capped(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        with pytest.raises(SearchError, match=str(MAX_PAGE_SIZE)):
            svc.run_search(fresh_db, SearchQuery(limit=MAX_PAGE_SIZE + 1))

    def test_page_size_setting_is_used(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        fresh_db.execute(
            "INSERT INTO setting (key, value, value_type, updated_at) "
            "VALUES ('search.default_page_size', '10', 'int', '2026-01-01T00:00:00.000Z')"
        )
        import os

        from pc import config as cfg

        registry = cfg.registry_index(cfg.load_registry())
        result = svc.run_search(
            fresh_db, SearchQuery(), registry=registry, yaml_defaults={}, env=os.environ
        )
        assert result["limit"] == 10, "页大小要读设置项（env > db > yaml > default）"


class TestTimeFields:
    """两个钟：入库时间（默认）与收到消息时间（`message.sent_at`）。"""

    def _seed_pair(self, conn: Any, source_id: int) -> dict[str, int]:
        """两条内容：一条有消息时间，一条没有（模拟手工上传）。"""
        return {
            "with_sent": _seed_item(
                conn,
                source_id=source_id,
                text="有消息时间的内容",
                first_seen_at="2026-03-01T00:00:00.000Z",
                sent_at="2026-01-05T00:00:00.000Z",
            ),
            "without_sent": _seed_item(
                conn,
                source_id=source_id,
                text="手工上传的内容",
                first_seen_at="2026-02-01T00:00:00.000Z",
                sent_at=None,
            ),
        }

    def test_default_is_ingest_time(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=1))
        assert result["time_field"] == "ingest"
        assert result["hits"][0]["first_seen_at"] > "2026-01-06"

    def test_sent_time_filters_on_message_time(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        ids = self._seed_pair(fresh_db, corpus["source_a"])
        result = svc.run_search(
            fresh_db,
            SearchQuery(limit=50, time_field="sent", since="2026-01-01", until="2026-01-31"),
        )
        assert _ids(result) == [ids["with_sent"]], "按消息时间筛，只有它落在范围里"
        assert result["time_field"] == "sent"
        assert result["filters_applied"]["time_field"] == "sent"

    def test_rows_without_sent_time_are_excluded_by_a_range(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        """`sent_at` 为空 = 没有消息时间，不该被任何时间范围"顺带"包含进来。"""
        ids = self._seed_pair(fresh_db, corpus["source_a"])
        result = svc.run_search(fresh_db, SearchQuery(limit=50, time_field="sent", since="2000-01-01"))
        assert ids["without_sent"] not in _ids(result)
        assert ids["with_sent"] in _ids(result)

    def test_sent_time_sort_puts_empty_values_last(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        ids = self._seed_pair(fresh_db, corpus["source_a"])
        result = svc.run_search(fresh_db, SearchQuery(limit=50, time_field="sent", sort="time"))
        order = _ids(result)
        assert order.index(ids["with_sent"]) < order.index(ids["without_sent"])
        assert result["hits"][-1]["sent_at"] is None, "没有消息时间的排在最后"

    def test_cursor_is_bound_to_its_time_field(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        """拿入库时间的游标去翻消息时间会翻出错页——必须明确报错。"""
        self._seed_pair(fresh_db, corpus["source_a"])
        first = svc.run_search(fresh_db, SearchQuery(limit=1, sort="time"))
        assert first["next_cursor"]
        with pytest.raises(SearchError, match="time-field"):
            svc.run_search(
                fresh_db,
                SearchQuery(limit=1, sort="time", time_field="sent", cursor=first["next_cursor"]),
            )

    def test_invalid_time_field_is_rejected(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        with pytest.raises(SearchError, match="时间字段"):
            svc.run_search(fresh_db, SearchQuery(limit=5, time_field="yesterday"))

    def test_keyword_with_sent_time_still_filters_by_keyword(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        """换时间字段只改"看哪个钟"，不能把关键词丢掉。"""
        ids = self._seed_pair(fresh_db, corpus["source_a"])
        result = svc.run_search(
            fresh_db, SearchQuery(q="手工上传", limit=50, time_field="sent", sort="time")
        )
        assert _ids(result) == [ids["without_sent"]]


class TestNameAndMediaFields:
    def test_hit_carries_the_file_name(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        content_id = _seed_item(
            fresh_db,
            source_id=corpus["source_a"],
            item_type="image",
            original_name="电脑壁纸.jpg",
            text=None,
        )
        hit = svc.run_search(fresh_db, SearchQuery(q="壁纸", limit=10))["hits"][0]
        assert hit["content_id"] == content_id
        assert hit["original_name"] == "电脑壁纸.jpg"
        assert "电脑壁纸.jpg" in hit["search_extra"]

    def test_hit_carries_mime_and_extension(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        media_hit = svc.run_search(fresh_db, SearchQuery(limit=50, item_types=("image",)))["hits"][0]
        assert media_hit["media"]["mime_detected"] == "image/jpeg"
        assert media_hit["media"]["extension"] == "jpg"


class TestErrors:
    def test_unknown_source_lists_known_ones(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        with pytest.raises(SearchError, match="找不到来源"):
            svc.run_search(fresh_db, SearchQuery(source="nope"))

    def test_unknown_tag_suggests_alternatives(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        with pytest.raises(SearchError, match="找不到标签"):
            svc.run_search(fresh_db, SearchQuery(tags=("blu_hair",)))

    def test_bad_time_format(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        with pytest.raises(SearchError, match="时间格式"):
            svc.run_search(fresh_db, SearchQuery(since="上周"))

    def test_bad_nsfw_level(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        with pytest.raises(SearchError, match="nsfw_level"):
            svc.run_search(fresh_db, SearchQuery(nsfw_max_level=9))

    def test_bad_page(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        with pytest.raises(SearchError, match="页码"):
            svc.run_search(fresh_db, SearchQuery(page=0))


class TestBudgets:
    #: FTS5 内部会自己发一批 ``--`` 前缀的索引查询，PRAGMA 也是引擎自己的动作；
    #: 语句计数只统计**应用发出的**语句（否则数出来的是 FTS5 的实现细节）。
    @staticmethod
    def _count_own_statements(conn: Any, run: Any) -> list[str]:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            run()
        finally:
            conn.set_trace_callback(None)
        return [
            sql
            for sql in statements
            if not sql.lstrip().startswith("--") and not sql.lstrip().upper().startswith("PRAGMA")
        ]

    def test_assembly_is_not_n_plus_one(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """§5.6 M2：组装固定三条 SQL。这里数的是**应用执行了多少条语句**。

        关键词一页的正常路径是 5 条（候选、候选内过滤、主查询、tag 批量、媒体批量）；
        这里留到 6 是给候选升级到 2000 那一档留的余量。纯过滤路径是 4 条
        （主查询、计数、tag、媒体）。
        """
        statements = self._count_own_statements(
            fresh_db, lambda: svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50))
        )
        assert len(statements) <= 6, f"关键词一页用了 {len(statements)} 条语句：{statements}"

        statements = self._count_own_statements(
            fresh_db, lambda: svc.run_search(fresh_db, SearchQuery(limit=50))
        )
        assert len(statements) <= 5, f"纯过滤一页用了 {len(statements)} 条语句：{statements}"

    def test_exhausted_candidates_do_not_escalate(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """候选没取满就不该升档：那是白白多跑两条 SQL（§5.6 M1 的分档是"不够才升"）。"""
        statements = self._count_own_statements(
            fresh_db, lambda: svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50))
        )
        assert len(statements) == 5, f"应当正好 5 条：{statements}"

    def test_no_per_row_queries(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """一页 50 条也不能变成 50 条 SQL：条数不随结果数增长。"""
        many: list[int] = []
        source_id = corpus["source_a"]
        for index in range(30):
            many.append(
                _seed_item(
                    fresh_db,
                    source_id=source_id,
                    text=f"批量内容第{index}条",
                    source_label="alpha",
                    first_seen_at=f"2026-02-{index + 1:02d}T00:00:00.000Z",
                )
            )
        one = self._count_own_statements(
            fresh_db, lambda: svc.run_search(fresh_db, SearchQuery(limit=1))
        )
        full = self._count_own_statements(
            fresh_db, lambda: svc.run_search(fresh_db, SearchQuery(limit=50))
        )
        assert len(full) <= len(one) + 1, f"一页 1 条用 {len(one)} 条，50 条用 {len(full)} 条"

    def test_timings_are_reported(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50))
        for stage in ("candidates", "filter", "main", "tags", "media", "total"):
            assert stage in result["timings"], stage
            assert result["timings"][stage] >= 0

    def test_incomplete_is_flagged_when_candidates_run_out(
        self, fresh_db: Any, corpus: dict[str, Any]
    ) -> None:
        """候选上限设成 1 时，一页都凑不满——必须如实标注，而不是让人以为"就这些"。"""
        engine = SqliteFtsSearch(fresh_db, candidate_caps=(1,))
        result = svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50), index=engine)
        assert result["candidate_cap"] == 1
        assert result["incomplete"] is True

    def test_filter_path_limit_is_respected(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        result = svc.run_search(fresh_db, SearchQuery(limit=1))
        assert len(result["hits"]) == 1
        assert result["has_more"] is True


class TestAdapterContract:
    def test_index_recomputes_one_row(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        """打标切片写完标签后要调它：标签名进 search_extra 与词级索引。"""
        content_id = corpus["ids"]["image"]
        tag_id = int(
            fresh_db.execute(
                "INSERT INTO tag (uuid, name, name_norm, namespace) "
                "VALUES ('t-tag', '夜雨', '夜雨', 'general') RETURNING tag_id"
            ).fetchone()[0]
        )
        fresh_db.execute(
            "INSERT INTO tag_assignment (content_id, tag_id, source) VALUES (?, ?, 'human')",
            (content_id, tag_id),
        )
        engine = SqliteFtsSearch(fresh_db)
        engine.index(content_id)
        assert (
            fresh_db.execute(
                "SELECT count(*) FROM content_word WHERE content_word MATCH ?", ('"夜雨"',)
            ).fetchone()[0]
            == 1
        )

    def test_delete_is_a_documented_no_op(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        engine = SqliteFtsSearch(fresh_db)
        assert engine.delete(corpus["ids"]["rain"]) is None

    def test_rebuild_keeps_hits(self, fresh_db: Any, corpus: dict[str, Any]) -> None:
        engine = SqliteFtsSearch(fresh_db)
        before = _ids(svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50)))
        engine.rebuild(batch_size=2)
        assert _ids(svc.run_search(fresh_db, SearchQuery(q="蓝发", limit=50))) == before
