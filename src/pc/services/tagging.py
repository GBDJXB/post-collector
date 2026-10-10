"""人工与建议共用的标签应用服务；输入生产者只生成 ClassificationResult。"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections.abc import Sequence
from typing import Any

from pc.adapters import sqlite as db
from pc.adapters.fts_search import SqliteFtsSearch
from pc.adapters.tag_normalizer import SqliteTagNormalizer, effective_assignment, normalize_tag
from pc.domain import ClassificationResult, TagCandidate, TagNamespace, TagSource

MANUAL_NAMESPACES = tuple(
    n.value for n in TagNamespace if n.value not in {"rating", "source", "system"}
)


class TaggingError(ValueError):
    """标签操作的可解释输入错误。"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def result(kind: str, key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"result_type": kind, "result_version": 1, key: rows, "total": len(rows)}


def manual_result(names: Sequence[str], namespace: str = "general") -> ClassificationResult:
    """CLI 人工输入生产者；名称由操作者定义。"""
    if namespace not in MANUAL_NAMESPACES:
        raise TaggingError("此入口支持普通标签，敏感度和系统标签由对应组件管理")
    return ClassificationResult(
        tags=tuple(TagCandidate(n, 1.0, namespace=TagNamespace(namespace)) for n in names),
        source=TagSource.HUMAN,
        decided_by="human",
    )


def _root(conn: sqlite3.Connection, actor_id: int | None) -> None:
    if actor_id is None:  # 本地 CLI 由操作系统权限保护，与 upload 入口一致。
        return
    row = conn.execute(
        "SELECT tier,status,disabled_at FROM app_user WHERE user_id=?", (actor_id,)
    ).fetchone()
    if row is None or row["tier"] != 0 or row["status"] != "active" or row["disabled_at"]:
        raise TaggingError("标签写操作需要有效 root 用户")


def _targets(
    conn: sqlite3.Connection, messages: Sequence[int], contents: Sequence[int]
) -> list[int]:
    if bool(messages) == bool(contents):
        raise TaggingError("请选择 message 或 content 目标，且两者互斥")
    targets: set[int] = set()
    for ident in dict.fromkeys(messages or contents):
        if not isinstance(ident, int) or isinstance(ident, bool) or ident <= 0:
            raise TaggingError("目标 ID 必须为正整数")
        field = "c.message_id" if messages else "c.content_id"
        rows = conn.execute(
            f"SELECT c.content_id FROM content_item c JOIN message m USING(message_id) "
            f"WHERE {field}=? AND c.deleted_at IS NULL AND c.status <> 'deleted' "
            "AND m.deleted_at IS NULL",
            (ident,),
        ).fetchall()
        if not rows:
            raise TaggingError(f"目标 {ident} 不存在、已删除或没有可操作部件")
        targets.update(int(r[0]) for r in rows)
    return sorted(targets)


