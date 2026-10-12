"""内存凭据与审计总闸；测试不联网。"""

from __future__ import annotations

import json

import pytest

from pc.adapters import sqlite as db


@pytest.fixture
def access():
    from pc.services.model_access import ModelAccess

    return ModelAccess()


def test_restart_and_key_replacement_need_explicit_rearm(fresh_db, access):
    access.set_key("owner", "TEST-KEY-ONE")
    assert access.status(fresh_db, "owner")["enabled"] is False
    with pytest.raises(ValueError):
        access.set_access(fresh_db, "owner", True, confirmed=False)
    assert db.read_settings(fresh_db) == {}
    access.set_access(fresh_db, "owner", True, confirmed=True)
    assert access.require(fresh_db, "owner") == "TEST-KEY-ONE"
    audit = [dict(r) for r in fresh_db.execute("SELECT * FROM setting_change")]
    assert len(audit) == 2 and "TEST-KEY-ONE" not in json.dumps(audit)
    access.set_key("owner", "TEST-KEY-TWO")
    assert access.status(fresh_db, "owner")["enabled"] is False
    from pc.services.model_access import ModelAccess

    assert ModelAccess().status(fresh_db, "owner")["enabled"] is False


def test_close_clears_credentials_arm_owner_and_previews(fresh_db, access):
    access.set_key("owner", "TEST-ONLY-CLOSE-KEY")
    access.set_access(fresh_db, "owner", True, confirmed=True)
    access.previews["old-preview"] = object()
    generation = access.generation

    access.close()

    assert access._key is None and access._owner is None
    assert access._armed is False and access.previews == {}
    assert access.generation > generation
    for session in ("owner", "other"):
        status = access.status(fresh_db, session)
        assert status["key_present"] is False and status["enabled"] is False
        with pytest.raises(ValueError, match="未放行"):
            access.require(fresh_db, session)
    # 关闭进程内凭据不改写持久策略，也不能借其恢复放行。
    assert db.read_settings(fresh_db)["privacy.local_only_mode"].value is False
    access.close()
    assert access._key is None and access.previews == {}


def test_environment_block_has_no_partial_write(fresh_db, access, monkeypatch):
    access.set_key("owner", "TEST-KEY")
    monkeypatch.setenv("PC__PRIVACY__LOCAL_ONLY_MODE", "true")
    with pytest.raises(ValueError, match="环境"):
        access.set_access(fresh_db, "owner", True, confirmed=True)
    assert db.read_settings(fresh_db) == {}
    assert fresh_db.execute("SELECT count(*) FROM setting_change").fetchone()[0] == 0


def test_gate_second_write_failure_rolls_back_all(fresh_db, access):
    access.set_key("owner", "TEST-KEY")
    fresh_db.execute(
        "CREATE TRIGGER fail_gate BEFORE INSERT ON setting WHEN NEW.key='ai.involvement' BEGIN SELECT RAISE(ABORT,'test'); END"
    )
    with pytest.raises(db.DatabaseError):
        access.set_access(fresh_db, "owner", True, confirmed=True)
    assert db.read_settings(fresh_db) == {}
    assert fresh_db.execute("SELECT count(*) FROM setting_change").fetchone()[0] == 0
    assert access.status(fresh_db, "owner")["enabled"] is False


def test_owner_logout_disable_and_no_key_in_status(fresh_db, access):
    access.set_key("owner", "TEST-KEY")
    access.set_access(fresh_db, "owner", True, confirmed=True)
    with pytest.raises(ValueError):
        access.require(fresh_db, "other-session")
    assert "TEST-KEY" not in json.dumps(access.status(fresh_db, "owner"))
    access.invalidate_session("owner")
    assert access.status(fresh_db, "owner")["key_present"] is False
    access.set_key("owner", "TEST-KEY")
    access.set_access(fresh_db, "owner", True, confirmed=True)
    access.set_access(fresh_db, "owner", False, confirmed=True)
    assert db.read_settings(fresh_db)["privacy.local_only_mode"].value is True
    assert access.status(fresh_db, "owner")["key_present"] is False


def test_default_yaml_policy_is_not_silently_dropped(fresh_db, monkeypatch):
    from pc import config
    from pc.services.model_access import ModelAccess

    monkeypatch.setattr(config, "load_app_defaults", lambda **_: {"ai.involvement": "off"})
    assert ModelAccess().settings(fresh_db).get("ai.involvement") == "off"


def test_runtime_pixel_policy_binds_preview_and_adoption_digest(fresh_db):
    from pc.services.model_access import ModelAccess

    assert ModelAccess(max_image_pixels=25).config_digest(fresh_db) != ModelAccess(max_image_pixels=26).config_digest(fresh_db)


@pytest.mark.parametrize("key", ["", "   ", "x\ny", "x\x00y", "x" * 4097])
def test_key_limits_without_echo(access, key):
    with pytest.raises(ValueError) as caught:
        access.set_key("owner", key)
    assert key not in str(caught.value) if key else True
