"""连接器工厂：把一条 ``source`` 行变成可用的 ``Connector``（DESIGN §1.3、§9.2）。

**这里是唯一一处"知道平台细节"的地方**，也是将来唯一需要解密凭证的地方：

* 来源配置（``source.config``，JSON）在这里解析；
* 凭证引用在 ``source.config`` 里只存**引用**而不是明文（DESIGN §6.1：主密钥进系统
  密钥库，密文进 ``credential_secret``），所以接入第一个平台型连接器时，解密只加在
  本模块，不会散落到业务代码里；
* 授权行（``source_authorization``）在这里交给连接器，连接器本身**不碰数据库**。

新增一个平台的步骤（写成清单是为了让"留够接口"这件事可核对）：

1. 在 ``pc/adapters/`` 下写一个类，实现 :class:`pc.ports.Connector` 的方法子集，
   并在 ``capabilities()`` 里如实声明 ``supports_pull`` / ``supports_push`` /
   ``supports_media_fetch``；
2. 在下面的 ``SUPPORTED_KINDS`` 里登记它的 ``kind``，并在 ``_build`` 里接上；
3. 不支持的接入方式（浏览器会话劫持、私有接口逆向、付费墙绕过）**不要实现**，
   这条写在 DESIGN §1.3 里，是设计决定而不是待办。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pc.adapters.local_files import LocalFilesConnector
from pc.ports.ingest import Connector

#: 已实现的来源类型 → 说明。``kind`` 的取值集合由 ``source.source_kind_chk`` 约束。
SUPPORTED_KINDS: dict[str, str] = {
    "export": "本地文件 / 官方导出目录（手工上传与导包导入共用同一实现）",
}

#: 尚未实现但设计上支持的来源类型 → 说明（报错时列出来，避免"这个 kind 到底支不支持"的猜测）
PLANNED_KINDS: dict[str, str] = {
    "bot_api": "平台监听型机器人：Telegram / Discord / QQ / 微信（DESIGN §9 阶段 2）",
    "webhook": "推送型回调：走 Connector.parse_event",
    "rss": "订阅源增量拉取",
    "user_api": "用户态 API（受平台条款约束，需逐案确认）",
}


class UnsupportedSourceKindError(RuntimeError):
    """``source.kind`` 还没有对应的实现（或根本不是合法取值）。"""


class SourceConfigError(RuntimeError):
    """``source.config`` 缺失、不是 JSON 对象，或缺了该类型必需的键。"""


def source_config(source: Mapping[str, Any]) -> dict[str, Any]:
    """解析 ``source.config``（数据库里是 TEXT + ``json_valid`` 校验）。

    空字符串当空对象处理——``source.config`` 的默认值就是 ``'{}'``，但它也可能被写成空串。
    """
    raw = source.get("config")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        raise SourceConfigError(f"source.config 必须是 JSON 字符串，实际是 {type(raw).__name__}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceConfigError(f"source.config 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise SourceConfigError("source.config 必须是 JSON 对象（'{...}'）")
    return parsed


def connector_for(
    source: Mapping[str, Any],
    authorizations: Sequence[Mapping[str, Any]] = (),
    *,
    roots: Sequence[Path | str] | None = None,
    batch_size: int | None = None,
    max_media_bytes: int | None = None,
    max_text_chars: int | None = None,
) -> Connector:
    """按 ``source.kind`` 构造连接器。

    Args:
        source: ``source`` 表的一行（映射）。
        authorizations: 该来源的 ``source_authorization`` 行；连接器用它回答 ``authorize()``。
        roots: 显式给出的目录（``pc import ./samples`` 走这条），优先于
            ``source.config["roots"]``。

    Raises:
        UnsupportedSourceKindError: ``kind`` 没有实现（消息里列出已支持与规划中的类型）。
        SourceConfigError: 配置缺失或不是合法 JSON 对象。
    """
    kind = str(source.get("kind") or "")
    if kind not in SUPPORTED_KINDS:
        planned = PLANNED_KINDS.get(kind)
        hint = f"「{kind}」在设计上属于：{planned}。" if planned else f"「{kind}」不是合法的 kind 取值。"
        raise UnsupportedSourceKindError(
            f"暂不支持这种来源类型：{hint}\n"
            f"  已实现：{'、'.join(sorted(SUPPORTED_KINDS))}\n"
            f"  设计上支持但尚未实现：{'、'.join(sorted(PLANNED_KINDS))}\n"
            "  新增适配器的步骤见 pc/adapters/registry.py 的模块文档。"
        )

    return _build(
        kind,
        source,
        authorizations,
        roots=roots,
        batch_size=batch_size,
        max_media_bytes=max_media_bytes,
        max_text_chars=max_text_chars,
    )


def _build(
    kind: str,
    source: Mapping[str, Any],
    authorizations: Sequence[Mapping[str, Any]],
    *,
    roots: Sequence[Path | str] | None,
    batch_size: int | None,
    max_media_bytes: int | None,
    max_text_chars: int | None,
) -> Connector:
    config = source_config(source)
    resolved_roots: list[Path | str] = list(roots or [])
    if not resolved_roots:
        from_config = config.get("roots")
        if isinstance(from_config, str):
            resolved_roots = [from_config]
        elif isinstance(from_config, list):
            resolved_roots = [str(item) for item in from_config]
    if not resolved_roots:
        raise SourceConfigError(
            f"来源「{source.get('name')}」没有可用的目录：在 source.config 里写 "
            '{"roots": ["D:/导出/2024"]}，或在命令行把路径传给 pc import。'
        )

    options: dict[str, Any] = {}
    if batch_size is not None:
        options["batch_size"] = batch_size
    if max_media_bytes is not None:
        options["max_media_bytes"] = max_media_bytes
    if max_text_chars is not None:
        options["max_text_chars"] = max_text_chars

    # kind 目前只有 'export'；分支写成显式映射，新增类型时必须在这里接上（漏接会报错，
    # 而不是静默退化成别的实现）。
    if kind == "export":
        return LocalFilesConnector(source, authorizations, roots=resolved_roots, **options)
    raise UnsupportedSourceKindError(  # pragma: no cover - SUPPORTED_KINDS 与 _build 必须同步
        f"{kind} 登记为已支持，但 _build 里没有对应分支（这是缺陷，不是配置问题）"
    )


__all__ = [
    "PLANNED_KINDS",
    "SUPPORTED_KINDS",
    "SourceConfigError",
    "UnsupportedSourceKindError",
    "connector_for",
    "source_config",
]
