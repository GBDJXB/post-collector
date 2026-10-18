"""查询结果的终端渲染：只消费业务数据，使用标准库。"""

from __future__ import annotations

import shutil
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

ELLIPSIS = "…"

#: 不占格的 Unicode 类别：Mn/Me 是组合记号与变体选择符，Cf 是零宽连接符与零宽空格
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cf"})

#: `pc item list` 的表头与列宽上限（显示格）。**每一列都必须有上限**：
#: tests/test_cli_table.py 有一条断言盯着，新增列却忘了给上限会直接红。
ITEM_LIST_HEADERS: tuple[str, ...] = (
    "id",
    "message_id",
    "类型",
    "状态",
    "来源",
    "内容",
    "扫描",
    "媒体",
    "备注",
    "建库时间",
)
ITEM_LIST_MAX_WIDTHS: dict[str, int] = {
    "message_id": 10,
    "id": 8,  # content_id 增长很慢
    "类型": 8,  # item_type ∈ text/image/video/audio/file
    "状态": 12,  # content_item.status 最长 quarantined（11）
    "来源": 16,  # 用户命名；默认来源 manual 只有 6
    "内容": 36,  # 文本摘要或媒体摘要；中文下 36 格＝18 个字
    "扫描": 12,  # media.scan_status ∈ clean/suspicious
    "媒体": 6,  # 媒体引用计数
    "备注": 24,  # 来源备注（人写的自由文本）
    "建库时间": 24,  # ISO-8601 UTC 毫秒固定 24 格，永不截断
}

#: `pc search` 的表头与列宽上限
SEARCH_HEADERS: tuple[str, ...] = (
    "id",
    "类型",
    "来源",
    "文件名",
    "命中",
    "内容",
    "标签",
    "备注",
    "时间",
)
SEARCH_MAX_WIDTHS: dict[str, int] = {
    "id": 8,
    "类型": 20,  # 可见部件的帖构成（例如 文本 + 图片）
    "来源": 16,
    "文件名": 32,  # basename；媒体项才有（文本项这一刀留空）
    "命中": 24,  # 命中的词，多个用空格隔开
    "内容": 40,  # 命中片段（带 [] 标记）或正文摘要
    "标签": 20,  # 标签名列表（置信度与来源在 --json 里）
    "备注": 20,  # 来源备注（人写的自由文本）
    "时间": 24,  # ISO-8601 UTC 毫秒固定 24 格
}

#: `pc source list` 的表头与列宽上限
SOURCE_LIST_HEADERS: tuple[str, ...] = (
    "id",
    "名称",
    "平台",
    "kind",
    "信任",
    "状态",
    "授权",
    "方式",
    "软删",
)
SOURCE_LIST_MAX_WIDTHS: dict[str, int] = {
    "id": 8,
    "名称": 20,
    "平台": 12,
    "kind": 10,  # bot_api/webhook/rss/export/user_api/manual
    "信任": 4,
    "状态": 6,  # 启用 / 停用
    "授权": 20,  # 最长"授权已撤销或已过期"（18 格）
    "方式": 24,  # 两个授权方式拼接：manual_export、root_self 恰好 24 格
    "软删": 24,  # ISO-8601 UTC 毫秒，或 "-"
}


def _display_width(text: str) -> int:
    """这个字符串在终端里占几格。

    - East Asian **W**（汉字、假名、谚文）与 **F**（全角）算 2 格；
    - **Mn / Me**（组合记号、变体选择符）与 **Cf**（零宽连接符、零宽空格）算 0 格；
      注意 ``unicodedata.combining()`` 靠不住——U+FE0F 这类变体选择符的规范组合类是 0，
      但仍然不占格，所以按**类别**判断；
    - 其余算 1 格，**包括歧义宽度 ``A``**（``…``、``·``、``—``、``“``）。

    歧义宽度按 1 处理是唯一一处主观判断，选择与 ``wcwidth`` / ``rich`` 的默认一致：
    如果某个终端把 `…` 渲染成 2 格，那么所有 Python 表格工具都会同样错位，所以更可能
    是 1 格。中文正文是 ``W``（无歧义），不受这个判断影响。
    """
    total = 0
    for char in text:
        if unicodedata.category(char) in _ZERO_WIDTH_CATEGORIES:
            continue
        total += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return total


def _truncate_display(text: str, limit: int) -> str:
    """按**显示宽度**截断到 ``limit`` 格，超出的部分换成 ``ELLIPSIS``。

    宽度是逐字符累加的，所以宽字符**不会被切成两半**（放不下就整字符停下）。
    代价是截断点可能落在一个组合序列中间（``e`` + 组合重音会只剩 ``e``），这是可接受
    的显示瑕疵，不值得为此引入字素簇库。
    """
    if limit < 2:
        raise ValueError(f"列宽上限至少要有 2 格，收到 {limit}")
    if _display_width(text) <= limit:
        return text
    budget = limit - _display_width(ELLIPSIS)
    kept: list[str] = []
    used = 0
    for char in text:
        width = _display_width(char)
        if used + width > budget:
            break
        kept.append(char)
        used += width
    return "".join(kept) + ELLIPSIS


