"""设置层：注册表加载、分层解析与不可变快照（DESIGN §6.2）。

本模块负责把 ``config/settings-registry.yaml``（声明式注册表）与
``config/app.yaml`` 的 ``defaults`` 段、``setting`` 表、环境变量合并成
一份**不可变配置快照**。

分层与优先级（DESIGN §6.2.4）：
    env  >  db  >  yaml  >  default

关键原则：大多数设置项**只有一个来源**。首次启动用 ``app.yaml`` 的 ``defaults``
播种 ``setting`` 表，此后以数据库为准；被数据库接管的键在界面上显示
「此项由设置页管理」（见 :func:`describe_source`）。这条规则是为了避免
「改了文件没生效」「界面上改了但重启被覆盖」这类最让人困惑的故障。

本模块刻意**只依赖标准库与领域层**：注册表校验是纯函数，可以在没有数据库、
甚至没有 PyYAML（会给出「先 uv sync」的人话提示）的环境里被测试。
与数据库的读写留在 ``pc.adapters.sqlite``，组合它们的活儿交给 CLI / 服务层。

几个刻意的判断（已记入 DESIGN 附录 C）：

* ``app.yaml`` 的 ``defaults`` 段出现注册表里没有的键 -> **报错**。拼错的键会被
  静默忽略，正是「改了文件没生效」的典型来源。
* ``defaults`` 段的值与注册表默认值不同 -> **不报错，只提示**。它是 L2 层的值，
  本来就允许开发者改；只是要让人知道「已播种的实例以数据库为准」。
* 播种只写 ``setting`` 表，**不写** ``setting_change``：它不是谁做的改动，
  而是一次初始化。审计表留给真正的变更。
* 「危险项需二次确认」「改动前先 dry-run」这类闸门编码在 :func:`plan_setting_change`
  里，而不是散落在界面代码中：界面、CLI、脚本走的是同一条校验路径。
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pc.domain import SettingsSource

#: 环境变量前缀与层级分隔符，例如 ``PC__DEDUP__NEAR_DUPLICATE_DISTANCE=8``
ENV_PREFIX = "PC__"
ENV_SEPARATOR = "__"

#: 默认路径（相对仓库根）
DEFAULT_REGISTRY_PATH = Path("config/settings-registry.yaml")
DEFAULT_APP_CONFIG_PATH = Path("config/app.yaml")

#: 本模块认识的注册表版本；注册表声明的版本必须与它一致
REGISTRY_SCHEMA_VERSION = 1

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

#: 选填字段
OPTIONAL_SPEC_FIELDS: tuple[str, ...] = (
    "range",
    "unit",
    "values",
    "value_labels",
    "dry_run_first",
    "note",
)

#: 支持的值类型
SUPPORTED_TYPES: tuple[str, ...] = ("bool", "int", "float", "str", "enum", "bytes", "duration")

#: 允许的费用标注
COST_KINDS: tuple[str, ...] = ("none", "api", "storage")

#: 设置项键名形态：``命名空间.项``，小写 + 下划线
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")

_BYTE_UNITS: Mapping[str, int] = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}

_DURATION_UNITS: Mapping[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_TRUE_WORDS = frozenset({"1", "true", "yes", "y", "on", "开", "是"})
_FALSE_WORDS = frozenset({"0", "false", "no", "n", "off", "关", "否"})

_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


# ---------------------------------------------------------------------------
# 异常：一律带可操作的人话提示（DESIGN §6.2.3）
# ---------------------------------------------------------------------------


class ConfigError(RuntimeError):
    """配置层问题的基类。消息是可直接打印给人看的，不需要堆栈。"""


class SettingsRegistryError(ConfigError):
    """注册表无法加载（缺依赖、文件不存在、声明本身不合法）。

    这属于**环境问题**而非程序缺陷，因此必须给出可操作的人话提示，
    而不是让用户看堆栈（DESIGN §6.2.3「出错时知道怎么办」）。
    """


class AppConfigError(ConfigError):
    """``config/app.yaml``（L2 开发配置）无法加载或与注册表对不上。"""


class SettingsValueError(ConfigError):
    """某个来源给出的值不合法（类型不对、越界、枚举值不存在）。

    与环境变量、数据库、YAML 里的手改内容有关，属于**输入问题**：
    提示里必须写清「哪个来源、哪个键、期望什么、收到什么」。
    """


# ---------------------------------------------------------------------------
# 注册项
# ---------------------------------------------------------------------------


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
        if not isinstance(raw, Mapping):
            raise SettingsRegistryError(f"每个设置项必须是一段映射，收到：{raw!r}")

        key = str(raw.get("key") or "(缺 key)")
        missing = [name for name in REQUIRED_SPEC_FIELDS if name not in raw]
        if missing:
            raise SettingsRegistryError(
                f"设置项 {key} 缺少必填字段：{', '.join(missing)}。"
                f"必填字段清单见 config/settings-registry.yaml 文件头（DESIGN §6.2.1）。"
            )

        unknown = [name for name in raw if name not in REQUIRED_SPEC_FIELDS + OPTIONAL_SPEC_FIELDS]
        if unknown:
            hint = ""
            close = difflib.get_close_matches(str(unknown[0]), list(REQUIRED_SPEC_FIELDS), n=1)
            if close:
                hint = f"；是不是想写 {close[0]}？"
            raise SettingsRegistryError(f"设置项 {key} 出现未知字段：{', '.join(unknown)}{hint}")

        if not isinstance(raw["key"], str) or not KEY_PATTERN.match(raw["key"]):
            raise SettingsRegistryError(
                f"设置项的 key 必须形如「命名空间.项名」（小写字母、数字、下划线），"
                f"收到：{raw['key']!r}。"
            )

        type_name = raw["type"]
        if type_name not in SUPPORTED_TYPES:
            raise SettingsRegistryError(
                f"设置项 {key} 的 type 不支持：{type_name!r}；"
                f"可用类型：{', '.join(SUPPORTED_TYPES)}。"
            )

        for name in ("label", "help", "impact"):
            if not isinstance(raw[name], str) or not raw[name].strip():
                raise SettingsRegistryError(f"设置项 {key} 的 {name} 必须是一句非空的说明文字。")

        for name in ("dangerous", "restart_required", "dry_run_first"):
            if name in raw and not isinstance(raw[name], bool):
                raise SettingsRegistryError(f"设置项 {key} 的 {name} 必须是 true / false。")

        cost = raw["cost"]
        if cost not in COST_KINDS:
            raise SettingsRegistryError(
                f"设置项 {key} 的 cost 不支持：{cost!r}；可用取值：{', '.join(COST_KINDS)}。"
            )

        recompute = raw["recompute"]
        if recompute is not None and (not isinstance(recompute, str) or not recompute.strip()):
            raise SettingsRegistryError(
                f"设置项 {key} 的 recompute 要么是 null（无需重算），要么是一句非空标识。"
            )

        requires_raw = raw["requires"]
        if isinstance(requires_raw, str) or not isinstance(requires_raw, Sequence):
            raise SettingsRegistryError(f"设置项 {key} 的 requires 必须是列表（没有依赖就写 []）。")
        requires = tuple(str(item) for item in requires_raw)

        setting_range = _parse_range(key, raw.get("range"))
        values = _parse_values(key, type_name, raw.get("values"))
        value_labels = _parse_value_labels(key, raw.get("value_labels"), values)

        spec = cls(
            key=raw["key"],
            label=raw["label"],
            help=raw["help"],
            impact=raw["impact"],
            default=raw["default"],
            type=type_name,
            range=setting_range,
            unit=raw.get("unit"),
            values=values,
            value_labels=value_labels,
            dangerous=bool(raw["dangerous"]),
            cost=cost,
            restart_required=bool(raw["restart_required"]),
            recompute=recompute,
            requires=requires,
            dry_run_first=bool(raw.get("dry_run_first", False)),
            note=raw.get("note"),
        )

        # 默认值必须在加载注册表时就被校验：注册表自己写错值是最该早发现的一类错误。
        # 只校验类型与范围，**不校验 requires 依赖链**——依赖要等运行期才知道是否满足，
        # 在加载期校验会让「依赖别的开关的项」根本无法声明（阶段 2 的外发开关就是这种）。
        _, shape_error = _check_shape(spec, spec.default)
        if shape_error:
            raise SettingsRegistryError(
                f"设置项 {key} 的默认值不合法：{shape_error}"
                f"（注册表 default={spec.default!r}，type={spec.type}）"
            )
        return spec

    def coerce(self, raw: Any) -> Any:
        """把外部来源的原始值转成该设置项的规范类型（不做范围与依赖校验）。"""
        return coerce_value(self, raw)

    def humanize(self, value: Any) -> str:
        """把值渲染成给人看的形式（枚举用中文标签，字节带单位）。"""
        return humanize_value(self, value)


def _parse_range(key: str, raw: Any) -> tuple[Any, Any] | None:
    """解析 ``range: [min, max]``；任一端为 null 表示该方向不设限。"""
    if raw is None:
        return None
    if isinstance(raw, str) or not isinstance(raw, Sequence) or len(raw) != 2:
        raise SettingsRegistryError(f"设置项 {key} 的 range 必须是 [最小值, 最大值]，任一端可写 null。")
    low, high = raw[0], raw[1]
    if low is None and high is None:
        raise SettingsRegistryError(f"设置项 {key} 的 range 两端都是 null，等同于不写 range。")
    for bound in (low, high):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, (int, float))):
            raise SettingsRegistryError(f"设置项 {key} 的 range 端点必须是数字或 null，收到 {bound!r}。")
    if low is not None and high is not None and low >= high:
        raise SettingsRegistryError(f"设置项 {key} 的 range 下限必须小于上限，收到 [{low}, {high}]。")
    return (low, high)


def _parse_values(key: str, type_name: str, raw: Any) -> tuple[str, ...] | None:
    """解析 ``values``：``enum`` 必填，其它类型不允许出现。"""
    if type_name == "enum":
        if isinstance(raw, str) or not isinstance(raw, Sequence) or not raw:
            raise SettingsRegistryError(
                f"设置项 {key} 是 enum，必须给出非空的 values 列表（可选值）。"
            )
        bad = [item for item in raw if not isinstance(item, str)]
        if bad:
            raise SettingsRegistryError(
                f"设置项 {key} 的 values 必须全是字符串，收到 {bad!r}。"
                "常见原因是 YAML 把裸写的 off / on / yes / no 当成了布尔值：请给取值加引号"
                '（例如 values: ["off", local_only]）。'
            )
        if len(set(raw)) != len(raw):
            raise SettingsRegistryError(f"设置项 {key} 的 values 里有重复取值。")
        return tuple(raw)
    if raw is not None:
        raise SettingsRegistryError(
            f"设置项 {key} 的 type 是 {type_name}，不该有 values（那是 enum 的字段）。"
        )
    return None


def _parse_value_labels(
    key: str, raw: Any, values: tuple[str, ...] | None
) -> Mapping[str, str]:
    """解析 ``value_labels``：中文标签的键必须是 values 里的取值。"""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SettingsRegistryError(f"设置项 {key} 的 value_labels 必须是「取值: 中文标签」的映射。")
    for name, label in raw.items():
        if not isinstance(name, str):
            raise SettingsRegistryError(
                f"设置项 {key} 的 value_labels 出现非字符串的键 {name!r}："
                "YAML 会把裸写的 off / on / yes / no 当成布尔值，请给取值加引号。"
            )
        if values is None or name not in values:
            raise SettingsRegistryError(
                f"设置项 {key} 的 value_labels 里出现未声明的取值 {name!r}；请先在 values 里声明。"
            )
        if not isinstance(label, str) or not label.strip():
            raise SettingsRegistryError(f"设置项 {key} 的 value_labels[{name}] 必须是非空中文说明。")
    return MappingProxyType(dict(raw))


def load_registry(path: Path | str = DEFAULT_REGISTRY_PATH) -> list[SettingSpec]:
    """加载并校验设置项注册表。

    Raises:
        SettingsRegistryError: 缺少 PyYAML（提示先 ``uv sync``）、文件不存在，
            或声明本身不合法（缺字段、键名重复、默认值越界等）。

    实现要点：注册表本身是版本化 YAML；新增设置项只加一段声明，
    设置页、校验、审计、文档四者都由它派生，不改前端代码。
    """
    data = _load_yaml(path, error_cls=SettingsRegistryError, what="设置项注册表")

    version = data.get("schema_version")
    if version != REGISTRY_SCHEMA_VERSION:
        raise SettingsRegistryError(
            f"设置项注册表的 schema_version 是 {version!r}，本程序支持 {REGISTRY_SCHEMA_VERSION}。"
            "注册表与本程序版本必须配套；升级请先同步代码与配置。"
        )

    raw_settings = data.get("settings")
    if isinstance(raw_settings, Mapping) or not isinstance(raw_settings, Sequence) or not raw_settings:
        raise SettingsRegistryError(
            "设置项注册表里必须有一个非空的 settings 列表，每项是一段声明（DESIGN §6.2.1）。"
        )

    specs = [SettingSpec.from_mapping(item) for item in raw_settings]

    duplicates = sorted(key for key, count in Counter(spec.key for spec in specs).items() if count > 1)
    if duplicates:
        raise SettingsRegistryError(f"设置项键名重复：{', '.join(duplicates)}。同一个键只能声明一次。")
    return specs


def registry_index(specs: Iterable[SettingSpec]) -> dict[str, SettingSpec]:
    """把注册项列表变成 ``key -> spec`` 的索引。"""
    return {spec.key: spec for spec in specs}


def _load_yaml(path: Path | str, *, error_cls: type[ConfigError], what: str) -> Mapping[str, Any]:
    """读取一个顶层为映射的 YAML 文件；错误一律转成人话。"""
    if not _pyyaml_available():
        raise error_cls(
            f"缺少依赖 PyYAML，无法读取{what}。请先在仓库根运行 `uv sync` 安装依赖。"
        )
    p = Path(path)
    if not p.is_file():
        raise error_cls(f"找不到{what}：{p}。请确认在仓库根目录运行，或用 --root 指定仓库根。")

    import yaml

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise error_cls(f"{what} 不是合法的 YAML（{p}）：{exc}") from exc
    if data is None:
        raise error_cls(f"{what} 是空文件（{p}）。")
    if not isinstance(data, Mapping):
        raise error_cls(f"{what} 的顶层必须是「键: 值」映射（{p}）。")
    return data


# ---------------------------------------------------------------------------
# L2：config/app.yaml 的 defaults 段
# ---------------------------------------------------------------------------


def load_app_defaults(
    path: Path | str = DEFAULT_APP_CONFIG_PATH,
    *,
    registry: Mapping[str, SettingSpec] | None = None,
) -> dict[str, Any]:
    """读取 ``config/app.yaml`` 的 ``defaults`` 段，即 L2 层要播种的值。

    给出 ``registry`` 时会顺手校验：出现在 ``defaults`` 里却不在注册表中的键
    **直接报错**——拼错的键只会被静默忽略，那正是最难查的一类故障。

    Raises:
        AppConfigError: 文件缺失 / 不是合法 YAML / 缺 ``defaults`` 段 / 出现未知键。
        SettingsValueError: 某个值不符合注册表的类型或范围。
    """
    data = _load_yaml(path, error_cls=AppConfigError, what="开发配置 config/app.yaml")

    version = data.get("schema_version")
    if version != REGISTRY_SCHEMA_VERSION:
        raise AppConfigError(
            f"config/app.yaml 的 schema_version 是 {version!r}，本程序支持 {REGISTRY_SCHEMA_VERSION}。"
        )

    raw_defaults = data.get("defaults")
    if not isinstance(raw_defaults, Mapping):
        raise AppConfigError(
            "config/app.yaml 缺少 defaults 段（首次启动播种 setting 表用的默认值）。"
        )

    if registry is not None:
        unknown = [key for key in raw_defaults if key not in registry]
        if unknown:
            hint = ""
            close = difflib.get_close_matches(str(unknown[0]), list(registry), n=1)
            if close:
                hint = f"；是不是想写 {close[0]}？"
            raise AppConfigError(
                f"config/app.yaml 的 defaults 段出现注册表里没有的键：{', '.join(unknown)}{hint}"
                "。新增设置项请先在 config/settings-registry.yaml 里声明。"
            )

    resolved: dict[str, Any] = {}
    for key, value in raw_defaults.items():
        spec = registry.get(key) if registry is not None else None
        if spec is None:
            resolved[key] = value
            continue
        try:
            resolved[key] = coerce_value(spec, value)
        except SettingsValueError as exc:
            raise SettingsValueError(f"config/app.yaml 的 defaults.{key} 不合法：{exc}") from exc
    return resolved


def app_default_warnings(
    yaml_defaults: Mapping[str, Any], registry: Mapping[str, SettingSpec]
) -> tuple[str, ...]:
    """报告 ``defaults`` 段与注册表默认值的差异。

    刻意只是**提示**而不是错误：L2 的 ``defaults`` 段本来就是给开发者改的，
    它就是解析链里 ``yaml`` 层。要让人知道的是「已经播种过的实例以数据库为准，
    改这里不会影响它」。
    """
    messages: list[str] = []
    for key, value in yaml_defaults.items():
        spec = registry.get(key)
        if spec is None or value == spec.default:
            continue
        messages.append(
            f"config/app.yaml 的 defaults.{key} = {value!r}，注册表默认是 {spec.default!r}；"
            "该值只对尚未播种的实例生效，已初始化实例以数据库为准。"
        )
    return tuple(messages)


def setting_seeds(
    registry: Mapping[str, SettingSpec], yaml_defaults: Mapping[str, Any] | None = None
) -> list[tuple[str, Any, str, str]]:
    """生成首次播种 ``setting`` 表用的行。

    Returns:
        ``(key, value, value_type, source)`` 四元组列表，顺序与注册表一致。
        ``source`` 是 ``yaml``（该键在 ``app.yaml`` 的 defaults 段里）或
        ``default``（只有注册表默认值）。

    刻意**不播种环境变量覆盖**：``PC__`` 前缀是运行时的临时覆盖，写进数据库
    就会变成「环境变量关掉之后值还在」的幽灵配置。
    """
    overrides = dict(yaml_defaults or {})
    unknown = [key for key in overrides if key not in registry]
    if unknown:
        raise AppConfigError(
            f"defaults 段里的这些键不在注册表中，无法播种：{', '.join(unknown)}。"
        )

    seeds: list[tuple[str, Any, str, str]] = []
    for key, spec in registry.items():
        if key in overrides:
            seeds.append((key, coerce_value(spec, overrides[key]), spec.type, "yaml"))
        else:
            seeds.append((key, spec.default, spec.type, "default"))
    return seeds


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def coerce_value(spec: SettingSpec, raw: Any) -> Any:
    """把外部来源的原始值转成规范类型；不合法时抛 :class:`SettingsValueError`。

    刻意宽容一点的地方：环境变量与 YAML 里的一切都是字符串（``"40"``、``"true"``、
    ``"200 GB"``），所以数字、布尔、字节、时长都接受对应的字符串写法——但**不接受**
    猜不出含义的输入，宁可报错也不猜。
    """
    if isinstance(raw, bool) and spec.type not in ("bool",):
        raise SettingsValueError(
            f"「{spec.label}」需要一个 {_type_label(spec.type)}，收到布尔值 {raw!r}。"
        )

    if spec.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            word = raw.strip().lower()
            if word in _TRUE_WORDS:
                return True
            if word in _FALSE_WORDS:
                return False
        raise SettingsValueError(
            f"「{spec.label}」需要 true / false，收到 {raw!r}（可写 开/关、true/false、1/0）。"
        )

    if spec.type == "int":
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and _NUMBER_PATTERN.match(raw.strip()):
            number = float(raw.strip())
            if number.is_integer():
                return int(number)
        raise SettingsValueError(f"「{spec.label}」需要一个整数，收到 {raw!r}。")

    if spec.type == "float":
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str) and _NUMBER_PATTERN.match(raw.strip()):
            return float(raw.strip())
        raise SettingsValueError(f"「{spec.label}」需要一个小数，收到 {raw!r}。")

    if spec.type == "bytes":
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            parsed = _parse_size(raw)
            if parsed is not None:
                return parsed
        raise SettingsValueError(
            f"「{spec.label}」需要一个字节数，收到 {raw!r}（可写 214748364800 或 200GB）。"
        )

    if spec.type == "duration":
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            parsed = _parse_duration(raw)
            if parsed is not None:
                return parsed
        raise SettingsValueError(
            f"「{spec.label}」需要一个时长，收到 {raw!r}（可写 30、30s、5m、2h、1d）。"
        )

    if spec.type == "enum":
        if isinstance(raw, str) and spec.values and raw in spec.values:
            return raw
        allowed = " / ".join(spec.values or ())
        raise SettingsValueError(f"「{spec.label}」只能是 {allowed} 之一，收到 {raw!r}。")

    if isinstance(raw, str):
        return raw
    raise SettingsValueError(f"「{spec.label}」需要一段文字，收到 {raw!r}。")


def _check_shape(spec: SettingSpec, value: Any) -> tuple[Any | None, str | None]:
    """校验与依赖无关的部分：类型、range、enum 取值。

    Returns:
        ``(规范值, 拒绝理由)``；通过时理由为 ``None``。
    """
    try:
        coerced = coerce_value(spec, value)
    except SettingsValueError as exc:
        return None, str(exc)

    low, high = spec.range if spec.range else (None, None)
    if low is not None and coerced < low:
        return coerced, _range_reason(spec, coerced, low, high)
    if high is not None and coerced > high:
        return coerced, _range_reason(spec, coerced, low, high)
    return coerced, None


def validate_value(
    spec: SettingSpec,
    value: Any,
    *,
    registry: Mapping[str, SettingSpec] | None = None,
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按注册项校验一个值：类型、range、enum 取值、requires 依赖链。

    Args:
        spec: 该设置项的声明。
        value: 待校验的原始值（允许是环境变量那种字符串形式）。
        registry: 全部注册项；给出后会把 ``requires`` 里的未知依赖当作未满足。
        values: 当前（或即将生效的）其它设置项取值，用于判断 ``requires`` 是否满足。

    Returns:
        ``{"ok": bool, "reason": str, "warning": str | None, "value": Any}``。
        ``reason`` 是给人看的拒绝理由，``warning`` 是「能存但要知情」的提示
        （危险项、需重启、需重算、需先 dry-run）。越界值必须在保存前被拦住
        （「输入 999999」这类错误不该运行到一半才崩）。
    """
    coerced, reason = _check_shape(spec, value)
    if reason is not None:
        return {"ok": False, "reason": reason, "warning": None, "value": coerced}

    unmet = _unmet_requires(spec, registry, values)
    if unmet:
        return {
            "ok": False,
            "reason": (
                f"「{spec.label}」依赖的设置项尚未满足：{', '.join(unmet)}。"
                "先处理依赖项，否则会出现无效组合（例如开了外发却没登记服务商）。"
            ),
            "warning": None,
            "value": coerced,
        }

    return {"ok": True, "reason": "", "warning": _warning_text(spec), "value": coerced}


