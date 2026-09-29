"""L2 摄入：统一信封 → ``message`` 行 + ``job`` 入队（DESIGN §2.1、§2.3）。

这一层**不碰字节**：媒体下载、扫描、哈希都在 L3。它只做三件事，每件都有明确的幂等键：

1. **落信封**：``message`` 上的 ``UNIQUE (source_id, platform_msg_id)`` 就是幂等键，
   重复投递用 ``ON CONFLICT DO NOTHING`` 直接忽略（不报错、不覆盖）；
2. **记哈希**：``raw_payload_hash`` 一律写，``raw_payload`` 本体按三档策略默认不写
   （DESIGN §3.2.1）——原始载荷不进库，但"它是什么"必须可自证；
3. **入队**：``job.idem_key`` 保证同一条消息只排一次 ``ingest.process``。

批次大小是刻意的参数：SQLite 是单写者，一次导入把一万条塞进一个事务会让写锁长时间
不释放（DESIGN §2.3「批量作业必须分批」）。默认每 200 条提交一次。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pc.adapters.sqlite import now_iso, transaction
from pc.domain import SourceMessage

#: 本层排的任务类型：一条消息一次处理（内部的取字节/扫描/哈希等步骤在同一任务里串行）
JOB_TYPE_PROCESS = "ingest.process"

#: 原始载荷的保留档位。三档（结构化信封+哈希 / +原始载荷 / +原始载荷并压缩）中的第一档，
#: 由设置项 ``ingest.raw_payload_retention`` 控制，该设置项在检索切片里注册。
RAW_PAYLOAD_RETENTION = "hash_only"

#: 一次事务最多写多少条消息
DEFAULT_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class IngestReport:
    """一次导入的结果；CLI 直接把它打印给人看。"""

    seen: int = 0
    inserted: int = 0
    duplicates: int = 0
    enqueued: int = 0
    message_ids: tuple[int, ...] = ()

    def merge(self, other: IngestReport) -> IngestReport:
        return IngestReport(
            seen=self.seen + other.seen,
            inserted=self.inserted + other.inserted,
            duplicates=self.duplicates + other.duplicates,
            enqueued=self.enqueued + other.enqueued,
            message_ids=self.message_ids + other.message_ids,
        )


def job_idem_key(message_id: int, job_type: str = JOB_TYPE_PROCESS) -> str:
    """任务幂等键。

    格式与 DESIGN §2.3 一致（``job_type:target_type:target_id:model_version``）；
    本任务不涉及模型，最后一段留空——**留空也要占位**，否则将来加模型版本时格式会变，
    已入队的老任务会与新任务对不上。
    """
    return f"{job_type}:message:{message_id}:"


def raw_payload_hash(payload: Mapping[str, Any] | None) -> str | None:
    """原始载荷的自证哈希（内容为空时返回 ``None``，不写一个空串的哈希）。"""
    if not payload:
        return None
    canonical = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ingest_messages(
    conn: sqlite3.Connection,
    source: Mapping[str, Any],
    messages: Sequence[SourceMessage],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> IngestReport:
    """把一批信封落库并入队。

    ``source`` 必须是 ``source`` 表的一行：``source_id`` 与 ``trust_level`` 都会写进
    ``message``（``source_trust`` 是**摄入当时**的门控副本，来源后来的信任级别变化
    不该改写历史判定）。
    """
    source_id = int(source["source_id"])
    trust = source.get("trust_level")
    trust_value = int(trust) if trust is not None else None
    report = IngestReport()
    size = max(1, int(batch_size))

    for start in range(0, len(messages), size):
        chunk = messages[start : start + size]
        with transaction(conn):
            for message in chunk:
                report = report.merge(
                    _ingest_one(conn, source_id, trust_value, message)
                )
    return report


def _ingest_one(
    conn: sqlite3.Connection,
    source_id: int,
    trust_value: int | None,
    message: SourceMessage,
) -> IngestReport:
    payload_hash = raw_payload_hash(message.raw_payload)
    row = conn.execute(
        "INSERT INTO message ("
        "  uuid, source_id, platform_msg_id, platform_thread_id, sender_display_name,"
        "  sender_is_bot, sent_at, message_type, text_raw, reply_to_platform_id, permalink,"
        "  forward_chain, media_refs, raw_payload_hash, source_trust"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (source_id, platform_msg_id) DO NOTHING "
        "RETURNING message_id",
        (
            str(uuid.uuid4()),
            source_id,
            message.platform_msg_id,
            message.platform_thread_id,
            message.sender_display_name,
            1 if message.sender_is_bot else 0,
            message.sent_at,
            message.message_type,
            message.text_raw,
            message.reply_to_platform_id,
            message.permalink,
            json.dumps(list(message.forward_chain), ensure_ascii=False),
            json.dumps(list(message.media_refs), ensure_ascii=False),
            payload_hash,
            trust_value,
        ),
    ).fetchone()

    if row is None:
        # ON CONFLICT DO NOTHING 的 RETURNING 不给行 —— 这条消息已经在了
        return IngestReport(seen=1, duplicates=1)

    message_id = int(row[0])
    enqueued = conn.execute(
        "INSERT INTO job (job_type, target_type, target_id, status, priority, attempts,"
        " max_attempts, run_after, idem_key) "
        "VALUES (?, 'message', ?, 'queued', 100, 0, ?, ?, ?) "
        "ON CONFLICT (idem_key) WHERE idem_key IS NOT NULL DO NOTHING "
        "RETURNING job_id",
        (
            JOB_TYPE_PROCESS,
            message_id,
            _max_attempts(conn),
            now_iso(),
            job_idem_key(message_id),
        ),
    ).fetchone()

    return IngestReport(
        seen=1,
        inserted=1,
        enqueued=1 if enqueued is not None else 0,
        message_ids=(message_id,),
    )


def _max_attempts(conn: sqlite3.Connection) -> int:
    """沿用本库已有的最大尝试次数（同表同作业类型保持一致），默认 5。

    刻意从表里取而不是在这里读配置：摄入层不该依赖配置层，而**已经跑起来的实例**
    用什么值应当以库里的既有行为为准。
    """
    row = conn.execute(
        "SELECT max_attempts FROM job WHERE job_type = ? ORDER BY job_id DESC LIMIT 1",
        (JOB_TYPE_PROCESS,),
    ).fetchone()
    return int(row[0]) if row is not None else 5


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "JOB_TYPE_PROCESS",
    "RAW_PAYLOAD_RETENTION",
    "IngestReport",
    "ingest_messages",
    "job_idem_key",
    "raw_payload_hash",
]
