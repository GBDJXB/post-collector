"""L2 配置段（``config/app.yaml`` 除 ``defaults`` 外的部分）的读取与校验。

这些段的消费者是摄入层与 worker：限额、安全开关、队列节拍。它们刻意不进设置页
（DESIGN §6.2 末段），因此**校验必须更严**：没有界面兜底，写错就只剩运行时的怪异行为。

最关键的一条断言是「已知键清单与仓库里那份真实文件一致」——清单过期会让某个键
被当成未知键拒掉，或者更糟：被静默忽略。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pc import config as cfg

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_APP_CONFIG = REPO_ROOT / cfg.DEFAULT_APP_CONFIG_PATH


def _write_app_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "app.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _load_real() -> dict[str, Any]:
    return cfg.load_app_config(REAL_APP_CONFIG)


# ---------------------------------------------------------------------------
# 清单与真实文件保持一致
# ---------------------------------------------------------------------------


def test_known_sections_match_the_real_file() -> None:
    """``APP_CONFIG_SECTIONS`` 必须与 config/app.yaml 的实际段名一致。"""
    import yaml

    data = yaml.safe_load(REAL_APP_CONFIG.read_text(encoding="utf-8"))
    real_sections = {
        key for key in data if key not in {"schema_version", "defaults"}
    }
    assert real_sections == set(cfg.APP_CONFIG_SECTIONS), (
        "config/app.yaml 的段名与 APP_CONFIG_SECTIONS 不一致；"
        "新增/删除段时必须同时改这张清单与它的测试"
    )


def test_known_keys_match_the_real_file() -> None:
    """每个段里的键集合也必须一致（防止有人加了键却忘了登记）。"""
    import yaml

    data = yaml.safe_load(REAL_APP_CONFIG.read_text(encoding="utf-8"))
    for name, known in cfg.APP_CONFIG_SECTIONS.items():
        assert set(data[name]) == set(known), (
            f"config/app.yaml 的 {name} 段键集合与清单不一致："
            f"文件 {sorted(data[name])} / 清单 {sorted(known)}"
        )


def test_real_file_loads_and_validates() -> None:
    data = _load_real()
    assert data["schema_version"] == cfg.REGISTRY_SCHEMA_VERSION


def test_real_file_has_no_missing_keys() -> None:
    assert cfg.config_warnings(_load_real()) == ()


# ---------------------------------------------------------------------------
# 严格校验
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = _write_app_config(
        tmp_path,
        f"schema_version: {cfg.REGISTRY_SCHEMA_VERSION}\ndefaults: {{}}\nsekurity:\n  allow_svg: true\n",
    )
    with pytest.raises(cfg.AppConfigError, match="未知的顶层键"):
        cfg.load_app_config(path)


def test_unknown_section_key_is_rejected_with_a_suggestion(tmp_path: Path) -> None:
    path = _write_app_config(
        tmp_path,
        f"schema_version: {cfg.REGISTRY_SCHEMA_VERSION}\ndefaults: {{}}\n"
        "ingest:\n  max_media_size: 1\n",
    )
    with pytest.raises(cfg.AppConfigError) as excinfo:
        cfg.load_app_config(path)
    message = str(excinfo.value)
    assert "ingest 段出现未知键" in message
    assert "max_media_bytes" in message, "应给出最接近的键名建议，否则等于让人猜"


def test_section_must_be_a_mapping(tmp_path: Path) -> None:
    path = _write_app_config(
        tmp_path,
        f"schema_version: {cfg.REGISTRY_SCHEMA_VERSION}\ndefaults: {{}}\nsecurity: 3\n",
    )
    with pytest.raises(cfg.AppConfigError, match="必须是「键: 值」映射"):
        cfg.load_app_config(path)


def test_schema_version_mismatch_is_rejected(tmp_path: Path) -> None:
    path = _write_app_config(tmp_path, "schema_version: 99\ndefaults: {}\n")
    with pytest.raises(cfg.AppConfigError, match="schema_version"):
        cfg.load_app_config(path)


def test_missing_file_is_a_human_error(tmp_path: Path) -> None:
    with pytest.raises(cfg.AppConfigError, match="找不到"):
        cfg.load_app_config(tmp_path / "nope.yaml")


# ---------------------------------------------------------------------------
# ingest / security / jobs 的取值与类型
# ---------------------------------------------------------------------------


def test_ingest_limits_from_the_real_file() -> None:
    limits = cfg.ingest_limits(_load_real())
    assert limits.max_media_bytes == 52_428_800  # 50 MB：媒体硬上限是安全基线
    assert limits.max_text_chars == 100_000
    assert limits.link_autofetch is False, "链接默认只记录不抓取（DESIGN §4.3）"


def test_ingest_limits_defaults_when_section_missing() -> None:
    assert cfg.ingest_limits({}) == cfg.IngestLimits()
    assert cfg.ingest_limits(None) == cfg.IngestLimits()


def test_ingest_limits_reject_wrong_types() -> None:
    with pytest.raises(cfg.AppConfigError, match="必须是整数"):
        cfg.ingest_limits({"ingest": {"max_media_bytes": "50MB"}})
    # bool 是 int 的子类：`true` 写进整数字段必须报错，而不是变成 1
    with pytest.raises(cfg.AppConfigError, match="必须是整数"):
        cfg.ingest_limits({"ingest": {"max_media_bytes": True}})
    with pytest.raises(cfg.AppConfigError, match="必须 ≥ 1"):
        cfg.ingest_limits({"ingest": {"max_text_chars": 0}})
    with pytest.raises(cfg.AppConfigError, match="必须是 true / false"):
        cfg.ingest_limits({"ingest": {"link_autofetch": "false"}})


def test_security_policy_from_the_real_file() -> None:
    policy = cfg.security_policy(_load_real())
    assert policy.sniff_mime_by_magic is True
    assert policy.fail_closed_on_scan_error is True, "扫描失败必须按未通过处理（DESIGN §6.5）"
    assert policy.allow_svg is False, "SVG 视为不可信 XML，默认拒绝"
    assert policy.allow_pdf_inline_render is False


def test_security_policy_defaults_are_conservative() -> None:
    policy = cfg.security_policy({})
    assert policy.fail_closed_on_scan_error is True
    assert policy.allow_svg is False
    assert policy.allow_pdf_inline_render is False
    assert policy.exif_strip_gps is True


def test_security_policy_rejects_string_booleans() -> None:
    with pytest.raises(cfg.AppConfigError, match="必须是 true / false"):
        cfg.security_policy({"security": {"allow_svg": "yes"}})


def test_job_settings_from_the_real_file() -> None:
    settings = cfg.job_settings(_load_real())
    assert settings.batch_size == 20
    assert settings.max_attempts_default == 5
    assert settings.backoff_seconds == (30, 120, 480, 1800, 7200)


def test_job_settings_backoff_for() -> None:
    settings = cfg.JobSettings()
    assert settings.backoff_for(1) == 30
    assert settings.backoff_for(5) == 7200
    # 超出表长时取最后一项，而不是越界或回绕
    assert settings.backoff_for(99) == 7200


def test_job_settings_reject_bad_backoff() -> None:
    with pytest.raises(cfg.AppConfigError, match="必须是整数列表"):
        cfg.job_settings({"jobs": {"backoff_seconds": "30,120"}})
    with pytest.raises(cfg.AppConfigError, match="不能是空列表"):
        cfg.job_settings({"jobs": {"backoff_seconds": []}})
    with pytest.raises(cfg.AppConfigError, match="≥ 1 的整数"):
        cfg.job_settings({"jobs": {"backoff_seconds": [30, 0]}})
    with pytest.raises(cfg.AppConfigError, match="≥ 1 的整数"):
        cfg.job_settings({"jobs": {"backoff_seconds": [30, True]}})


# ---------------------------------------------------------------------------
# 缺口提示（不静默）
# ---------------------------------------------------------------------------


def test_missing_section_is_reported() -> None:
    warnings = cfg.config_warnings({"schema_version": 1})
    assert any("缺少 ingest 段" in message for message in warnings)
    assert any("缺少 security 段" in message for message in warnings)


def test_missing_key_is_reported() -> None:
    warnings = cfg.config_warnings({"ingest": {"max_media_bytes": 1}})
    assert any("ingest 段缺少这些键" in message and "max_text_chars" in message for message in warnings)


def test_config_warnings_are_silent_for_none() -> None:
    assert cfg.config_warnings(None) == ()