def _range_reason(spec: SettingSpec, value: Any, low: Any, high: Any) -> str:
    if low is not None and high is not None:
        bound = f"在 {low} ~ {high} 之间"
    elif low is not None:
        bound = f"不小于 {low}"
    else:
        bound = f"不大于 {high}"
    unit = f"（单位：{spec.unit}）" if spec.unit else ""
    return f"「{spec.label}」必须{bound}{unit}，收到 {value}。"


def _unmet_requires(
    spec: SettingSpec,
    registry: Mapping[str, SettingSpec] | None,
    values: Mapping[str, Any] | None,
) -> list[str]:
    """返回未满足的依赖项。

    依赖可能指向另一个设置项（例如 ``ai.involvement``），也可能指向一个
    「状态标志」（例如阶段 2 的 ``ai.provider_policy_registered``）。两种情况
    都按同一规则处理：**拿不到值就当未满足**（fail closed）。
    """
    unmet: list[str] = []
    for dep in spec.requires:
        if values is not None and dep in values and _truthy(values[dep]):
            continue
        if registry is not None and dep not in registry:
            unmet.append(f"{dep}（注册表里没有这一项，可能尚未实现）")
        else:
            unmet.append(dep)
    return unmet


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
    if isinstance(value, (Mapping, Sequence)):
        return len(value) > 0
    return bool(value)


