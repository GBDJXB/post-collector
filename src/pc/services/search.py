"""检索业务规则：把用户意图翻成结构化过滤器，并守住可见性、分页与预算政策。

这一层做四件事，每件都有明确的"为什么在这里"：

1. **可见性政策**（DESIGN §3.3、§6.7）：默认只看 ``active`` 且 ``nsfw_level <= 0``（safe）、
   排除软删、按整帖签名折叠重复帖；查询与输出都只包含可见部件。放宽必须由调用方显式要求（``--status`` /
   ``--nsfw-level`` / ``--include-duplicates``）——"默认安全"不能靠调用方记得传参。
2. **把名字翻成 id**：``--source`` 与 ``--tag`` 接受名字，这里解析成 id；查不到时给出
   近似建议，而不是让 SQL 静默返回空结果。
3. **分页与页大小**：页大小来自设置项 ``search.default_page_size``（四层解析，
   env > db > yaml > default），并被 **50 的硬上限**截住——这个上限与 §5.6 的性能预算绑定，
   写死在 :data:`pc.adapters.fts_search.MAX_PAGE_SIZE`，不走设置。
4. **不返回精确总数**：关键词检索的 ``total`` 恒为 ``None``（§5.6 M1b），只有纯过滤列表
   才给按帖计数（标签估计或 5000 截断计数）。

引擎可替换：本层只把过滤器交给 :class:`pc.ports.store.SearchIndex`，不认识任何 FTS5 语法。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any

from pc import config as cfg
from pc.adapters.fts_search import (
    MAX_PAGE_SIZE,
    TIME_FIELD_INGEST,
    TIME_FIELDS,
    SearchQueryError,
    SqliteFtsSearch,
)
from pc.adapters.sqlite import read_settings, stale_search_row_count
from pc.adapters.tag_normalizer import SqliteTagNormalizer
from pc.domain import TagNamespace
from pc.services.sources import get_source, list_sources

__all__ = [
    "PAGE_SIZE_KEY",
    "SearchError",
    "SearchQuery",
    "default_page_size",
    "run_search",
    "stale_derived_count",
]

#: 每页条数的设置项键（上限与性能预算绑定，见 DESIGN §5.6）
PAGE_SIZE_KEY = "search.default_page_size"

#: 设置项读不到时的兜底页大小（与注册表默认值一致）
_FALLBACK_PAGE_SIZE = 40

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SearchError(RuntimeError):
    """检索请求本身不合法（来源/标签不存在、页大小越界、时间格式不对），消息面向人。"""


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """一次检索请求（CLI 与将来的 HTTP 端点共用的结构化形状）。"""

    q: str = ""
    source: str | None = None
    item_types: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    since: str | None = None
    until: str | None = None
    #: 时间字段：``ingest`` 看内容项第一次入库的时间（默认），``sent`` 看消息时间
    #: （``message.sent_at``）。手工上传的 ``sent_at`` 是空的——按 ``sent`` 过滤会排除它们。
    time_field: str = TIME_FIELD_INGEST
    nsfw_max_level: int = 0
    include_duplicates: bool = False
    sort: str | None = None
    limit: int | None = None
    cursor: str | None = None
    page: int = 1


def stale_derived_count(conn: Any) -> int:
    """还有多少行"有可检索文本、却还没分词"（升级后没重算过的老行）。"""
    return stale_search_row_count(conn)


def default_page_size(
    conn: Any,
    *,
    registry: Mapping[str, Any] | None = None,
    yaml_defaults: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """解析 ``search.default_page_size``（四层）并硬性截到 50。

    三种失败分得很清（注册表的异常是分层的，这里按层分别处置）：

    * 注册表里**没有这个键** → 报错（有人在注册表里删了它，不能装作没这回事）；
    * 值**不合法** → 报错（提示里带来源，便于定位是谁写坏的）；
    * 连注册表都拿不到（既没有注册表也没有任何一层提供值）→ 退回注册表默认值 40。
      这是**唯一**的降级路径：它对应"配置文件不在手边"（例如 `uv tool install`
      装的包暂时不带 config 目录），此时让检索彻底不可用比用 40 条更糟。
    """
    try:
        resolved = cfg.resolve(
            PAGE_SIZE_KEY,
            registry=registry,
            yaml_defaults=yaml_defaults,
            db_values=read_settings(conn),
            env=os.environ if env is None else env,
        )
    except cfg.SettingsRegistryError as exc:
        raise SearchError(str(exc)) from exc
    except cfg.SettingsValueError as exc:
        raise SearchError(str(exc)) from exc
    except cfg.ConfigError:
        return _FALLBACK_PAGE_SIZE
    resolved_limit = int(resolved.value)
    if resolved_limit > MAX_PAGE_SIZE:
        # 设置项本身有 range 校验，走到这里说明注册表被改宽了——仍然按预算上限截住
        return MAX_PAGE_SIZE
    return max(1, resolved_limit)


def run_search(
    conn: Any,
    query: SearchQuery,
    *,
    registry: Mapping[str, Any] | None = None,
    yaml_defaults: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    index: Any | None = None,
) -> dict[str, Any]:
    """执行一次检索，返回可直接渲染的结果字典（见 ``SearchIndex.query`` 的返回约定）。

    ``time_field="sent"`` 时 ``--since/--until`` 与时间排序都看 ``message.sent_at``；
    该列为空的行（手工上传刻意留空）会被时间范围排除——"没有消息时间"与"消息时间不在
    范围内"不是一回事，后者不该把前者混进来。

    Raises:
        SearchError: 来源/标签不存在、页大小或页码越界、时间字段非法、时间格式不合法、
            游标损坏或与当前时间字段不匹配。
    """
    if query.page < 1:
        raise SearchError(f"页码从 1 开始，收到 page={query.page}。")
    if query.time_field not in TIME_FIELDS:
        raise SearchError(
            f"时间字段只能是 {' 或 '.join(TIME_FIELDS)}，收到 {query.time_field!r}"
            "（ingest=入库时间，sent=收到消息的时间）。"
        )
    if not 0 <= query.nsfw_max_level <= 4:
        raise SearchError(
            f"nsfw_level 只能是 0~4，收到 {query.nsfw_max_level}（0=safe 默认，4=restricted）。"
        )
    size = query.limit if query.limit else default_page_size(
        conn, registry=registry, yaml_defaults=yaml_defaults, env=env
    )
    if size < 1:
        raise SearchError(f"每页条数至少 1，收到 {size}。")
    if size > MAX_PAGE_SIZE:
        raise SearchError(
            f"每页最多 {MAX_PAGE_SIZE} 条（收到 {size}）。这个上限与检索性能预算绑定"
            "（DESIGN §5.6），不是可以调大的参数；要看更多就翻页。"
        )

    filters: dict[str, Any] = {
        "statuses": tuple(query.statuses or ("active",)),
        "nsfw_max_level": int(query.nsfw_max_level),
        "item_types": tuple(query.item_types),
        "time_field": query.time_field,
        "since": _normalize_time(query.since, end=False),
        "until": _normalize_time(query.until, end=True),
        "fold_duplicates": not query.include_duplicates,
    }
    if query.source:
        filters["source_id"] = _resolve_source(conn, query.source)
    if query.tags:
        filters["tag_ids"] = _resolve_tags(conn, query.tags)

    engine = index if index is not None else SqliteFtsSearch(conn)
    try:
        result = engine.query(
            query.q,
            filters,
            query.sort,
            query.page,
            limit=size,
            cursor=query.cursor,
        )
    except SearchQueryError as exc:
        raise SearchError(str(exc)) from exc
    result["filters_applied"] = {
        key: value for key, value in filters.items() if value not in (None, (), False)
    }
    result["filters_applied"]["fold_duplicates"] = filters["fold_duplicates"]
    result.update(result_type="search", result_version=1)
    return result


def _resolve_source(conn: Any, ref: str) -> int:
    source = get_source(conn, ref)
    if source is None:
        known = [row["name"] for row in list_sources(conn)]
        hint = f"现有来源：{'、'.join(known)}" if known else "还没有任何来源"
        raise SearchError(f"找不到来源「{ref}」。{hint}（`pc source list` 可查）")
    return int(source["source_id"])


def _resolve_tags(conn: Any, refs: Sequence[str]) -> list[int]:
    """把 ``name`` 或 ``namespace:name`` 解析成 tag_id；查不到时给近似建议。"""
    ids: list[int] = []
    for ref in refs:
        namespace, separator, name = ref.partition(":")
        if not separator or namespace not in {n.value for n in TagNamespace}:
            namespace, name = "", ref
        try:
            row = SqliteTagNormalizer(conn).resolve(name if namespace else ref, namespace or None)
        except ValueError as exc:
            raise SearchError(str(exc)) from exc
        rows = [row] if row is not None else []
        if not rows:
            known = [row[0] for row in conn.execute("SELECT name FROM tag ORDER BY name")]
            near = get_close_matches(name or ref, known, n=3, cutoff=0.6)
            hint = f"最接近的是：{'、'.join(near)}" if near else "现有标签：无" if not known else ""
            raise SearchError(f"找不到标签「{ref}」。{hint}".rstrip())
        ids.append(int(rows[0]["tag_id"]))
    return ids


def _normalize_time(value: str | None, *, end: bool) -> str | None:
    """接受 ``YYYY-MM-DD``（自动补全当天边界）或完整 ISO-8601；其它一律报错。"""
    if not value:
        return None
    text = value.strip()
    if _DATE_ONLY.match(text):
        return f"{text}T23:59:59.999Z" if end else f"{text}T00:00:00.000Z"
    if "T" not in text:
        raise SearchError(
            f"时间格式不对：{value!r}。用 2026-09-30 或 2026-09-30T12:00:00Z 这样的写法。"
        )
    return text
