"""设置层集成测试：在临时 SQLite 库里验证播种、审计与回滚（DESIGN §6.2.2）。

库由 ``tests/conftest.py`` 的 ``applied_schema`` 夹具提供：模块专属的临时文件
加上已应用的 0001。因此本文件**不需要** ``integration`` marker，也不依赖
Docker / PostgreSQL——这是换到 SQLite 之后最直接的一处收益。
这里守的是三条容易被实现悄悄破坏的契约：

* 播种**只插不改**——重复运行不会覆盖任何已经初始化的实例；
* ``setting`` 与 ``setting_change`` **同生共死**——任何一步失败都整体回滚，
  不会留下「值变了但没记录」或「记录写了但值没变」；
* 危险项必须显式确认才落库（这道闸门在适配器里再拦一次，不靠调用方自觉）。

与原 PostgreSQL 版的差异只在机制：占位符 ``%s`` -> ``?``、去掉 ``::jsonb`` 强转、
JSON 列以 ``TEXT`` 存储（读原始列时需自行 ``json.loads``，见
``test_write_records_before_and_after`` 的注释）。断言语义一条都没有放宽。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pc import config as cfg
from pc.adapters import sqlite as db
from pc.domain import SettingsSource

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
    result = db.seed_settings(applied_schema, seeds)
    assert result["inserted"] == [key for key, *_ in seeds]
    assert result["existing"] == []

    rows = db.read_settings(applied_schema)
    assert set(rows) == {key for key, *_ in seeds}
    assert rows["inbox.enabled"].value is False
    assert rows["inbox.enabled"].value_type == "bool"
    assert rows["inbox.enabled"].source == "yaml"
    # 播种不是「谁做的改动」，因此不写审计表
    assert change_count(applied_schema) == 0


def test_seed_never_overwrites_an_existing_value(applied_schema: Any, seeds: list[Any]) -> None:
    db.write_setting_change(
        applied_schema,
        key="search.default_page_size",
        value=120,
        value_type="int",
        changed_via="ui",
    )
    result = db.seed_settings(applied_schema, seeds)
    assert result["inserted"] == []
    assert "search.default_page_size" in result["existing"]
    assert db.read_settings(applied_schema)["search.default_page_size"].value == 120


def test_seed_rejects_a_source_outside_the_check_constraint(applied_schema: Any) -> None:
    with pytest.raises(db.DatabaseError, match="source"):
        db.seed_settings(applied_schema, [("legacy.setting", 1, "int", "bogus")])
    assert "legacy.setting" not in db.read_settings(applied_schema)


def test_read_settings_keeps_json_types(applied_schema: Any) -> None:
    """``TEXT`` 列里的 JSON 文本由适配器 ``_decode`` 解回 Python 对象。

    与 PostgreSQL 版（psycopg 解 jsonb）行为一致，所以这里的断言写法原样保留。
    """
    rows = db.read_settings(applied_schema)
    assert rows["ai.involvement"].value == "local_only"
    assert rows["retention.max_total_bytes"].value == 214748364800
    assert rows["privacy.local_only_mode"].value is True


def test_write_records_before_and_after(applied_schema: Any) -> None:
    before_revision = db.settings_revision(applied_schema)
    result = db.write_setting_change(
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
    assert db.settings_revision(applied_schema) > before_revision
    assert db.read_settings(applied_schema)["dedup.near_duplicate_distance"].value == 9

    row = applied_schema.execute(
        "SELECT before_state, after_state, changed_via, applied, precheck_result, applied_at "
        "FROM setting_change WHERE change_id = ?",
        (result["change_id"],),
    ).fetchone()
    # SQLite 没有 jsonb：适配器把 JSON 写成 TEXT，直接读原始列拿到的是字符串。
    # 因此这里解码后再比较——语义与 PostgreSQL 版完全一致，仍然是「存的是数字 6、
    # 数字 9、对象」，而不是字符串 "6"。适配器的 _decode() 只服务于 read_settings，
    # 原始 SQL 读 setting_change 时由测试自己解码。
    assert json.loads(row[0]) == 6
    assert json.loads(row[1]) == 9
    assert row[2] == "ui"
    # SQLite 没有布尔类型，applied 按 STRICT 表定义落成 INTEGER 1（CHECK 限定 0/1）
    assert row[3] == 1
    assert json.loads(row[4]) == {"source": "test"}
    assert row[5] is not None


def test_failed_write_rolls_back_both_tables(applied_schema: Any) -> None:
    """审计写入失败时，``setting`` 的新值也不能留下——反之同样不行。"""
    before_value = db.read_settings(applied_schema)["dedup.near_duplicate_distance"].value
    before_changes = change_count(applied_schema)

    with pytest.raises(db.DatabaseError, match="回滚"):
        # actor 指向不存在的 app_user：setting_change 的外键会失败
        db.write_setting_change(
            applied_schema,
            key="dedup.near_duplicate_distance",
            value=11,
            value_type="int",
            changed_via="ui",
            actor=999_999,
        )

    assert db.read_settings(applied_schema)["dedup.near_duplicate_distance"].value == before_value
    assert change_count(applied_schema) == before_changes


def test_dangerous_write_requires_explicit_confirmation(applied_schema: Any) -> None:
    with pytest.raises(db.DatabaseError, match="危险项"):
        db.write_setting_change(
            applied_schema, key="inbox.enabled", value=True, value_type="bool", dangerous=True
        )

    result = db.write_setting_change(
        applied_schema,
        key="inbox.enabled",
        value=True,
        value_type="bool",
        dangerous=True,
        confirmed=True,
        precheck={"reviewed_by": "test"},
    )
    assert result["applied"] is True
    assert db.read_settings(applied_schema)["inbox.enabled"].value is True


def test_rejects_unknown_changed_via_and_source(applied_schema: Any) -> None:
    """``setting_change.changed_via`` 的取值由 CHECK 约束固定，代码要先一步挡住。

    ``cli`` 目前**不是**合法取值：命令行还没有写入路径，走 CLI 改设置属于
    「先把审计字段编出来」的那类偷懒，宁可在这里撞墙。
    """
    with pytest.raises(db.DatabaseError, match="changed_via"):
        db.write_setting_change(
            applied_schema, key="inbox.enabled", value=False, value_type="bool", changed_via="cli"
        )
    with pytest.raises(db.DatabaseError, match="source"):
        db.write_setting_change(
            applied_schema, key="inbox.enabled", value=False, value_type="bool", source="bogus"
        )


def test_snapshot_reads_the_database_layer(
    applied_schema: Any, specs: dict[str, cfg.SettingSpec]
) -> None:
    """配置层与适配器拼在一起时，「这个值从数据库来」必须成立。"""
    rows = db.read_settings(applied_schema)
    snap = cfg.snapshot(
        registry=specs,
        db_values=rows,
        env={},
        revision=db.settings_revision(applied_schema),
    )
    for key, row in rows.items():
        assert snap.get(key) == row.value
        assert snap.source_of(key) is SettingsSource.DB
    assert snap.explain("search.default_page_size").startswith("数据库 setting 表")
    assert snap.revision == db.settings_revision(applied_schema)