def _warning_text(spec: SettingSpec) -> str | None:
    """把「能存但要知情」的事项拼成一句提示（DESIGN §6.2.1 的四个字段）。"""
    parts: list[str] = []
    if spec.dangerous:
        parts.append("危险项：保存需要二次确认，并记入审计")
    if spec.dry_run_first:
        parts.append("会改动已有数据：保存前必须先跑一次 dry-run 预览")
    if spec.recompute:
        parts.append(f"保存后需要重算：{spec.recompute}（系统会先给出影响预估）")
    if spec.restart_required:
        parts.append("需要重启服务后生效")
    if not parts:
        return None
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 变更计划（写入前的闸门）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingChangePlan:
    """一次设置变更的结论：能否写入、写入什么、要不要人再确认一次。"""

    key: str
    ok: bool
    value: Any = None
    reason: str = ""
    warnings: tuple[str, ...] = ()
    requires_confirmation: bool = False
    requires_precheck: bool = False

    @property
    def blocked(self) -> bool:
        """是否还需要人补一个动作（确认或预览）才能继续。"""
        return self.requires_confirmation or self.requires_precheck


def plan_setting_change(
    spec: SettingSpec,
    raw_value: Any,
    *,
    registry: Mapping[str, SettingSpec] | None = None,
    values: Mapping[str, Any] | None = None,
    confirmed: bool = False,
    precheck: Mapping[str, Any] | None = None,
) -> SettingChangePlan:
    """决定「这次改动能不能落库」，把界面/CLI/脚本共用的闸门收在一处。

    规则（DESIGN §6.2.2）：

    * 类型、范围、依赖不合法 -> 直接拒绝，附人话理由；
    * ``dry_run_first`` 的项 -> 没带 ``precheck``（dry-run 预览结果）就拒绝，
      因为这类项「设得比当前占用量小会立刻触发一轮清理」；
    * ``dangerous`` 的项 -> 没有 ``confirmed=True`` 就拒绝，只回一句「要确认什么」。
    """
    result = validate_value(spec, raw_value, registry=registry, values=values)
    warnings = tuple(w for w in (result["warning"],) if w)
    if not result["ok"]:
        return SettingChangePlan(key=spec.key, ok=False, reason=result["reason"], warnings=warnings)

    value = result["value"]
    if spec.dry_run_first and not precheck:
        return SettingChangePlan(
            key=spec.key,
            ok=False,
            value=value,
            reason=(
                f"「{spec.label}」会改动已有数据，必须先跑一次 dry-run 预览并把报告带进来"
                "（界面会先展示影响范围，确认后才写入）。"
            ),
            warnings=warnings,
            requires_precheck=True,
        )

    if spec.dangerous and not confirmed:
        return SettingChangePlan(
            key=spec.key,
            ok=False,
            value=value,
            reason=(
                f"「{spec.label}」是危险项，需要明确确认后才能保存"
                f"（影响：{spec.impact}）。"
            ),
            warnings=warnings,
            requires_confirmation=True,
        )

    return SettingChangePlan(key=spec.key, ok=True, value=value, warnings=warnings)


