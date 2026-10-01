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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pc.adapters.magic_scan import MagicNumberScanner, declared_mime_for_name, item_type_for
from pc.adapters.registry import SourceConfigError, UnsupportedSourceKindError, connector_for
from pc.adapters.sqlite import now_iso, transaction
from pc.config import IngestLimits, JobSettings
from pc.domain import (
    ITEM_TYPE_LABELS,
    MediaTooLargeError,
    ObjectKey,
    filename_from_ref,
    normalize_text,
    search_extra_value,
    search_tokens,
)
from pc.services.ingest import JOB_TYPE_PROCESS
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

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # -- 队列 ---------------------------------------------------------------

    def recover_stale(self) -> int:
        """把 ``locked_at`` 超时的 ``running`` 任务重置为排队（DESIGN §2.3 崩溃恢复）。"""
        cutoff = _iso_minutes_ago(self._stale_minutes)
        with transaction(self._conn):
            cursor = self._conn.execute(
                "UPDATE job SET status = 'queued', locked_by = NULL, locked_at = NULL, "
                "updated_at = ? WHERE status = 'running' AND (locked_at IS NULL OR locked_at < ?)",
                (now_iso(), cutoff),
            )
        return int(cursor.rowcount or 0)

    def claim(self) -> dict[str, Any] | None:
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
                "ORDER BY priority, job_id LIMIT 1",
                (moment,),
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
        except PermanentError as exc:
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

    def _collect(self, message: Mapping[str, Any], source: Mapping[str, Any]) -> Collected:
        """事务之外的全部慢活：取字节、扫描、哈希、落盘。

        落盘也在这里（不是写入阶段）：字节进对象存储与数据库行是两件事，先落盘再写行，
        中途崩溃只会留下一个没人引用的对象（由对账任务处理），不会留下"有行没字节"的
        悬空引用。
        """
        text_raw = message.get("text_raw") or ""
        text_normalized = normalize_text(text_raw) if text_raw.strip() else ""
        text_hash = (
            hashlib.sha256(text_normalized.encode("utf-8")).hexdigest() if text_normalized else None
        )

        refs = self._refs(message)
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
                blobs.append(self._collect_one(connector, ref))

        return Collected(
            text_raw=text_raw or None,
            text_normalized=text_normalized or None,
            text_hash=text_hash,
            blobs=tuple(blobs),
        )

    def _collect_one(self, connector: Any, ref: str) -> MediaBlob:
        name = _ref_name(ref)
        declared = declared_mime_for_name(name)
        data = connector.fetch_media(ref)
        verdict = self._scanner.scan(data, declared_mime=declared)
        digest = hashlib.sha256(data).hexdigest()
        if self._tombstoned(digest):
            raise PermanentError(
                f"该内容曾被硬删（哈希墓地里已有 {digest[:12]}…），按设计拒绝重新导入：{ref}"
            )
        # 扫描不通过的字节进隔离区（物理分离），不通过的也照存——丢掉就没法复核了
        target = self._quarantine if not verdict.passed else self._storage
        key = target.put(data)
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

    def _tombstoned(self, digest: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM hash_tombstone WHERE hash = ? AND alg = 'sha256' "
            "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
            (digest, now_iso()),
        ).fetchone()
        return row is not None

    def _persist(
        self, message: Mapping[str, Any], source: Mapping[str, Any], collected: Collected
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
        quarantined = 0
        linked = 0

        # 先把存储后端行准备好。它自己是一个短事务，**不能**放进下面那个事务里：
        # SQLite 不支持嵌套 BEGIN（db.transaction 会报"事务里不能开事务"）。
        for blob in collected.blobs:
            self._backend_id(
                "local-quarantine" if blob.quarantined else "local",
                self._quarantine.root if blob.quarantined else self._storage.root,
            )

        with transaction(self._conn):
            existing = self._conn.execute(
                "SELECT count(*) FROM content_item WHERE message_id = ? AND deleted_at IS NULL",
                (message_id,),
            ).fetchone()[0]
            if existing:
                # 幂等护栏：崩溃恢复后重新执行会走到这里
                return "succeeded", f"已处理过（已有 {existing} 个内容项，跳过）"

            ordinal = 0
            if collected.text_raw is not None and collected.text_normalized is not None:
                self._insert_text_item(message_id, collected, shared_parts, origin_note, ordinal, source)
                ordinal += 1

            for blob in collected.blobs:
                _, is_quarantined = self._insert_media_item(
                    message_id, blob, shared_parts, origin_note, ordinal, source
                )
                ordinal += 1
                linked += 1
                if is_quarantined:
                    quarantined += 1

        return (
            "succeeded",
            f"内容项 {ordinal} 个（隔离 {quarantined} 个，媒体引用 {linked} 个）",
        )

    def _insert_text_item(
        self,
        message_id: int,
        collected: Collected,
        shared_parts: tuple[Any, ...],
        origin_note: str | None,
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
                "  search_extra, original_name, content_hash, char_count, byte_size,"
                "  dup_group_id, canonical_id, first_seen_at"
                ") VALUES (?, ?, 'text', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?) "
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
                    collected.text_hash,
                    len(collected.text_normalized or ""),
                    len((collected.text_raw or "").encode("utf-8")),
                    group,
                    group,
                    now_iso(),
                ),
            ).fetchone()[0]
        )

    def _insert_media_item(
        self,
        message_id: int,
        blob: MediaBlob,
        shared_parts: tuple[Any, ...],
        origin_note: str | None,
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
                "  original_name, byte_size, status, first_seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING content_id",
                (
                    str(uuid.uuid4()),
                    message_id,
                    item_type,
                    ordinal,
                    # 媒体项今天没有正文；caption/OCR 落地后它们会自动经由这里进词级索引
                    search_tokens(None, None, None, extra),
                    extra,
                    blob.name,
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
