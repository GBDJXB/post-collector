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
from pc.services import compositions, ingest, sources, viewing
from pc.services.links import normalize_url
from pc.services.runtime import build_worker


def identity(digests, text: str) -> str:
    normalized = normalize_text(text)
    text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None
    if len(digests) <= 1:
        return "upload:" + ":".join([*digests, *([text_hash] if text_hash else [])])
    payload = json.dumps([list(digests), text_hash], separators=(",", ":"))
    return "upload:multi:v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ordered_parts(value, images, *, max_text_chars):
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("有序条目不是合法 JSON") from exc
    if not isinstance(parsed, list) or not parsed or len(parsed) > 500:
        raise ValueError("有序条目数量无效")
    used = []
    total_text = 0
    ordered = []
    for part in parsed:
        if not isinstance(part, dict):
            raise ValueError("有序条目格式无效")
        kind = part.get("kind")
        if kind == "text" and set(part) == {"kind", "text"} and isinstance(part["text"], str):
            if not normalize_text(part["text"]):
                raise ValueError("文本条目不能为空")
            total_text += len(part["text"])
            ordered.append(dict(part))
        elif kind == "link" and set(part) == {"kind", "url", "description"} and isinstance(part["description"], str):
            url = normalize_url(part["url"])
            total_text += len(part["description"])
            ordered.append({"kind": "link", "url": url, "description": part["description"]})
        elif kind == "image" and set(part) == {"kind", "index"} and type(part["index"]) is int:
            index = part["index"]
            if index < 0 or index >= len(images) or index in used:
                raise ValueError("图片引用不存在或重复")
            used.append(index)
            ordered.append(dict(part))
        else:
            raise ValueError("有序条目格式无效")
        if total_text > max_text_chars:
            raise ValueError("文本超过长度上限")
    if sorted(used) != list(range(len(images))):
        raise ValueError("图片必须全部在有序条目中引用一次")
    return ordered


def _submit_atomic(conn, *, source, envelope, key, refs, paths, limits, security, jobs,
                   name, description, tags, note):
    """同步完成扫描后在一次事务内归档、创建组合并写入自身标签。"""
    worker = build_worker(conn, paths, limits=limits, security=security, jobs=jobs)
    scoped_source = dict(source)
    if refs:
        config = json.loads(source["config"])
        config["roots"] = list(dict.fromkeys([*config.get("roots", []), str(paths.data_root / "upload-staging")]))
        scoped_source["config"] = json.dumps(config, ensure_ascii=False)
    mapping = {"source_id": source["source_id"], "text_raw": envelope.text_raw,
               "media_refs": json.dumps(list(refs)),
               "ordered_parts": json.dumps(list(envelope.ordered_parts), ensure_ascii=False)}
    collected = worker._collect(mapping, scoped_source, defer_media=True)
    if any(not blob.passed or blob.mime_detected not in {
        "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/heic"
    } for blob in collected.blobs):
        raise ValueError("图片扫描未通过；本次归档和组合均未创建，隔离字节已保留")
    if collected.blobs:
        import hashlib as _hashlib

        from pc.adapters.safe_files import open_scoped

        for blob in collected.blobs:
            with open_scoped(blob.ref, [paths.data_root / "upload-staging"]) as handle:
                data = handle.read(limits.max_media_bytes + 1)
            if len(data) != blob.size_bytes or _hashlib.sha256(data).hexdigest() != blob.sha256:
                raise ValueError("图片在扫描后发生变化")
            worker._storage.put(data)
        worker._backend_id("local", worker._storage.root)
    with db.transaction(conn):
        worker._require_authorized(int(source["source_id"]))
        report = ingest._ingest_one(conn, int(source["source_id"]), source.get("trust_level"),
                                     envelope, note.strip() or None, enqueue=False)
        message_id = conn.execute(
            "SELECT message_id FROM message WHERE source_id=? AND platform_msg_id=?",
            (source["source_id"], key),
        ).fetchone()[0]
        if report.inserted:
            message = dict(conn.execute("SELECT * FROM message WHERE message_id=?", (message_id,)).fetchone())
            worker._persist(message, scoped_source, collected, nested=True)
        ids = [row[0] for row in conn.execute(
            "SELECT c.content_id FROM content_item c JOIN message m USING(message_id) "
            "WHERE c.message_id=? AND c.status='active' AND c.nsfw_level<=0 "
            "AND c.deleted_at IS NULL AND m.deleted_at IS NULL ORDER BY c.ordinal,c.content_id",
            (message_id,),
        )]
        expected = len(envelope.ordered_parts) if envelope.ordered_parts else int(bool(envelope.text_raw)) + len(refs)
        if len(ids) != expected:
            raise ValueError("部分成员不可用；本次归档和组合均未创建")
        composition_id = compositions.write(conn, "create", name=name, description=description,
                                              contents=ids, max_text_chars=limits.max_text_chars,
                                              nested=True)["composition_id"]
        if tags:
            compositions.tag_change(conn, composition_id, tags, nested=True)
        conn.execute("INSERT INTO audit_log(actor_type,action,target_type,target_id,after_state,result,user_agent) "
                     "VALUES ('user','upload.submit','message',?,?,?,'local_root')",
                     (message_id, json.dumps({"duplicate": bool(report.duplicates), "status": "succeeded"}), "succeeded"))
    return {"message_id": message_id, "duplicate": bool(report.duplicates), "status": "succeeded",
            "job_status": "succeeded", "content_ids": ids, "composition_id": composition_id,
            "detail": "内容和组合已保存"}


