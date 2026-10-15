"""本机 root 的独立有序组合；组合标签与内容标签分别保存。"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from pc.adapters import sqlite as db
from pc.adapters.tag_normalizer import SqliteTagNormalizer, effective_assignment
from pc.domain import TagSource
from pc.services import tagging


def _id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**63 - 1:
        raise ValueError("ID 须为 SQLite 范围内的正整数")
    return value


def _text(value: str, limit: int, *, name=False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("组合名称或说明超过长度上限")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in value):
        raise ValueError("组合文本包含控制字符")
    if name and (not value.strip() or any(c in value for c in "\n\r\t")):
        raise ValueError("组合名称须为非空单行文本")
    return value.strip() if name else value


def _get(conn, ident):
    row = conn.execute(
        "SELECT * FROM composition WHERE composition_id=? AND deleted_at IS NULL", (_id(ident),)
    ).fetchone()
    if row is None:
        raise ValueError("组合不存在或已删除")
    return dict(row)


def _members(conn, ids: Sequence[int]):
    if not ids:
        raise ValueError("组合至少需要一个成员")
    for ident in dict.fromkeys(ids):
        row = conn.execute(
            "SELECT 1 FROM content_item c JOIN message m USING(message_id) "
            "WHERE c.content_id=? AND c.status='active' AND c.nsfw_level<=0 "
            "AND c.deleted_at IS NULL AND m.deleted_at IS NULL",
            (_id(ident),),
        ).fetchone()
        if row is None:
            raise ValueError(f"成员 {ident} 不存在或不可见")


def write(
    conn,
    operation,
    *,
    ident=None,
    name=None,
    description=None,
    contents=(),
    actor_id=None,
    max_text_chars=100000,
    nested=False,
):
    """编辑与审计同事务；成员列表保持重复及参数顺序。"""
    if operation not in {"create", "update", "set-members", "delete"}:
        raise ValueError("未知组合操作")
    if name is not None:
        name = _text(name, 255, name=True)
    if description is not None:
        description = _text(description, max_text_chars)
    with db.transaction(conn, nested=nested):
        tagging._root(conn, actor_id)
        before = None if operation == "create" else _get(conn, ident)
        now = db.now_iso()
        if operation == "create":
            if name is None:
                raise ValueError("请提供组合名称")
            _members(conn, contents)
            ident = conn.execute(
                "INSERT INTO composition(name,description,created_at,updated_at) VALUES (?,?,?,?) RETURNING composition_id",
                (name, description or "", now, now),
            ).fetchone()[0]
        elif operation == "update":
            if name is None and description is None:
                raise ValueError("请选择要修改的名称或说明")
            conn.execute(
                "UPDATE composition SET name=?,description=?,updated_at=? WHERE composition_id=?",
                (
                    name if name is not None else before["name"],
                    description if description is not None else before["description"],
                    now,
                    ident,
                ),
            )
        elif operation == "delete":
            conn.execute(
                "UPDATE composition SET deleted_at=?,updated_at=? WHERE composition_id=?",
                (now, now, ident),
            )
        if operation in {"create", "set-members"}:
            if operation == "set-members":
                _members(conn, contents)
            before_members = [
                r[0]
                for r in conn.execute(
                    "SELECT content_id FROM composition_member WHERE composition_id=? ORDER BY position",
                    (ident,),
                )
            ]
            conn.execute("DELETE FROM composition_member WHERE composition_id=?", (ident,))
            conn.executemany(
                "INSERT INTO composition_member VALUES (?,?,?)",
                [(ident, position, item) for position, item in enumerate(contents)],
            )
            conn.execute("UPDATE composition SET updated_at=? WHERE composition_id=?", (now, ident))
            before = {"composition": before, "members": before_members}
        after = dict(
            conn.execute("SELECT * FROM composition WHERE composition_id=?", (ident,)).fetchone()
        )
        if operation in {"create", "set-members"}:
            after = {"composition": after, "members": list(contents)}
        tagging._audit(
            conn, f"composition.{operation}", "composition", ident, before, after, actor_id
        )
    return {
        "result_type": "composition_operation",
        "result_version": 1,
        "composition_id": ident,
        "operation": operation,
    }


def _read(conn, ident):
    composition = _get(conn, ident)
    tags = [
        dict(r)
        for r in conn.execute(
            "SELECT t.tag_id,t.name,t.namespace FROM composition_tag_assignment ta JOIN tag t USING(tag_id) "
            f"WHERE ta.composition_id=? AND {effective_assignment()} ORDER BY t.namespace,t.name_norm",
            (ident,),
        )
    ]
    members = []
    visible = {}
    for row in conn.execute(
        "SELECT cm.position,c.*,m.deleted_at AS message_deleted,"
        "(SELECT url_normalized FROM link WHERE content_id=c.content_id AND deleted_at IS NULL ORDER BY link_id LIMIT 1) AS url "
        "FROM composition_member cm "
        "JOIN content_item c USING(content_id) JOIN message m USING(message_id) "
        "WHERE cm.composition_id=? ORDER BY cm.position",
        (ident,),
    ):
        if (
            row["deleted_at"]
            or row["message_deleted"]
            or row["status"] != "active"
            or row["nsfw_level"] > 0
        ):
            members.append({"position": row["position"], "available": False})
            continue
        item = {
            "content_id": row["content_id"],
            "message_id": row["message_id"],
            "item_type": row["item_type"],
            "text": row["text_content"] or "",
            "url": row["url"],
            "tags": [],
            "media": [],
        }
        visible.setdefault(row["content_id"], []).append(item)
        members.append({"position": row["position"], "available": True, **item})
    ids = list(visible)
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        marks = ",".join("?" for _ in batch)
        for row in conn.execute(
            "SELECT ta.content_id,t.tag_id,t.name,t.namespace FROM tag_assignment ta JOIN tag t USING(tag_id) "
            f"WHERE ta.content_id IN ({marks}) AND {effective_assignment()} ORDER BY t.namespace,t.name_norm",
            batch,
        ):
            for item in visible[row["content_id"]]:
                item["tags"].append({k: row[k] for k in ("tag_id", "name", "namespace")})
        for row in conn.execute(
            "SELECT cm.content_id,cm.ordinal,cm.role,m.media_id,m.mime_detected,m.size_bytes FROM content_media cm "
            "JOIN media m USING(media_id) "
            f"WHERE cm.content_id IN ({marks}) AND m.deleted_at IS NULL AND m.quarantine_key IS NULL "
            "AND m.scan_status IN ('clean','skipped') ORDER BY cm.ordinal,m.media_id,cm.role",
            batch,
        ):
            for item in visible[row["content_id"]]:
                item["media"].append({k: row[k] for k in dict(row) if k != "content_id"})
    return {**composition, "tags": tags, "members": members}


def show(conn, ident):
    with db.transaction(conn, immediate=False):
        data = _read(conn, ident)
    return {"result_type": "composition", "result_version": 1, **data}


def list_compositions(conn, *, names=(), match_member_tags=False, limit=50, offset=0, name=None, composition_id=None):
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("每页 1~100 条，offset 须非负")
    if name is not None and (not isinstance(name, str) or len(name) > 255):
        raise ValueError("组合名称检索词过长或类型无效")
    if composition_id is not None:
        composition_id = _id(composition_id)
    with db.transaction(conn, immediate=False):
        wanted = {tagging._tag(conn, tag)["tag_id"] for tag in names}
        predicates, params = [], []
        if name and name.strip():
            predicates.append("instr(lower(co.name), lower(?)) > 0")
            params.append(name.strip())
        if composition_id is not None:
            predicates.append("co.composition_id=?")
            params.append(composition_id)
        for tag_id in sorted(wanted):
            own = (
                "EXISTS(SELECT 1 FROM composition_tag_assignment ta WHERE ta.composition_id=co.composition_id AND ta.tag_id=? AND "
                + effective_assignment()
                + ")"
            )
            params.append(tag_id)
            if match_member_tags:
                own = (
                    "("
                    + own
                    + " OR EXISTS(SELECT 1 FROM composition_member cm JOIN content_item c USING(content_id) JOIN message m USING(message_id) JOIN tag_assignment ta USING(content_id) WHERE cm.composition_id=co.composition_id AND ta.tag_id=? AND "
                    + effective_assignment()
                    + " AND c.deleted_at IS NULL AND m.deleted_at IS NULL AND c.status='active' AND c.nsfw_level<=0))"
                )
                params.append(tag_id)
            predicates.append(own)
        where = "co.deleted_at IS NULL" + (" AND " + " AND ".join(predicates) if predicates else "")
        total = conn.execute(
            "SELECT count(*) FROM composition co WHERE " + where, params
        ).fetchone()[0]
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT composition_id FROM composition co WHERE "
                + where
                + " ORDER BY co.created_at DESC,co.composition_id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        ]
        rows = [_read(conn, ident) for ident in ids]
        for row in rows:
            row["matches"] = [
                {"tag_id": t["tag_id"], "target": "composition"}
                for t in row["tags"]
                if t["tag_id"] in wanted
            ]
            if match_member_tags:
                row["matches"].extend(
                    {"tag_id": t["tag_id"], "target": "member", "position": item["position"]}
                    for item in row["members"]
                    if item["available"]
                    for t in item["tags"]
                    if t["tag_id"] in wanted
                )
    return {
        "result_type": "compositions",
        "result_version": 1,
        "compositions": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


def tag_change(conn, ident, names, *, remove=False, namespace="general", actor_id=None, nested=False):
    candidates = SqliteTagNormalizer(conn).resolve_conflicts(
        tagging.manual_result(names, namespace).tags
    )
    if not candidates:
        raise ValueError("请提供标签")
    changed = []
    with db.transaction(conn, nested=nested):
        tagging._root(conn, actor_id)
        _get(conn, ident)
        for candidate in candidates:
            tag_id = (
                tagging._tag(conn, candidate.tag)["tag_id"]
                if remove
                else tagging._ensure_tag(conn, candidate, TagSource.HUMAN, actor_id)
            )
            before = conn.execute(
                "SELECT * FROM composition_tag_assignment WHERE composition_id=? AND tag_id=? AND source='human'",
                (ident, tag_id),
            ).fetchone()
            if (remove and (before is None or before["revoked_at"] is not None)) or (
                not remove and before and before["revoked_at"] is None
            ):
                continue
            now = db.now_iso()
            if remove:
                conn.execute(
                    "UPDATE composition_tag_assignment SET revoked_at=?,updated_at=? WHERE assign_id=?",
                    (now, now, before["assign_id"]),
                )
                assign_id = before["assign_id"]
            else:
                assign_id = conn.execute(
                    "INSERT INTO composition_tag_assignment(composition_id,tag_id,assigned_by,assigned_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(composition_id,tag_id,source) DO UPDATE SET assigned_by=excluded.assigned_by,assigned_at=excluded.assigned_at,updated_at=excluded.updated_at,revoked_at=NULL RETURNING assign_id",
                    (ident, tag_id, actor_id, now, now),
                ).fetchone()[0]
            after = dict(
                conn.execute(
                    "SELECT * FROM composition_tag_assignment WHERE assign_id=?", (assign_id,)
                ).fetchone()
            )
            tagging._audit(
                conn,
                "composition.tag.remove" if remove else "composition.tag.add",
                "composition_tag_assignment",
                assign_id,
                dict(before) if before else None,
                after,
                actor_id,
            )
            changed.append(tag_id)
    return {"composition_id": ident, "changed_tag_ids": changed}


def show_tags(conn, ident):
    with db.transaction(conn, immediate=False):
        _get(conn, ident)
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT ta.*,t.name,t.namespace FROM composition_tag_assignment ta JOIN tag t USING(tag_id) WHERE composition_id=? ORDER BY t.namespace,t.name_norm",
                (ident,),
            )
        ]
    return tagging.result("composition_assignments", "assignments", rows)


def review_composition_suggestions(conn, ids, *, confirm, actor_id=None, expected_runs=None, model_access=None):
    """组合建议独立审核，不修改任何成员关联。"""
    return tagging.review_suggestions(conn, ids, confirm=confirm, actor_id=actor_id, expected_runs=expected_runs, composition=True, model_access=model_access)
