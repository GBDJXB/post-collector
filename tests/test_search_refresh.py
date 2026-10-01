"""派生检索列的重算（``refresh_search_derived``）：老行怎么变得可搜。

两列派生数据（``text_search``、``search_extra``）由应用层算出来写在表里，索引只是把
它们搬进倒排表。所以"搜不到"有两种原因，处置也不同：

* 索引坏了 → 重建索引（``reindex_fts``）；
* 派生列过期或从来没算过 → **先重算再重建**（本文件测的就是这一步）。

最现实的场景：本仓库在接检索层之前落库的内容，``text_search`` 是 NULL
（那时刻意留空，因为还没有分词器）。不重算的话，1~2 字中文查询对这批内容永远无结果，
而且**不报错**——只是搜不到。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from pc.adapters import sqlite as db


def _seed_source(conn: Any, name: str = "mine", platform: str = "local") -> int:
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
    msg_id: str = "m1",
    sender: str | None = None,
    text_search: str | None = None,
    tags: tuple[str, ...] = (),
    deleted: bool = False,
) -> int:
    """直接写一条内容项；``text_search`` 默认留空，模拟升级前落库的老行。"""
    message_id = int(
        conn.execute(
            "INSERT INTO message (uuid, source_id, platform_msg_id, sender_display_name, text_raw) "
            "VALUES (?, ?, ?, ?, ?) RETURNING message_id",
            (str(uuid.uuid4()), source_id, msg_id, sender, text),
        ).fetchone()[0]
    )
    content_id = int(
        conn.execute(
            "INSERT INTO content_item "
            "(uuid, message_id, item_type, ordinal, text_content, text_search, deleted_at) "
            "VALUES (?, ?, 'text', 0, ?, ?, ?) RETURNING content_id",
            (
                str(uuid.uuid4()),
                message_id,
                text,
                text_search,
                "2026-01-01T00:00:00.000Z" if deleted else None,
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


def _match(conn: Any, table: str, term: str) -> int:
    return int(
        conn.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH ?", (f'"{term}"',)
        ).fetchone()[0]
    )


def _row(conn: Any, content_id: int) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM content_item WHERE content_id = ?", (content_id,)).fetchone())


class TestStaleCount:
    def test_counts_rows_with_text_but_no_tokens(self, fresh_db: Any) -> None:
        source_id = _seed_source(fresh_db)
        _seed_item(fresh_db, source_id=source_id, text="蓝发精灵")
        assert db.stale_search_row_count(fresh_db) == 1

    def test_rows_without_any_searchable_text_are_not_stale(self, fresh_db: Any) -> None:
        """四列全空的媒体项没什么可分词的，不该被反复当成"还没分词"。"""
        source_id = _seed_source(fresh_db)
        _seed_item(fresh_db, source_id=source_id, text=None)
        assert db.stale_search_row_count(fresh_db) == 0

    def test_soft_deleted_rows_are_not_counted(self, fresh_db: Any) -> None:
        source_id = _seed_source(fresh_db)
        _seed_item(fresh_db, source_id=source_id, text="蓝发精灵", deleted=True)
        assert db.stale_search_row_count(fresh_db) == 0

    def test_rows_with_tokens_are_not_stale(self, fresh_db: Any) -> None:
        source_id = _seed_source(fresh_db)
        _seed_item(fresh_db, source_id=source_id, text="蓝发精灵", text_search="蓝 发 蓝发")
        assert db.stale_search_row_count(fresh_db) == 0


class TestRefresh:
    def test_old_row_becomes_searchable_by_one_and_two_characters(self, fresh_db: Any) -> None:
        """这条是整件事的目的：老内容在重算之后 1 字与 2 字都能搜到。"""
        source_id = _seed_source(fresh_db)
        content_id = _seed_item(fresh_db, source_id=source_id, text="蓝发精灵在雨里")

        before = _row(fresh_db, content_id)
        assert before["text_search"] is None
        assert _match(fresh_db, "content_word", "蓝发") == 0, "没重算之前词级索引里什么都没有"

        result = db.refresh_search_derived(fresh_db)
        assert result["updated"] == 1

        after = _row(fresh_db, content_id)
        tokens = after["text_search"].split()
        assert {"蓝", "发", "蓝发", "雨", "精灵"} <= set(tokens)
        assert _match(fresh_db, "content_word", "蓝发") == 1, "2 字查询：trigram 命中 0，词级索引必须命中"
        assert _match(fresh_db, "content_word", "雨") == 1, "1 字查询同理"
        assert _match(fresh_db, "content_fts", "蓝发") == 0, "design 点名的退化点：2 字在 trigram 上是 0"
        assert _match(fresh_db, "content_fts", "蓝发精") == 1

    def test_tag_names_enter_search_extra(self, fresh_db: Any) -> None:
        """``search_extra`` 含标签名；改标签之后必须重算，否则检索落后于事实（§5.1）。"""
        source_id = _seed_source(fresh_db, name="我的素材", platform="local")
        content_id = _seed_item(
            fresh_db, source_id=source_id, text="一张图", tags=("blue_hair", "rain")
        )
        db.refresh_search_derived(fresh_db)
        row = _row(fresh_db, content_id)
        assert row["search_extra"].startswith("我的素材 local")
        assert "blue_hair" in row["search_extra"] and "rain" in row["search_extra"]

        assert _match(fresh_db, "content_fts", "blue_hair") == 1, "标签名要能被搜到"
        assert "blue_hair" in row["text_search"].split(), "短查询也要能命中（词级索引）"

    def test_sender_name_enters_search_extra(self, fresh_db: Any) -> None:
        source_id = _seed_source(fresh_db)
        content_id = _seed_item(fresh_db, source_id=source_id, text="一段话", sender="蓝发精灵")
        db.refresh_search_derived(fresh_db)
        assert "蓝发精灵" in _row(fresh_db, content_id)["search_extra"]

    def test_is_idempotent(self, fresh_db: Any) -> None:
        source_id = _seed_source(fresh_db)
        _seed_item(fresh_db, source_id=source_id, text="蓝发精灵")
        first = db.refresh_search_derived(fresh_db)
        second = db.refresh_search_derived(fresh_db)
        assert first["updated"] == 1
        assert second["updated"] == 0, "值没变就不写：重算命令要能安全地反复跑"
        assert second["scanned"] == 1

    def test_batching_covers_every_row(self, fresh_db: Any) -> None:
        source_id = _seed_source(fresh_db)
        for index in range(5):
            _seed_item(fresh_db, source_id=source_id, text=f"第{index}条", msg_id=f"m{index}")
        result = db.refresh_search_derived(fresh_db, batch_size=2)
        assert result["updated"] == 5
        assert result["total"] == 5

    def test_progress_is_reported(self, fresh_db: Any) -> None:
        source_id = _seed_source(fresh_db)
        for index in range(3):
            _seed_item(fresh_db, source_id=source_id, text=f"第{index}条", msg_id=f"m{index}")
        seen: list[tuple[int, int]] = []
        db.refresh_search_derived(fresh_db, batch_size=2, progress=lambda done, total: seen.append((done, total)))
        assert seen[-1] == (3, 3)

    def test_refresh_then_reindex_keeps_hits(self, fresh_db: Any) -> None:
        """重算 + 重建索引之后命中不变：索引是派生数据，重建不该改变结果。"""
        source_id = _seed_source(fresh_db)
        _seed_item(fresh_db, source_id=source_id, text="蓝发精灵在雨里")
        db.refresh_search_derived(fresh_db)
        before = _match(fresh_db, "content_word", "蓝发")
        db.reindex_fts(fresh_db)
        assert _match(fresh_db, "content_word", "蓝发") == before == 1

    def test_empty_database_is_fine(self, fresh_db: Any) -> None:
        result = db.refresh_search_derived(fresh_db)
        assert result == {"scanned": 0, "updated": 0, "total": 0}

    def test_soft_deleted_rows_are_refreshed_too(self, fresh_db: Any) -> None:
        """软删的行也要算——它可能被恢复，恢复后立刻可搜才不意外。"""
        source_id = _seed_source(fresh_db)
        content_id = _seed_item(fresh_db, source_id=source_id, text="蓝发精灵", deleted=True)
        assert db.refresh_search_derived(fresh_db)["updated"] == 1
        assert "蓝发" in _row(fresh_db, content_id)["text_search"].split()


class TestSearchExtraShape:
    def test_json_columns_still_valid_after_refresh(self, fresh_db: Any) -> None:
        """重算只碰两列 TEXT，不该影响其它 JSON 列（``json_valid`` 约束仍然成立）。"""
        source_id = _seed_source(fresh_db)
        content_id = _seed_item(fresh_db, source_id=source_id, text="蓝发精灵")
        db.refresh_search_derived(fresh_db)
        row = _row(fresh_db, content_id)
        json.loads(row["sensitive_flags"])  # CHECK (json_valid(...)) 兜底，这里再确认一次
        assert row["status"] == "active"
        assert row["updated_at"] >= row["created_at"]
