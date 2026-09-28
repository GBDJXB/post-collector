"""设置层：注册表加载与分层解析（DESIGN §6.2）。

本模块负责把 ``config/settings-registry.yaml``（声明式注册表）与
``config/app.yaml`` 的 ``defaults`` 段、``setting`` 表、环境变量合并成
一份**不可变配置快照**。

分层与优先级（DESIGN §6.2.4）：
    env  >  db  >  yaml  >  default

关键原则：大多数设置项**只有一个来源**。首次启动用 ``app.yaml`` 的 ``defaults``
播种 ``setting`` 表，此后以数据库为准；YAML 中对应键视为 ``seeded_by_ui``，
界面必须显示「此项由设置页管理」。这条规则是为了避免
「改了文件没生效」「界面上改了但重启被覆盖」这类最让人困惑的故障。

骨架阶段状态：函数签名与契约已定，**实现留空**。CLI 的 ``config show``
会以明确的人话提示失败（而不是抛堆栈），见 ``pc.cli``。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pc.domain import SettingsSource

#: 环境变量前缀与层级分隔符，例如 ``PC__DEDUP__NEAR_DUPLICATE_DISTANCE=8``
ENV_PREFIX = "PC__"
ENV_SEPARATOR = "__"

#: 默认路径（相对仓库根）
DEFAULT_REGISTRY_PATH = Path("config/settings-registry.yaml")
DEFAULT_APP_CONFIG_PATH = Path("config/app.yaml")

#: 每个注册项必须包含的字段及其存在理由（DESIGN §6.2.1）
REQUIRED_SPEC_FIELDS: tuple[str, ...] = (
    "key",  # 校验与默认值
    "type",
    "default",
    "label",  # 普通用户的唯一信息来源
    "help",
    "impact",  # 改大/改小分别会发生什么——最易漏也最有价值
    "dangerous",  # 是否二次确认
    "cost",  # none | api | storage
    "restart_required",  # 改完就生效，还是要重启
    "recompute",  # 改后哪些派生数据需要重算
    "requires",  # 依赖其他设置项，防止无效组合
)

#: 支持的值类型
SUPPORTED_TYPES: tuple[str, ...] = ("bool", "int", "float", "str", "enum", "bytes", "duration")


class SettingsRegistryError(RuntimeError):
    """注册表无法加载（缺依赖或文件不存在）。

    这属于**环境问题**而非程序缺陷，因此必须给出可操作的人话提示，
    而不是让用户看堆栈（DESIGN §6.2.3「出错时知道怎么办」）。
    """


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """一个设置项的声明（来自 ``settings-registry.yaml``）。"""

    key: str
    label: str
    help: str
    impact: str
    default: Any
    type: str = "str"
    range: tuple[Any, Any] | None = None
    unit: str | None = None
    values: tuple[str, ...] | None = None
    value_labels: Mapping[str, str] = field(default_factory=dict)
    dangerous: bool = False
    cost: str = "none"
    restart_required: bool = False
    recompute: str | None = None
    requires: tuple[str, ...] = ()
    dry_run_first: bool = False
    note: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> SettingSpec:
        """从 YAML 映射构造；缺字段应报错并指出缺了哪个键。"""
        raise NotImplementedError("实现阶段补齐：DESIGN §6.2.1 的字段规范与校验报错")


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """一个生效值及其来源；``source`` 回答「这个值从哪来」。"""

    key: str
    value: Any
    source: SettingsSource
    spec: SettingSpec | None = None

    def explain(self) -> str:
        """输出一句人话解释，供 ``pc config show --explain`` 使用。"""
        raise NotImplementedError("实现阶段补齐：DESIGN §6.2.4")


def load_registry(path: Path | str = DEFAULT_REGISTRY_PATH) -> list[SettingSpec]:
    """加载并校验设置项注册表。

    Raises:
        SettingsRegistryError: 缺少 PyYAML（提示先 ``uv sync``）或文件不存在。

    实现要点：注册表本身是版本化 YAML；新增设置项只加一段声明，
    设置页、校验、审计、文档四者都由它派生，不改前端代码。
    """
    if not _pyyaml_available():
        raise SettingsRegistryError(
            "缺少依赖 PyYAML，无法读取设置项注册表。请先在仓库根运行 `uv sync` 安装依赖。"
        )
    p = Path(path)
    if not p.exists():
        raise SettingsRegistryError(
            f"找不到设置项注册表：{p}。请确认在仓库根目录运行，或检查 config/ 是否完整。"
        )
    raise NotImplementedError("实现阶段补齐解析与 schema_version 校验：DESIGN §6.2.1")


def validate_value(spec: SettingSpec, value: Any) -> dict[str, Any]:
    """按注册项校验一个值：类型、range、enum 取值、requires 依赖链。

    Returns:
        ``{"ok": bool, "reason": str, "warning": str | None}``。
        越界值必须在保存前被拦住（「输入 999999」这类错误不该运行到一半才崩）。
    """
    raise NotImplementedError("实现阶段补齐：DESIGN §6.2.1")


def resolve(
    key: str,
    *,
    registry: Mapping[str, SettingSpec] | None = None,
    yaml_defaults: Mapping[str, Any] | None = None,
    db_values: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedSetting:
    """解析一个键的生效值与来源，优先级 env > db > yaml > default。"""
    raise NotImplementedError("实现阶段补齐：DESIGN §6.2.4 的来源可解释性")


def env_override_key(key: str) -> str:
    """把 ``dedup.near_duplicate_distance`` 映射为 ``PC__DEDUP__NEAR_DUPLICATE_DISTANCE``。"""
    return ENV_PREFIX + key.replace(".", ENV_SEPARATOR).upper()


def env_overrides(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """收集当前进程中所有 ``PC__`` 环境变量覆盖（键名保持原始形式）。"""
    source = env if env is not None else os.environ
    return {k: v for k, v in source.items() if k.startswith(ENV_PREFIX)}


def snapshot(
    *,
    registry: Mapping[str, SettingSpec] | None = None,
    yaml_defaults: Mapping[str, Any] | None = None,
    db_values: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """构建一份不可变配置快照，供单个任务或单次请求全程使用。

    配置以快照形式注入，而不是全局可变单例：避免一次任务内配置中途变化
    导致状态不一致（DESIGN §6.2.2）。
    """
    raise NotImplementedError("实现阶段补齐：DESIGN §6.2.2 的热更新与快照注入")


def _pyyaml_available() -> bool:
    """判断 PyYAML 是否可用，用于给出「先 uv sync」这类可操作提示。"""
    import importlib.util

    return importlib.util.find_spec("yaml") is not None


__all__ = [
    "DEFAULT_APP_CONFIG_PATH",
    "DEFAULT_REGISTRY_PATH",
    "ENV_PREFIX",
    "ENV_SEPARATOR",
    "REQUIRED_SPEC_FIELDS",
    "SUPPORTED_TYPES",
    "ResolvedSetting",
    "SettingSpec",
    "SettingsRegistryError",
    "env_override_key",
    "env_overrides",
    "load_registry",
    "resolve",
    "snapshot",
    "validate_value",
]
