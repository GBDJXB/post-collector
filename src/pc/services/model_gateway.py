"""单次同意外发网关：先持久落账、再无事务联网、最后原子保存建议。"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from pc.adapters import sqlite as db
from pc.adapters.deepseek_model import ENDPOINT, MODEL, PROMPT_VERSION, DeepSeekModel, ModelError
from pc.services import model_tagging as mt
from pc.services import tagging
from pc.services.model_access import POLICY_VERSION

# 整个进程只有一个外发名额；非阻塞获取，没有队列。
_FLIGHT = threading.Lock()


@dataclass
class Preview:
    session: str
    generation: int
    expires: float
    target: dict
    input_digest: str
    catalog_digest: str
    authorization_digest: str
    config_digest: str
    consumed: bool = False


class ModelGateway:
    def __init__(self, access, *, session_valid, provider_factory=DeepSeekModel):
        self.access = access
        self.session_valid = session_valid
        self.provider_factory = provider_factory

    def _authorized(self, conn, session, actor_id):
        if not self.session_valid(session):
            self.access.invalidate_session(session)
            raise ValueError('会话已失效，请重新进入本机 Web')
        tagging._root(conn, actor_id)
        return self.access.require(conn, session)

    def _prune(self):
        now = self.access.clock()
        for ident, preview in list(self.access.previews.items()):
            if preview.expires <= now:
                del self.access.previews[ident]

    def _preview(self, session, ident):
        self._prune()
        preview = self.access.previews.get(ident)
        if preview is None or preview.session != session or preview.generation != self.access.generation:
            raise ValueError('预览已失效或不属于当前会话，请重新预览')
        return preview

    @staticmethod
    def _remaining(conn):
        floor = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        count = conn.execute('SELECT count(*) FROM egress_log WHERE created_at>=?', (floor,)).fetchone()[0]
        remaining = max(0, 20 - count)
        if not remaining:
            raise ValueError('滚动24小时20次外发预算已用尽；失败与结果未知也计次')
        return remaining

    def preview(self, conn, session, *, content_id=None, composition_id=None, actor_id=None):
        with self.access.lock, db.transaction(conn, immediate=False):
            self._authorized(conn, session, actor_id)
            self._prune()
            if len(self.access.previews) >= 32 or sum(p.session == session for p in self.access.previews.values()) >= 8:
                raise ValueError('未过期预览过多；每会话8个、进程32个')
            remaining = self._remaining(conn)
            prepared = mt.build_input(conn, content_id=content_id, composition_id=composition_id, max_image_pixels=self.access.max_image_pixels)
            catalog = mt.load_catalog(conn)
            wire = mt.request_bytes(prepared, catalog)
            config_digest = self.access.config_digest(conn)
            ident = str(uuid.uuid4())
            self.access.previews[ident] = Preview(session, self.access.generation, self.access.clock() + 300, prepared.target, prepared.fingerprint, catalog.fingerprint, prepared.authorization_digest, config_digest)
            return {'preview_id': ident, 'expires_in_seconds': 300, 'target': prepared.target,
                    'parts': [{k: part[k] for k in ('position', 'item_type', 'text')} | {'images': [{k: v for k, v in image.items() if k != 'data'} for image in part['images']]} for part in prepared.parts],
                    'candidate_catalog': catalog.public, 'candidate_count': len(catalog.records), 'text_bytes': prepared.text_bytes,
                    'image_count': prepared.image_count, 'image_bytes': prepared.image_bytes, 'request_bytes': len(wire),
                    'provider': 'deepseek', 'endpoint': ENDPOINT, 'model': MODEL, 'prompt_version': PROMPT_VERSION,
                    'policy_version': POLICY_VERSION, 'max_output_tokens': 2048, 'attempts_remaining': remaining,
                    'metadata_warning': '发送原始图片，不剥离 EXIF 等元数据；尺寸未知时未验证像素限制。不会发送文件名、路径、来源名或备注。',
                    'catalog_warning': '会发送完整已有候选目录的规范名和命名空间，不发送别名、描述、DB ID或统计。',
                    'unknown_outcome_warning': '结果未知仍可能计费，失败也消耗次数；次数上限不是金额上限，不自动重试。'}

    @staticmethod
    def _status(conn, ident):
        row = conn.execute('SELECT request_id,status,error_class FROM egress_log WHERE request_id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('外发记录不存在，请重新预览')
        return dict(row)

    @staticmethod
    def _finish(conn, ident, status, *, error_class=None, provider=None, run_id=None):
        conn.execute('UPDATE egress_log SET status=?,error_class=?,provider_request_id=?,usage=?,run_id=?,finished_at=? WHERE request_id=?',
                     (status, error_class, provider.provider_request_id if provider else None, mt.encoded(provider.usage).decode() if provider else None, run_id, db.now_iso(), ident))

    def send(self, conn, session, ident, *, confirmed=False, actor_id=None):
        if confirmed is not True:
            raise ValueError('必须对本次预览明确确认外发')
        with self.access.lock:
            self._authorized(conn, session, actor_id)
            preview = self._preview(session, ident)
            if preview.consumed:
                return self._status(conn, ident)
        if not _FLIGHT.acquire(blocking=False):
            raise ValueError('已有模型请求进行中；不支持并发或排队')
        try:
            with self.access.lock, db.transaction(conn):
                key = self._authorized(conn, session, actor_id)
                preview = self._preview(session, ident)
                if preview.consumed:
                    return self._status(conn, ident)
                self._remaining(conn)
                prepared = mt.build_input(conn, **preview.target, max_image_pixels=self.access.max_image_pixels)
                catalog = mt.load_catalog(conn)
                config_digest = self.access.config_digest(conn)
                if (prepared.fingerprint, prepared.authorization_digest, catalog.fingerprint, config_digest) != (preview.input_digest, preview.authorization_digest, preview.catalog_digest, preview.config_digest):
                    del self.access.previews[ident]
                    raise ValueError('目标、候选目录、授权或策略已变化，须重新预览并确认')
                wire = mt.request_bytes(prepared, catalog)
                conn.execute("INSERT INTO egress_log(request_id,actor,content_id,composition_id,purpose,provider,model,prompt_version,policy_version,input_digest,catalog_digest,authorization_digest,config_digest,text_bytes,image_bytes,image_count,candidate_count,request_bytes,status,created_at) VALUES (?,?,?,?,'tagging','deepseek',?,?,?,?,?,?,?,?,?,?,?,?, 'prepared',?)",
                             (ident, str(actor_id) if actor_id is not None else 'local_root', preview.target.get('content_id'), preview.target.get('composition_id'), MODEL, PROMPT_VERSION, POLICY_VERSION, prepared.fingerprint, catalog.fingerprint, prepared.authorization_digest, config_digest, prepared.text_bytes, prepared.image_bytes, prepared.image_count, len(catalog.records), len(wire), db.now_iso()))
            # 单次消费发生在已提交的落账之后；任何发送失败不恢复该授权。
            preview.consumed = True
            try:
                provider = self.provider_factory(key)
            except Exception as error:
                logging.getLogger(__name__).warning('模型外发失败 request_id=%s class=%s', ident, type(error).__name__)
                with db.transaction(conn):
                    self._finish(conn, ident, 'failed', error_class='provider_failure')
                return self._status(conn, ident)
            with self.access.lock, db.transaction(conn):
                try:
                    self._authorized(conn, session, actor_id)
                    fresh = mt.build_input(conn, **preview.target, max_image_pixels=self.access.max_image_pixels)
                    if (
                        preview.generation != self.access.generation
                        or self.access.config_digest(conn) != config_digest
                        or fresh.fingerprint != prepared.fingerprint
                        or fresh.authorization_digest != prepared.authorization_digest
                        or mt.load_catalog(conn).fingerprint != catalog.fingerprint
                    ):
                        raise ValueError('模型放行状态或输入改变')
                except ValueError:
                    self._finish(conn, ident, 'stale', error_class='stale')
                    return self._status(conn, ident)
                conn.execute("UPDATE egress_log SET status='dispatching',dispatched_at=? WHERE request_id=?", (db.now_iso(), ident))
            try:
                result = provider.classify({'request': wire, 'catalog': catalog.records})
                if result is None:
                    raise ModelError('invalid_output')
            except ModelError as error:
                status = 'outcome_unknown' if error.unknown else 'failed'
                with db.transaction(conn):
                    self._finish(conn, ident, status, error_class=error.code)
                return self._status(conn, ident)
            except Exception as error:
                logging.getLogger(__name__).warning('模型外发失败 request_id=%s class=%s', ident, type(error).__name__)
                # 传输意外异常只落稳定分类；可能已经发出，不猜测计费结果。
                with db.transaction(conn):
                    self._finish(conn, ident, 'outcome_unknown', error_class='network')
                return self._status(conn, ident)
            snapshot = mt.make_snapshot(prepared, catalog, provider.selected_ids, config_digest)
            try:
                with self.access.lock, db.transaction(conn):
                    try:
                        self._authorized(conn, session, actor_id)
                        if preview.generation != self.access.generation or self.access.config_digest(conn) != config_digest:
                            raise ValueError('放行状态改变')
                        mt.validate_snapshot(conn, snapshot, target=preview.target, selected_ids=provider.selected_ids, max_image_pixels=self.access.max_image_pixels)
                    except ValueError:
                        self._finish(conn, ident, 'stale', error_class='stale', provider=provider)
                        return self._status(conn, ident)
                    version = provider.model_version()
                    model = conn.execute('SELECT model_version_id FROM model_version WHERE capability=? AND model_name=? AND revision=? AND prompt_version=?', (version['capability'], version['model_name'], version['revision'], version['prompt_version'])).fetchone()
                    model_id = model[0] if model else conn.execute('INSERT INTO model_version(capability,model_name,revision,prompt_version) VALUES (?,?,?,?) RETURNING model_version_id', (version['capability'], version['model_name'], version['revision'], version['prompt_version'])).fetchone()[0]
                    applied = tagging.apply_classification(conn, replace(result, model_version_id=model_id), actor_id=actor_id, existing_tag_ids=provider.selected_ids, input_ref=mt.encoded(snapshot).decode(), **{'composition_ids' if 'composition_id' in preview.target else 'content_ids': list(preview.target.values())})
                    self._finish(conn, ident, 'succeeded', provider=provider, run_id=applied['run_ids'][0])
                return self._status(conn, ident) | {'run_id': applied['run_ids'][0]}
            except (sqlite3.Error, db.DatabaseError, ValueError) as error:
                logging.getLogger(__name__).warning('模型外发失败 request_id=%s class=%s', ident, type(error).__name__)
                with db.transaction(conn):
                    self._finish(conn, ident, 'failed', error_class='write_failure', provider=provider)
                return self._status(conn, ident)
        finally:
            _FLIGHT.release()
