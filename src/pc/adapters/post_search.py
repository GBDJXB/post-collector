"""帖级查询规划；FTS 文档仍是各个内容部件。"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from pc.domain import ITEM_TYPES, post_composition_label, post_type_mask


class PostSearch:
    """与 FTS 部件解释、组装共用的帖级查询方法。"""

    def _post_driver(self, tag_ids: list[int], *, limit: int) -> tuple[str, list[Any]]:
        from pc.adapters.fts_search import TAG_SCAN_LIMIT

        if not tag_ids:
            return "FROM post p", []
        marks = ",".join("?" for _ in tag_ids)
        sizes = sorted(
            (int(row[1]), int(row[0]))
            for row in self._conn.execute(
                "SELECT tag_id, CASE WHEN usage_count > 0 THEN usage_count ELSE "
                "(SELECT count(*) FROM tag_assignment ta WHERE ta.tag_id=tag.tag_id AND ta.revoked_at IS NULL) END "
                f"FROM tag WHERE tag_id IN ({marks})",
                tag_ids,
            )
        )
        total = max(
            1, self._conn.execute("SELECT coalesce(max(message_id),0) FROM post").fetchone()[0]
        )
        density = 1.0
        for size, _tag in sizes:
            density *= min(1.0, size / total)
        if density and limit / density <= TAG_SCAN_LIMIT:
            return "FROM post p", []
        return (
            "FROM (SELECT DISTINCT c.message_id FROM tag_assignment ta JOIN content_item c INDEXED BY content_item_lookup_idx USING(content_id) "
            "WHERE ta.tag_id=? AND ta.revoked_at IS NULL) t CROSS JOIN post p ON p.message_id=t.message_id",
            [sizes[0][1]],
        )

    def _visible(self, filters: dict[str, Any], alias: str = "c") -> tuple[str, list[Any]]:
        statuses = tuple(filters.get("statuses") or ("active",))
        return (
            f"{alias}.deleted_at IS NULL AND {alias}.status IN ({','.join('?' for _ in statuses)})"
            f" AND {alias}.nsfw_level <= ?",
            [*statuses, int(filters.get("nsfw_max_level", 0))],
        )

    def _post_conditions(
        self, filters: dict[str, Any], plans: list[dict[str, Any]], *, alias: str = "p"
    ) -> tuple[str, list[Any]]:
        visible, values = self._visible(filters)
        clauses = [
            f"EXISTS (SELECT 1 FROM content_item c WHERE c.message_id={alias}.message_id AND {visible})",
            f"EXISTS (SELECT 1 FROM message m INDEXED BY message_visible_idx WHERE m.message_id={alias}.message_id AND m.deleted_at IS NULL)",
        ]
        params = list(values)
        if filters.get("source_id") is not None:
            clauses[-1] = (
                f"EXISTS (SELECT 1 FROM message m INDEXED BY message_visible_idx "
                f"WHERE m.message_id={alias}.message_id AND m.deleted_at IS NULL AND m.source_id=?)"
            )
            params.append(int(filters["source_id"]))
        column = "sent_at" if filters.get("time_field") == "sent" else "first_seen_at"
        for key, op in (("since", ">="), ("until", "<=")):
            if filters.get(key):
                clauses.append(f"{alias}.{column} {op} ?")
                params.append(filters[key])
        types = tuple(filters.get("item_types") or ())
        if types:
            mask_sql = (
                "sum(DISTINCT CASE c.item_type "
                + " ".join(f"WHEN '{name}' THEN {1 << i}" for i, name in enumerate(ITEM_TYPES))
                + " ELSE 0 END)"
            )
            pure = [1 << ITEM_TYPES.index(name) for name in types if name in ITEM_TYPES]
            options = [f"mask IN ({','.join('?' for _ in pure)})"] if pure else []
            if "mixed" in types:
                options.append("(mask & (mask - 1)) != 0")
            clauses.append(
                f"EXISTS (SELECT {mask_sql} AS mask FROM content_item c "
                f"WHERE c.message_id={alias}.message_id AND {visible} HAVING {' OR '.join(options) or '0'})"
            )
            params.extend([*values, *pure])
        for tag in filters.get("tag_ids") or ():
            clauses.append(
                f"EXISTS (SELECT 1 FROM content_item c CROSS JOIN tag_assignment ta USING(content_id) "
                f"WHERE c.message_id={alias}.message_id AND {visible} AND ta.tag_id=? AND ta.revoked_at IS NULL)"
            )
            params.extend([*values, int(tag)])
        for plan in plans:
            for key, table in (("trigram", "content_fts"), ("word", "content_word")):
                if plan.get(key):
                    clauses.append(
                        f"EXISTS (SELECT 1 FROM content_item c WHERE c.message_id={alias}.message_id "
                        f"AND {visible} AND c.content_id IN (SELECT rowid FROM {table} WHERE {table} MATCH ?))"
                    )
                    params.extend([*values, plan[key]])
        return " AND ".join(clauses), params

    def _post_where(
        self,
        filters: dict[str, Any],
        plans: list[dict[str, Any]],
        *,
        candidates_match: bool = False,
    ) -> tuple[str, list[Any]]:
        where, params = self._post_conditions(filters, [] if candidates_match else plans)
        if filters.get("fold_duplicates", True):
            guard, guard_params = self._post_conditions(filters, plans, alias="k")
            where += (
                " AND (p.content_signature IS NULL OR p.canonical_post_id IS NULL "
                "OR p.canonical_post_id=p.message_id OR NOT EXISTS (SELECT 1 FROM post k "
                f"WHERE k.message_id=p.canonical_post_id AND k.content_signature=p.content_signature AND {guard}))"
            )
            params.extend(guard_params)
        return where, params

    @staticmethod
    def _post_cursor(field: str, moment: str | None, message_id: int) -> str:
        value = json.dumps([2, field, moment, message_id], separators=(",", ":"))
        return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")

    @staticmethod
    def _post_cursor_where(cursor: str, field: str) -> tuple[str, list[Any]]:
        from pc.adapters.fts_search import SearchQueryError

        try:
            value = json.loads(
                base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            )
            if not isinstance(value, list) or len(value) != 4 or value[0] != 2:
                raise ValueError("旧内容项游标已失效，请重新查询")
            _, encoded_field, moment, message_id = value
            if encoded_field != field:
                raise ValueError(
                    f"这个游标是按 {encoded_field} 生成的，当前 --time-field 是 {field}，请重新查询"
                )
            if (
                type(message_id) is not int
                or message_id < 1
                or (moment is not None and not isinstance(moment, str))
            ):
                raise ValueError("游标字段无效")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise SearchQueryError(f"翻页游标坏了或不兼容：{exc}；请重新查询") from exc
        column = "p.sent_at" if field == "sent" else "p.first_seen_at"
        if moment is None:
            if field != "sent":
                raise SearchQueryError("入库时间游标不能包含空时间")
            return f"{column} IS NULL AND p.message_id < ?", [message_id]
        return f"(({column}, p.message_id) < (?, ?) OR {column} IS NULL)", [moment, message_id]

    def _post_candidates(
        self, plans: list[dict[str, Any]], filters: dict[str, Any], cap: int
    ) -> list[tuple[int, float]]:
        from pc.adapters.fts_search import BM25_WEIGHTS

        expressions: dict[str, list[str]] = {}
        for plan in plans:
            for key, table in (("trigram", "content_fts"), ("word", "content_word")):
                if plan.get(key):
                    expressions.setdefault(table, []).append(plan[key])
        visible, params = self._visible(filters)
        # 少量隐藏文档不应导致整套 FTS 排名重跑；多部件/大量隐藏仍靠倍增兜底。
        width = cap + 1 + max(32, cap // 8)
        while True:
            ctes, selects, stats, match_params = [], [], [], []
            for i, (table, terms) in enumerate(expressions.items()):
                ranking = (
                    "bm25(" + ",".join(map(str, BM25_WEIGHTS)) + ")"
                    if table == "content_fts"
                    else "bm25()"
                )
                ctes.append(
                    f"ranked_{i} AS MATERIALIZED (SELECT rowid AS content_id, rank AS score "
                    f"FROM {table} WHERE {table} MATCH ? AND rank MATCH ? ORDER BY rank LIMIT ?)"
                )
                match_params.extend(
                    [" OR ".join(f"({term})" for term in terms), ranking, width + 1]
                )
                selects.append(
                    f"SELECT c.message_id, r.score FROM ranked_{i} r CROSS JOIN content_item c "
                    f"INDEXED BY content_item_lookup_idx USING(content_id) WHERE {visible}"
                )
                stats.extend(
                    [
                        f"(SELECT count(*) FROM ranked_{i}) AS n{i}",
                        f"(SELECT max(score) FROM ranked_{i}) AS tail{i}",
                    ]
                )
            rows = self._conn.execute(
                "WITH "
                + ", ".join(ctes)
                + ", matches AS MATERIALIZED ("
                + " UNION ALL ".join(selects)
                + "), candidates AS MATERIALIZED (SELECT message_id, min(score) AS score FROM matches "
                "GROUP BY message_id ORDER BY score, message_id DESC LIMIT ?) "
                "SELECT candidates.message_id, candidates.score, stats.* FROM (SELECT "
                + ", ".join(stats)
                + ") stats LEFT JOIN candidates ON 1=1 ORDER BY score, message_id DESC",
                [*match_params, *params * len(expressions), cap + 1],
            ).fetchall()
            candidates = [(int(row[0]), float(row[1])) for row in rows if row[0] is not None]
            # 各索引要么已耗尽，要么未读取部分不会改善第 cap 帖的分数。
            # 候选截断允许同分帖留在预算外；已取候选按 message_id 稳定排序。
            threshold = candidates[cap - 1][1] if len(candidates) > cap else None
            if all(
                rows[0][f"n{i}"] <= width
                or (threshold is not None and rows[0][f"tail{i}"] >= threshold)
                for i in range(len(expressions))
            ):
                return candidates
            width *= 2

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
        # 多条 SQL 共享读快照，防止过滤后状态变化导致命中理由或部件漂移。
        if self._conn.in_transaction:
            return self._query_posts(q, filters, sort, page, limit=limit, cursor=cursor)
        started = time.perf_counter()
        self._conn.execute("BEGIN")
        try:
            result = self._query_posts(q, filters, sort, page, limit=limit, cursor=cursor)
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()
        result["timings"]["total"] = (time.perf_counter() - started) * 1000
        return result

    def _query_posts(
        self,
        q: str,
        filters: dict[str, Any] | None = None,
        sort: str | None = None,
        page: int = 1,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from pc.adapters.fts_search import COUNT_CAP, MAX_PAGE_SIZE, SearchQueryError, _plan_terms

        started = time.perf_counter()
        filters = dict(filters or {})
        terms = (q or "").split()
        plans = [plan for term in terms if (plan := _plan_terms([term]))]
        order = sort or ("relevance" if plans else "time")
        if order not in ("relevance", "time"):
            raise SearchQueryError(f"排序方式只能是 relevance 或 time，收到 {order!r}")
        if not plans:
            order = "time"
        if cursor and order != "time":
            raise SearchQueryError("相关度排序不接受时间游标")
        size = min(MAX_PAGE_SIZE, max(1, int(limit or self._page_size)))
        field = filters.get("time_field") or "ingest"
        column = "p.sent_at" if field == "sent" else "p.first_seen_at"
        where, params = self._post_where(filters, plans)
        from_sql, driver_params = (
            self._post_driver(list(filters.get("tag_ids") or ()), limit=size)
            if not plans
            else ("FROM post p", [])
        )
        timings: dict[str, float] = {}
        filtered: list[tuple[int, float]] = []
        cap_used = None
        truncated = False
        offset = max(0, (page - 1) * size) if not cursor else 0
        if plans:
            single_match = (
                len(plans) == 1 and sum(bool(plans[0].get(key)) for key in ("trigram", "word")) == 1
            )
            where, params = self._post_where(
                filters,
                plans,
                candidates_match=single_match,
            )
            # 标签查询仍以 2000 为预算；相关度页凑齐后可提前停止读取。
            budget = self._caps[-1]
            first = min(budget, max(self._caps[0], (offset + size + 1) * 20))
            caps = (
                tuple(dict.fromkeys((first, budget)))
                if filters.get("tag_ids") and order == "relevance"
                else self._caps[-1:] if filters.get("tag_ids") else self._caps
            )
            for cap in caps:
                mark = time.perf_counter()
                candidates = self._post_candidates(plans, filters, cap)
                truncated = len(candidates) > cap
                candidates = candidates[:cap]
                timings["candidates"] = (
                    timings.get("candidates", 0) + (time.perf_counter() - mark) * 1000
                )
                cap_used = budget if filters.get("tag_ids") else cap
                mark = time.perf_counter()
                if candidates:
                    marks = ",".join("?" for _ in candidates)
                    ok = {
                        row[0]
                        for row in self._conn.execute(
                            f"SELECT p.message_id FROM post p WHERE p.message_id IN ({marks}) AND {where}",
                            [*[key for key, _ in candidates], *params],
                        )
                    }
                    filtered = [(key, score) for key, score in candidates if key in ok]
                timings["filter"] = timings.get("filter", 0) + (time.perf_counter() - mark) * 1000
                if not truncated or (order == "relevance" and len(filtered) > offset + size):
                    break
            if filtered:
                where = "p.message_id IN (" + ",".join("?" for _ in filtered) + ")"
                params = [key for key, _ in filtered]
            else:
                where, params = "0", []
        total = None
        total_kind = None
        if not plans:
            mark = time.perf_counter()
            tags = filters.get("tag_ids") or ()
            if tags:
                usage = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT usage_count FROM tag WHERE tag_id IN ("
                        + ",".join("?" for _ in tags)
                        + ")",
                        tags,
                    )
                ]
                if usage and min(usage) > 0:
                    total, total_kind = min(usage), "estimate"
            if total is None:
                total = self._conn.execute(
                    f"SELECT count(*) FROM (SELECT 1 {from_sql} WHERE {where} LIMIT ?)",
                    [*driver_params, *params, COUNT_CAP + 1],
                ).fetchone()[0]
                total_kind = "at_least" if total > COUNT_CAP else "exact"
                total = min(total, COUNT_CAP)
            timings["count"] = (time.perf_counter() - mark) * 1000
        if cursor:
            condition, cursor_params = self._post_cursor_where(cursor, field)
            where += " AND (" + condition + ")"
            params.extend(cursor_params)
        scores = dict(filtered)
        prefix, page_columns = "", "p.*"
        if order == "time":
            ordering = f"{column} DESC, p.message_id DESC"
            page_ordering = ordering
        elif filtered:
            prefix = (
                "ranked_posts(message_id, position) AS MATERIALIZED (VALUES "
                + ",".join(f"(?,{i})" for i in range(len(filtered)))
                + "), "
            )
            from_sql = "FROM ranked_posts r CROSS JOIN post p ON p.message_id=r.message_id"
            driver_params = [key for key, _ in filtered]
            where, params = "1", []
            page_columns = "p.*, r.position AS search_position"
            page_ordering, ordering = "r.position", "p.search_position"
        else:
            ordering = "p.message_id DESC"
            page_ordering = ordering
        visible, visible_params = self._visible(filters)
        mark = time.perf_counter()
        rows = self._conn.execute(
            f"WITH {prefix}page AS MATERIALIZED (SELECT {page_columns} {from_sql} WHERE {where} ORDER BY {page_ordering} LIMIT ? OFFSET ?) "
            "SELECT c.*, p.first_seen_at AS post_first_seen_at, p.sent_at AS post_sent_at, "
            "m.source_id, m.origin_note, m.sender_display_name, s.name AS source_name "
            "FROM page p JOIN content_item c ON c.message_id=p.message_id "
            "JOIN message m ON m.message_id=p.message_id JOIN source s ON s.source_id=m.source_id "
            f"WHERE {visible} ORDER BY {ordering}, c.ordinal, c.content_id",
            [*driver_params, *params, size + 1, offset, *visible_params],
        ).fetchall()
        timings["main"] = (time.perf_counter() - mark) * 1000
        keys = list(dict.fromkeys(int(row["message_id"]) for row in rows))
        has_more = len(keys) > size
        keys = keys[:size]
        selected = [row for row in rows if row["message_id"] in keys]
        parts, tail = self._assemble(
            selected, [row["content_id"] for row in selected], terms, filters
        )
        timings.update(tail)
        by_post: dict[int, list[dict[str, Any]]] = {}
        for row, part in zip(selected, parts, strict=True):
            by_post.setdefault(row["message_id"], []).append(part)
        hits = []
        for key in keys:
            group = by_post[key]
            row = next(row for row in selected if row["message_id"] == key)
            mask = post_type_mask([part["item_type"] for part in group])
            hit = dict(group[0])
            hit.update(
                message_id=key,
                parts=group,
                first_seen_at=row["post_first_seen_at"],
                sent_at=row["post_sent_at"],
                item_type="mixed" if mask & (mask - 1) else group[0]["item_type"],
                composition=post_composition_label([part["item_type"] for part in group]),
                original_name=" / ".join(
                    part["original_name"] for part in group if part["original_name"]
                )
                or None,
                text="\n".join(part["text"] for part in group if part["text"]),
                matched_terms=list(
                    dict.fromkeys(term for part in group for term in part["matched_terms"])
                ),
                matched_fields=list(
                    dict.fromkeys(name for part in group for name in part["matched_fields"])
                ),
                tags=[tag for part in group for tag in part["tags"]],
                snippet=next((part["snippet"] for part in group if part["snippet"]), None),
                media={
                    **next(
                        (part["media"] for part in group if part["media"]["count"]), {"count": 0}
                    ),
                    "count": sum(part["media"]["count"] for part in group),
                    "items": [item for part in group for item in part["media"].get("items", [])],
                },
                score=scores.get(key),
            )
            hits.append(hit)
        next_cursor = None
        if has_more and hits and order == "time":
            last = hits[-1]
            next_cursor = self._post_cursor(
                field,
                last["sent_at"] if field == "sent" else last["first_seen_at"],
                last["message_id"],
            )
        timings["total"] = (time.perf_counter() - started) * 1000
        return {
            "hits": hits,
            "total": total,
            "total_kind": total_kind,
            "candidates": len(filtered) if plans else None,
            "candidate_cap": cap_used,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "incomplete": truncated,
            "terms": terms,
            "sort": order,
            "time_field": field,
            "limit": size,
            "page": page,
            "timings": timings,
        }
