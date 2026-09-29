"""来源登记：``source`` 与 ``source_authorization`` 必须同生共死（DESIGN §1.3、§6.1）。

「凭证 + 授权范围 + 授权证明」三者齐备才允许采集，所以**建来源时就要把授权一起写下**，
不能留一条"只有 source 行、没有授权记录"的中间状态：那正是连接器 ``authorize()``
要拒绝的东西，而让人手工补一条授权记录是最容易忘记也最容易被糊弄过去的一步。

本模块同时负责那行默认的 ``storage_backend`` 记录：``media.storage_backend_id`` 与
``object_key`` 一起定位对象，后端行缺失会让"这个键属于哪个根目录"无从判断。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pc.adapters.registry import SourceConfigError, source_config
from pc.adapters.sqlite import now_iso, transaction

#: ``source.kind`` 的合法取值（与 ``source_kind_chk`` 一致）
SOURCE_KINDS: tuple[str, ...] = ("bot_api", "webhook", "rss", "export", "user_api")

#: ``source_authorization.mode`` 的合法取值（与 CHECK 一致）
AUTHORIZATION_MODES: tuple[str, ...] = (
    "owner_token",
    "admin_consent",
    "public_feed",
    "manual_export",
)


class SourceError(RuntimeError):
    """来源登记失败（重名、取值非法、找不到等），消息面向人。"""


def _require_kind(kind: str) -> str:
    if kind not in SOURCE_KINDS:
        raise SourceError(f"kind={kind!r} 不合法；可用取值：{', '.join(SOURCE_KINDS)}。")
    return kind


def _require_mode(mode: str) -> str:
    if mode not in AUTHORIZATION_MODES:
        raise SourceError(
            f"授权方式 mode={mode!r} 不合法；可用取值：{', '.join(AUTHORIZATION_MODES)}。"
        )
    return mode


def create_source(
    conn: sqlite3.Connection,
    *,
    name: str,
    platform: str,
    kind: str,
    trust_level: int = 1,
    external_ref: str | None = None,
    config: Mapping[str, Any] | None = None,
    authorization_mode: str = "manual_export",
    scope: Mapping[str, Any] | None = None,
    evidence_ref: str | None = None,
    granted_by: int | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """建来源并同时写下它的授权记录，返回 ``{source_id, authz_id, ...}``。

    ``trust_level``：1=可信自有、2=已授权群、3=外部不确定。手工上传自己的文件属于 1。
    """
    _require_kind(kind)
    _require_mode(authorization_mode)
    if trust_level not in (1, 2, 3):
        raise SourceError(f"trust_level={trust_level!r} 不合法；只能是 1 / 2 / 3。")

    existing = conn.execute("SELECT source_id FROM source WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        raise SourceError(
            f"已经有一个叫「{name}」的来源（source_id={existing[0]}）。"
            "换个名字，或先用 `pc source list` 看看它是不是你要的那个。"
        )

    payload = json.dumps(dict(config or {}), ensure_ascii=False)
    scope_payload = json.dumps(
        dict(scope or {"message_types": ["text", "media", "mixed"], "range": "all"}),
        ensure_ascii=False,
    )
    with transaction(conn):
        source_id = conn.execute(
            "INSERT INTO source (uuid, name, platform, kind, external_ref, config, trust_level, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1) RETURNING source_id",
            (str(uuid.uuid4()), name, platform, kind, external_ref, payload, trust_level),
        ).fetchone()[0]
        authz_id = conn.execute(
            "INSERT INTO source_authorization "
            "(source_id, mode, scope, evidence_ref, granted_by, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING authz_id",
            (source_id, authorization_mode, scope_payload, evidence_ref, granted_by, expires_at),
        ).fetchone()[0]
    return {
        "source_id": int(source_id),
        "authz_id": int(authz_id),
        "name": name,
        "kind": kind,
        "trust_level": trust_level,
        "authorization_mode": authorization_mode,
    }


def get_source(conn: sqlite3.Connection, ref: str | int) -> dict[str, Any] | None:
    """按 ``source_id``（整数或数字字符串）或 ``name`` 找来源。"""
    text = str(ref)
    if text.isdigit():
        row = conn.execute("SELECT * FROM source WHERE source_id = ?", (int(text),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM source WHERE name = ?", (text,)).fetchone()
    return dict(row) if row is not None else None


def require_source(conn: sqlite3.Connection, ref: str | int) -> dict[str, Any]:
    source = get_source(conn, ref)
    if source is None:
        known = [row["name"] for row in conn.execute("SELECT name FROM source ORDER BY name")]
        hint = f"现有来源：{', '.join(known)}" if known else "还没有任何来源，先 `pc source add`。"
        raise SourceError(f"找不到来源「{ref}」。{hint}")
    return source


def load_authorizations(conn: sqlite3.Connection, source_id: int) -> list[dict[str, Any]]:
    """读一个来源的全部授权行；连接器用它回答 ``authorize()``。"""
    rows = conn.execute(
        "SELECT * FROM source_authorization WHERE source_id = ? ORDER BY authz_id",
        (source_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def authorization_state(authorizations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把授权行归纳成一句人话：是否有效、什么模式、什么时候过期。"""
    moment = now_iso()
    live: list[Mapping[str, Any]] = []
    for row in authorizations:
        if row.get("revoked_at"):
            continue
        expires_at = row.get("expires_at")
        if expires_at and str(expires_at) <= moment:
            continue
        live.append(row)
    if not live:
        reason = "没有授权记录" if not authorizations else "授权已撤销或已过期"
        return {"ok": False, "reason": reason, "modes": (), "expires_at": None}
    return {
        "ok": True,
        "reason": "授权有效",
        "modes": tuple(sorted({str(row.get("mode")) for row in live})),
        "expires_at": min(
            (str(row["expires_at"]) for row in live if row.get("expires_at")), default=None
        ),
    }