def _pad_display(text: str, width: int) -> str:
    """按显示宽度右侧补空格到 ``width`` 格（已经超宽时原样返回，不截断）。"""
    return text + " " * max(0, width - _display_width(text))


def _flatten_cell(cell: Any) -> str:
    """单元格 → 单行字符串：``None`` 当空串，控制字符（换行、制表符）换成空格。

    换行必须在排版前处理掉，否则一个多行备注会把整张表撕成两半。
    """
    if cell is None:
        return ""
    text = str(cell)
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)


def _prepare_row(
    row: Sequence[Any], headers: Sequence[str], limits: Mapping[str, int]
) -> list[str]:
    """按表头取单元格、拍平成单行、按列上限截断。行比表头短则补空，长则忽略多余的。"""
    prepared: list[str] = []
    for index, header in enumerate(headers):
        text = _flatten_cell(row[index] if index < len(row) else None)
        limit = limits.get(header)
        prepared.append(_truncate_display(text, limit) if limit else text)
    return prepared


def _print_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    max_widths: Mapping[str, int] | None = None,
    terminal_width: int | None = None,
) -> None:
    """等宽列打印；不用第三方表格库，免得为了排版多一个依赖。

    ``max_widths`` 按**列名**给每列的上限（显示格）：列顺序调整了也不会错配，漏给某列
    则那一列无上限。单元格先按上限截断、再算列宽，因此无论内容多长，行宽都有确定上界。

    **保留行尾填充**（不做 rstrip）：补到定宽之后，"每行显示宽度严格相等"就是列对齐的
    等价判据，测试可以直接断言整行宽度，而不必按列切分字符串。
    """
    limits = dict(max_widths or {})
    cells = [_prepare_row(row, headers, limits) for row in rows]

    widths: list[int] = []
    for index, header in enumerate(headers):
        column = [cells[row][index] for row in range(len(cells))] or [header]
        natural = max(_display_width(item) for item in [header, *column])
        limit = limits.get(header)
        widths.append(min(limit, natural) if limit else natural)

    headers = list(headers)
    if terminal_width is not None:
        available = max(12, terminal_width)
        while len(widths) > 2 and sum(widths) + 2 * len(widths) > available:
            headers.pop()
            widths.pop()
            for row in cells:
                row.pop()
        while sum(widths) + 2 * len(widths) > available and max(widths) > 2:
            i = max(range(len(widths)), key=widths.__getitem__)
            widths[i] -= 1
        cells = [
            [
                _truncate_display(cell, widths[i]) if widths[i] >= 2 else cell
                for i, cell in enumerate(row)
            ]
            for row in cells
        ]
    print(
        "  "
        + "  ".join(
            _pad_display(
                _truncate_display(header, widths[i]) if widths[i] >= 2 else header, widths[i]
            )
            for i, header in enumerate(headers)
        )
    )
    print("  " + "  ".join("-" * width for width in widths))
    for row in cells:
        print("  " + "  ".join(_pad_display(cell, widths[i]) for i, cell in enumerate(row)))


def _short_text(text: Any, limit: int = 60) -> str:
    """没有命中片段时给一行正文摘要（表格自己还会按显示宽度截断）。"""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _media_summary(media: Mapping[str, Any]) -> str:
    """媒体项的描述：宽高与缩略图有无（缩略图本切片还不生成，只能如实说）。"""
    if not media or not media.get("count"):
        return "（空）"
    if "width" not in media:
        return f"媒体 x{media['count']}"
    width, height = media.get("width"), media.get("height")
    size = f"{width}x{height}" if width and height else "尺寸未知"
    thumb = "有缩略图" if media.get("thumb_key") else "无缩略图"
    return f"媒体 x{media['count']}（{size}，{thumb}）"


def _print_search_timings(timings: Mapping[str, float], *, show: bool) -> None:
    """打印各段耗时（DESIGN §5.6 M6）。

    ``--timings`` 时逐段列出；总耗时超过 200 ms 时无论有没有这个开关都提示一行——
    慢查询不该只在"你恰好加了参数"的时候才被发现。
    """
    if show:
        parts = "  ".join(
            f"{stage}={timings[stage]:.1f}ms"
            for stage in ("candidates", "filter", "count", "main", "tags", "media", "total")
            if stage in timings
        )
        print(f"  耗时：{parts}")
    if timings.get("total", 0) > 200:
        print(
            f"  · 慢查询：这一段用了 {timings['total']:.0f} ms（预算 P99 < 500 ms，见 DESIGN §5.6）"
        )


