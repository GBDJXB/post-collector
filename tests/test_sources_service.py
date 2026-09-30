"""来源登记服务：三者齐备、目录持久化、默认存储后端。

这一层最要紧的两条不变式：
* ``source`` 与 ``source_authorization`` **同生共死**——不能出现"有来源、没授权"的
  中间状态，那正是连接器会拒绝、而人又最容易忘记补上的状态；
* 本次用到的目录必须**写进 source.config**——worker 在另一个时刻取字节，它只读那里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pc.adapters.registry import source_config
from pc.services import sources as svc


class TestCreateSource:
    def test_creates_source_and_authorization_together(self, fresh_db: Any) -> None:
        info = svc.create_source(
            fresh_db, name="mine", platform="local", kind="export", trust_level=1
        )
        assert info["source_id"] > 0
        rows = svc.load_authorizations(fresh_db, info["source_id"])
        assert len(rows) == 1, "建来源必须同时写下授权记录"
        assert rows[0]["mode"] == "manual_export"
        assert svc.authorization_state(rows)["ok"] is True

    def test_duplicate_name_is_a_human_error(self, fresh_db: Any) -> None:
        svc.create_source(fresh_db, name="mine", platform="local", kind="export")
        with pytest.raises(svc.SourceError, match="已经有一个叫"):
            svc.create_source(fresh_db, name="mine", platform="local", kind="export")

    def test_bad_kind_is_rejected_before_touching_the_database(self, fresh_db: Any) -> None:
        with pytest.raises(svc.SourceError, match="kind="):
            svc.create_source(fresh_db, name="x", platform="local", kind="telepathy")
        assert fresh_db.execute("SELECT count(*) FROM source").fetchone()[0] == 0

    def test_bad_trust_level_is_rejected(self, fresh_db: Any) -> None:
        with pytest.raises(svc.SourceError, match="trust_level"):
            svc.create_source(fresh_db, name="x", platform="local", kind="export", trust_level=9)

    def test_bad_authorization_mode_is_rejected(self, fresh_db: Any) -> None:
        with pytest.raises(svc.SourceError, match="mode="):
            svc.create_source(
                fresh_db, name="x", platform="local", kind="export", authorization_mode="vibes"
            )

    def test_config_is_stored_as_json(self, fresh_db: Any) -> None:
        info = svc.create_source(
            fresh_db,
            name="mine",
            platform="local",
            kind="export",
            config={"roots": ["/tmp/dump"]},
        )
        row = svc.get_source(fresh_db, info["source_id"])
        assert row is not None
        assert source_config(row)["roots"] == ["/tmp/dump"]

    def test_evidence_and_expiry_are_recorded(self, fresh_db: Any) -> None:
        info = svc.create_source(
            fresh_db,
            name="mine",
            platform="local",
            kind="export",
            evidence_ref="receipt-2024.pdf",
            expires_at="2099-01-01T00:00:00.000Z",
        )
        rows = svc.load_authorizations(fresh_db, info["source_id"])
        assert rows[0]["evidence_ref"] == "receipt-2024.pdf"
        assert svc.authorization_state(rows)["expires_at"] == "2099-01-01T00:00:00.000Z"


class TestLookup:
    def test_get_by_name_and_by_id(self, fresh_db: Any) -> None:
        info = svc.create_source(fresh_db, name="mine", platform="local", kind="export")
        by_name = svc.get_source(fresh_db, "mine")
        by_id = svc.get_source(fresh_db, info["source_id"])
        assert by_name is not None and by_id is not None
        assert by_name["source_id"] == by_id["source_id"]

    def test_missing_source_is_a_human_error(self, fresh_db: Any) -> None:
        with pytest.raises(svc.SourceError, match="找不到来源"):
            svc.require_source(fresh_db, "nope")

    def test_hint_lists_existing_names(self, fresh_db: Any) -> None:
        svc.create_source(fresh_db, name="alpha", platform="local", kind="export")
        with pytest.raises(svc.SourceError, match="alpha"):
            svc.require_source(fresh_db, "beta")

    def test_empty_hint_points_at_the_add_command(self, fresh_db: Any) -> None:
        with pytest.raises(svc.SourceError, match="pc source add"):
            svc.require_source(fresh_db, "beta")

    def test_list_sources_includes_authorization_state(self, fresh_db: Any) -> None:
        svc.create_source(fresh_db, name="mine", platform="local", kind="export")
        rows = svc.list_sources(fresh_db)
        assert len(rows) == 1
        assert rows[0]["authorization"]["ok"] is True


class TestAuthorizationState:
    def test_revoked_is_not_ok(self) -> None:
        state = svc.authorization_state([{"mode": "manual_export", "revoked_at": "2026-01-01T00:00:00.000Z"}])
        assert state["ok"] is False
        assert "撤销" in state["reason"]

    def test_expired_is_not_ok(self) -> None:
        state = svc.authorization_state(
            [{"mode": "manual_export", "expires_at": "2000-01-01T00:00:00.000Z"}]
        )
        assert state["ok"] is False
        assert "过期" in state["reason"]

    def test_empty_list_says_there_is_no_record(self) -> None:
        state = svc.authorization_state([])
        assert state["ok"] is False
        assert state["reason"] == "没有授权记录"

    def test_soonest_expiry_wins(self) -> None:
        state = svc.authorization_state(
            [
                {"mode": "a", "expires_at": "2099-01-01T00:00:00.000Z"},
                {"mode": "b", "expires_at": "2030-01-01T00:00:00.000Z"},
            ]
        )
        assert state["expires_at"] == "2030-01-01T00:00:00.000Z"


class TestMergeRoots:
    def test_roots_are_appended_and_deduplicated(self, fresh_db: Any) -> None:
        info = svc.create_source(
            fresh_db, name="mine", platform="local", kind="export", config={"roots": ["/a"]}
        )
        source = svc.require_source(fresh_db, info["source_id"])
        merged = svc.merge_roots(fresh_db, source, ["/b", "/a"])
        assert merged == ["/a", "/b"]

    def test_merge_is_persisted(self, fresh_db: Any) -> None:
        info = svc.create_source(fresh_db, name="mine", platform="local", kind="export")
        source = svc.require_source(fresh_db, info["source_id"])
        svc.merge_roots(fresh_db, source, ["/b"])
        reread = svc.require_source(fresh_db, info["source_id"])
        assert source_config(reread)["roots"] == ["/b"]

    def test_other_config_keys_survive(self, fresh_db: Any) -> None:
        info = svc.create_source(
            fresh_db, name="mine", platform="local", kind="export", config={"note": "keep me"}
        )
        source = svc.require_source(fresh_db, info["source_id"])
        svc.merge_roots(fresh_db, source, ["/b"])
        assert source_config(svc.require_source(fresh_db, info["source_id"]))["note"] == "keep me"


class TestDefaultBackend:
    def test_creates_the_row_once(self, fresh_db: Any, tmp_path: Path) -> None:
        first = svc.ensure_default_backend(fresh_db, tmp_path / "media")
        assert first["created"] is True
        second = svc.ensure_default_backend(fresh_db, tmp_path / "media")
        assert second["created"] is False
        assert second["backend_id"] == first["backend_id"]
        assert second["root_changed"] is False

    def test_root_change_is_reported_not_hidden(self, fresh_db: Any, tmp_path: Path) -> None:
        svc.ensure_default_backend(fresh_db, tmp_path / "media")
        moved = svc.ensure_default_backend(fresh_db, tmp_path / "media2")
        assert moved["root_changed"] is True, (
            "媒体根目录被改过必须报出来：对象键不变、落盘位置变了，老内容会找不到"
        )
        row = fresh_db.execute(
            "SELECT config FROM storage_backend WHERE name = 'local'"
        ).fetchone()
        assert "media2" in str(row[0])

    def test_quarantine_backend_is_a_separate_row(self, fresh_db: Any, tmp_path: Path) -> None:
        media = svc.ensure_default_backend(fresh_db, tmp_path / "media", name="local")
        quarantine = svc.ensure_default_backend(
            fresh_db, tmp_path / "quarantine", name="local-quarantine"
        )
        assert media["backend_id"] != quarantine["backend_id"]


# ---------------------------------------------------------------------------
# root 手工上传的来源
#
# "root 本人就是来源"这件事要在数据里说得清：kind=manual + mode=root_self，而且**不要
# 占位凭证**。授权依然是一条真实记录，只是它的含义是"操作者本人在场"。
# ---------------------------------------------------------------------------


class TestManualKind:
    def test_manual_kind_defaults_to_root_self(self, fresh_db: Any) -> None:
        info = svc.create_source(fresh_db, name="manual", platform="local", kind="manual")
        assert info["authorization_mode"] == "root_self"
        rows = svc.load_authorizations(fresh_db, info["source_id"])
        assert [row["mode"] for row in rows] == ["root_self"]

    def test_export_kind_still_defaults_to_manual_export(self, fresh_db: Any) -> None:
        info = svc.create_source(fresh_db, name="dump", platform="local", kind="export")
        assert info["authorization_mode"] == "manual_export", "既有行为不能因为新增 kind 而改变"

    def test_default_mode_table(self) -> None:
        assert svc.default_authorization_mode("manual") == "root_self"
        assert svc.default_authorization_mode("export") == "manual_export"
        assert svc.default_authorization_mode("bot_api") == svc.DEFAULT_AUTHORIZATION_MODE, (
            "认不出的 kind 要有兜底值，而不是抛错——真正的校验在 _require_kind 那里"
        )

    def test_manual_kind_is_accepted_by_the_service(self) -> None:
        assert "manual" in svc.SOURCE_KINDS
        assert "root_self" in svc.AUTHORIZATION_MODES


class TestEnsureManualSource:
    def test_creates_then_reuses(self, fresh_db: Any) -> None:
        first = svc.ensure_manual_source(fresh_db)
        assert first["created"] is True
        assert first["source"]["kind"] == "manual"
        assert first["source"]["trust_level"] == 1
        assert first["authorization"]["ok"] is True
        assert first["authorization"]["modes"] == ("root_self",)

        second = svc.ensure_manual_source(fresh_db)
        assert second["created"] is False
        assert second["source"]["source_id"] == first["source"]["source_id"]
        assert fresh_db.execute("SELECT count(*) FROM source").fetchone()[0] == 1
        assert fresh_db.execute("SELECT count(*) FROM source_authorization").fetchone()[0] == 1

    def test_no_credential_fields_are_needed(self, fresh_db: Any) -> None:
        """手工上传不需要任何凭证字段——这正是"凭证可选"的落点。"""
        result = svc.ensure_manual_source(fresh_db)
        evidence = fresh_db.execute(
            "SELECT evidence_ref FROM source_authorization WHERE source_id = ?",
            (result["source"]["source_id"],),
        ).fetchone()[0]
        assert "无外部凭证" in str(evidence)
        assert svc.authorization_state(
            svc.load_authorizations(fresh_db, result["source"]["source_id"])
        )["ok"] is True

    def test_name_clash_with_another_kind_is_refused(self, fresh_db: Any) -> None:
        """同名来源的 kind 不对时必须报错，不能默默拿来用。

        默默用它的后果很具体：身份策略会从"按内容哈希"变成"按相对路径"，重传同一张图
        就变成两条——而这正是手工上传最不能接受的行为。
        """
        svc.create_source(fresh_db, name=svc.MANUAL_SOURCE_NAME, platform="local", kind="export")
        with pytest.raises(svc.SourceError, match="kind 是 export"):
            svc.ensure_manual_source(fresh_db)

    def test_revoked_authorization_is_not_resurrected(self, fresh_db: Any) -> None:
        """撤销是有意为之：再跑一次 upload 不该把授权悄悄恢复。"""
        first = svc.ensure_manual_source(fresh_db)
        fresh_db.execute(
            "UPDATE source_authorization SET revoked_at = ? WHERE source_id = ?",
            ("2026-01-01T00:00:00.000Z", first["source"]["source_id"]),
        )
        again = svc.ensure_manual_source(fresh_db)
        assert again["created"] is False
        assert again["authorization"]["ok"] is False, "撤销必须继续生效，authorize() 才有意义"
