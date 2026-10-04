"""FTS5 检索适配器：DESIGN §5.1 的双索引查询 + §5.6 的达成机制。

**为什么是两个索引**：`content_fts`（trigram）擅长任意位置的子串，但**查询串少于 3 个
字符时命中 0 行且不报错**；`content_word`（unicode61）建在应用层分词结果上，负责
1~2 字的词级查询（中文最常见的查询）。查询构造先看每个词的长度，再决定用哪个索引取
候选，两个索引的命中部件合并后按帖去重，再逐词判断同帖可见部件是否满足 AND。
`雨 蓝发精` 这种混合长度查询因此可以跨部件命中。

**达成机制**（§5.6，实现时必须遵守）：

* M1：纯过滤/浏览走 B 树索引 + **键集分页**，不用 ``OFFSET``；关键词走 ``MATCH`` 取
  bm25 前 N 条候选（N 从 500 升到 2000 两档），在候选集内做过滤；候选扫完仍凑不满一页
  时返回 ``incomplete=True``，由上层如实告知"结果可能不完整"。
* M1b：关键词检索**不返回精确总数**（``COUNT(*)`` 可能比取一页还贵），只给候选数。
* M2：组装固定三条 SQL（主查询 + tag 批量 + 媒体批量），**不允许 N+1**——有测试用
  语句计数盯着。
* M6：返回各段耗时（candidates / filter / main / tags / media / total），慢查询一眼能
  看出是哪一段。

分词与转义：用户输入一律当**普通文本**——每个词用 FTS5 短语引号包起来（内部引号翻倍），
多词之间是 ``AND``。于是不可能出现语法注入，也不可能因为输入里的 ``NEAR``/``*``/``^``
意外改变语义。

引擎可替换：本类只实现 :class:`pc.ports.store.SearchIndex`，业务层拿到的过滤器是结构化
字典，换引擎时替换本文件与 post_search.py 的适配实现。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping, Sequence
from typing import Any

from pc.adapters import sqlite as db
from pc.adapters.post_search import PostSearch
from pc.adapters.tag_normalizer import effective_assignment
from pc.domain import is_cjk, normalize_text, tokenize_document, tokenize_query

__all__ = [
    "BM25_WEIGHTS",
    "MAX_PAGE_SIZE",
    "TIME_FIELDS",
    "TIME_FIELD_INGEST",
    "TIME_FIELD_SENT",
    "SearchQueryError",
    "SqliteFtsSearch",
]

#: ``content_fts`` 的列权重（列序：text_content, ocr_text, caption, search_extra）。
#: 没有评估集之前不调参，这组值只是"正文 > 配文 > 标签与来源 > OCR"这个常识排序。
BM25_WEIGHTS: tuple[float, ...] = (1.0, 0.5, 0.8, 0.4)

#: 候选集上限的两档（DESIGN §5.6 M1）：先 500，不够再升到 2000
DEFAULT_CANDIDATE_CAPS: tuple[int, ...] = (500, 2000)

#: 每页条数硬上限：与 §5.6 的预算绑定，设置项也只能到 50
MAX_PAGE_SIZE = 50

#: 时间字段的两种取值：入库时间（内容项第一次被看到）与消息时间（`message.sent_at`）。
#: 手工上传的 `sent_at` 是空的（本地文件的 mtime 不是"发布时间"，所以刻意留空），
#: 因此按消息时间过滤会排除它们——这是有意的，见 `_where` 与 CLI 的 footer 说明。
TIME_FIELD_INGEST = "ingest"
TIME_FIELD_SENT = "sent"
TIME_FIELDS: tuple[str, ...] = (TIME_FIELD_INGEST, TIME_FIELD_SENT)

#: 精确统计的上限（§5.6 M1b）：超过它就不再数下去，改为如实说"至少这么多"。
#: 十万条库上 `COUNT(*)` 要几百毫秒，而它换来的信息是"还有更多"——不值。
COUNT_CAP = 5000

#: 标签查询的两条路径用**估算的扫描行数**来挑（见 :meth:`SqliteFtsSearch._tag_driver`）：
#: 预计沿时间索引扫不超过这么多行时，就让时间索引驱动、标签用 EXISTS 逐行判定；
#: 否则物化最小的标签集当驱动表。两张表上的实测差一个数量级（DESIGN §3.7）。
TAG_SCAN_LIMIT = 3000

#: 提供给 snippet 的窗口半径（字符数）
SNIPPET_RADIUS = 24


class SearchQueryError(ValueError):
    """查询本身不合法（游标坏了、页码越界等），消息面向人。"""


def _phrase(term: str) -> str:
    """把一个词变成 FTS5 短语：引号包裹、内部引号翻倍。

    这样用户输入的 ``NEAR`` / ``*`` / ``^`` / 引号都只是普通字符——**不做语法透传**，
    否则一次手滑就能构造出语义完全不同的查询，甚至是语法错误。
    """
    escaped = term.replace('"', '""')
    return f'"{escaped}"'


def _prefix(token: str) -> str:
    """把拉丁词元变成 FTS5 前缀查询（``portra*`` 命中 portrait）。

    星号是我们自己加的，用户输入里的 ``*`` 从来不会走到这条路上。
    """
    return f"{token}*"


def _and_expr(parts: Sequence[str]) -> str:
    return " AND ".join(parts)


def _has_cjk(text: str) -> bool:
    return any(is_cjk(char) for char in text)


def _plan_terms(terms: Sequence[str]) -> dict[str, Any]:
    """把用户的词分派到两个索引上（§5.1 的"先看长度与分词结果，再决定查哪个索引"）。

    规则与理由：

    * **CJK 且 ≥3 字** → ``content_fts``（trigram）：子串与容错。trigram 少于 3 字命中 0，
      所以这条路径的长度下限是硬要求。
    * **CJK 且 1~2 字** → ``content_word``：中文最常见的查询，必须走词级索引。
    * **拉丁/数字/下划线** → ``content_word`` + **前缀匹配**。拉丁词在词级索引里是整词
      token（``blue_hair`` 经 unicode61 会分成 ``blue`` ``hair`` 两个相邻 token），
      前缀查询同时满足"整词"与"只记得开头几个字母"两种输入；走 trigram 反而会让
      ``blue_hair``（下划线）命中不了 ``blue hair``（空格）。
    * **没有任何可搜词元的词**（纯标点、emoji、单个引号）→ 丢掉。让它们进 MATCH 会得到
      空短语，FTS5 直接报语法错——用户输入 ``*`` 不该看到一个数据库错误。
    """
    trigram_terms: list[str] = []
    word_parts: list[str] = []
    for term in terms:
        text = normalize_text(term)
        if not text or not tokenize_document(text):
            continue
        if _has_cjk(text):
            if len(text) >= 3:
                trigram_terms.append(text)
            else:
                word_parts.append(_phrase(text))
        else:
            word_parts.extend(_prefix(token) for token in tokenize_query(text))
    if not trigram_terms and not word_parts:
        return {}
    return {
        "trigram": _and_expr([_phrase(term) for term in trigram_terms]) or None,
        "word": _and_expr(word_parts) or None,
        "source": "content_fts" if trigram_terms else "content_word",
    }


class SqliteFtsSearch(PostSearch):
    """``SearchIndex`` 的 FTS5 实现。

    构造时接收一个已打开的连接：**适配器不自己开数据库连接**（分层的硬要求，
    见 AGENTS「迁移与数据」）。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        candidate_caps: Sequence[int] = DEFAULT_CANDIDATE_CAPS,
        page_size: int = MAX_PAGE_SIZE,
    ) -> None:
        self._conn = conn
        self._caps = tuple(sorted({max(1, int(cap)) for cap in candidate_caps})) or (500,)
        self._page_size = min(MAX_PAGE_SIZE, max(1, int(page_size)))

    # -- SearchIndex 端口 ---------------------------------------------------

    def index(self, doc_id: int) -> None:
        """重算这一行的派生列（分词结果与检索补充字段）。

        索引行由触发器从 ``content_item`` 搬进 FTS5，所以这里**不写倒排表**，只把
        派生列算新——打标切片写完 ``tag_assignment`` 之后必须调用它，否则
        "按标签名搜"会落后于事实。
        """
        db.refresh_search_derived(self._conn, content_ids=[int(doc_id)])

    def index_many(self, doc_ids: Sequence[int]) -> None:
        """批量刷新标签索引，参与调用方事务；独立调用时自建事务。"""
        if not doc_ids:
            return
        if self._conn.in_transaction:
            db.refresh_tag_search(self._conn, doc_ids)
        else:
            with db.transaction(self._conn):
                db.refresh_tag_search(self._conn, doc_ids)

    def delete(self, doc_id: int) -> None:
        """有意为之的空操作。

        软删只置 ``deleted_at``，检索侧用 ``WHERE c.deleted_at IS NULL`` 兜住；
        硬删会触发 ``content_fts_ad`` / ``content_word_ad``，索引行自动清理。
        这个方法是端口契约的一部分（将来换 Meilisearch 时它必须真的删文档）。
        """
        return None

    def rebuild(self, *, batch_size: int = 500) -> dict[str, Any]:
        """全量重算派生列 + 重建两个索引；``pc db reindex --recompute-derived`` 的同一条路。"""
        refreshed = db.refresh_search_derived(self._conn, batch_size=batch_size)
        reindexed = db.reindex_fts(self._conn)
        return {"refreshed": refreshed, "reindexed": reindexed}

    # -- 组装（M2：主查询 + tag 批量 + 媒体批量） ---------------------------

    def _assemble(
        self,
        rows: Sequence[Any],
        ids: Sequence[int],
        terms: Sequence[str],
        filters: Mapping[str, Any],
        scores: Mapping[int, float] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, float]]:
        timings: dict[str, float] = {}
        if not ids:
            return [], {"tags": 0.0, "media": 0.0}
        marks = ",".join("?" * len(ids))

        mark = time.perf_counter()
        tags: dict[int, list[dict[str, Any]]] = {}
        for row in self._conn.execute(
            "SELECT ta.content_id, t.name, t.namespace, ta.confidence, ta.source"
            "  FROM tag_assignment ta JOIN tag t ON t.tag_id = ta.tag_id"
            f" WHERE ta.content_id IN ({marks}) AND {effective_assignment()}"
            " ORDER BY t.namespace, t.name",
            list(ids),
        ).fetchall():
            tags.setdefault(int(row["content_id"]), []).append(
                {
                    "name": row["name"],
                    "namespace": row["namespace"],
                    "confidence": row["confidence"],
                    "source": row["source"],
                }
            )
        timings["tags"] = (time.perf_counter() - mark) * 1000

        mark = time.perf_counter()
        media: dict[int, dict[str, Any]] = {}
        for row in self._conn.execute(
            "SELECT cm.content_id, m.object_key, m.thumb_key, m.width, m.height, m.scan_status,"
            "       m.mime_detected, m.extension, m.storage_backend_id, m.quarantine_key"
            "  FROM content_media cm JOIN media m ON m.media_id = cm.media_id"
            f" WHERE cm.content_id IN ({marks}) ORDER BY cm.ordinal",
            list(ids),
        ).fetchall():
            content_id = int(row["content_id"])
            entry = media.setdefault(
                content_id,
                {
                    "count": 0,
                    "object_key": row["object_key"],
                    "thumb_key": row["thumb_key"],
                    "width": row["width"],
                    "height": row["height"],
                    "scan_status": row["scan_status"],
                    "mime_detected": row["mime_detected"],
                    "extension": row["extension"],
                    "storage_backend_id": row["storage_backend_id"],
                    "quarantine_key": row["quarantine_key"],
                    "items": [],
                },
            )
            entry["count"] += 1
            entry["items"].append(dict(row))
            if entry["thumb_key"] is None:
                entry["thumb_key"] = row["thumb_key"]
            if row["width"] and (not entry["width"] or row["width"] > entry["width"]):
                entry["width"] = row["width"]
            if row["height"] and (not entry["height"] or row["height"] > entry["height"]):
                entry["height"] = row["height"]
        timings["media"] = (time.perf_counter() - mark) * 1000

        hits: list[dict[str, Any]] = []
        for row in rows:
            content_id = int(row["content_id"])
            fields = {
                "text_content": row["text_content"] or "",
                "ocr_text": row["ocr_text"] or "",
                "caption": row["caption"] or "",
                "search_extra": row["search_extra"] or "",
            }
            matched, matched_fields = self._explain(fields, terms)
            hits.append(
                {
                    "content_id": content_id,
                    "item_type": row["item_type"],
                    "status": row["status"],
                    "nsfw_level": int(row["nsfw_level"]),
                    "first_seen_at": row["first_seen_at"],
                    "original_name": row["original_name"],
                    "source_id": int(row["source_id"]),
                    "source": row["source_name"],
                    "sent_at": row["sent_at"],
                    "origin_note": row["origin_note"],
                    "sender": row["sender_display_name"],
                    "text": fields["text_content"],
                    "search_extra": fields["search_extra"],
                    "snippet": self._snippet(fields, terms),
                    "matched_terms": matched,
                    "matched_fields": matched_fields,
                    "tags": tags.get(content_id, []),
                    "media": media.get(content_id, {"count": 0}),
                    "score": None if scores is None else scores.get(content_id),
                    "filters_applied": self._describe_filters(filters),
                }
            )
        return hits, timings

    @staticmethod
    def _describe_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
        """把生效的过滤器原样带回去：结果要能解释"为什么它在这里"（§5.5）。"""
        described = {
            "statuses": list(filters.get("statuses") or ("active",)),
            "nsfw_max_level": filters.get("nsfw_max_level"),
            "item_types": list(filters.get("item_types") or ()),
            "source_id": filters.get("source_id"),
            "tag_ids": list(filters.get("tag_ids") or ()),
            "since": filters.get("since"),
            "until": filters.get("until"),
            "fold_duplicates": bool(filters.get("fold_duplicates", True)),
        }
        return {key: value for key, value in described.items() if value not in (None, [], ())}

    @staticmethod
    def _explain(fields: Mapping[str, str], terms: Sequence[str]) -> tuple[list[str], list[str]]:
        """哪些词命中了、命中在哪些字段（§5.5 的"为什么它出现在这里"）。

        判定是**字面出现**：能进结果集就说明索引侧匹配过，这里只负责说清是哪个词、
        落在哪一列，不额外发起查询（否则就是 N+1）。
        """
        matched: list[str] = []
        matched_fields: list[str] = []
        for term in terms:
            needle = term.casefold()
            hit_fields = [name for name, text in fields.items() if needle in text.casefold()]
            if hit_fields:
                matched.append(term)
                for name in hit_fields:
                    if name not in matched_fields:
                        matched_fields.append(name)
        return matched, matched_fields

    @staticmethod
    def _snippet(fields: Mapping[str, str], terms: Sequence[str]) -> str | None:
        """在已取回的文本里截一段窗口并标出命中词（应用层做，不额外查库）。

        刻意**不用** FTS5 的 ``snippet()``：那需要为每一行再跑一次 FTS 查询，50 行就是
        50 次调用——正是 §5.6 说的 N+1。文本已经在这条 SQL 里取回来了（限长截断）。
        """
        if not terms:
            return None
        for name in ("text_content", "caption", "ocr_text", "search_extra"):
            text = fields.get(name) or ""
            if not text:
                continue
            lowered = text.casefold()
            positions = [(lowered.find(term.casefold()), term) for term in terms]
            hits = [(index, term) for index, term in positions if index >= 0]
            if not hits:
                continue
            index, term = min(hits, key=lambda item: item[0])
            start = max(0, index - SNIPPET_RADIUS)
            end = min(len(text), index + len(term) + SNIPPET_RADIUS)
            window = text[start:end]
            local = window.casefold().find(term.casefold())
            if local >= 0:
                window = (
                    f"{window[:local]}[{window[local : local + len(term)]}]"
                    f"{window[local + len(term) :]}"
                )
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            return f"{prefix}{window}{suffix}"
        return None