def render_result(
    result: Mapping[str, Any], *, width: int | None = None, timings: bool = False
) -> int:
    """默认终端宽度由 TTY 决定，重定向使用 120 格。"""
    if type(result.get("result_version")) is not int or result["result_version"] != 1:
        raise ValueError("不支持的结果版本")
    kind = result.get("result_type")
    schemas = {
        "tags": ("tags", ("tag_id", "name", "namespace", "status", "usage_count", "aliases")),
        "assignments": (
            "assignments",
            (
                "assign_id",
                "name",
                "message_id",
                "content_id",
                "namespace",
                "source",
                "confidence",
                "revoked_at",
            ),
        ),
        "composition_assignments": ("assignments", ("assign_id", "composition_id", "name", "namespace", "revoked_at")),
        "aliases": ("aliases", ("synonym_id", "alias", "name", "namespace")),
    }
    width = (
        width
        if width is not None
        else (shutil.get_terminal_size((120, 24)).columns if sys.stdout.isatty() else 120)
    )
    if kind == "search":
        if not isinstance(result.get("hits"), list):
            raise ValueError("检索结果缺少 hits 数组")
        required = {
            "message_id",
            "composition",
            "source",
            "original_name",
            "matched_terms",
            "snippet",
            "text",
            "media",
            "tags",
            "origin_note",
            "sent_at",
            "first_seen_at",
        }
        for hit in result["hits"]:
            if not isinstance(hit, dict) or not required.issubset(hit):
                raise ValueError("命中记录结构不完整")
            if (
                not isinstance(hit["tags"], list)
                or any(not isinstance(t, dict) or "name" not in t for t in hit["tags"])
                or not isinstance(hit["media"], dict)
                or not isinstance(hit["matched_terms"], list)
                or any(not isinstance(t, str) for t in hit["matched_terms"])
            ):
                raise ValueError("命中记录标签、媒体或命中词结构错误")
        if result["hits"]:
            if not {
                "total",
                "sort",
                "candidates",
                "incomplete",
                "next_cursor",
                "has_more",
                "timings",
            }.issubset(result):
                raise ValueError("检索结果分页结构不完整")
            if not isinstance(result["timings"], dict) or any(
                not isinstance(t, (int, float)) for t in result["timings"].values()
            ):
                raise ValueError("检索耗时结构错误")
        if not result["hits"]:
            print("  没有匹配的内容。")
            print("  中文 1~2 字走词级索引，3 字以上走子串索引；也可按文件名或标签查询。")
            return 0
        return _render_search(result, width, timings)
    if kind not in schemas:
        raise ValueError("不支持的结果类型")
    key, fields = schemas[kind]
    rows = result.get(key)
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError(f"结果需要 {key} 对象数组")
    if any(not set(fields).issubset(r) for r in rows):
        raise ValueError("记录缺少展示所需字段")
    if not rows:
        print("  没有记录。")
        return 0
    headers = tuple(fields)
    limits = {f: (24 if f in {"name", "alias", "aliases"} else 16) for f in fields}
    table = [
        [
            (" ".join(str(v) for v in r[f]) if isinstance(r.get(f), list) else r.get(f))
            for f in fields
        ]
        for r in rows
    ]
    _print_table(headers, table, max_widths=limits, terminal_width=width)
    print(f"  共 {len(rows)} 条记录")
    return 0


def _render_search(result: Mapping[str, Any], width: int, timings: bool) -> int:
    table = [
        [
            hit["message_id"],
            hit["composition"],
            hit["source"],
            hit["original_name"] or "-",
            " ".join(hit["matched_terms"]) or "-",
            hit["snippet"] or _short_text(hit["text"]) or _media_summary(hit["media"]),
            " ".join(tag["name"] for tag in hit["tags"]) or "-",
            hit["origin_note"] or "-",
            # 「时间」列显示**当前所选字段**的值：按消息时间搜时，显示入库时间会让人对不上
            (hit["sent_at"] if result.get("time_field") == "sent" else hit["first_seen_at"]) or "-",
        ]
        for hit in result["hits"]
    ]
    _print_table(SEARCH_HEADERS, table, max_widths=SEARCH_MAX_WIDTHS, terminal_width=width)
    if result["total"] is not None:
        shown = len(result["hits"])
        kind = result.get("total_kind") or "exact"
        if kind == "exact":
            head = f"  共 {result['total']} 条命中"
        elif kind == "at_least":
            head = (
                f"  至少 {result['total']} 条命中（超过 {result['total']} 条就不再数下去，"
                "精确总数对翻页没有用；§5.6 M1b）"
            )
        else:
            head = f"  约 {result['total']} 条命中（按标签用量估算，不是精确计数）"
        print(f"{head}，上面显示 {shown} 条（每页上限 50）")
    else:
        order_text = "时间倒序" if result["sort"] == "time" else "相关度排序"
        print(
            f"  候选 {result['candidates']} 条（{order_text}；关键词检索不统计精确总数，"
            f"这是第 {result.get('page', 1)} 页）"
        )
        if result["incomplete"]:
            print("  注意：候选已扫完仍未凑满一页，结果可能不完整（放宽条件或换更常见的词）。")
    if result["next_cursor"]:
        print(f"  下一页：pc search … --cursor {result['next_cursor']}")
    elif result["has_more"] and result["total"] is None:
        print(f"  下一页：pc search … --page {int(result.get('page', 1)) + 1}")
    if result.get("time_field") == "sent":
        print(
            "  时间用的是「收到消息时间」（sent_at；「时间」列与排序都按它）。"
            "手工上传的内容 sent_at 为空，会被时间范围排除、排在最后。"
        )
    _print_search_timings(result["timings"], show=timings)
    return 0
