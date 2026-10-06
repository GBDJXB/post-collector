"""本机 root 浏览器提交，复用摄入、扫描与定向 worker。"""

from __future__ import annotations

import hashlib
import json

from pc.adapters import sqlite as db
from pc.adapters import upload_files
from pc.domain import (
    SourceMessage,
    SourceNotAuthorizedError,
    local_source_authorized,
    normalize_text,
)
from pc.services import ingest, sources, viewing
from pc.services.runtime import build_worker


def identity(digests, text: str) -> str:
    normalized = normalize_text(text)
    text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None
    if len(digests) <= 1:
        return "upload:" + ":".join([*digests, *([text_hash] if text_hash else [])])
    payload = json.dumps([list(digests), text_hash], separators=(",", ":"))
    return "upload:multi:v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def submit(conn, *, paths, limits, security, jobs, text, note, images):
    if len(text) > limits.max_text_chars or len(note) > 4096:
        raise ValueError("文本或备注超过长度上限")
    if len(images) > upload_files.MAX_IMAGES:
        raise ValueError("一次最多上传 19 张图片")
    if not normalize_text(text) and not images:
        raise ValueError("请输入文本或选择图片")
    text = text if normalize_text(text) else ""
    source = sources.ensure_manual_source(conn)["source"]
    if not local_source_authorized(source, sources.load_authorizations(conn, int(source["source_id"])), db.now_iso()):
        raise SourceNotAuthorizedError("来源停用、已删除或授权失效，拒绝上传")
    root = paths.data_root / "upload-staging"
    directory = None
    enqueued = False
    try:
        refs, digests = (), ()
        if images:
            directory, refs, digests = upload_files.stage(root, images, limit=limits.max_media_bytes)
            sources.merge_roots(conn, source, [str(root)])
        key = identity(digests, text)
        envelope = SourceMessage(
            source_id=int(source["source_id"]), platform_msg_id=key,
            message_type="mixed" if text and refs else "media" if refs else "text",
            text_raw=text or None, media_refs=refs,
        )
        report = ingest.ingest_messages(conn, source, [envelope], origin_note=note)
        row = conn.execute(
            "SELECT message_id FROM message WHERE source_id=? AND platform_msg_id=?",
            (source["source_id"], key),
        ).fetchone()
        message_id = int(row[0])
        enqueued = bool(report.inserted)
        # 重复请求的文件属于本次，旧任务继续读取它自己的暂存引用。
        if directory and not enqueued:
            upload_files.cleanup(root, directory)
            directory = None
        worker = build_worker(conn, paths, limits=limits, security=security, jobs=jobs)
        worker.run_message(message_id)
        job = conn.execute(
            "SELECT status,last_error FROM job WHERE idem_key=?", (ingest.job_idem_key(message_id),)
        ).fetchone()
        status = job["status"] if job else "dead"
        quarantined = conn.execute(
            "SELECT count(*) FROM content_item WHERE message_id=? AND status='quarantined'",
            (message_id,),
        ).fetchone()[0]
        try:
            visible = viewing.detail(conn, message_id=message_id, content_id=None)["parts"]
        except viewing.ViewError:
            visible = []
        messages = {
            "succeeded": "内容处理完成", "queued": "内容已入队，请运行 pc work 接续处理",
            "running": "内容正在处理", "failed": "处理失败，任务等待重试，请运行 pc work",
            "dead": "处理终止，请检查服务终端与任务记录",
        }
        outcome = "quarantined" if quarantined else status
        hint = "部分或全部图片已隔离，只有可见内容可以查看" if quarantined else messages.get(status, status)
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO audit_log(actor_type,action,target_type,target_id,after_state,result,user_agent) "
                "VALUES ('user','upload.submit','message',?,?,?,'local_root')",
                (message_id, json.dumps({"duplicate": bool(report.duplicates), "status": outcome}), "succeeded"),
            )
        return {"message_id": message_id, "duplicate": bool(report.duplicates), "status": outcome,
                "job_status": status, "content_ids": [part["content_id"] for part in visible], "detail": hint}
    finally:
        if directory and not enqueued:
            upload_files.cleanup(root, directory)