def _audit(
    conn: sqlite3.Connection,
    action: str,
    kind: str,
    ident: int,
    before: Any,
    after: Any,
    actor_id: int | None,
    *,
    suggested: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO audit_log(actor_type,actor_id,action,target_type,target_id,before_state,"
        "after_state,result,user_agent) VALUES (?,?,?,?,?,?,?,'succeeded',?)",
        (
            "llm" if suggested else "user",
            actor_id,
            action,
            kind,
            ident,
            _json(before),
            _json(after),
            "suggestion_producer" if suggested else ("local_root" if actor_id is None else "root"),
        ),
    )


def _tag(conn: sqlite3.Connection, name: str, namespace: str | None = None) -> sqlite3.Row:
    row = SqliteTagNormalizer(conn).resolve(name, namespace)
    if row is None:
        raise TaggingError(f"标签不存在：{name}")
    if row["status"] == "merged":
        raise TaggingError("合并标签需要使用目标标签")
    return row


def _ensure_tag(
    conn: sqlite3.Connection, candidate: TagCandidate, source: TagSource, actor_id: int | None
) -> int:
    normalizer = SqliteTagNormalizer(conn)
    row = normalizer.resolve(candidate.tag, str(candidate.namespace))
    if row is None:
        ident = int(
            conn.execute(
                "INSERT INTO tag(uuid,name,name_norm,namespace,kind,status,created_by) "
                "VALUES (?,?,?,?,?,?,?) RETURNING tag_id",
                (
                    str(uuid.uuid4()),
                    candidate.tag.strip(),
                    normalizer.normalize(candidate.tag),
                    str(candidate.namespace),
                    str(source),
                    "pending" if source == TagSource.SUGGESTED else "active",
                    actor_id,
                ),
            ).fetchone()[0]
        )
        _audit(
            conn,
            "tag.create",
            "tag",
            ident,
            None,
            dict(_tag(conn, candidate.tag)),
            actor_id,
            suggested=source == TagSource.SUGGESTED,
        )
        return ident
    if row["status"] == "merged":
        raise TaggingError("合并标签需要使用目标标签")
    if row["status"] == "pending" and source == TagSource.HUMAN:
        conn.execute(
            "UPDATE tag SET status='active',kind='human',updated_at=? WHERE tag_id=?",
            (db.now_iso(), row["tag_id"]),
        )
        _audit(
            conn,
            "tag.activate",
            "tag",
            row["tag_id"],
            dict(row),
            dict(_tag(conn, candidate.tag)),
            actor_id,
        )
    return int(row["tag_id"])


def _assign(
    conn: sqlite3.Connection,
    content: int,
    tag: int,
    source: TagSource,
    candidate: TagCandidate,
    model: int | None,
    actor: int | None,
) -> bool:
    before = conn.execute(
        "SELECT * FROM tag_assignment WHERE content_id=? AND tag_id=? AND source=?",
        (content, tag, str(source)),
    ).fetchone()
    evidence = _json(candidate.evidence)
    confidence = 1.0 if source == TagSource.HUMAN else candidate.confidence
    if (
        before
        and before["revoked_at"] is None
        and (
            source == TagSource.HUMAN
            or (
                before["evidence"] == evidence
                and before["confidence"] == confidence
                and before["model_version_id"] == model
            )
        )
    ):
        return False
    now = db.now_iso()
    ident = int(
        conn.execute(
            "INSERT INTO tag_assignment(content_id,tag_id,source,confidence,model_version_id,evidence,"
            "assigned_by,assigned_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(content_id,tag_id,source) DO UPDATE SET confidence=excluded.confidence,"
            "model_version_id=excluded.model_version_id,evidence=excluded.evidence,"
            "assigned_by=excluded.assigned_by,assigned_at=excluded.assigned_at,"
            "updated_at=excluded.updated_at,revoked_at=NULL RETURNING assign_id",
            (content, tag, str(source), confidence, model, evidence, actor, now, now),
        ).fetchone()[0]
    )
    after = conn.execute("SELECT * FROM tag_assignment WHERE assign_id=?", (ident,)).fetchone()
    _audit(
        conn,
        "tag.assign",
        "tag_assignment",
        ident,
        dict(before) if before else None,
        dict(after),
        actor,
        suggested=source == TagSource.SUGGESTED,
    )
    return True


def _record(
    conn: sqlite3.Connection,
    content: int,
    classification: ClassificationResult,
    candidates: Sequence[TagCandidate],
) -> None:
    conn.execute(
        "INSERT INTO classification_run(content_id,capability,model_version_id,output,status) "
        "VALUES (?,?,?,?,'succeeded')",
        (
            content,
            "manual" if classification.source == TagSource.HUMAN else "llm",
            classification.model_version_id,
            _json(
                {
                    "source": str(classification.source),
                    "decided_by": classification.decided_by,
                    "flags": classification.flags,
                    "tags": [
                        {
                            "tag": c.tag,
                            "namespace": str(c.namespace),
                            "confidence": c.confidence,
                            "evidence": c.evidence,
                        }
                        for c in candidates
                    ],
                }
            ),
        ),
    )


def _refresh(conn: sqlite3.Connection, tags: Sequence[int], contents: Sequence[int]) -> None:
    for ident in sorted(set(tags)):
        conn.execute(
            "WITH usage AS (SELECT count(DISTINCT ta.content_id) AS n "
            f"FROM tag_assignment ta WHERE ta.tag_id=? AND {effective_assignment()}) "
            "UPDATE tag SET usage_count=(SELECT n FROM usage),updated_at=? "
            "WHERE tag_id=? AND usage_count<>(SELECT n FROM usage)",
            (ident, db.now_iso(), ident),
        )
    SqliteFtsSearch(conn).index_many(sorted(set(contents)))


def apply_classification(
    conn: sqlite3.Connection,
    classification: ClassificationResult,
    *,
    message_ids: Sequence[int] = (),
    content_ids: Sequence[int] = (),
    actor_id: int | None = None,
) -> dict[str, Any]:
    """共同写入入口：生产者在事务外完成请求，此处只接收已解析的结构化结果。"""
    if classification.source not in (TagSource.HUMAN, TagSource.SUGGESTED):
        raise TaggingError("当前入口支持 human 与 suggested 来源")
    if classification.rating is not None or not classification.tags:
        raise TaggingError("请提供普通标签；敏感度判定由对应组件管理")
    for c in classification.tags:
        if str(c.namespace) not in MANUAL_NAMESPACES:
            raise TaggingError("当前入口支持普通标签命名空间")
        if (
            not isinstance(c.confidence, (int, float))
            or not math.isfinite(c.confidence)
            or not 0 <= c.confidence <= 1
        ):
            raise TaggingError("标签置信度必须在 0~1 内")
        normalize_tag(c.tag)
        if not isinstance(c.evidence, dict):
            raise TaggingError("标签证据必须为对象")
        _json(c.evidence)
    candidates = SqliteTagNormalizer(conn).resolve_conflicts(classification.tags)
    with db.transaction(conn):
        _root(conn, actor_id)
        targets = _targets(conn, message_ids, content_ids)
        tags = [_ensure_tag(conn, c, classification.source, actor_id) for c in candidates]
        changed: list[int] = []
        for target in targets:
            applied = [
                _assign(
                    conn,
                    target,
                    tag,
                    classification.source,
                    c,
                    classification.model_version_id,
                    actor_id,
                )
                for tag, c in zip(tags, candidates, strict=True)
            ]
            if any(applied):
                _record(conn, target, classification, candidates)
                changed.append(target)
        _refresh(conn, tags, changed)
    return {"content_ids": targets, "changed_content_ids": changed, "tag_ids": tags}


def remove_tags(
    conn: sqlite3.Connection,
    names: Sequence[str],
    *,
    message_ids: Sequence[int] = (),
    content_ids: Sequence[int] = (),
    actor_id: int | None = None,
) -> dict[str, Any]:
    if not names:
        raise TaggingError("请选择要撤销的标签")
    with db.transaction(conn):
        _root(conn, actor_id)
        targets = _targets(conn, message_ids, content_ids)
        tags = sorted({int(_tag(conn, n)["tag_id"]) for n in names})
        changed: set[int] = set()
        for target in targets:
            for tag in tags:
                row = conn.execute(
                    "SELECT * FROM tag_assignment WHERE content_id=? AND tag_id=? "
                    "AND source='human' AND revoked_at IS NULL",
                    (target, tag),
                ).fetchone()
                if row:
                    _revoke(conn, row, actor_id, "tag.remove")
                    changed.add(target)
        _refresh(conn, tags, sorted(changed))
    return {"content_ids": targets, "changed_content_ids": sorted(changed), "tag_ids": tags}


def _revoke(conn: sqlite3.Connection, row: sqlite3.Row, actor: int | None, action: str) -> None:
    now = db.now_iso()
    conn.execute(
        "UPDATE tag_assignment SET revoked_at=?,updated_at=? WHERE assign_id=?",
        (now, now, row["assign_id"]),
    )
    after = dict(row) | {"revoked_at": now, "updated_at": now}
    _audit(conn, action, "tag_assignment", row["assign_id"], dict(row), after, actor)


def review_suggestions(
    conn: sqlite3.Connection, ids: Sequence[int], *, confirm: bool, actor_id: int | None = None
) -> dict[str, Any]:
    if not ids:
        raise TaggingError("请选择建议关联 ID")
    with db.transaction(conn):
        _root(conn, actor_id)
        rows = []
        for ident in dict.fromkeys(ids):
            row = conn.execute(
                "SELECT ta.*,t.name,t.namespace,t.status FROM tag_assignment ta "
                "JOIN tag t USING(tag_id) WHERE assign_id=? AND source='suggested'",
                (ident,),
            ).fetchone()
            if row is None:
                raise TaggingError(f"建议关联不存在：{ident}")
            _targets(conn, (), [row["content_id"]])
            rows.append(row)
        changed, tags = [], []
        for row in rows:
            if row["revoked_at"] is not None:
                continue
            if confirm:
                classification = manual_result([row["name"]], row["namespace"])
                c = TagCandidate(
                    row["name"],
                    1.0,
                    {
                        "suggestion_id": row["assign_id"],
                        "model_version_id": row["model_version_id"],
                        "suggestion_evidence": json.loads(row["evidence"]),
                    },
                    TagNamespace(row["namespace"]),
                )
                _ensure_tag(conn, c, TagSource.HUMAN, actor_id)
                _assign(conn, row["content_id"], row["tag_id"], TagSource.HUMAN, c, None, actor_id)
                _record(conn, row["content_id"], classification, [c])
            _revoke(conn, row, actor_id, "tag.confirm" if confirm else "tag.reject")
            changed.append(row["content_id"])
            tags.append(row["tag_id"])
        _refresh(conn, tags, changed)
    return {"assignment_ids": list(dict.fromkeys(ids)), "changed_content_ids": sorted(set(changed))}


def list_tags(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = [dict(r) for r in conn.execute("SELECT t.*,(SELECT count(DISTINCT ca.composition_id) FROM composition_tag_assignment ca JOIN composition co USING(composition_id) WHERE ca.tag_id=t.tag_id AND ca.revoked_at IS NULL AND co.deleted_at IS NULL AND t.status IN ('active','deprecated')) AS composition_usage_count FROM tag t ORDER BY namespace,name_norm,tag_id")]
    for row in rows:
        row["aliases"] = json.loads(row["aliases"])
        row["external_ref"] = json.loads(row["external_ref"])
    return result("tags", "tags", rows)


def show_tags(
    conn: sqlite3.Connection, *, message_ids: Sequence[int] = (), content_ids: Sequence[int] = ()
) -> dict[str, Any]:
    targets = _targets(conn, message_ids, content_ids)
    rows = []
    for start in range(0, len(targets), 500):
        batch = targets[start : start + 500]
        rows.extend(
            dict(r)
            for r in conn.execute(
                "SELECT ta.*,t.name,t.namespace,t.status AS tag_status,c.message_id FROM tag_assignment ta "
                "JOIN tag t USING(tag_id) JOIN content_item c USING(content_id) "
                f"WHERE ta.content_id IN ({','.join('?' for _ in batch)}) "
                "ORDER BY ta.content_id,t.namespace,t.name_norm,ta.assign_id",
                batch,
            )
        )
    for row in rows:
        row["evidence"] = json.loads(row["evidence"])
    return result("assignments", "assignments", rows)


def list_aliases(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = _tag(conn, name)
    return result(
        "aliases",
        "aliases",
        [
            dict(r)
            for r in conn.execute(
                "SELECT ts.*,t.name,t.namespace FROM tag_synonym ts JOIN tag t USING(tag_id) "
                "WHERE ts.tag_id=? ORDER BY alias_norm",
                (row["tag_id"],),
            )
        ],
    )


def change_alias(
    conn: sqlite3.Connection, alias: str, *, name: str | None = None, actor_id: int | None = None
) -> dict[str, Any]:
    norm = normalize_tag(alias)
    with db.transaction(conn):
        _root(conn, actor_id)
        before = conn.execute("SELECT * FROM tag_synonym WHERE alias_norm=?", (norm,)).fetchone()
        if name is not None:
            tag = _tag(conn, name)
            if conn.execute("SELECT 1 FROM tag WHERE name_norm=?", (norm,)).fetchone():
                raise TaggingError("别名与已有规范名冲突")
            if before and before["tag_id"] != tag["tag_id"]:
                raise TaggingError("别名已属于另一个标签")
            if before:
                return {"tag_id": tag["tag_id"], "changed": False}
            conn.execute(
                "INSERT INTO tag_synonym(tag_id,alias,alias_norm,source,confidence) "
                "VALUES (?,?,?,'human',1)",
                (tag["tag_id"], alias.strip(), norm),
            )
            ident = tag["tag_id"]
        else:
            if before is None:
                raise TaggingError("别名不存在")
            ident = before["tag_id"]
            conn.execute("DELETE FROM tag_synonym WHERE alias_norm=?", (norm,))
        aliases = [
            r[0]
            for r in conn.execute(
                "SELECT alias FROM tag_synonym WHERE tag_id=? ORDER BY alias_norm", (ident,)
            )
        ]
        conn.execute(
            "UPDATE tag SET aliases=?,updated_at=? WHERE tag_id=?",
            (_json(aliases), db.now_iso(), ident),
        )
        after = conn.execute("SELECT * FROM tag_synonym WHERE alias_norm=?", (norm,)).fetchone()
        _audit(
            conn,
            "tag.alias.add" if name is not None else "tag.alias.remove",
            "tag",
            ident,
            dict(before) if before else None,
            dict(after) if after else None,
            actor_id,
        )
        contents = [
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT ta.content_id FROM tag_assignment ta "
                f"WHERE ta.tag_id=? AND {effective_assignment()}",
                (ident,),
            )
        ]
        _refresh(conn, [ident], contents)
    return {"tag_id": ident, "changed": True}
