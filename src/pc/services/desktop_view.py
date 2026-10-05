"""CLI 与本机 Web 共用的查看操作，媒体准备完成后调用系统适配器。"""

from pathlib import Path
from typing import Any

from pc.adapters import view_files, windows_view
from pc.services import viewing


class DesktopError(viewing.ViewError):
    """系统操作失败，保留复制是否已经完成。"""

    def __init__(self, message: str, *, copied: bool = False):
        super().__init__(message)
        self.copied = copied


def execute(
    conn: Any,
    *,
    message_id: int | None,
    content_id: int | None,
    action: str,
    copy: bool,
    cache_root: Path,
    limit: int,
) -> dict[str, Any]:
    if action not in {"show", "open", "reveal", "copy"}:
        raise viewing.ViewError("未知查看操作")
    copy = copy or action == "copy"
    result = viewing.detail(conn, message_id=message_id, content_id=content_id)
    kind = viewing.preflight(result, copy=copy)
    if action == "show" and kind != "text":
        raise viewing.ViewError("终端图片展示尚未通过 DST 集成验收；show 当前支持纯文本与 --json。")
    if action != "show" or copy:
        windows_view.require_windows()
    body = viewing.text_body(result)
    if copy and kind == "text" and "\0" in body:
        raise viewing.ViewError("正文含 NUL，无法完整复制到剪贴板")
    files = None
    if action in {"open", "reveal"} or (copy and kind == "image"):
        images = view_files.read_images(conn, result, limit=limit)
        files = view_files.materialize(result, images, cache_root=cache_root, kind=kind)
    if viewing.detail(conn, message_id=message_id, content_id=content_id) != result:
        raise viewing.ViewError("目标在准备期间发生变化，请重新执行")
    copied = False
    try:
        if copy:
            if kind == "text":
                windows_view.copy_text(body)
            else:
                assert files is not None
                windows_view.copy_files(files.files)
            copied = True
        if action in {"open", "reveal"}:
            assert files is not None
            operation = windows_view.open_files if action == "open" else windows_view.reveal_files
            operation(files.open_files)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DesktopError(str(exc), copied=copied) from exc
    return {
        "action": action,
        "copied": copied,
        "body": body,
        "directory": str(files.directory) if files else None,
    }
