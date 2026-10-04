"""按 ID 读取可见内容详情；媒体与标签分批组装，共享读快照。"""

from __future__ import annotations

import sqlite3
from typing import Any

from pc.adapters.tag_normalizer import effective_assignment


def read_detail(
    conn: sqlite3.Connection, *, message_id: int | None, content_id: int | None
) -> dict[str, Any]:
    if (message_id is None) == (content_id is None):
        raise ValueError("请选择一个 --message 或 --content")
    target = message_id if message_id is not None else content_id
    if target is None or not 0 < target <= 2**63 - 1:
        raise ValueError("目标 ID 必须为 SQLite 范围内的正整数")
    column = "c.message_id" if message_id is not None else "c.content_id"
    if conn.in_transaction:
        raise ValueError("详情读取需要独立读快照")
    conn.execute("BEGIN")
    try:
        rows = conn.execute(
            "SELECT c.content_id, c.message_id, c.item_type, c.ordinal, c.text_content,"
            " c.original_name, c.first_seen_at, m.sent_at, m.origin_note, m.source_id,"
            " s.name AS source_name FROM content_item c"
            " JOIN message m ON m.message_id=c.message_id"
            " JOIN source s ON s.source_id=m.source_id"
            f" WHERE {column}=? AND m.deleted_at IS NULL AND c.deleted_at IS NULL"
            " AND c.status='active' AND c.nsfw_level<=0 ORDER BY c.ordinal,c.content_id",
            (target,),
        ).fetchall()
        if not rows:
            raise ValueError("目标不存在或没有可见内容")
        parts = [
            {
                "content_id": row["content_id"],
                "item_type": row["item_type"],
                "ordinal": row["ordinal"],
                "text": row["text_content"] or "",
                "original_name": row["original_name"],
                "first_seen_at": row["first_seen_at"],
                "tags": [],
                "media": [],
            }
            for row in rows
        ]
        by_id = {part["content_id"]: part for part in parts}
        ids = list(by_id)
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            marks = ",".join("?" * len(batch))
            for row in conn.execute(
                "SELECT cm.content_id, cm.ordinal, cm.role, m.media_id, m.sha256,"
                " m.size_bytes, m.mime_detected, m.object_key, m.storage_backend_id,"
                " m.scan_status, m.quarantine_key, m.deleted_at, m.width, m.height"
                " FROM content_media cm JOIN media m ON m.media_id=cm.media_id"
                f" WHERE cm.content_id IN ({marks})"
                " ORDER BY cm.ordinal,m.media_id,cm.role",
                batch,
            ):
                by_id[row["content_id"]]["media"].append(
                    {key: row[key] for key in dict(row) if key != "content_id"}
                )
            for row in conn.execute(
                "SELECT ta.content_id,t.name,t.namespace,ta.confidence,ta.source"
                " FROM tag_assignment ta JOIN tag t ON t.tag_id=ta.tag_id"
                f" WHERE ta.content_id IN ({marks}) AND {effective_assignment()}"
                " ORDER BY t.namespace,t.name",
                batch,
            ):
                by_id[row["content_id"]]["tags"].append(
                    {key: row[key] for key in dict(row) if key != "content_id"}
                )
        first = rows[0]
        result = {
            "result_type": "content",
            "result_version": 1,
            "message_id": first["message_id"],
            "source_id": first["source_id"],
            "source": first["source_name"],
            "sent_at": first["sent_at"],
            "origin_note": first["origin_note"],
            "parts": parts,
        }
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
