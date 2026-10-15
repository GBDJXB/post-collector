"""本机 Web v2：内容项与组合共享候选、排序和分页；CLI 帖级 v1 不变。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from pc.adapters.fts_search import _plan_terms
from pc.adapters.tag_normalizer import effective_assignment
from pc.domain import normalize_text
from pc.services import search


def _term_match(term: str, position: int) -> tuple[str, list[str], list[str]]:
    """物化一次 FTS 命中供内容项和所有组合复用，不截断成员。"""
    plan = _plan_terms([term])
    clauses = []
    definitions = []
    params = []
    for table, expression in (("content_fts", plan.get("trigram")), ("content_word", plan.get("word"))):
        if expression:
            candidate = f"term_{position}_{table}"
            definitions.append(f"{candidate} AS MATERIALIZED (SELECT rowid FROM {table} WHERE {table} MATCH ?)")
            clauses.append(f"c.content_id IN (SELECT rowid FROM {candidate})")
            params.append(expression)
    return " AND ".join(clauses), definitions, params


def run_web_search(conn, *, q: str = "", kind: str = "all", sort: str | None = None,
                   limit: int = 40, cursor: str | None = None, page: int = 1,
                   source: str | None = None, item_types: list[str] | None = None,
                   statuses: list[str] | None = None, tags: list[str] | None = None,
                   since: str | None = None, until: str | None = None,
                   time_field: str = "ingest", nsfw_max_level: int = 0,
                   include_duplicates: bool = False) -> dict[str, Any]:
    """全局时间顺序键集页；组合仅由名称、说明或可见成员命中。"""
    if kind not in {"all", "content", "composition"} or sort not in {None, "time"}:
        raise ValueError("统一检索目前仅支持全局时间排序")
    if not 1 <= limit <= 50 or page < 1:
        raise ValueError("检索分页参数无效")
    needle = normalize_text(q)
    if len(needle) > 4096:
        raise ValueError("关键词过长")
    terms = [term for term in needle.split() if _plan_terms([term])]
    if len(terms) > 32:
        raise ValueError("关键词不能超过 32 个")
    planned = [_term_match(term, position) for position, term in enumerate(terms)]
    matches = [match_sql for match_sql, _, _ in planned]
    definitions = [definition for _, group, _ in planned for definition in group]
    term_params = [value for _, _, values in planned for value in values]
    incomplete = False
    # 每个词独立命中；组合可以由自身字段与不同可见成员共同满足。
    cwhere = ("c.deleted_at IS NULL AND c.status='active' AND "
              f"c.nsfw_level<={nsfw_max_level} AND m.deleted_at IS NULL")
    member_constraints: list[str] = []
    member_params: list[Any] = []
    if source:
        member_constraints.append("m.source_id=?")
        member_params.append(search._resolve_source(conn, source))
    typed = [item_type for item_type in item_types or () if item_type != "mixed"]
    if statuses:
        member_constraints.append("c.status IN (SELECT value FROM json_each(?))")
        member_params.append(json.dumps(statuses))
    member_filter = " AND ".join((cwhere, *member_constraints))
    tag_ids = search._resolve_tags(conn, tags or ())
    kinds: list[str] = []
    branch_params: list[list[Any]] = []
    params: list[Any] = []
    if time_field not in {"ingest", "sent"}:
        raise ValueError("时间字段无效")
    if kind != "composition":
        clause = member_filter
        params.extend(member_params)
        if item_types:
            clause += " AND c.item_type IN (SELECT value FROM json_each(?))" if typed else " AND 0"
            if typed:
                params.append(json.dumps(typed))
        for match_sql in matches:
            clause += " AND (" + match_sql + ")"
        for tag_id in tag_ids:
            clause += (" AND EXISTS (SELECT 1 FROM tag_assignment ta WHERE ta.content_id=c.content_id "
                       f"AND ta.tag_id=? AND {effective_assignment()})")
            params.append(tag_id)
        kinds.append(
            "SELECT 'content' AS kind,c.content_id AS entity_id,c.message_id AS message_id,"
            "c.item_type AS item_type,c.text_content AS text," +
            ("m.sent_at" if time_field == "sent" else "c.first_seen_at") + " AS sort_time,"
            "c.original_name AS original_name,'' AS name,'' AS description,s.name AS source_name "
            "FROM content_item c" + (" INDEXED BY content_item_time_idx" if matches and time_field == "ingest" else "") +
            " JOIN message m USING(message_id) "
            "JOIN source s ON s.source_id=m.source_id WHERE " + clause
        )
        branch_params.append(params)
        params = []
    if kind != "content":
        clause = "co.deleted_at IS NULL"
        for position, (term, match_sql) in enumerate(zip(terms, matches, strict=True)):
            candidate = f"term_{position}_compositions"
            definitions.append(candidate + " AS MATERIALIZED (SELECT DISTINCT cm.composition_id "
                               "FROM composition_member cm CROSS JOIN content_item c ON c.content_id=cm.content_id "
                               "CROSS JOIN message m ON m.message_id=c.message_id WHERE " + cwhere +
                               " AND " + match_sql + ")")
            clauses = ["instr(lower(co.name),lower(?))>0", "instr(lower(co.description),lower(?))>0",
                       f"co.composition_id IN (SELECT composition_id FROM {candidate})"]
            params.extend((term, term))
            clause += " AND (" + " OR ".join(clauses) + ")"
        if member_constraints or item_types:
            member_from = (" FROM composition_member cm JOIN content_item c ON c.content_id=cm.content_id "
                           "JOIN message m ON m.message_id=c.message_id "
                           "WHERE cm.composition_id=co.composition_id AND " + member_filter)
            variants = []
            if not item_types or typed:
                variants.append("EXISTS (SELECT 1" + member_from +
                                (" AND c.item_type IN (SELECT value FROM json_each(?))" if typed else "") + ")")
                params.extend(member_params)
                if typed:
                    params.append(json.dumps(typed))
            if "mixed" in (item_types or ()):
                variants.append("(SELECT COUNT(DISTINCT c.item_type)" + member_from + ")>1")
                params.extend(member_params)
            clause += " AND (" + " OR ".join(variants) + ")"
        for tag_id in tag_ids:
            clause += (" AND EXISTS (SELECT 1 FROM composition_tag_assignment cta "
                       "JOIN tag t ON t.tag_id=cta.tag_id WHERE cta.composition_id=co.composition_id "
                       f"AND cta.tag_id=? AND {effective_assignment('cta')})")
            params.append(tag_id)
        composition_time = (
            "(SELECT MAX(m.sent_at) FROM composition_member cm "
            "JOIN content_item c ON c.content_id=cm.content_id "
            "JOIN message m ON m.message_id=c.message_id "
            "WHERE cm.composition_id=co.composition_id AND " + cwhere + ")"
        ) if time_field == "sent" else "co.created_at"
        kinds.append(
            "SELECT 'composition' AS kind,co.composition_id AS entity_id,NULL AS message_id,"
            "'composition' AS item_type,NULL AS text," + composition_time + " AS sort_time,"
            "NULL AS original_name,co.name AS name,co.description AS description,NULL AS source_name "
            "FROM composition co WHERE " + clause
        )
        branch_params.append(params)
    fingerprint = hashlib.sha256(json.dumps([
        needle, kind, source, item_types, statuses, tags, since, until,
        time_field, nsfw_max_level, include_duplicates,
    ], ensure_ascii=False).encode()).hexdigest()[:16]
    paging: list[Any] = []
    predicates: list[str] = []
    start = search._normalize_time(since, end=False)
    end = search._normalize_time(until, end=True)
    if start:
        predicates.append("sort_time>=?")
        paging.append(start)
    if end:
        predicates.append("sort_time<=?")
        paging.append(end)
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(decoded, list) or len(decoded) != 4 or decoded[0] != fingerprint:
                raise ValueError
            stamp, last_kind, last_id = decoded[1:]
            if not isinstance(stamp, str) or last_kind not in {"content", "composition"} or type(last_id) is not int:
                raise ValueError
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("检索游标无效") from exc
        predicates.append("(COALESCE(sort_time,''),kind,entity_id) < (?,?,?)")
        paging.extend((stamp, last_kind, last_id))
    # 每种实体先独立取够本页；全局排序只合并至多两组有界结果。
    branch_limit = limit + 1 if cursor else page * limit + 1
    bounded = []
    bound_params: list[Any] = []
    for branch, values in zip(kinds, branch_params, strict=True):
        bounded_branch = "SELECT * FROM (" + branch + ")"
        if predicates:
            bounded_branch += " WHERE " + " AND ".join(predicates)
        bounded_branch += " ORDER BY sort_time DESC,entity_id DESC LIMIT ?"
        bounded.append("SELECT * FROM (" + bounded_branch + ")")
        bound_params.extend((*values, *paging, branch_limit))
    sql = ("WITH " + ",".join([*definitions, "entities AS (" + " UNION ALL ".join(bounded) + ")"]) +
           " SELECT * FROM entities ORDER BY sort_time DESC,kind DESC,entity_id DESC LIMIT ? OFFSET ?")
    rows = conn.execute(sql, [*term_params, *bound_params, limit + 1,
                              0 if cursor else (page - 1) * limit]).fetchall()
    has_more = len(rows) > limit
    shown = rows[:limit]
    hits = [{**dict(row), "content_id": row["entity_id"] if row["kind"] == "content" else None,
             "composition_id": row["entity_id"] if row["kind"] == "composition" else None,
             "tags": []}
            for row in shown]
    ids_by_kind = {kind_name: [hit["entity_id"] for hit in hits if hit["kind"] == kind_name]
                   for kind_name in ("content", "composition")}
    tag_rows: list[tuple[str, Any]] = []
    if ids_by_kind["content"]:
        tag_rows.extend(("content", row) for row in conn.execute(
            "SELECT ta.content_id AS entity_id,t.name,t.namespace FROM tag_assignment ta "
            "JOIN tag t ON t.tag_id=ta.tag_id "
            f"WHERE ta.content_id IN (SELECT value FROM json_each(?)) AND {effective_assignment()} "
            "ORDER BY t.namespace,t.name_norm", (json.dumps(ids_by_kind["content"]),),
        ))
    if ids_by_kind["composition"]:
        tag_rows.extend(("composition", row) for row in conn.execute(
            "SELECT cta.composition_id AS entity_id,t.name,t.namespace FROM composition_tag_assignment cta "
            "JOIN tag t ON t.tag_id=cta.tag_id "
            f"WHERE cta.composition_id IN (SELECT value FROM json_each(?)) AND {effective_assignment('cta')} "
            "ORDER BY t.namespace,t.name_norm", (json.dumps(ids_by_kind["composition"]),),
        ))
    by_key = {(hit["kind"], hit["entity_id"]): hit for hit in hits}
    for tag_kind, row in tag_rows:
        by_key[(tag_kind, row["entity_id"])]["tags"].append({"name": row["name"], "namespace": row["namespace"]})
    next_cursor = None
    if has_more and shown:
        tail = shown[-1]
        data = [fingerprint, tail["sort_time"] or "", tail["kind"], tail["entity_id"]]
        next_cursor = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return {"result_version": 2, "hits": hits, "page": page, "total": None,
            "total_kind": "not_counted", "has_more": has_more, "next_cursor": next_cursor,
            "incomplete": incomplete, "sort": "time"}