# ---------------------------------------------------------------------------
# 分层解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """一个生效值及其来源；``source`` 回答「这个值从哪来」。"""

    key: str
    value: Any
    source: SettingsSource
    spec: SettingSpec | None = None

    def explain(self) -> str:
        """输出一句人话解释，供 ``pc config show --explain`` 使用。"""
        return describe_source(self.key, self.source)

    def humanize(self) -> str:
        """把生效值渲染成给人看的形式。"""
        if self.spec is None:
            return str(self.value)
        return self.spec.humanize(self.value)


def resolve(
    key: str,
    *,
    registry: Mapping[str, SettingSpec] | None = None,
    yaml_defaults: Mapping[str, Any] | None = None,
    db_values: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedSetting:
    """解析一个键的生效值与来源，优先级 env > db > yaml > default。

    ``db_values`` 可以是普通映射，也可以是 ``pc.adapters.sqlite.SettingRow``
    这类带 ``value`` 属性的行对象（``setting`` 表读出来的就是后者）。
    ``env`` 传**环境变量本身**（如 ``os.environ``），本函数负责把
    ``PC__DEDUP__NEAR_DUPLICATE_DISTANCE`` 这类名字映射回设置项键名。

    Raises:
        SettingsRegistryError: 给了注册表，但键不在注册表里。
        SettingsValueError: 生效值不合法（提示里带来源，便于定位是谁写坏的）。
        ConfigError: 该键既没有注册表默认值，也没有任何来源提供值。
    """
    spec = registry.get(key) if registry is not None else None
    if registry is not None and spec is None:
        raise SettingsRegistryError(
            f"未知设置项：{key}。它不在 config/settings-registry.yaml 里；"
            "新增设置项请先在注册表加一段声明，界面与校验会自动出现该项。"
        )

    raw_env = env_overrides_by_key(env, registry=registry)[0].get(key)
    if raw_env is not None:
        return ResolvedSetting(
            key,
            _coerce_from(spec, raw_env, where=f"环境变量 {env_override_key(key)}"),
            SettingsSource.ENV,
            spec,
        )

    if db_values is not None and key in db_values:
        return ResolvedSetting(
            key,
            _coerce_from(spec, _row_value(db_values[key]), where="数据库 setting 表"),
            SettingsSource.DB,
            spec,
        )

    if yaml_defaults is not None and key in yaml_defaults:
        return ResolvedSetting(
            key,
            _coerce_from(spec, yaml_defaults[key], where=f"{DEFAULT_APP_CONFIG_PATH}:defaults.{key}"),
            SettingsSource.YAML,
            spec,
        )

    if spec is not None:
        return ResolvedSetting(key, spec.default, SettingsSource.DEFAULT, spec)

    raise ConfigError(
        f"设置项 {key} 没有注册表默认值，也没有 YAML / 数据库 / 环境变量提供值，无法解析。"
    )


def _coerce_from(spec: SettingSpec | None, raw: Any, *, where: str) -> Any:
    if spec is None:
        return raw
    try:
        return coerce_value(spec, raw)
    except SettingsValueError as exc:
        raise SettingsValueError(f"{where} 给出的值不合法：{exc}") from exc


def _row_value(raw: Any) -> Any:
    """从 ``SettingRow`` 这类行对象里取值；普通值原样返回。"""
    if isinstance(raw, (str, bytes, Mapping, Sequence, int, float, bool)):
        return raw
    return getattr(raw, "value", raw)


def describe_source(key: str, source: SettingsSource) -> str:
    """把来源枚举翻译成一句人话，回答「这个值为什么是这样」。"""
    if source is SettingsSource.ENV:
        return f"环境变量 {env_override_key(key)}（优先级最高，本次运行有效）"
    if source is SettingsSource.DB:
        return "数据库 setting 表（此项由设置页管理，改 config/app.yaml 不再影响它）"
    if source is SettingsSource.YAML:
        return f"{DEFAULT_APP_CONFIG_PATH.as_posix()} 的 defaults 段（仅对未播种的实例生效）"
    return f"注册表默认值（{DEFAULT_REGISTRY_PATH.as_posix()} 的 default）"


# ---------------------------------------------------------------------------
# 不可变快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """一份不可变配置快照，供单个任务或单次请求全程使用。

    配置以快照形式注入，而不是全局可变单例：避免一次任务内配置中途变化
    导致状态不一致（DESIGN §6.2.2）。``revision`` 对应设置页发布的那条
    单调递增版本号（取 ``setting_change`` 的最大编号），用于判断
    「我这份快照是不是旧的」。

    ``env_warnings`` 与 ``orphans`` 是刻意保留的两个「不对劲」清单：拼错的
    环境变量、注册表里已经删掉但数据库里还留着的键——它们正是「改了没生效」
    的两个主要来源，必须能被看见。
    """

    values: Mapping[str, Any]
    sources: Mapping[str, SettingsSource]
    specs: Mapping[str, SettingSpec] = field(default_factory=dict)
    env_warnings: tuple[str, ...] = ()
    orphans: tuple[str, ...] = ()
    revision: int = 0
    schema_version: int = REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        object.__setattr__(self, "specs", MappingProxyType(dict(self.specs)))

    def get(self, key: str, default: Any = None) -> Any:
        """取生效值；缺失时返回 ``default``。"""
        return self.values.get(key, default)

    def source_of(self, key: str) -> SettingsSource | None:
        """取某个键的来源。"""
        return self.sources.get(key)

    def explain(self, key: str) -> str:
        """一句人话解释某个键的来源；未知键返回提示而不是抛异常。"""
        source = self.sources.get(key)
        if source is None:
            return f"未知设置项：{key}"
        return describe_source(key, source)

    def as_dict(self) -> dict[str, Any]:
        """展开成可直接 ``json.dumps`` 的结构（供 ``pc config show --json``）。"""
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "settings": {
                key: {
                    "value": value,
                    "value_text": (
                        self.specs[key].humanize(value) if key in self.specs else str(value)
                    ),
                    "source": str(self.sources.get(key, SettingsSource.DEFAULT)),
                    "source_explain": self.explain(key),
                }
                for key, value in self.values.items()
            },
            "env_warnings": list(self.env_warnings),
            "orphans": list(self.orphans),
        }


def snapshot(
    *,
    registry: Mapping[str, SettingSpec] | None = None,
    yaml_defaults: Mapping[str, Any] | None = None,
    db_values: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    revision: int = 0,
) -> SettingsSnapshot:
    """构建一份不可变配置快照，供单个任务或单次请求全程使用。

    未给出 ``registry`` 时，快照只能包含 ``yaml_defaults`` 与 ``db_values``
    里出现过的键（没有默认值可依）；正常路径一定会给出注册表。
    """
    specs = dict(registry or {})
    _, env_warnings = env_overrides_by_key(env, registry=registry or None)

    keys: list[str] = list(specs)
    for extra in (yaml_defaults or {}, db_values or {}):
        keys.extend(key for key in extra if key not in specs and key not in keys)

    values: dict[str, Any] = {}
    sources: dict[str, SettingsSource] = {}
    for key in keys:
        resolved = resolve(
            key,
            # 注册表外的键（数据库里的孤儿行）不参与校验，否则一个历史遗留键
            # 会让整条 `pc config show` 直接报错——那正是需要被看见而不是被打断的情况。
            registry=registry if key in specs else None,
            yaml_defaults=yaml_defaults,
            db_values=db_values,
            env=env,
        )
        values[key] = resolved.value
        sources[key] = resolved.source

    orphans = tuple(
        sorted(key for key in (db_values or {}) if key not in specs)
    )
    return SettingsSnapshot(
        values=values,
        sources=sources,
        specs=specs,
        env_warnings=env_warnings,
        orphans=orphans,
        revision=revision,
        schema_version=REGISTRY_SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# 环境变量覆盖
# ---------------------------------------------------------------------------


def env_override_key(key: str) -> str:
    """把 ``dedup.near_duplicate_distance`` 映射为 ``PC__DEDUP__NEAR_DUPLICATE_DISTANCE``。"""
    return ENV_PREFIX + key.replace(".", ENV_SEPARATOR).upper()


def key_from_env(name: str) -> str | None:
    """把 ``PC__DEDUP__NEAR_DUPLICATE_DISTANCE`` 映射回 ``dedup.near_duplicate_distance``。"""
    if not name.startswith(ENV_PREFIX):
        return None
    body = name[len(ENV_PREFIX) :]
    if not body:
        return None
    return body.replace(ENV_SEPARATOR, ".").lower()


def env_overrides(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """收集当前进程中所有 ``PC__`` 环境变量覆盖（键名保持原始形式）。"""
    source = env if env is not None else os.environ
    return {k: v for k, v in source.items() if k.startswith(ENV_PREFIX)}


def env_overrides_by_key(
    env: Mapping[str, str] | None = None,
    *,
    registry: Mapping[str, SettingSpec] | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """把 ``PC__`` 环境变量映射成 ``设置项键 -> 原始字符串``。

    Returns:
        ``(mapped, warnings)``。``warnings`` 只收**可疑**的那些，且是可直接打印的
        整句：拼错的设置项会被静默忽略，而那正是「我明明设了却没生效」的头号原因，
        必须报出来。

    刻意不把「不属于设置项」的 ``PC__`` 变量都报成可疑：``PC__`` 前缀同时服务于
    L2/L3 配置键（``PC__DB__PATH``、``PC__PATHS__MEDIA_ROOT`` 等），它们由各自的
    配置段消费。判定规则是**看命名空间**：落在注册表已有的命名空间下、却不在注册表
    里 -> 大概率是拼错了，报出来；命名空间本身就不属于设置项 -> 安静跳过。
    """
    mapped: dict[str, str] = {}
    warnings: list[str] = []
    namespaces = {key.split(".", 1)[0] for key in registry} if registry is not None else set()

    for name, value in sorted(env_overrides(env).items()):
        key = key_from_env(name)
        if key is None:
            continue
        if registry is not None and key not in registry:
            warning = _env_warning(name, key, registry, namespaces)
            if warning:
                warnings.append(warning)
            continue
        mapped[key] = value
    return mapped, tuple(warnings)


def _env_warning(
    name: str, key: str, registry: Mapping[str, SettingSpec], namespaces: set[str]
) -> str | None:
    """判断一个 ``PC__`` 变量是否值得提醒；不值得就返回 ``None``。"""
    if "." not in key:
        return (
            f"环境变量 {name} 不是合法的配置键形态（应形如 PC__命名空间__项名），已忽略。"
        )
    namespace = key.split(".", 1)[0]
    if namespace not in namespaces:
        return None  # 属于别的配置层（db、paths、jobs 等），由各自的配置段消费
    hint = ""
    close = difflib.get_close_matches(key, list(registry), n=1)
    if close:
        hint = f"；是不是想写 {env_override_key(close[0])}？"
    return (
        f"环境变量 {name} 落在设置项命名空间 {namespace} 下，"
        f"但注册表里没有 {key} 这一项，已忽略{hint}"
    )


# ---------------------------------------------------------------------------
# 渲染与解析辅助
# ---------------------------------------------------------------------------


def humanize_value(spec: SettingSpec, value: Any) -> str:
    """把值渲染成给人看的形式：枚举用中文标签，字节与时长带单位。"""
    if spec.type == "bool":
        return "开" if value else "关"
    if spec.type == "enum" and spec.values and isinstance(value, str):
        return spec.value_labels.get(value, value)
    if spec.type == "bytes" and isinstance(value, int):
        return _format_size(value)
    if spec.type == "duration" and isinstance(value, int):
        return _format_duration(value)
    if spec.unit and isinstance(value, (int, float)):
        return f"{value} {spec.unit}"
    if spec.values and isinstance(value, str) and value in spec.values:
        return spec.value_labels.get(value, value)
    return str(value)


def _format_size(size: int) -> str:
    for unit, factor in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size and size % factor == 0:
            return f"{size // factor} {unit}"
    return f"{size} 字节"


def _format_duration(seconds: int) -> str:
    for unit, factor in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds and seconds % factor == 0:
            return f"{seconds // factor} {unit}"
    return f"{seconds} 秒"


def _parse_size(text: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", text)
    if not match:
        return None
    number, unit = match.group(1), match.group(2).lower()
    if unit and unit not in _BYTE_UNITS:
        return None
    return int(float(number) * _BYTE_UNITS.get(unit, 1))


def _parse_duration(text: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", text)
    if not match:
        return None
    number, unit = match.group(1), match.group(2).lower()
    if unit and unit not in _DURATION_UNITS:
        return None
    return int(float(number) * _DURATION_UNITS.get(unit, 1))


def _type_label(type_name: str) -> str:
    return {
        "bool": "开关（true / false）",
        "int": "整数",
        "float": "小数",
        "str": "文字",
        "enum": "选项",
        "bytes": "字节数",
        "duration": "时长",
    }.get(type_name, type_name)


def dump_json(payload: Mapping[str, Any]) -> str:
    """统一的 JSON 输出（不转义中文，便于人肉阅读与贴进 issue）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _pyyaml_available() -> bool:
    """判断 PyYAML 是否可用，用于给出「先 uv sync」这类可操作提示。"""
    import importlib.util

    return importlib.util.find_spec("yaml") is not None


__all__ = [
    "COST_KINDS",
    "DEFAULT_APP_CONFIG_PATH",
    "DEFAULT_REGISTRY_PATH",
    "ENV_PREFIX",
    "ENV_SEPARATOR",
    "KEY_PATTERN",
    "OPTIONAL_SPEC_FIELDS",
    "REGISTRY_SCHEMA_VERSION",
    "REQUIRED_SPEC_FIELDS",
    "SUPPORTED_TYPES",
    "AppConfigError",
    "ConfigError",
    "ResolvedSetting",
    "SettingChangePlan",
    "SettingSpec",
    "SettingsRegistryError",
    "SettingsSnapshot",
    "SettingsValueError",
    "app_default_warnings",
    "coerce_value",
    "describe_source",
    "dump_json",
    "env_override_key",
    "env_overrides",
    "env_overrides_by_key",
    "humanize_value",
    "key_from_env",
    "load_app_defaults",
    "load_registry",
    "plan_setting_change",
    "registry_index",
    "resolve",
    "setting_seeds",
    "snapshot",
    "validate_value",
]
