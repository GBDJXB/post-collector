"""L3 处理管线：单进程 worker（DESIGN §2.1、§2.3、§10.3）。

一条消息一次任务（``ingest.process``），任务内部按顺序做完"取字节 → 扫描 → 哈希 →
去重 → 落盘 → 建内容项"。三条硬约束贯穿全文：

* **事务之外不写、事务之内不做慢活**：下载、扫描、哈希、落盘全在事务外，最后用一个
  短事务把所有结果写下去。SQLite 是单写者，把磁盘读进事务里等于让所有写者排队。
* **确定性失败进死信，不重试**：文件不存在、类型不放行、命中哈希墓地都是"重试一次
  也不会变"的错误（DESIGN §2.3 的重试策略）。退避重试只留给真正的偶发错误。
* **幂等护栏**：处理前先看这条消息有没有内容项，有就直接成功返回。崩溃恢复把
  ``running`` 重置为 ``queued`` 之后，重新执行不会产生第二份内容。

隔离区的字节是**物理分离**的：扫描不通过的媒体写进 ``quarantine_root``（一个独立的
``storage_backend`` 行），而不是主媒体目录。这一条是 L2 红线的邻居——匿名提报的隔离
要求同样落在"字节与主存储分开"这个物理事实上。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pc.adapters.magic_scan import MagicNumberScanner, declared_mime_for_name, item_type_for
from pc.adapters.registry import SourceConfigError, UnsupportedSourceKindError, connector_for
from pc.adapters.safe_files import UnsafeLocalPathError
from pc.adapters.sqlite import now_iso, post_digest_rows, post_digests, transaction
from pc.config import IngestLimits, JobSettings
from pc.domain import (
    ITEM_TYPE_LABELS,
    MediaTooLargeError,
    ObjectKey,
    filename_from_ref,
    local_source_authorized,
    normalize_text,
    post_signature,
    post_type_mask,
    search_extra_value,
    search_tokens,
)
from pc.services.ingest import JOB_TYPE_PROCESS
from pc.services.links import normalize_url
from pc.services.sources import ensure_default_backend, load_authorizations

#: 已被抢占但超过这么久没动静的任务，视为进程崩溃遗留，启动时重置为排队
STALE_LOCK_MINUTES = 15

#: 扫描结论 → ``media.scan_status``。
#: 注意 ``infected`` 永远不会出现：本阶段没有病毒引擎，"不通过"一律记 ``suspicious``，
#: 不去冒充一个并不存在的判定。
SCAN_STATUS_BY_VERDICT: dict[tuple[bool, bool], str] = {
    (True, False): "clean",
    (True, True): "suspicious",
    (False, False): "suspicious",
    (False, True): "suspicious",
}


class PermanentError(RuntimeError):
    """确定性失败：重试同样的输入只会得到同样的结论。

    进死信而不是退避重试——把"这个文件类型不放行"重试五次，只是把同一条错误抄写五遍，
    还会让真正需要重试的任务排在后面。
    """


@dataclass(frozen=True, slots=True)
class MediaBlob:
    """一个已经取回、扫描、落盘的媒体对象（``_collect`` 的产物）。"""

    ref: str
    key: ObjectKey
    sha256: str
    size_bytes: int
    mime_detected: str | None
    mime_declared: str | None
    extension: str | None
    name: str | None  # 引用里的文件名（只留 basename）；进 content_item.original_name
    passed: bool
    gray: bool
    scan_engine: str
    scan_reasons: tuple[str, ...]

    @property
    def scan_status(self) -> str:
        return SCAN_STATUS_BY_VERDICT[(self.passed, self.gray)]

    @property
    def quarantined(self) -> bool:
        return not self.passed


@dataclass(frozen=True, slots=True)
class Collected:
    """``_collect`` 的全部产物：文本信息 + 已落盘的媒体对象。"""

    text_raw: str | None = None
    text_normalized: str | None = None
    text_hash: str | None = None
    blobs: tuple[MediaBlob, ...] = ()
    ordered_parts: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class WorkReport:
    """一次 ``run_once`` 的结果。"""

    recovered: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead: int = 0
    quarantined: int = 0
    media_linked: int = 0
    notes: tuple[tuple[int, str, str], ...] = field(default=())

    def with_outcome(self, job_id: int, status: str, note: str, **counters: int) -> WorkReport:
        return WorkReport(
            recovered=self.recovered + counters.get("recovered", 0),
            claimed=self.claimed + counters.get("claimed", 0),
            succeeded=self.succeeded + counters.get("succeeded", 0),
            failed=self.failed + counters.get("failed", 0),
            dead=self.dead + counters.get("dead", 0),
            quarantined=self.quarantined + counters.get("quarantined", 0),
            media_linked=self.media_linked + counters.get("media_linked", 0),
            notes=(*self.notes, (job_id, status, note)),
        )


def _iso_minutes_ago(minutes: int) -> str:
    moment = datetime.now(UTC) - timedelta(minutes=minutes)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def _ref_name(ref: str) -> str:
    """取引用里的文件名部分，用于猜"声明的类型"（规则见 ``domain.filename_from_ref``）。

    猜类型需要一个字符串，而"没有名字"（``None``）在这里等价于空串——反正魔数嗅探会
    给出真正的判定，声明值只是用来与它比对的参考。
    """
    return filename_from_ref(ref) or ""


class Worker:
    """单进程 worker：抢占任务、执行、落账（DESIGN §2.3 的四条单写者纪律）。

    依赖全部注入（存储、扫描器、队列参数），因此可以在测试里换成假实现；
    连接器由 ``connector_for`` 按来源构造，worker 自己不碰任何平台细节。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        storage: Any,
        quarantine: Any,
        scanner: MagicNumberScanner | None = None,
        limits: IngestLimits | None = None,
        job_settings: JobSettings | None = None,
        worker_id: str | None = None,
        stale_lock_minutes: int = STALE_LOCK_MINUTES,
        upload_root: Path | None = None,
    ) -> None:
        self._conn = conn
        self._storage = storage
        self._quarantine = quarantine
        self._scanner = scanner or MagicNumberScanner()
        self._limits = limits or IngestLimits()
        self._jobs = job_settings or JobSettings()
        self._worker_id = worker_id or f"{os.getpid()}@{platform_module.node() or 'local'}"
        self._stale_minutes = stale_lock_minutes
        self._backend_ids: dict[str, int] = {}
        self._upload_root = upload_root

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # -- 队列 ---------------------------------------------------------------

    def recover_stale(self, *, message_id: int | None = None) -> int:
        """恢复超时 running：尚有次数则排队，耗尽则死信并审计。"""
        cutoff = _iso_minutes_ago(self._stale_minutes)
        target_sql = " AND target_type='message' AND target_id=? AND job_type='ingest.process'" if message_id is not None else ""
        target_args = (message_id,) if message_id is not None else ()
        with transaction(self._conn):
            exhausted = self._conn.execute(
                "SELECT * FROM job WHERE status = 'running' AND attempts >= max_attempts "
                "AND (locked_at IS NULL OR locked_at < ?)" + target_sql, (cutoff, *target_args)
            ).fetchall()
            for row in exhausted:
                moment = now_iso()
                reason = "最后一次尝试时进程退出，超时恢复后次数已耗尽"
                self._conn.execute(
                    "UPDATE job SET status='dead', locked_by=NULL, locked_at=NULL, "
                    "finished_at=?, updated_at=?, dead_reason=?, last_error=? WHERE job_id=?",
                    (moment, moment, reason, reason, row["job_id"]),
                )
                self._conn.execute(
                    "INSERT INTO audit_log(actor_type,action,target_type,target_id,after_state,result) "
                    "VALUES ('system', ?, ?, ?, ?, 'dead')",
                    (row["job_type"], row["target_type"], row["target_id"], json.dumps({"status":"dead", "note":reason})),
                )
            cursor = self._conn.execute(
                "UPDATE job SET status = 'queued', locked_by = NULL, locked_at = NULL, "
                "updated_at = ? WHERE status = 'running' AND (locked_at IS NULL OR locked_at < ?)" + target_sql,
                (now_iso(), cutoff, *target_args),
            )
        for row in exhausted:
            if row['target_type'] == 'message':
                self._cleanup_upload(int(row['target_id']))
        return int(cursor.rowcount or 0) + len(exhausted)

    def claim(self, *, message_id: int | None = None) -> dict[str, Any] | None:
        """原子抢占一条任务（``BEGIN IMMEDIATE`` + 选一条 + 标记，见 DESIGN §2.3）。

        ``attempts`` 在这里就 +1：抢占即一次尝试，进程被杀也要算数，否则一个能稳定
        搞崩 worker 的任务会被无限重试。
        """
        moment = now_iso()
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT job_id FROM job "
                "WHERE status IN ('queued', 'failed') AND run_after <= ? "
                "  AND attempts < max_attempts "
                + ("AND target_type='message' AND target_id=? AND job_type='ingest.process' " if message_id is not None else "")
                + "ORDER BY priority, job_id LIMIT 1",
                (moment, message_id) if message_id is not None else (moment,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE job SET status = 'running', locked_by = ?, locked_at = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE job_id = ?",
                (self._worker_id, moment, moment, int(row[0])),
            )
            claimed = self._conn.execute(
                "SELECT * FROM job WHERE job_id = ?", (int(row[0]),)
            ).fetchone()
        return dict(claimed) if claimed is not None else None

    def run_once(self, *, limit: int | None = None) -> WorkReport:
        """处理到队列空（或达到 ``limit``）。"""
        report = WorkReport()
        recovered = self.recover_stale()
        if recovered:
            report = report.with_outcome(0, "recovered", f"重置了 {recovered} 条超时任务", recovered=recovered)
        while limit is None or report.claimed < limit:
            job = self.claim()
            if job is None:
                break
            status, note = self._run_job(job)
            report = report.with_outcome(
                int(job["job_id"]),
                status,
                note,
                claimed=1,
                **{status: 1} if status in {"succeeded", "failed", "dead"} else {},
            )
        return report

    # -- 任务执行 -----------------------------------------------------------

    def run_message(self, message_id: int) -> WorkReport:
        """只抢占指定消息的可执行任务，不改动其他任务。"""
        recovered = self.recover_stale(message_id=message_id)
        job = self.claim(message_id=message_id)
        if job is None:
            return WorkReport(recovered=recovered)
        status, note = self._run_job(job)
        return WorkReport(recovered=recovered).with_outcome(
            int(job["job_id"]), status, note, claimed=1,
            **{status: 1} if status in {"succeeded", "failed", "dead"} else {},
        )

    def _cleanup_upload(self, message_id: int) -> None:
        if self._upload_root is None:
            return
        from pc.adapters.upload_files import cleanup_refs

        message = self._load("message", message_id)
        if message is not None:
            try:
                cleanup_refs(self._upload_root, self._refs(message))
            except (OSError, RuntimeError, ValueError):
                # 终态已经落账；清理失败保留字节供人工检查。
                import logging

                logging.getLogger(__name__).exception("上传暂存清理失败")

    def _run_job(self, job: Mapping[str, Any]) -> tuple[str, str]:
        try:
            if job.get("job_type") != JOB_TYPE_PROCESS:
                raise PermanentError(f"未知的任务类型：{job.get('job_type')!r}")
            if job.get("target_type") != "message":
                raise PermanentError(f"本 worker 只处理 message 目标，收到 {job.get('target_type')!r}")
            status, note = self._process_message(int(job["target_id"]))
            # 成功也必须落账：只在异常分支写状态的话，任务会永远停在 running，
            # 而 running 不会被再次抢占——看起来"处理过了"，其实什么都没记下。
            return self._finalize(job, status, note)
        except (PermanentError, UnsafeLocalPathError) as exc:
            return self._finalize(job, "dead", str(exc))
        except MediaTooLargeError as exc:
            return self._finalize(job, "dead", str(exc))
        except KeyError as exc:
            # 连接器契约：引用的对象已不存在（附件被删、文件被移走）。重试不会让它回来。
            return self._finalize(job, "dead", f"引用的媒体已不存在：{exc}")
        except (UnsupportedSourceKindError, SourceConfigError) as exc:
            return self._finalize(job, "dead", str(exc).splitlines()[0])
        except Exception as exc:
            attempts = int(job.get("attempts") or 1)
            max_attempts = int(job.get("max_attempts") or self._jobs.max_attempts_default)
            detail = f"{type(exc).__name__}: {exc}"
            if attempts >= max_attempts:
                return self._finalize(job, "dead", f"连续 {attempts} 次失败：{detail}")
            return self._finalize(job, "failed", detail)

    def _finalize(self, job: Mapping[str, Any], status: str, note: str) -> tuple[str, str]:
        """写任务结局（并写审计）。这是唯一改 ``job`` 状态的地方。"""
        job_id = int(job["job_id"])
        moment = now_iso()
        attempts = int(job.get("attempts") or 1)
        fields = {
            "status": status,
            "locked_by": None,
            "locked_at": None,
            "updated_at": moment,
            "last_error": note if status in {"failed", "dead"} else None,
            "dead_reason": note if status == "dead" else None,
            "finished_at": moment if status in {"succeeded", "dead"} else None,
            "run_after": moment
            if status == "succeeded"
            else _iso_after(self._jobs.backoff_for(attempts)),
        }
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE job SET status = :status, locked_by = :locked_by, locked_at = :locked_at, "
                "updated_at = :updated_at, last_error = :last_error, dead_reason = :dead_reason, "
                "finished_at = :finished_at, run_after = :run_after WHERE job_id = :job_id",
                {**fields, "job_id": job_id},
            )
            self._conn.execute(
                "INSERT INTO audit_log (actor_type, action, target_type, target_id, after_state, result) "
                "VALUES ('system', ?, ?, ?, ?, ?)",
                (
                    str(job.get("job_type")),
                    str(job.get("target_type")),
                    int(job.get("target_id") or 0),
                    json.dumps({"status": status, "attempts": attempts, "note": note}, ensure_ascii=False),
                    status,
                ),
            )
        if status in {'succeeded', 'dead'}:
            self._cleanup_upload(int(job.get('target_id') or 0))
        return status, note

    # -- 单条消息 -----------------------------------------------------------

    def _process_message(self, message_id: int) -> tuple[str, str]:
        message = self._load("message", message_id)
        if message is None:
            raise PermanentError(f"消息 {message_id} 不存在（可能已被删除）")
        if message.get("deleted_at"):
            raise PermanentError("消息已软删，跳过处理")
        source = self._load("source", int(message["source_id"]))
        if source is None:
            raise PermanentError(f"消息所属的来源 {message['source_id']} 不存在")

        collected = self._collect(message, source)
        return self._persist(message, source, collected)

    def _load(self, table: str, key: int) -> dict[str, Any] | None:
        column = {"message": "message_id", "source": "source_id"}[table]
        row = self._conn.execute(f"SELECT * FROM {table} WHERE {column} = ?", (key,)).fetchone()
        return dict(row) if row is not None else None

    def _collect(self, message: Mapping[str, Any], source: Mapping[str, Any], *, defer_media: bool = False) -> Collected:
        """事务之外的全部慢活：取字节、扫描、哈希、落盘。

        落盘也在这里（不是写入阶段）：字节进对象存储与数据库行是两件事，先落盘再写行，
        中途崩溃只会留下一个没人引用的对象（由对账任务处理），不会留下"有行没字节"的
        悬空引用。
        """
        self._require_authorized(int(source["source_id"]))
        try:
            ordered = json.loads(message.get("ordered_parts") or "[]")
        except json.JSONDecodeError as exc:
            raise PermanentError("有序信封损坏") from exc
        if not isinstance(ordered, list):
            raise PermanentError("有序信封须为数组")
        text_raw = (message.get("text_raw") or "") if not ordered else ""
        text_normalized = normalize_text(text_raw) if text_raw.strip() else ""
        text_hash = (
            hashlib.sha256(text_normalized.encode("utf-8")).hexdigest() if text_normalized else None
        )

        refs = self._refs(message)
        if text_hash and self._tombstoned(text_hash, alg="content_hash"):
            raise PermanentError("文本命中哈希墓地，拒绝重新导入")
        used_refs: set[str] = set()
        for part in ordered:
            if not isinstance(part, dict) or part.get("kind") not in {"text", "image", "link"}:
                raise PermanentError("有序信封条目无效")
            if part["kind"] == "text":
                if not isinstance(part.get("text"), str):
                    raise PermanentError("有序文本须为字符串")
                value = normalize_text(part["text"])
                if not value or self._tombstoned(hashlib.sha256(value.encode()).hexdigest(), alg="content_hash"):
                    raise PermanentError("有序文本为空或命中哈希墓地")
            if part["kind"] == "image":
                ref = part.get("ref")
                if not isinstance(ref, str) or ref not in refs or ref in used_refs:
                    raise PermanentError("有序图片引用缺失或重复")
                used_refs.add(ref)
            if part["kind"] == "link":
                if not isinstance(part.get("description"), str):
                    raise PermanentError("有序链接说明无效")
                try:
                    normalize_url(part["url"])
                except (KeyError, ValueError, TypeError) as exc:
                    raise PermanentError("有序链接无效") from exc
        if ordered and len(used_refs) != len(refs):
            raise PermanentError("有序信封未包含所有图片")
        blobs: list[MediaBlob] = []
        if refs:
            connector = connector_for(
                source,
                load_authorizations(self._conn, int(source["source_id"])),
                max_media_bytes=self._limits.max_media_bytes,
            )
            if not connector.authorize():
                raise PermanentError(
                    "来源未通过授权校验：凭证 / 授权范围 / 授权证明三者不齐备（DESIGN §1.3）"
                )
            capabilities = connector.capabilities()
            if not capabilities.get("supports_media_fetch", False):
                raise PermanentError(
                    f"来源 {source.get('name')} 声明不支持取媒体（supports_media_fetch=false），"
                    "但这条消息带媒体引用"
                )
            for ref in refs:
                blobs.append(self._collect_one(connector, ref, defer_media=True) if defer_media
                             else self._collect_one(connector, ref))

        return Collected(
            text_raw=text_raw or None,
            text_normalized=text_normalized or None,
            text_hash=text_hash,
            blobs=tuple(blobs),
            ordered_parts=tuple(ordered),
        )

    def _collect_one(self, connector: Any, ref: str, *, defer_media: bool = False) -> MediaBlob:
        name = _ref_name(ref)
        declared = declared_mime_for_name(name)
        data = connector.fetch_media(ref)
        verdict = self._scanner.scan(data, declared_mime=declared)
        if self._upload_root is not None:
            from pc.adapters.safe_files import absolute_path

            path = absolute_path(ref)
            if path.is_relative_to(absolute_path(self._upload_root)) and verdict.details.get("mime_detected") not in {
                "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/heic",
            }:
                verdict = replace(verdict, passed=False, reasons=(*verdict.reasons, "浏览器上传仅支持指定图片格式"))
        digest = hashlib.sha256(data).hexdigest()
        if self._tombstoned(digest):
            raise PermanentError(
                f"该内容曾被硬删（哈希墓地里已有 {digest[:12]}…），按设计拒绝重新导入：{ref}"
            )
        # 扫描不通过的字节进隔离区（物理分离），不通过的也照存——丢掉就没法复核了
        target = self._quarantine if not verdict.passed else self._storage
        key = ObjectKey(digest) if defer_media and verdict.passed else target.put(data)
        return MediaBlob(
            ref=ref,
            key=key,
            sha256=digest,
            size_bytes=len(data),
            mime_detected=verdict.details.get("mime_detected"),
            mime_declared=declared,
            extension=os.path.splitext(name)[1].lower().lstrip(".") or None,
            name=filename_from_ref(ref),
            passed=verdict.passed,
            gray=verdict.gray,
            scan_engine=verdict.engine,
            scan_reasons=tuple(verdict.reasons),
        )

    def _refs(self, message: Mapping[str, Any]) -> tuple[str, ...]:
        raw = message.get("media_refs") or "[]"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - 由 json_valid 挡住
            raise PermanentError(f"message.media_refs 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, list):
            raise PermanentError("message.media_refs 必须是 JSON 数组")
        return tuple(str(item) for item in parsed)

    def _require_authorized(self, source_id: int) -> None:
        source = self._load("source", source_id)
        if source is None or not local_source_authorized(source, load_authorizations(self._conn, source_id), now_iso()):
            raise PermanentError("来源停用、已删除或授权失效，拒绝处理")

    def _tombstoned(self, digest: str, *, alg: str = "sha256") -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM hash_tombstone WHERE hash = ? AND alg = ? "
            "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
            (digest, alg, now_iso()),
        ).fetchone()
        return row is not None

    def _persist(
        self, message: Mapping[str, Any], source: Mapping[str, Any], collected: Collected, *, nested: bool = False
    ) -> tuple[str, str]:
        """一个短事务写下全部结果（DESIGN §2.3 的单写者纪律）。"""
        message_id = int(message["message_id"])
        # search_extra 是 content_fts 的合成列（DESIGN §5.1）：来源名、平台、发送者名，
        # 再加上每个内容项各自的类型/文件名与整条消息共用的来源备注。**逐项合成**而不是
        # 先拼一个公共串：类型与文件名是 per-item 的（一条消息里的图与文本项不一样）。
        # 标签名与同义词在打标落地后由检索层的 index()/rebuild() 重算进来。
        shared_parts = (
            source.get("name"),
            source.get("platform"),
            message.get("sender_display_name"),
        )
        origin_note = message.get("origin_note")
        # 消息时间抄到内容项上：它是**派生的**（事实源仍是 message.sent_at），只为让
        # "按消息时间排序"能在单表索引上走完（跨表 ORDER BY 用不上任何索引，见 0006）。
        sent_at = message.get("sent_at")
        quarantined = 0
        linked = 0

        # 先把存储后端行准备好。它自己是一个短事务，**不能**放进下面那个事务里：
        # SQLite 不支持嵌套 BEGIN（db.transaction 会报"事务里不能开事务"）。
        for blob in collected.blobs:
            self._backend_id(
                "local-quarantine" if blob.quarantined else "local",
                self._quarantine.root if blob.quarantined else self._storage.root,
            )

        with transaction(self._conn, nested=nested):
            self._require_authorized(int(source["source_id"]))
            if collected.text_hash and self._tombstoned(collected.text_hash, alg="content_hash"):
                raise PermanentError("文本命中哈希墓地，拒绝重新导入")
            if any(self._tombstoned(blob.sha256) for blob in collected.blobs):
                raise PermanentError("媒体命中哈希墓地，拒绝重新导入")
            existing = self._conn.execute(
                "SELECT count(*) FROM content_item WHERE message_id = ? AND deleted_at IS NULL",
                (message_id,),
            ).fetchone()[0]
            if existing:
                # 幂等护栏：崩溃恢复后重新执行会走到这里
                return "succeeded", f"已处理过（已有 {existing} 个内容项，跳过）"

            ordinal = 0
            part_types: list[str] = []
            if collected.ordered_parts:
                by_ref = {blob.ref: blob for blob in collected.blobs}
                for part in collected.ordered_parts:
                    kind = part["kind"]
                    if kind == "text":
                        raw = part["text"]
                        normalized = normalize_text(raw)
                        digest = hashlib.sha256(normalized.encode()).hexdigest()
                        if self._tombstoned(digest, alg="content_hash"):
                            raise PermanentError("有序文本命中哈希墓地")
                        self._insert_text_item(message_id, Collected(raw, normalized, digest), shared_parts, origin_note, sent_at, ordinal, source)
                    elif kind == "link":
                        self._insert_link_item(message_id, part, shared_parts, origin_note, sent_at, ordinal)
                    else:
                        blob = by_ref.get(part.get("ref"))
                        if blob is None:
                            raise PermanentError("有序图片缺少可用媒体引用")
                        _, is_quarantined = self._insert_media_item(message_id, blob, shared_parts, origin_note, sent_at, ordinal, source)
                        linked += 1
                        quarantined += int(is_quarantined)
                    part_types.append(item_type_for(blob.mime_detected) if kind == "image" else kind)
                    ordinal += 1
            else:
                if collected.text_raw is not None and collected.text_normalized is not None:
                    self._insert_text_item(
                        message_id, collected, shared_parts, origin_note, sent_at, ordinal, source
                    )
                    part_types.append("text")
                    ordinal += 1
                for blob in collected.blobs:
                    _, is_quarantined = self._insert_media_item(
                        message_id, blob, shared_parts, origin_note, sent_at, ordinal, source
                    )
                    part_types.append(item_type_for(blob.mime_detected))
                    ordinal += 1
                    linked += 1
                    quarantined += int(is_quarantined)

            # 帖行是检索的结果单位（migrations/0007 的文件头），与内容项同一个事务写：
            # 两者不一致时，检索会给出"有帖无部件"或"有部件无帖"的错页。
            self._upsert_post(
                message_id, part_types=part_types, sent_at=sent_at
            )

        return (
            "succeeded",
            f"内容项 {ordinal} 个（隔离 {quarantined} 个，媒体引用 {linked} 个）",
        )

    def _upsert_post(
        self,
        message_id: int,
        *,
        part_types: Sequence[str],
        sent_at: str | None,
    ) -> None:
        """写这条消息的帖子行：构成摘要 + 整帖签名 + 帖级归组。

        ``part_count``/``type_mask``/``first_seen_at`` 的口径必须与
        ``adapters.sqlite.sync_post_rows``（批量重算）逐字一致，否则重算会把它们反复改写：
        因此 ``first_seen_at`` 取的是**部件里最早的**那个，而不是"这次插入的时刻"。
        """
        if not part_types:
            return  # 没有部件就没有帖子行（空消息不该在结果里出现）
        signature = post_signature(post_digests(post_digest_rows(self._conn, [message_id]))[message_id])
        first_seen_at = self._conn.execute(
            "SELECT min(first_seen_at) FROM content_item WHERE message_id = ? AND deleted_at IS NULL",
            (message_id,),
        ).fetchone()[0]
        # 帖级归组：同签名里 message_id 最小的当代表（把"自己"也算进去，这样第一条帖的
        # 代表就是它自己，重算两次不会来回改）。签名缺失 = 不折叠（NULL）。
        canonical: int | None = None
        if signature is not None:
            row = self._conn.execute(
                "SELECT min(message_id) FROM post WHERE content_signature = ?", (signature,)
            ).fetchone()
            existing = int(row[0]) if row and row[0] is not None else message_id
            canonical = min(message_id, existing)
        self._conn.execute(
            "INSERT INTO post (message_id, part_count, type_mask, first_seen_at, sent_at,"
            " content_signature, canonical_post_id, dup_group_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET part_count = excluded.part_count,"
            " type_mask = excluded.type_mask, first_seen_at = excluded.first_seen_at,"
            " sent_at = excluded.sent_at, content_signature = excluded.content_signature,"
            " canonical_post_id = excluded.canonical_post_id,"
            " dup_group_id = excluded.dup_group_id, updated_at = ?",
            (
                message_id,
                len(part_types),
                post_type_mask(part_types),
                first_seen_at,
                sent_at,
                signature,
                canonical,
                canonical,
                now_iso(),
            ),
        )

    def _insert_text_item(
        self,
        message_id: int,
        collected: Collected,
        shared_parts: tuple[Any, ...],
        origin_note: str | None,
        sent_at: str | None,
        ordinal: int,
        source: Mapping[str, Any],
    ) -> int:
        # 文本项的文件名留空：本地来源的纯文本文件在 L2 之后就没有名字了（它没有 media ref），
        # 补齐它需要给信封加字段，属于另一刀（见 AGENTS 的检索约束）。
        extra = search_extra_value(
            *shared_parts, "text", ITEM_TYPE_LABELS["text"], None, origin_note
        )
        group = None
        if collected.text_hash:
            prior = self._conn.execute(
                "SELECT content_id, dup_group_id FROM content_item "
                "WHERE content_hash = ? AND deleted_at IS NULL ORDER BY content_id LIMIT 1",
                (collected.text_hash,),
            ).fetchone()
            if prior is not None:
                # 同一段文本出现在两条消息里：归组而不是拒绝（0002 迁移的语义）
                group = int(prior["dup_group_id"] or prior["content_id"])
                self._conn.execute(
                    "UPDATE content_item SET dup_group_id = ?, canonical_id = COALESCE(canonical_id, ?), "
                    "updated_at = ? WHERE content_id = ?",
                    (group, group, now_iso(), int(prior["content_id"])),
                )
        return int(
            self._conn.execute(
                "INSERT INTO content_item ("
                "  uuid, message_id, item_type, ordinal, text_content, text_search,"
                "  search_extra, original_name, sent_at, content_hash, char_count, byte_size,"
                "  dup_group_id, canonical_id, first_seen_at"
                ") VALUES (?, ?, 'text', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?) "
                "RETURNING content_id",
                (
                    str(uuid.uuid4()),
                    message_id,
                    ordinal,
                    # text_content 是内容项正文（**不加改动**；不可变原文另外存在
                    # message.text_raw 里），它喂给 content_fts 的 trigram 索引
                    collected.text_raw,
                    # text_search 是应用层分词结果（DESIGN §5.1），喂给 content_word。
                    # 与 content_fts 相同的四列一起分词：否则"2 字的标签名/来源名"在词级
                    # 索引里无处可查，1~2 字查询会静默落空。
                    search_tokens(collected.text_raw, None, None, extra),
                    extra,
                    sent_at,
                    collected.text_hash,
                    len(collected.text_normalized or ""),
                    len((collected.text_raw or "").encode("utf-8")),
                    group,
                    group,
                    now_iso(),
                ),
            ).fetchone()[0]
        )

    def _insert_link_item(self, message_id, part, shared_parts, origin_note, sent_at, ordinal):
        """独立链接保留人工说明，URL 索引与规范键不触发任何网络访问。"""
        url = normalize_url(part["url"])
        description = part["description"]
        normalized = normalize_text(description)
        canonical = url.split("#", 1)[0]
        digest = hashlib.sha256(json.dumps([canonical, normalized], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if self._tombstoned(digest, alg="content_hash"):
            raise PermanentError("链接命中哈希墓地")
        extra = search_extra_value(*shared_parts, "link", ITEM_TYPE_LABELS["link"], canonical, origin_note)
        content_id = self._conn.execute(
            "INSERT INTO content_item(uuid,message_id,item_type,ordinal,text_content,text_search,"
            "search_extra,content_hash,char_count,byte_size,sent_at,first_seen_at) "
            "VALUES (?,?,'link',?,?,?,?,?,?,?,?,?) RETURNING content_id",
            (str(uuid.uuid4()), message_id, ordinal, description,
             search_tokens(description, None, None, extra), extra, digest,
             len(normalized), len(description.encode()), sent_at, now_iso()),
        ).fetchone()[0]
        scheme, authority_path = url.split("://", 1)
        authority = authority_path.split("/", 1)[0]
        domain = authority.rsplit(":", 1)[0] if not authority.startswith("[") and ":" in authority else authority
        self._conn.execute(
            "INSERT INTO link(content_id,url_raw,url_normalized,url_canonical,domain,scheme,description) "
            "VALUES (?,?,?,?,?,?,?)",
            (content_id, part["url"], url, canonical, domain, scheme, description),
        )
        return int(content_id)

    def _insert_media_item(
        self,
        message_id: int,
        blob: MediaBlob,
        shared_parts: tuple[Any, ...],
        origin_note: str | None,
        sent_at: str | None,
        ordinal: int,
        source: Mapping[str, Any],
    ) -> tuple[int, bool]:
        status = "quarantined" if blob.quarantined else "active"
        item_type = item_type_for(blob.mime_detected)
        # 文件名来自这条内容自己的引用（多附件消息里每个附件各带各的名字）；扩展名会随它
        # 一起进 search_extra，于是 `pc search jpg` 这类查询不用额外读 media 表。
        extra = search_extra_value(
            *shared_parts, item_type, ITEM_TYPE_LABELS.get(item_type), blob.name, origin_note
        )
        content_id = int(
            self._conn.execute(
                "INSERT INTO content_item ("
                "  uuid, message_id, item_type, ordinal, text_search, search_extra,"
                "  original_name, sent_at, byte_size, status, first_seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING content_id",
                (
                    str(uuid.uuid4()),
                    message_id,
                    item_type,
                    ordinal,
                    # 媒体项今天没有正文；caption/OCR 落地后它们会自动经由这里进词级索引
                    search_tokens(None, None, None, extra),
                    extra,
                    blob.name,
                    sent_at,
                    blob.size_bytes,
                    status,
                    now_iso(),
                ),
            ).fetchone()[0]
        )
        media_id = self._reuse_or_insert_media(blob, content_id)
        self._conn.execute(
            "INSERT INTO content_media (content_id, media_id, role, ordinal) VALUES (?, ?, 'primary', 0) "
            "ON CONFLICT (content_id, media_id, role) DO NOTHING",
            (content_id, media_id),
        )
        if blob.quarantined or blob.gray:
            self._enqueue_review(content_id, blob)
        return content_id, blob.quarantined

    def _reuse_or_insert_media(self, blob: MediaBlob, content_id: int) -> int:
        """按 ``media.sha256`` 去重：同一份字节只有一行 media，多个内容项各自引用它。

        复用时不改已有行：``scan_status`` 是**字节**的属性，同一份字节先前的判定仍然
        成立；本次的判定落在内容项的状态上（一次"扩展名骗人"的重复出现不该把已经放行
        的行改回可疑）。
        """
        row = self._conn.execute(
            "SELECT media_id FROM media WHERE sha256 = ?", (blob.sha256,)
        ).fetchone()
        if row is not None:
            return int(row[0])
        backend_name = "local-quarantine" if blob.quarantined else "local"
        backend_root = self._quarantine.root if blob.quarantined else self._storage.root
        backend_id = self._backend_id(backend_name, backend_root)
        return int(
            self._conn.execute(
                "INSERT INTO media ("
                "  uuid, first_seen_content_id, sha256, size_bytes, mime_detected, mime_declared,"
                "  extension, storage_backend_id, object_key, storage_class, scan_status,"
                "  scan_engine, scan_at, quarantine_key"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'hot', ?, ?, ?, ?) RETURNING media_id",
                (
                    str(uuid.uuid4()),
                    content_id,
                    blob.sha256,
                    blob.size_bytes,
                    blob.mime_detected,
                    blob.mime_declared,
                    blob.extension,
                    backend_id,
                    blob.key.value,
                    blob.scan_status,
                    blob.scan_engine,
                    now_iso(),
                    blob.key.value if blob.quarantined else None,
                ),
            ).fetchone()[0]
        )

    def _enqueue_review(self, content_id: int, blob: MediaBlob) -> None:
        self._conn.execute(
            "INSERT INTO review_task (uuid, kind, target_type, target_id, priority, payload, status) "
            "VALUES (?, 'scan_gray', 'content_item', ?, 100, ?, 'pending')",
            (
                str(uuid.uuid4()),
                content_id,
                json.dumps(
                    {
                        "ref": blob.ref,
                        "passed": blob.passed,
                        "gray": blob.gray,
                        "reasons": list(blob.scan_reasons),
                        "mime_detected": blob.mime_detected,
                        "mime_declared": blob.mime_declared,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    def _backend_id(self, name: str, root: Any) -> int:
        if name not in self._backend_ids:
            result = ensure_default_backend(self._conn, root, name=name)
            self._backend_ids[name] = int(result["backend_id"])
        return self._backend_ids[name]


def _iso_after(seconds: int) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=max(0, seconds))
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


__all__ = [
    "STALE_LOCK_MINUTES",
    "Collected",
    "MediaBlob",
    "PermanentError",
    "WorkReport",
    "Worker",
]
