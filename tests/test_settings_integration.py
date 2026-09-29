"""设置层集成测试：在临时库里验证播种、审计与回滚（DESIGN §6.2.2）。

需要真实 PostgreSQL，未提供连接串时整文件跳过（见 ``tests/conftest.py``）。
这里守的是三条容易被实现悄悄破坏的契约：

* 播种**只插不改**——重复运行不会覆盖任何已经初始化的实例；
* ``setting`` 与 ``setting_change`` **同生共死**——任何一步失败都整体回滚，
  不会留下「值变了但没记录」或「记录写了但值没变」；
* 危险项必须显式确认才落库（这道闸门在适配器里再拦一次，不靠调用方自觉）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pc import config as cfg
from pc.adapters import postgres as pg
from pc.domain import SettingsSource

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def specs() -> dict[str, cfg.SettingSpec]:
    return cfg.registry_index(cfg.load_registry(REPO_ROOT / cfg.DEFAULT_REGISTRY_PATH))


@pytest.fixture(scope="module")
def seeds(specs: dict[str, cfg.SettingSpec]) -> list[tuple[str, Any, str, str]]:
    defaults = cfg.load_app_defaults(REPO_ROOT / cfg.DEFAULT_APP_CONFIG_PATH, registry=specs)
    return cfg.setting_seeds(specs, defaults)


def change_count(conn: Any) -> int:
    row = conn.execute("SELECT count(*) FROM setting_change").fetchone()
    return int(row[0])


def test_first_seed_inserts_every_setting(applied_schema: Any, seeds: list[Any]) -> None:
    result = pg.seed_settings(applied_schema, seeds)
    assert result["inserted"] == [key for key, *_ in seeds]
    assert result["existing"] == []

    rows = pg.read_settings(applied_schema)
    assert set(rows) == {key for key, *_ in seeds}
    assert rows["inbox.enabled"].value is False
    assert rows["inbox.enabled"].value_type == "bool"
    assert rows["inbox.enabled"].source == "yaml"
    # 播种不是「谁做的改动」，因此不写审计表
    assert change_count(applied_schema) == 0


def test_seed_never_overwrites_an_existing_value(applied_schema: Any, seeds: list[Any]) -> None:
    pg.write_setting_change(
        applied_schema,
        key="search.default_page_size",
        value=120,
        value_type="int",
        changed_via="ui",
    )
    result = pg.seed_settings(applied_schema, seeds)
    assert result["inserted"] == []
    assert "search.default_page_size" in result["existing"]
    assert pg.read_settings(applied_schema)["search.default_page_size"].value == 120


def test_seed_rejects_a_source_outside_the_check_constraint(applied_schema: Any) -> None:
    with pytest.raises(pg.DatabaseError, match="source"):
        pg.seed_settings(applied_schema, [("legacy.setting", 1, "int", "bogus")])
    assert "legacy.setting" not in pg.read_settings(applied_schema)


def test_read_settings_keeps_json_types(applied_schema: Any) -> None:
    rows = pg.read_settings(applied_schema)
    assert rows["ai.involvement"].value == "local_only"
    assert rows["retention.max_total_bytes"].value == 214748364800
    assert rows["privacy.local_only_mode"].value is True


def test_write_records_before_and_after(applied_schema: Any) -> None:
    before_revision = pg.settings_revision(applied_schema)
    result = pg.write_setting_change(
        applied_schema,
        key="dedup.near_duplicate_distance",
        value=9,
        value_type="int",
        changed_via="ui",
        precheck={"source": "test"},
    )
    assert result["applied"] is True
    assert result["before"] == 6
    assert result["after"] == 9
    assert pg.settings_revision(applied_schema) > before_revision
    assert pg.read_settings(applied_schema)["dedup.near_duplicate_distance"].value == 9

    row = applied_schema.execute(
        "SELECT before_state, after_state, changed_via, applied, precheck_result, applied_at "
        "FROM setting_change WHERE change_id = %s",
        (result["change_id"],),
    ).fetchone()
    assert row[0] == 6
    assert row[1] == 9
    assert row[2] == "ui"
    assert row[3] is True
    assert row[4] == {"source": "test"}
    assert row[5] is not None


def test_failed_write_rolls_back_both_tables(applied_schema: Any) -> None:
    """审计写入失败时，``setting`` 的新值也不能留下——反之同样不行。"""
    before_value = pg.read_settings(applied_schema)["dedup.near_duplicate_distance"].value
    before_changes = change_count(applied_schema)

    with pytest.raises(pg.DatabaseError, match="回滚"):
        # actor 指向不存在的 app_user：setting_change 的外键会失败
        pg.write_setting_change(
            applied_schema,
            key="dedup.near_duplicate_distance",
            value=11,
            value_type="int",
            changed_via="ui",
            actor=999_999,
        )

    assert pg.read_settings(applied_schema)["dedup.near_duplicate_distance"].value == before_value
    assert change_count(applied_schema) == before_changes


def test_dangerous_write_requires_explicit_confirmation(applied_schema: Any) -> None:
    with pytest.raises(pg.DatabaseError, match="危险项"):
        pg.write_setting_change(
            applied_schema, key="inbox.enabled", value=True, value_type="bool", dangerous=True
        )

    result = pg.write_setting_change(
        applied_schema,
        key="inbox.enabled",
        value=True,
        value_type="bool",
        dangerous=True,
        confirmed=True,
        precheck={"reviewed_by": "test"},
    )
    assert result["applied"] is True
    assert pg.read_settings(applied_schema)["inbox.enabled"].value is True


def test_rejects_unknown_changed_via_and_source(applied_schema: Any) -> None:
    """``setting_change.changed_via`` 的取值由 CHECK 约束固定，代码要先一步挡住。

    ``cli`` 目前**不是**合法取值：命令行还没有写入路径，走 CLI 改设置属于
    「先把审计字段编出来」的那类偷懒，宁可在这里撞墙。
    """
    with pytest.raises(pg.DatabaseError, match="changed_via"):
        pg.write_setting_change(
            applied_schema, key="inbox.enabled", value=False, value_type="bool", changed_via="cli"
        )
    with pytest.raises(pg.DatabaseError, match="source"):
        pg.write_setting_change(
            applied_schema, key="inbox.enabled", value=False, value_type="bool", source="bogus"
        )


def test_snapshot_reads_the_database_layer(
    applied_schema: Any, specs: dict[str, cfg.SettingSpec]
) -> None:
    """配置层与适配器拼在一起时，「这个值从数据库来」必须成立。"""
    rows = pg.read_settings(applied_schema)
    snap = cfg.snapshot(
        registry=specs,
        db_values=rows,
        env={},
        revision=pg.settings_revision(applied_schema),
    )
    for key, row in rows.items():
        assert snap.get(key) == row.value
        assert snap.source_of(key) is SettingsSource.DB
    assert snap.explain("search.default_page_size").startswith("数据库 setting 表")
    assert snap.revision == pg.settings_revision(applied_schema)