def submit(conn, *, paths, limits, security, jobs, text, note, images, parts=None,
           create_composition=False, composition_name="", composition_description="", composition_tags=""):
    if parts is not None and text:
        raise ValueError("有序条目不能与旧文本字段混用")
    ordered = _ordered_parts(parts, images, max_text_chars=limits.max_text_chars)
    if len(text) > limits.max_text_chars or len(note) > 4096:
        raise ValueError("文本或备注超过长度上限")
    if len(images) > upload_files.MAX_IMAGES:
        raise ValueError("一次最多上传 19 张图片")
    if ordered is None and not normalize_text(text) and not images:
        raise ValueError("请输入文本或选择图片")
    text = text if normalize_text(text) else ""
    if create_composition:
        compositions._text(composition_name, 255, name=True)
        compositions._text(composition_description, limits.max_text_chars)
    elif composition_name or composition_description or composition_tags:
        raise ValueError("未选择创建组合却提供了组合字段")
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
            if not create_composition:
                sources.merge_roots(conn, source, [str(root)])
        if ordered is not None:
            ordered = [
                {"kind": "image", "ref": refs[part["index"]]} if part["kind"] == "image" else part
                for part in ordered
            ]
            identity_parts = [
                ["image", digests[part["index"]]] if part["kind"] == "image"
                else ["text", normalize_text(part["text"])] if part["kind"] == "text"
                else ["link", normalize_url(part["url"]).split("#", 1)[0], normalize_text(part["description"])]
                for part in json.loads(parts)
            ]
            key = "upload:ordered:v1:" + hashlib.sha256(json.dumps(identity_parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        else:
            key = identity(digests, text)
        envelope = SourceMessage(
            source_id=int(source["source_id"]), platform_msg_id=key,
            message_type="mixed" if text and refs else "media" if refs else "text",
            text_raw=text or None, media_refs=refs, ordered_parts=tuple(ordered or ()),
        )
        if create_composition:
            from pc.adapters.local_storage import LocalStorageBackend
            from pc.domain import ObjectKey
            from pc.services.pipeline import PermanentError

            storage = LocalStorageBackend(paths.media_root)
            fresh = [digest for digest in digests if not storage.exists(ObjectKey(digest))]
            try:
                return _submit_atomic(conn, source=source, envelope=envelope, key=key, refs=refs,
                                      paths=paths, limits=limits, security=security, jobs=jobs,
                                      name=composition_name, description=composition_description,
                                      tags=[tag.strip() for tag in composition_tags.splitlines() if tag.strip()],
                                      note=note)
            except BaseException as exc:
                # 只清理这次新增且库中无人引用的对象；其他 worker 正在运行时保留给对账。
                with db.transaction(conn):
                    busy = conn.execute("SELECT 1 FROM job WHERE status IN ('queued','running','failed') LIMIT 1").fetchone()
                    if not busy:
                        for digest in dict.fromkeys(fresh):
                            linked = conn.execute("SELECT 1 FROM media WHERE sha256=? LIMIT 1", (digest,)).fetchone()
                            if not linked:
                                storage.delete(ObjectKey(digest))
                if isinstance(exc, PermanentError):
                    raise ValueError(str(exc)) from exc
                raise
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
        if job is None and conn.execute(
            "SELECT 1 FROM audit_log WHERE action='upload.submit' AND target_type='message' "
            "AND target_id=? AND result='succeeded' "
            "AND json_extract(after_state, '$.status')='succeeded' LIMIT 1",
            (str(message_id),),
        ).fetchone():
            # 原子上传不创建任务；仅使用同事务提交的成功审计确认归档完成。
            status = "succeeded"
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