def list_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """列出全部来源及其授权状态（含已软删的，``deleted_at`` 会标出来）。"""
    result: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM source ORDER BY source_id"):
        source = dict(row)
        state = authorization_state(load_authorizations(conn, int(source["source_id"])))
        source["authorization"] = state
        result.append(source)
    return result


def merge_roots(conn: sqlite3.Connection, source: Mapping[str, Any], roots: Sequence[str]) -> list[str]:
    """把本次实际使用的目录并进 ``source.config["roots"]``。

    **必须持久化**：worker 在另一个时刻取字节，它只会读 ``source.config``——如果目录
    只存在于那次命令行的参数里，导入能成功而处理阶段拿不到引用（无法续跑）。
    """
    try:
        config = source_config(source)
    except SourceConfigError as exc:
        # 服务层的错误类型统一成 SourceError，CLI 只需要捕获一种
        raise SourceError(str(exc)) from exc

    existing = config.get("roots")
    ordered: list[str] = []
    for item in list(existing) if isinstance(existing, list) else []:
        ordered.append(str(item))
    for item in roots:
        text = str(item)
        if text not in ordered:
            ordered.append(text)
    if ordered == (existing if isinstance(existing, list) else []):
        return ordered
    config["roots"] = ordered
    with transaction(conn):
        conn.execute(
            "UPDATE source SET config = ?, updated_at = ? WHERE source_id = ?",
            (json.dumps(config, ensure_ascii=False), now_iso(), int(source["source_id"])),
        )
    return ordered


def ensure_default_backend(
    conn: sqlite3.Connection, root: str | Path, *, name: str = "local"
) -> dict[str, Any]:
    """确保存在那行默认的本地存储后端，返回 ``{backend_id, created, root_changed}``。

    ``root_changed`` 是有用的一条信息而不是噪音：媒体根目录被改过，意味着已有
    ``object_key`` 指向的字节可能已经不在这个根下了（对象键不变、落盘位置变了）。
    调用方应当把它报出来，而不是默默接受。
    """
    text = str(root)
    row = conn.execute("SELECT backend_id, config FROM storage_backend WHERE name = ?", (name,)).fetchone()
    if row is None:
        with transaction(conn):
            backend_id = conn.execute(
                "INSERT INTO storage_backend (name, kind, config, root_prefix, is_default, writable) "
                "VALUES (?, 'local', ?, ?, 1, 1) RETURNING backend_id",
                (name, json.dumps({"root": text}, ensure_ascii=False), text),
            ).fetchone()[0]
        return {"backend_id": int(backend_id), "created": True, "root_changed": False}

    try:
        previous = json.loads(row["config"] or "{}")
    except json.JSONDecodeError:  # pragma: no cover - 由 json_valid 挡住
        previous = {}
    root_changed = str(previous.get("root") or "") != text
    if root_changed:
        with transaction(conn):
            conn.execute(
                "UPDATE storage_backend SET config = ?, root_prefix = ?, updated_at = ? "
                "WHERE backend_id = ?",
                (json.dumps({"root": text}, ensure_ascii=False), text, now_iso(), int(row["backend_id"])),
            )
    return {"backend_id": int(row["backend_id"]), "created": False, "root_changed": root_changed}


__all__ = [
    "AUTHORIZATION_MODES",
    "SOURCE_KINDS",
    "SourceError",
    "authorization_state",
    "create_source",
    "ensure_default_backend",
    "get_source",
    "list_sources",
    "load_authorizations",
    "merge_roots",
    "require_source",
]
