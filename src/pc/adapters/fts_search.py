"""FTS5 检索适配器：DESIGN §5.1 的双索引查询 + §5.6 的达成机制。

**为什么是两个索引**：`content_fts`（trigram）擅长任意位置的子串，但**查询串少于 3 个
字符时命中 0 行且不报错**；`content_word`（unicode61）建在应用层分词结果上，负责
1~2 字的词级查询（中文最常见的查询）。查询构造先看每个词的长度，再决定用哪个索引取
候选，另一个索引退化成"候选集内的过滤器"——所以 `雨 蓝发精` 这种混合长度的查询依然是
**一条 SQL**，不会漏掉任何一半。

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
字典，换 Meilisearch 时改这一个文件。
"""

from __future__ import annotations

import base64
import binascii
import sqlite3
import time
from collections.abc import Mapping, Sequence
from typing import Any

from pc.adapters import sqlite as db
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


class SqliteFtsSearch:
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

    def query(
        self,
        q: str,
        filters: dict[str, Any] | None = None,
        sort: str | None = None,
        page: int = 1,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        filters = dict(filters or {})
        terms = [term for term in (q or "").split() if term]
        size = min(self._page_size, max(1, int(limit or self._page_size)))
        order = sort or ("relevance" if terms else "time")
        if order not in {"relevance", "time"}:
            raise SearchQueryError(f"排序方式只能是 relevance 或 time，收到 {order!r}")
        if order == "relevance" and not terms:
            order = "time"  # 没有关键词就没有相关度可言，退化成时间序

        timings: dict[str, float] = {}
        # 有没有关键词决定走哪条路径；`sort` 只决定**排法**，不能拿它当"没有关键词"用：
        # 早先的写法是"只有 relevance 才算关键词查询"，于是 `--sort time 雨` 会退化成
        # 纯过滤列表、把关键词整个丢掉（返回一堆不相干的结果，还不报错）。
        plan = self._candidate_plan(terms)

        if plan is None:
            return self._query_by_filter(
                terms=terms,
                filters=filters,
                size=size,
                cursor=cursor,
                page=page,
                timings=timings,
                started=started,
            )
        return self._query_by_keyword(
            terms=terms,
            plan=plan,
            filters=filters,
            size=size,
            page=page,
            cursor=cursor,
            order=order,
            timings=timings,
            started=started,
        )

    # -- 两条路径 -----------------------------------------------------------

    def _query_by_filter(
        self,
        *,
        terms: Sequence[str],
        filters: Mapping[str, Any],
        size: int,
        cursor: str | None,
        page: int,
        timings: dict[str, float],
        started: float,
    ) -> dict[str, Any]:
        """纯过滤/浏览：B 树索引 + 键集分页，没有候选集（§5.6 M1 的第一条路径）。"""
        driver, tag_clauses, driver_params, suffix_params = self._tag_driver(
            filters.get("tag_ids") or (), limit=size
        )
        time_column = self._time_column(filters)
        base_where, base_params = self._where(filters, skip_tags=True)
        if tag_clauses:
            base_where = base_where + " AND " + " AND ".join(tag_clauses)
        # 参数顺序必须与 SQL 文本一致：驱动子查询（FROM）→ 基础条件（WHERE）→ 追加的 EXISTS
        base_params = [*driver_params, *base_params, *suffix_params]
        where, params = base_where, list(base_params)
        offset = 0
        if cursor:
            moment, content_id = self._decode_cursor(cursor, field=self._time_field(filters))
            where += f" AND ({time_column}, c.content_id) < (?, ?)"
            params += [moment, content_id]
        else:
            offset = max(0, (max(1, int(page)) - 1) * size)

        mark = time.perf_counter()
        rows = self._fetch_page(
            where,
            params,
            size + 1,
            offset,
            driver=driver,
            order_column=time_column,
            filters=filters,
        )
        timings["main"] = (time.perf_counter() - mark) * 1000
        has_more = len(rows) > size
        page_rows = rows[:size]
        page_ids = [int(row["content_id"]) for row in page_rows]

        mark = time.perf_counter()
        total, total_kind = self._total_for(
            base_where, base_params, filters, driver=driver
        )
        timings["count"] = (time.perf_counter() - mark) * 1000

        hits, tail = self._assemble(page_rows, page_ids, terms, filters)
        timings.update(tail)
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            moment = last["sent_at"] if time_column == "m.sent_at" else last["first_seen_at"]
            next_cursor = self._encode_cursor(
                self._time_field(filters), str(moment), int(last["content_id"])
            )
        timings["total"] = (time.perf_counter() - started) * 1000
        return {
            "hits": hits,
            "total": total,
            "total_kind": total_kind,
            "candidates": None,
            "candidate_cap": None,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "incomplete": False,
            "terms": list(terms),
            "sort": "time",
            "time_field": self._time_field(filters),
            "limit": size,
            "timings": timings,
        }

    def _query_by_keyword(
        self,
        *,
        terms: Sequence[str],
        plan: Mapping[str, Any],
        filters: Mapping[str, Any],
        size: int,
        page: int,
        cursor: str | None,
        order: str,
        timings: dict[str, float],
        started: float,
    ) -> dict[str, Any]:
        """关键词：bm25 取候选 → 候选集内过滤 → 组装一页（§5.6 M1 / M1b / M2）。

        ``order="time"`` 时仍以候选集为范围，只是把这一页按时间倒序取（并支持键集游标）——
        关键词与排序是两件事，不能因为换了排法就把关键词丢掉。
        """
        offset = max(0, (max(1, int(page)) - 1) * size)
        filtered: list[tuple[int, float]] = []
        cap_used: int | None = None
        for cap in self._caps:
            mark = time.perf_counter()
            candidates = self._candidate_ids(plan, cap)
            timings["candidates"] = (time.perf_counter() - mark) * 1000
            cap_used = cap
            mark = time.perf_counter()
            filtered = self._filter_candidates(plan, candidates, filters)
            timings["filter"] = (time.perf_counter() - mark) * 1000
            # 候选没取满 = 索引里就这么多，升到更大的 N 只会拿回同一批行（§5.6 M1）
            if len(filtered) > offset + size or cap == self._caps[-1]:
                break
            if len(candidates) < cap:
                break

        incomplete = len(filtered) <= offset + size and cap_used == self._caps[-1]
        scores = dict(filtered)
        mark = time.perf_counter()
        if order == "time":
            page_rows, has_more, next_cursor = self._time_ordered_page(
                [content_id for content_id, _score in filtered],
                size=size,
                offset=offset,
                cursor=cursor,
                time_field=self._time_field(filters),
            )
            window_ids = [int(row["content_id"]) for row in page_rows]
            timings["main"] = (time.perf_counter() - mark) * 1000
            hits, tail = self._assemble(page_rows, window_ids, terms, filters, scores)
            timings.update(tail)
            timings["total"] = (time.perf_counter() - started) * 1000
            return {
                "hits": hits,
                "total": None,  # M1b：关键词检索不给精确总数
                "total_kind": None,
                "candidates": len(filtered),
                "candidate_cap": cap_used,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "incomplete": incomplete,
                "terms": list(terms),
                "sort": "time",
                "time_field": self._time_field(filters),
                "limit": size,
                "timings": timings,
            }

        window = filtered[offset : offset + size]
        page_ids = [content_id for content_id, _score in window]
        rows = self._fetch_rows_by_ids(page_ids)
        timings["main"] = (time.perf_counter() - mark) * 1000
        # 主查询按 id 取，相关度顺序由候选集决定（候选是 bm25 排好的）
        ranks = {content_id: rank for rank, (content_id, _score) in enumerate(filtered)}
        rows.sort(key=lambda row: ranks.get(int(row["content_id"]), 10**9))
        hits, tail = self._assemble(rows, [int(row["content_id"]) for row in rows], terms, filters, scores)
        timings.update(tail)

        timings["total"] = (time.perf_counter() - started) * 1000
        return {
            "hits": hits,
            "total": None,  # M1b：关键词检索不给精确总数
            "total_kind": None,
            "candidates": len(filtered),
            "candidate_cap": cap_used,
            "has_more": len(filtered) > offset + size,
            "next_cursor": None,  # 相关度排序的下一页是候选集内的页码，游标对 bm25 无意义
            "incomplete": incomplete,
            "terms": list(terms),
            "sort": "relevance",
            "time_field": self._time_field(filters),
            "limit": size,
            "page": max(1, int(page)),
            "timings": timings,
        }

    def _time_ordered_page(
        self,
        ids: Sequence[int],
        *,
        size: int,
        offset: int,
        cursor: str | None,
        time_field: str,
    ) -> tuple[list[Any], bool, str | None]:
        """在候选 id 集内按时间倒序取一页（关键词 + `--sort time` 这条组合）。"""
        if not ids:
            return [], False, None
        column = "m.sent_at" if time_field == TIME_FIELD_SENT else "c.first_seen_at"
        marks = ",".join("?" * len(ids))
        where = f"c.content_id IN ({marks})"
        params: list[Any] = list(ids)
        real_offset = 0
        if cursor:
            moment, content_id = self._decode_cursor(cursor, field=time_field)
            where += f" AND ({column}, c.content_id) < (?, ?)"
            params += [moment, content_id]
        else:
            real_offset = offset
        rows = self._fetch_page(where, params, size + 1, real_offset, order_column=column)
        has_more = len(rows) > size
        page_rows = rows[:size]
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            moment = last["sent_at"] if time_field == TIME_FIELD_SENT else last["first_seen_at"]
            next_cursor = self._encode_cursor(time_field, str(moment), int(last["content_id"]))
        return page_rows, has_more, next_cursor

    @staticmethod
    def _time_field(filters: Mapping[str, Any]) -> str:
        return str(filters.get("time_field") or TIME_FIELD_INGEST)

    def _time_column(self, filters: Mapping[str, Any]) -> str:
        """当前时间字段对应的列（``sent`` 走消息时间，其余一律入库时间）。"""
        return "m.sent_at" if self._time_field(filters) == TIME_FIELD_SENT else "c.first_seen_at"

    def _from_clause_for(self, filters: Mapping[str, Any], driver: str | None = None) -> str:
        """按时间字段选 FROM：按消息时间排序时**由 message 驱动**（否则要整体排序）。

        ``sent`` 路径用 ``CROSS JOIN`` 强制连接顺序，理由与标签驱动集相同（见
        :meth:`_from_clause`）：不加这一句，SQLite 可能仍从 content_item 出发再按
        ``m.sent_at`` 排序，十万行一次排序就足以击穿 §5.6 的 P99。
        """
        if self._time_field(filters) != TIME_FIELD_SENT:
            return self._from_clause(driver)
        if driver is not None:
            return (
                f"  FROM message m CROSS JOIN ({driver}) AS t"
                " CROSS JOIN content_item c ON c.message_id = m.message_id"
                " AND c.content_id = t.content_id"
                "  JOIN source s ON s.source_id = m.source_id"
            )
        return (
            "  FROM message m CROSS JOIN content_item c ON c.message_id = m.message_id"
            "  JOIN source s ON s.source_id = m.source_id"
        )

    # -- 候选与过滤 ---------------------------------------------------------

    def _candidate_plan(self, terms: Sequence[str]) -> dict[str, Any] | None:
        """决定"用哪个索引取候选、用哪个索引当过滤器"。

        长 CJK 词（≥3 字）走 trigram，短 CJK 词（1~2 字）与拉丁词走词级；两边都有时用
        trigram 取候选（子串召回更宽）、词级当过滤器。反过来也行，但 trigram 的候选集
        对"任意位置的字串"更合适。
        """
        return _plan_terms(terms) or None

    def _candidate_ids(self, plan: Mapping[str, Any], cap: int) -> list[tuple[int, float]]:
        """取 bm25 排名前 ``cap`` 个候选，带回分数（越小越相关）。"""
        if plan["source"] == "content_fts":
            weights = ", ".join(str(weight) for weight in BM25_WEIGHTS)
            sql = (
                "SELECT rowid, bm25(content_fts, " + weights + ") AS score FROM content_fts "
                "WHERE content_fts MATCH ? ORDER BY bm25(content_fts, " + weights + ") LIMIT ?"
            )
            expr = plan["trigram"]
        else:
            sql = (
                "SELECT rowid, bm25(content_word) AS score FROM content_word "
                "WHERE content_word MATCH ? ORDER BY bm25(content_word) LIMIT ?"
            )
            expr = plan["word"]
        rows = self._conn.execute(sql, (expr, int(cap))).fetchall()
        return [(int(row[0]), float(row[1])) for row in rows]

    def _filter_candidates(
        self,
        plan: Mapping[str, Any],
        candidates: Sequence[tuple[int, float]],
        filters: Mapping[str, Any],
    ) -> list[tuple[int, float]]:
        """在候选集内做过滤（§5.6 M1）：一条 SQL 拿到仍然合格的 content_id。"""
        if not candidates:
            return []
        ids = [content_id for content_id, _score in candidates]
        marks = ",".join("?" * len(ids))
        clauses = [f"c.content_id IN ({marks})"]
        params: list[Any] = list(ids)
        extra_where, extra_params = self._where(filters, skip_ids=True)
        clauses.append(extra_where)
        params += extra_params
        other, other_table = (
            ("word", "content_word") if plan["source"] == "content_fts" else ("trigram", "content_fts")
        )
        if plan[other]:
            # 另一侧的词当过滤器：候选来自一个索引，另一个索引里的词也必须命中
            clauses.append(
                f"c.content_id IN (SELECT rowid FROM {other_table} WHERE {other_table} MATCH ?)"
            )
            params.append(plan[other])
        sql = (
            "SELECT c.content_id FROM content_item c"
            "  JOIN message m ON m.message_id = c.message_id"
            f" WHERE {' AND '.join(clauses)}"
        )
        ok = {int(row[0]) for row in self._conn.execute(sql, params).fetchall()}
        return [(content_id, score) for content_id, score in candidates if content_id in ok]

    # -- SQL 组装 -----------------------------------------------------------

    _SELECT_HIT = (
        "SELECT c.content_id, c.item_type, c.status, c.nsfw_level, c.first_seen_at,"
        "       c.canonical_id, c.dup_group_id, c.byte_size, c.char_count,"
        "       substr(c.text_content, 1, 400) AS text_content,"
        "       substr(c.ocr_text, 1, 200) AS ocr_text,"
        "       substr(c.caption, 1, 200) AS caption,"
        "       substr(c.search_extra, 1, 300) AS search_extra,"
        "       c.original_name,"
        "       m.source_id, m.sent_at, m.origin_note, m.sender_display_name,"
        "       s.name AS source_name"
    )

    _JOINS = (
        "  JOIN message m ON m.message_id = c.message_id"
        "  JOIN source s ON s.source_id = m.source_id"
    )

    @classmethod
    def _from_clause(cls, driver: str | None = None) -> str:
        """FROM 子句；``driver`` 给定时用 **CROSS JOIN 强制**它当驱动表。

        为什么要强制：SQLite 会自己挑连接顺序，而"沿时间索引扫全表 + 逐行 EXISTS"
        与"先取小集合再按主键回表"在十万条上差一个数量级。标签集是已知的小集合
        （见 :data:`TAG_DRIVER_LIMIT`），这里明确要求以它为外层循环，不让规划器猜。
        """
        if driver is None:
            return f"  FROM content_item c{cls._JOINS}"
        return (
            f"  FROM ({driver}) AS t CROSS JOIN content_item c ON c.content_id = t.content_id"
            f"{cls._JOINS}"
        )

    def _tag_driver(
        self, tag_ids: Sequence[int], *, limit: int
    ) -> tuple[str | None, list[str], list[Any], list[Any]]:
        """把标签过滤翻译成"驱动集 + 其余 EXISTS 条件"（§5.6 M1 的求交规则）。

        两条路都能给出正确结果，差别只在代价，所以按**估算的扫描行数**挑：

        * 沿时间索引扫到凑满一页需要 ``limit / 命中密度`` 行，密度按各标签规模的乘积估
          （标签之间近似独立，合成数据与真实标签分布都符合这个假设）；
        * 物化最小标签集当驱动表需要"最小集合大小"次主键回表。

        密度高（如一个 12% 的常见标签）时时间索引扫几百行就凑满了，花几万次回表是浪费；
        密度低（如三个各 8% 的标签求交，密度千分之一）时时间索引要扫掉整张表，
        这时必须换成"小集合驱动"。两张表上都实测过，差一个数量级。

        Returns:
            ``(驱动子查询 SQL 或 None, 追加的 WHERE 子句, 驱动参数, 追加子句的参数)``。
            **参数必须按 SQL 里的出现顺序拼**：驱动子查询在 FROM 里（最前），
            追加子句在 WHERE 的末尾（最后）——顺序错了不会报错，只会静默查不到东西。
        """
        ids = [int(tag_id) for tag_id in tag_ids]
        if not ids:
            return None, [], [], []
        sizes = sorted((self._tag_size(tag_id), tag_id) for tag_id in ids)
        if self._tag_scan_is_cheap(sizes, limit=limit):
            return None, [self._tag_exists_clause()] * len(sizes), [], [tag for _s, tag in sizes]
        smallest = sizes[0][1]
        others = [tag_id for _size, tag_id in sizes[1:]]
        return (
            "SELECT content_id FROM tag_assignment"
            " WHERE tag_id = ? AND revoked_at IS NULL",
            [self._tag_exists_clause()] * len(others),
            [smallest],
            others,
        )

    def _tag_scan_is_cheap(self, sizes: Sequence[tuple[int, int]], *, limit: int) -> bool:
        """沿时间索引扫到凑满一页，预计要扫的行数是否可接受。"""
        total = self._corpus_size_estimate()
        density = 1.0
        for size, _tag_id in sizes:
            density *= min(1.0, max(size, 0) / total)
        if density <= 0:
            return False  # 有空集合：扫多久都凑不满一页，交给驱动集路径快速判定
        return (limit / density) <= TAG_SCAN_LIMIT

    def _corpus_size_estimate(self) -> int:
        """内容量级的**廉价**估计：主键最大值（一次索引查找，不是一次全表计数）。

        只用来挑查询计划，不参与任何对外输出的数字，所以允许有偏差（软删留下的空洞
        会让它偏大，那只会让估算更保守）。
        """
        row = self._conn.execute("SELECT coalesce(max(content_id), 0) FROM content_item").fetchone()
        return max(int(row[0]) if row else 0, 1)

    @staticmethod
    def _tag_exists_clause() -> str:
        return (
            "EXISTS (SELECT 1 FROM tag_assignment ta WHERE ta.content_id = c.content_id"
            " AND ta.tag_id = ? AND ta.revoked_at IS NULL)"
        )

    def _tag_size(self, tag_id: int) -> int:
        """标签集合的规模——只用来**挑查询计划**，所以优先用 ``usage_count``。

        计数器还没人维护时（打标切片之前恒为 0）退回真实计数：这里多花几毫秒
        换来的是"驱动集阈值"判断正确，而阈值判断错了会让查询慢一个数量级。
        """
        row = self._conn.execute(
            "SELECT coalesce(usage_count, 0) FROM tag WHERE tag_id = ?", (tag_id,)
        ).fetchone()
        if row and int(row[0]) > 0:
            return int(row[0])
        counted = self._conn.execute(
            "SELECT count(*) FROM tag_assignment WHERE tag_id = ? AND revoked_at IS NULL",
            (tag_id,),
        ).fetchone()
        return int(counted[0]) if counted else 0

    def _where(
        self, filters: Mapping[str, Any], *, skip_ids: bool = False, skip_tags: bool = False
    ) -> tuple[str, list[Any]]:
        """把结构化过滤器翻成 WHERE 子句（候选集内过滤与纯过滤路径共用一套）。

        ``skip_tags`` 时不含标签条件——那条路由 :meth:`_tag_driver` 以"驱动集 + EXISTS"
        的形式给（见那里的理由）。
        """
        clauses: list[str] = ["c.deleted_at IS NULL"]
        params: list[Any] = []

        statuses = tuple(filters.get("statuses") or ("active",))
        clauses.append(f"c.status IN ({','.join('?' * len(statuses))})")
        params += list(statuses)

        nsfw = filters.get("nsfw_max_level")
        if nsfw is not None:
            clauses.append("c.nsfw_level <= ?")
            params.append(int(nsfw))

        item_types = tuple(filters.get("item_types") or ())
        if item_types:
            clauses.append(f"c.item_type IN ({','.join('?' * len(item_types))})")
            params += list(item_types)

        if filters.get("source_id") is not None:
            clauses.append("m.source_id = ?")
            params.append(int(filters["source_id"]))

        since = filters.get("since")
        if since:
            clauses.append(f"{self._time_column(filters)} >= ?")
            params.append(str(since))
        until = filters.get("until")
        if until:
            clauses.append(f"{self._time_column(filters)} <= ?")
            params.append(str(until))

        if not skip_tags:
            for tag_id in filters.get("tag_ids") or ():
                clauses.append(self._tag_exists_clause())
                params.append(int(tag_id))

        if filters.get("fold_duplicates", True):
            # canonical 折叠：同一段文本只留代表项。**守卫**：代表项自己被软删或不是
            # active 时不能把整组都藏起来——那种情况下这一行仍然要出现。
            clauses.append(
                "(c.canonical_id IS NULL OR c.canonical_id = c.content_id OR NOT EXISTS ("
                "  SELECT 1 FROM content_item k WHERE k.content_id = c.canonical_id"
                "   AND k.deleted_at IS NULL AND k.status IN ('active')))"
            )

        if not skip_ids and filters.get("content_ids") is not None:
            ids = [int(item) for item in filters["content_ids"]]
            if not ids:
                clauses.append("0")
            else:
                clauses.append(f"c.content_id IN ({','.join('?' * len(ids))})")
                params += ids
        return " AND ".join(clauses), params

    def _fetch_page(
        self,
        where: str,
        params: Sequence[Any],
        count: int,
        offset: int = 0,
        *,
        driver: str | None = None,
        order_column: str = "c.first_seen_at",
        filters: Mapping[str, Any] | None = None,
    ) -> list[Any]:
        source = self._from_clause_for(filters or {}, driver) if filters is not None else self._from_clause(driver)
        sql = (
            f"{self._SELECT_HIT}{source} WHERE {where}"
            f" ORDER BY {order_column} DESC, c.content_id DESC LIMIT ?"
        )
        args = [*params, int(count)]
        if offset:
            sql += " OFFSET ?"
            args.append(int(offset))
        return self._conn.execute(sql, args).fetchall()

    def _fetch_rows_by_ids(self, ids: Sequence[int]) -> list[Any]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self._conn.execute(
            f"{self._SELECT_HIT}{self._from_clause()} WHERE c.content_id IN ({marks})", list(ids)
        ).fetchall()

    def _total_for(
        self,
        where: str,
        params: Sequence[Any],
        filters: Mapping[str, Any],
        *,
        driver: str | None,
    ) -> tuple[int, str]:
        """这一步的"一共多少条"（§5.6 M1b：**不返回精确总数**）。

        三种来源，按代价从低到高：

        * **标签维度**：直接用 ``tag.usage_count``（denormalized 计数器，打标切片维护）。
          它是交集的上界，所以叫"约 N 条"。计数器还没人维护（全为 0）时退回计数——
          与其报一个恒为 0 的数，不如老实数一遍。
        * **其余条件**：数到 :data:`COUNT_CAP` 就停，叫"至少 N 条"。
        * 没被截断时才是真正的精确值。

        Returns:
            ``(数量, 种类)``，种类 ∈ ``{"exact", "at_least", "estimate"}``。
        """
        tag_ids = [int(tag_id) for tag_id in filters.get("tag_ids") or ()]
        if tag_ids:
            usage = [
                int(row[0])
                for row in self._conn.execute(
                    "SELECT coalesce(usage_count, 0) FROM tag WHERE tag_id IN ({})".format(
                        ",".join("?" * len(tag_ids))
                    ),
                    tag_ids,
                ).fetchall()
            ]
            if usage and min(usage) > 0:
                return min(usage), "estimate"
        total, capped = self._count_capped(where, params, filters=filters, driver=driver)
        return total, "at_least" if capped else "exact"

    def _count_capped(
        self,
        where: str,
        params: Sequence[Any],
        *,
        filters: Mapping[str, Any] | None = None,
        driver: str | None = None,
    ) -> tuple[int, bool]:
        """数到 :data:`COUNT_CAP` 就停（§5.6 M1b）。

        Returns:
            ``(数量, 是否被截断)``。被截断时数量就是上限值，调用方要如实说"至少这么多"，
            而不是把它当成精确总数——那正是 M1b 要避免的误导。
        """
        source = self._from_clause_for(filters or {}, driver)
        sql = f"SELECT count(*) FROM (SELECT 1{source} WHERE {where} LIMIT ?)"
        value = int(self._conn.execute(sql, [*params, COUNT_CAP + 1]).fetchone()[0])
        if value > COUNT_CAP:
            return COUNT_CAP, True
        return value, False

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
            f" WHERE ta.content_id IN ({marks}) AND ta.revoked_at IS NULL"
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
            "       m.mime_detected, m.extension"
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
                },
            )
            entry["count"] += 1
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
    def _explain(
        fields: Mapping[str, str], terms: Sequence[str]
    ) -> tuple[list[str], list[str]]:
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

    # -- 游标（键集分页） ---------------------------------------------------

    @staticmethod
    def _encode_cursor(time_field: str, moment: str, content_id: int) -> str:
        """游标里带**时间字段**：拿入库时间的游标去翻消息时间会翻出错页，必须能查出来。"""
        raw = f"{time_field}|{moment}|{content_id}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, *, field: str) -> tuple[str, int]:
        """解码游标并校验它属于当前的时间字段（不一致时明确报错，而不是静默给错页）。"""
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(padded.encode()).decode()
            encoded_field, _, rest = raw.partition("|")
            moment, _, content_id = rest.rpartition("|")
            if not moment or not content_id:
                raise ValueError("游标缺少字段")
            parsed = int(content_id)
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise SearchQueryError(
                "翻页游标坏了（它应当来自上一次结果的 next_cursor）。"
                "想从头看就重新查询，不要手写游标。"
            ) from exc
        if encoded_field != field:
            raise SearchQueryError(
                f"这个游标是按「{encoded_field}」时间的翻页位置生成的，当前查询用的是"
                f"「{field}」。两者的顺序不同，混用会翻出错页——请重新查询，或用"
                f"--time-field {encoded_field} 继续上一次的翻页。"
            )
        return moment, parsed
