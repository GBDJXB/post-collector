"""详情展示的业务规则，系统操作与文件生成由适配器实现。"""

from __future__ import annotations

from typing import Any

from pc.adapters.content_detail import read_detail


class ViewError(ValueError):
    """目标或展示操作不可用。"""


def detail(conn: Any, *, message_id: int | None, content_id: int | None) -> dict[str, Any]:
    try:
        return read_detail(conn, message_id=message_id, content_id=content_id)
    except ValueError as exc:
        raise ViewError(str(exc)) from exc


def content_kind(result: dict[str, Any]) -> str:
    text = False
    images = False
    for part in result["parts"]:
        if part["item_type"] not in {"text", "image"}:
            raise ViewError(f"暂不支持展示内容类型：{part['item_type']}")
        # 图片部件的原始说明正文也属于混合内容。
        text |= part["item_type"] == "text" or bool(part["text"])
        images |= part["item_type"] == "image" or bool(part["media"])
    return "mixed" if text and images else "image" if images else "text"


def preflight(result: dict[str, Any], *, copy: bool) -> str:
    kind = content_kind(result)
    if copy and kind == "mixed":
        raise ViewError("混合内容暂不支持 --copy，请分别选择文本或图片。")
    return kind


def text_body(result: dict[str, Any]) -> str:
    return "\n\n".join(part["text"] for part in result["parts"] if part["text"])
