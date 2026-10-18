"""配置层单元测试：不碰数据库，任何时候都必须通过。

覆盖 DESIGN §6.2 的四件事：

* 注册表加载与校验（缺字段、未知字段、键名重复、默认值越界都要在加载时报错）；
* 取值校验（类型、range、enum、requires 依赖链，提示必须是人话）；
* 分层解析的来源可解释性（env > db > yaml > default，且能回答「为什么是这个值」）；
* 不可变快照与「不对劲清单」（拼错的 ``PC__`` 环境变量、注册表外的孤儿键）。

刻意不连数据库：设置项语义是纯函数，能在没有数据库、只有标准库的环境里被验证。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pc import config as cfg
from pc.domain import SettingsSource

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / cfg.DEFAULT_REGISTRY_PATH
APP_CONFIG = REPO_ROOT / cfg.DEFAULT_APP_CONFIG_PATH


@pytest.fixture(scope="module")
def specs() -> dict[str, cfg.SettingSpec]:
    return cfg.registry_index(cfg.load_registry(REGISTRY))


@pytest.fixture(scope="module")
def app_defaults(specs: dict[str, cfg.SettingSpec]) -> dict[str, Any]:
    return cfg.load_app_defaults(APP_CONFIG, registry=specs)


def write_registry(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def spec_of(**overrides: Any) -> cfg.SettingSpec:
    """造一个最小合法声明，便于单点测试。"""
    raw: dict[str, Any] = {
        "key": "demo.flag",
        "type": "bool",
        "default": False,
        "label": "演示开关",
        "help": "这是一句大白话说明。",
        "impact": "打开 → 有副作用；关闭 → 没有副作用",
        "dangerous": False,
        "cost": "none",
        "restart_required": False,
        "recompute": None,
        "requires": [],
    }
    raw.update(overrides)
    return cfg.SettingSpec.from_mapping(raw)


class TestShippedRegistry:
    """随仓库分发的注册表本身必须永远可加载。"""

    def test_loads_and_declares_expected_keys(self, specs: dict[str, cfg.SettingSpec]) -> None:
        assert set(specs) == {
            "ai.involvement",
            "dedup.near_duplicate_distance",
            "inbox.enabled",
            "privacy.local_only_mode",
            "retention.max_total_bytes",
            "search.default_page_size",
        }

    def test_enum_keeps_the_off_value_as_a_string(self, specs: dict[str, cfg.SettingSpec]) -> None:
        """YAML 1.1 会把裸写的 off 解析成布尔假；取值必须加引号才安全。"""
        involvement = specs["ai.involvement"]
        assert involvement.values == ("off", "local_only", "assist", "advisory_auto")
        assert involvement.value_labels["off"].startswith("完全不参与")

    def test_range_with_open_upper_bound(self, specs: dict[str, cfg.SettingSpec]) -> None:
        assert specs["retention.max_total_bytes"].range == (1073741824, None)

    def test_every_key_is_covered_by_a_human_label(self, specs: dict[str, cfg.SettingSpec]) -> None:
        for spec in specs.values():
            assert spec.label and spec.help and spec.impact


class TestLoadRegistryErrors:
    def test_missing_file_says_where_to_run(self, tmp_path: Path) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match="仓库根"):
            cfg.load_registry(tmp_path / "nope.yaml")

    def test_rejects_wrong_schema_version(self, tmp_path: Path) -> None:
        path = write_registry(tmp_path, "schema_version: 99\nsettings: []\n")
        with pytest.raises(cfg.SettingsRegistryError, match="schema_version"):
            cfg.load_registry(path)

    def test_rejects_empty_settings(self, tmp_path: Path) -> None:
        path = write_registry(tmp_path, "schema_version: 1\nsettings: []\n")
        with pytest.raises(cfg.SettingsRegistryError, match="settings"):
            cfg.load_registry(path)

    def test_names_the_missing_field(self, tmp_path: Path) -> None:
        path = write_registry(
            tmp_path,
            "schema_version: 1\nsettings:\n"
            "  - key: demo.flag\n    type: bool\n    default: false\n",
        )
        with pytest.raises(cfg.SettingsRegistryError) as excinfo:
            cfg.load_registry(path)
        assert "label" in str(excinfo.value)
        assert "impact" in str(excinfo.value)

    def test_suggests_the_intended_field_on_typo(self) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match="是不是想写 dangerous"):
            spec_of(dangerouss=True)

    def test_rejects_bad_key_shape(self) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match="命名空间"):
            spec_of(key="JustAKey")

    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match="type"):
            spec_of(type="list")

    def test_rejects_unquoted_enum_values_with_a_hint(self) -> None:
        """values 里出现布尔值几乎总是 YAML 把裸写的 off/on 解析掉了。"""
        with pytest.raises(cfg.SettingsRegistryError, match="加引号"):
            spec_of(type="enum", default="off", values=["off", False])

    def test_rejects_enum_without_values(self) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match="values"):
            spec_of(type="enum", default="off")

    def test_rejects_labels_for_undeclared_values(self) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match="未声明"):
            spec_of(
                type="enum",
                default="a",
                values=["a", "b"],
                value_labels={"a": "甲", "c": "丙"},
            )

    def test_rejects_default_out_of_range_at_load_time(self) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match="默认值不合法"):
            spec_of(type="int", default=999, range=[0, 20])

    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match="下限必须小于上限"):
            spec_of(type="int", default=5, range=[20, 0])

    @pytest.mark.parametrize("type_name, default", [("str", "demo"), ("enum", "off"), ("bool", False)])
    def test_rejects_range_on_non_numeric_type(self, type_name: str, default: Any) -> None:
        options = {"values": ["off", "on"]} if type_name == "enum" else {}
        with pytest.raises(cfg.SettingsRegistryError, match=r"range.*type"):
            spec_of(type=type_name, default=default, range=[0, 10], **options)

    def test_direct_spec_with_invalid_range_returns_reason(self) -> None:
        spec = replace(spec_of(type="str", default="demo"), range=(0, 10))
        result = cfg.validate_value(spec, "demo")
        assert result["ok"] is False
        assert "range" in result["reason"]

    def test_rejects_duplicate_keys(self, tmp_path: Path) -> None:
        body = "schema_version: 1\nsettings:\n"
        for _ in range(2):
            body += (
                "  - key: demo.flag\n    type: bool\n    default: false\n"
                "    label: 演示开关\n    help: 说明\n    impact: 影响\n"
                "    dangerous: false\n    cost: none\n    restart_required: false\n"
                "    recompute: null\n    requires: []\n"
            )
        with pytest.raises(cfg.SettingsRegistryError, match="重复"):
            cfg.load_registry(write_registry(tmp_path, body))


class TestCoerceAndValidate:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("开", True), ("0", False), (False, False), ("off", False)],
    )
    def test_bool_accepts_human_spellings(self, raw: Any, expected: bool) -> None:
        assert cfg.coerce_value(spec_of(), raw) is expected

    def test_int_accepts_numeric_strings(self) -> None:
        spec = spec_of(key="search.default_page_size", type="int", default=40, range=[10, 200])
        assert cfg.coerce_value(spec, "80") == 80

    def test_bytes_accepts_unit_suffix(self) -> None:
        spec = spec_of(
            key="retention.max_total_bytes", type="bytes", default=214748364800, range=[1, None]
        )
        assert cfg.coerce_value(spec, "200GB") == 200 * 1000**3
        assert cfg.coerce_value(spec, "2 GiB") == 2 * 1024**3

    def test_byte_display_preserves_decimal_and_binary_meanings(self) -> None:
        spec = spec_of(key="retention.max_total_bytes", type="bytes", default=214748364800)
        assert cfg.humanize_value(spec, cfg.coerce_value(spec, "200GB")) == "200 GB"
        assert cfg.humanize_value(spec, cfg.coerce_value(spec, "200GiB")) == "200 GiB"
        assert cfg.humanize_value(spec, cfg.coerce_value(spec, "1KB")) == "1 KB"
        assert cfg.humanize_value(spec, cfg.coerce_value(spec, "1KiB")) == "1 KiB"
        assert cfg.humanize_value(spec, 0) == "0 字节"
        assert cfg.humanize_value(spec, 1025) == "1025 字节"

    def test_duration_normalizes_to_seconds(self) -> None:
        spec = spec_of(key="demo.wait", type="duration", default=30)
        assert cfg.coerce_value(spec, "5m") == 300
        assert cfg.coerce_value(spec, "2h") == 7200

    def test_rejects_bool_for_numeric_setting(self) -> None:
        spec = spec_of(key="demo.count", type="int", default=1)
        with pytest.raises(cfg.SettingsValueError, match="布尔值"):
            cfg.coerce_value(spec, True)

    def test_range_violation_explains_the_bound(self) -> None:
        spec = spec_of(key="search.default_page_size", type="int", default=40, range=[10, 200])
        result = cfg.validate_value(spec, 999)
        assert result["ok"] is False
        assert "10 ~ 200" in result["reason"]
        assert "「演示开关」" in result["reason"]

    def test_enum_violation_lists_allowed_values(self) -> None:
        spec = spec_of(
            key="ai.involvement",
            type="enum",
            default="off",
            values=["off", "local_only"],
            value_labels={"off": "完全不参与"},
        )
        result = cfg.validate_value(spec, "auto")
        assert result["ok"] is False
        assert "off / local_only" in result["reason"]

    def test_valid_value_returns_coerced_value_and_warning(self) -> None:
        spec = spec_of(dangerous=True, recompute="classify")
        result = cfg.validate_value(spec, "yes")
        assert result["ok"] is True
        assert result["value"] is True
        assert "二次确认" in result["warning"]
        assert "classify" in result["warning"]

    def test_requires_is_fail_closed_when_dependency_is_unset(
        self, specs: dict[str, cfg.SettingSpec]
    ) -> None:
        gated = spec_of(
            key="ai.egress_enabled",
            type="bool",
            default=False,
            dangerous=True,
            requires=["ai.provider_policy_registered"],
        )
        result = cfg.validate_value(gated, True, registry={**specs, gated.key: gated})
        assert result["ok"] is False
        assert "ai.provider_policy_registered" in result["reason"]

    def test_requires_is_satisfied_when_dependency_is_truthy(self) -> None:
        gated = spec_of(key="ai.egress_enabled", requires=["ai.involvement"])
        result = cfg.validate_value(
            gated, True, values={"ai.involvement": "assist"}, registry={"ai.egress_enabled": gated}
        )
        assert result["ok"] is True

    def test_no_warning_for_plain_setting(self) -> None:
        assert cfg.validate_value(spec_of(), True)["warning"] is None


class TestPlanSettingChange:
    def test_blocks_dangerous_item_until_confirmed(self) -> None:
        plan = cfg.plan_setting_change(spec_of(dangerous=True), True)
        assert plan.ok is False
        assert plan.requires_confirmation is True
        assert plan.blocked is True
        assert "危险项" in plan.reason

    def test_allows_dangerous_item_once_confirmed(self) -> None:
        plan = cfg.plan_setting_change(spec_of(dangerous=True), True, confirmed=True)
        assert plan.ok is True
        assert plan.value is True

    def test_blocks_dry_run_first_item_without_a_precheck_report(self) -> None:
        plan = cfg.plan_setting_change(
            spec_of(dry_run_first=True, dangerous=True), True, confirmed=True
        )
        assert plan.ok is False
        assert plan.requires_precheck is True
        assert "dry-run" in plan.reason

    def test_allows_dry_run_first_item_with_a_precheck_report(self) -> None:
        plan = cfg.plan_setting_change(
            spec_of(dry_run_first=True, dangerous=True),
            True,
            confirmed=True,
            precheck={"would_delete": 3},
        )
        assert plan.ok is True

    def test_invalid_value_is_refused_before_any_confirmation_talk(self) -> None:
        spec = spec_of(key="demo.count", type="int", default=1, range=[0, 10], dangerous=True)
        plan = cfg.plan_setting_change(spec, 99)
        assert plan.ok is False
        assert plan.requires_confirmation is False


class TestAppDefaults:
    def test_loads_defaults_for_every_setting(
        self, specs: dict[str, cfg.SettingSpec], app_defaults: dict[str, Any]
    ) -> None:
        assert set(app_defaults) == set(specs)

    def test_unknown_key_in_defaults_is_an_error_not_a_silent_no_op(
        self, tmp_path: Path, specs: dict[str, cfg.SettingSpec]
    ) -> None:
        path = tmp_path / "app.yaml"
        path.write_text(
            "schema_version: 1\ndefaults:\n  search.default_page_siz: 40\n", encoding="utf-8"
        )
        with pytest.raises(cfg.AppConfigError, match=re.escape("是不是想写 search.default_page_size")):
            cfg.load_app_defaults(path, registry=specs)

    def test_missing_defaults_section_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "app.yaml"
        path.write_text("schema_version: 1\ndb:\n  dsn: x\n", encoding="utf-8")
        with pytest.raises(cfg.AppConfigError, match="defaults"):
            cfg.load_app_defaults(path)

    def test_drift_against_registry_is_only_a_warning(self, specs: dict[str, cfg.SettingSpec]) -> None:
        messages = cfg.app_default_warnings({"search.default_page_size": 80}, specs)
        assert len(messages) == 1
        assert "以数据库为准" in messages[0]

    def test_no_warning_when_values_match(
        self, specs: dict[str, cfg.SettingSpec], app_defaults: dict[str, Any]
    ) -> None:
        assert cfg.app_default_warnings(app_defaults, specs) == ()

    def test_seeds_mark_the_source_per_key(self, specs: dict[str, cfg.SettingSpec]) -> None:
        seeds = {(key, value): source for key, value, _, source in cfg.setting_seeds(specs, {})}
        assert seeds[("search.default_page_size", 40)] == "default"
        seeds = {
            (key, value): source
            for key, value, _, source in cfg.setting_seeds(specs, {"search.default_page_size": 60})
        }
        assert seeds[("search.default_page_size", 60)] == "yaml"


class TestResolve:
    def test_env_beats_everything(self, specs: dict[str, cfg.SettingSpec]) -> None:
        resolved = cfg.resolve(
            "dedup.near_duplicate_distance",
            registry=specs,
            yaml_defaults={"dedup.near_duplicate_distance": 6},
            db_values={"dedup.near_duplicate_distance": 7},
            env={"PC__DEDUP__NEAR_DUPLICATE_DISTANCE": "8"},
        )
        assert resolved.value == 8
        assert resolved.source is SettingsSource.ENV
        assert "PC__DEDUP__NEAR_DUPLICATE_DISTANCE" in resolved.explain()

    def test_db_beats_yaml_and_default(self, specs: dict[str, cfg.SettingSpec]) -> None:
        resolved = cfg.resolve(
            "search.default_page_size",
            registry=specs,
            yaml_defaults={"search.default_page_size": 40},
            db_values={"search.default_page_size": 80},
        )
        assert resolved.value == 80
        assert resolved.source is SettingsSource.DB
        assert "设置页管理" in resolved.explain()

    def test_yaml_beats_default(self, specs: dict[str, cfg.SettingSpec]) -> None:
        resolved = cfg.resolve(
            "search.default_page_size", registry=specs, yaml_defaults={"search.default_page_size": 60}
        )
        assert resolved.value == 60
        assert resolved.source is SettingsSource.YAML

    def test_falls_back_to_registry_default(self, specs: dict[str, cfg.SettingSpec]) -> None:
        resolved = cfg.resolve("search.default_page_size", registry=specs)
        assert resolved.value == 40
        assert resolved.source is SettingsSource.DEFAULT

    def test_accepts_setting_row_objects(self, specs: dict[str, cfg.SettingSpec]) -> None:
        """CLI 直接把适配器读出的行对象丢进来，不该要求先手工拆包。"""

        class Row:
            def __init__(self, value: Any) -> None:
                self.value = value

        resolved = cfg.resolve(
            "search.default_page_size", registry=specs, db_values={"search.default_page_size": Row(80)}
        )
        assert resolved.value == 80

    def test_unknown_key_names_the_registry(self, specs: dict[str, cfg.SettingSpec]) -> None:
        with pytest.raises(cfg.SettingsRegistryError, match=re.escape("settings-registry.yaml")):
            cfg.resolve("nope.nope", registry=specs)

    def test_bad_source_value_is_loud_and_blames_the_source(
        self, specs: dict[str, cfg.SettingSpec]
    ) -> None:
        with pytest.raises(cfg.SettingsValueError, match="环境变量"):
            cfg.resolve(
                "search.default_page_size", registry=specs, env={"PC__SEARCH__DEFAULT_PAGE_SIZE": "big"}
            )


class TestEnvMapping:
    def test_key_and_env_name_round_trip(self) -> None:
        name = cfg.env_override_key("dedup.near_duplicate_distance")
        assert name == "PC__DEDUP__NEAR_DUPLICATE_DISTANCE"
        assert cfg.key_from_env(name) == "dedup.near_duplicate_distance"

    def test_foreign_namespaces_are_not_reported(
        self, specs: dict[str, cfg.SettingSpec]
    ) -> None:
        """db / paths 这类键属于 L2/L3 配置段，不该被报成「拼错了」。"""
        mapped, warnings = cfg.env_overrides_by_key(
            {"PC__DB__PATH": "x", "PC__PATHS__MEDIA_ROOT": "/data/media"}, registry=specs
        )
        assert mapped == {}
        assert warnings == ()

    def test_typo_inside_a_setting_namespace_is_reported(
        self, specs: dict[str, cfg.SettingSpec]
    ) -> None:
        _, warnings = cfg.env_overrides_by_key(
            {"PC__DEDUP__NEAR_DUPLICATE_DISTANC": "4"}, registry=specs
        )
        assert len(warnings) == 1
        assert "PC__DEDUP__NEAR_DUPLICATE_DISTANCE" in warnings[0]

    def test_malformed_name_is_reported(self, specs: dict[str, cfg.SettingSpec]) -> None:
        _, warnings = cfg.env_overrides_by_key({"PC__OOPS": "1"}, registry=specs)
        assert len(warnings) == 1
        assert "PC__OOPS" in warnings[0]


class TestSnapshot:
    def test_is_immutable(self, specs: dict[str, cfg.SettingSpec]) -> None:
        snap = cfg.snapshot(registry=specs, env={})
        with pytest.raises(TypeError):
            snap.values["search.default_page_size"] = 1  # type: ignore[index]

    def test_reports_sources_and_revision(self, specs: dict[str, cfg.SettingSpec]) -> None:
        snap = cfg.snapshot(
            registry=specs,
            db_values={"search.default_page_size": 80},
            env={"PC__DEDUP__NEAR_DUPLICATE_DISTANCE": "9"},
            revision=3,
        )
        assert snap.get("search.default_page_size") == 80
        assert snap.source_of("search.default_page_size") is SettingsSource.DB
        assert snap.source_of("dedup.near_duplicate_distance") is SettingsSource.ENV
        assert snap.revision == 3
        assert snap.get("nope.nope") is None

    def test_lists_orphan_rows_that_left_the_registry(
        self, specs: dict[str, cfg.SettingSpec]
    ) -> None:
        snap = cfg.snapshot(registry=specs, db_values={"legacy.setting": 1}, env={})
        assert snap.orphans == ("legacy.setting",)
        assert snap.get("legacy.setting") == 1

    def test_as_dict_is_json_friendly(self, specs: dict[str, cfg.SettingSpec]) -> None:
        import json

        payload = cfg.snapshot(registry=specs, env={}).as_dict()
        text = json.dumps(payload, ensure_ascii=False)
        assert "search.default_page_size" in text
        assert payload["settings"]["inbox.enabled"]["value_text"] == "关"

    def test_partial_snapshot_without_registry_allows_explicit_values(self) -> None:
        snap = cfg.snapshot(yaml_defaults={"a.b": 1}, env={})
        assert snap.get("a.b") == 1
        assert snap.source_of("a.b") is SettingsSource.YAML
