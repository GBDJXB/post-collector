"""本机模型放行与进程内凭据；状态响应不含密钥。"""

from __future__ import annotations

import hashlib
import json
import threading
import time

from pc import config
from pc.adapters import sqlite as db
from pc.services import tagging

POLICY_VERSION = "tagging-policy-v1"


def _allowed(settings):
    return settings.get("privacy.local_only_mode") is False and settings.get("ai.involvement") in {
        "assist",
        "advisory_auto",
    }


class ModelAccess:
    def __init__(self, *, registry=None, yaml_defaults=None, max_image_pixels=100_000_000, clock=time.monotonic):
        self.registry = (
            registry if registry is not None else config.registry_index(config.load_registry())
        )
        self.yaml_defaults = dict(config.load_app_defaults(registry=self.registry) if yaml_defaults is None else yaml_defaults)
        self.max_image_pixels = max_image_pixels
        self.clock = clock
        self.lock = threading.RLock()
        self.generation = 0
        self.previews = {}
        self._key = None
        self._owner = None
        self._armed = False

    def settings(self, conn, *, proposed=None):
        values = db.read_settings(conn)
        values.update(proposed or {})
        return config.snapshot(
            registry=self.registry,
            yaml_defaults=self.yaml_defaults,
            db_values=values,
            revision=db.settings_revision(conn),
        )

    def config_digest(self, conn):
        settings = self.settings(conn)
        value = {
            "policy": POLICY_VERSION,
            "max_image_pixels": self.max_image_pixels,
            "privacy": settings.get("privacy.local_only_mode"),
            "involvement": settings.get("ai.involvement"),
            "revision": settings.revision,
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _clear(self):
        self._key = None
        self._owner = None
        self._armed = False
        self.generation += 1
        self.previews.clear()

    def close(self) -> None:
        """服务关闭时撤销所有进程内凭据和预览，不改写持久设置。"""
        with self.lock:
            self._clear()

    def set_key(self, session, key):
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 4096
            or any(not 33 <= ord(c) <= 126 for c in key)
        ):
            raise ValueError("密钥须为非空 ASCII 字符，不含空白或控制字符，最多4096字符")
        with self.lock:
            self._clear()
            self._key, self._owner = key, session

    def delete_key(self, session):
        with self.lock:
            if self._owner == session:
                self._clear()

    def invalidate_session(self, session):
        self.delete_key(session)

    def require(self, conn, session):
        with self.lock:
            if (
                self._owner != session
                or not self._key
                or not self._armed
                or not _allowed(self.settings(conn))
            ):
                raise ValueError("模型外发未放行，请设置密钥并明确开启")
            return self._key

    def status(self, conn, session):
        with self.lock:
            present = self._owner == session and self._key is not None
            return {
                "key_present": present,
                "enabled": present and self._armed and _allowed(self.settings(conn)),
                "policy_version": POLICY_VERSION,
            }

    def set_access(self, conn, session, enabled, *, confirmed=False, actor_id=None):
        if type(enabled) is not bool or confirmed is not True:
            raise ValueError("须明确确认修改模型外发总闸")
        with self.lock:
            if enabled and (self._owner != session or self._key is None):
                raise ValueError("请先设置当前会话的模型密钥")
            if not enabled:
                self._clear()
            changes = {"privacy.local_only_mode": not enabled}
            if enabled:
                changes["ai.involvement"] = "assist"
            with db.transaction(conn):
                tagging._root(conn, actor_id)
                proposed = self.settings(conn, proposed=changes)
                if enabled and not _allowed(proposed):
                    raise ValueError("环境配置仍禁止模型外发，设置未写入")
                for key, value in changes.items():
                    spec = self.registry[key]
                    precheck = {
                        "purpose": "tagging",
                        "policy_version": POLICY_VERSION,
                        "explicit_web_confirmation": True,
                    }
                    plan = config.plan_setting_change(
                        spec,
                        value,
                        registry=self.registry,
                        values=proposed.values,
                        confirmed=confirmed,
                        precheck=precheck,
                    )
                    if not plan.ok:
                        raise ValueError(plan.reason)
                    db.write_setting_change(
                        conn,
                        key=key,
                        value=plan.value,
                        value_type=spec.type,
                        actor=actor_id,
                        precheck=precheck,
                        dangerous=spec.dangerous,
                        confirmed=confirmed,
                    )
            self._armed = enabled
            self.generation += 1
            self.previews.clear()
            return self.status(conn, session)
