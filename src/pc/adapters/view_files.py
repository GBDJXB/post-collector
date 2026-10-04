"""生成独立查看副本，验证媒体与缓存路径，输出静态 HTML。"""

from __future__ import annotations

import hashlib
import html
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pc.adapters.magic_scan import sniff_mime
from pc.adapters.safe_files import _check_redirects, absolute_path, open_scoped
from pc.domain import ObjectKey
from pc.services.viewing import ViewError, text_body

IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/heic": ".heic",
}
MAX_VIEW_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class ViewFiles:
    directory: Path
    files: tuple[Path, ...]
    open_files: tuple[Path, ...]


def read_images(conn: Any, result: dict[str, Any], *, limit: int) -> list[tuple[int, bytes, str]]:
    """读取已通过扫描的归档原图，按部件、媒体关联顺序返回。"""
    media_rows = [(p["content_id"], m) for p in result["parts"] for m in p["media"]]
    if any(p["item_type"] == "image" and not p["media"] for p in result["parts"]):
        raise ViewError("图片部件缺少媒体记录")
    backends = {}
    ids = sorted(
        {m["storage_backend_id"] for _, m in media_rows if m["storage_backend_id"] is not None}
    )
    for start in range(0, len(ids), 500):
        batch = ids[start : start + 500]
        marks = ",".join("?" * len(batch))
        for row in conn.execute(
            f"SELECT backend_id,kind,config FROM storage_backend WHERE backend_id IN ({marks})",
            batch,
        ):
            backends[row["backend_id"]] = row
    images = []
    total = 0
    for content_id, media in media_rows:
        if (
            media["deleted_at"]
            or media["quarantine_key"]
            or media["scan_status"] not in {"clean", "skipped"}
        ):
            raise ViewError("媒体已删除、隔离或尚未通过扫描")
        extension = IMAGE_EXTENSIONS.get(media["mime_detected"])
        if extension is None:
            raise ViewError(f"暂不支持展示媒体类型：{media['mime_detected']}")
        backend = backends.get(media["storage_backend_id"])
        if backend is None or backend["kind"] != "local":
            raise ViewError("媒体需要可用的本地存储后端")
        configuration = json.loads(backend["config"])
        root_value = configuration.get("root") if isinstance(configuration, dict) else None
        if not isinstance(root_value, str) or not Path(root_value).is_absolute():
            raise ViewError("媒体后端 root 必须是绝对路径")
        root = absolute_path(root_value)
        try:
            key = ObjectKey(media["sha256"])
        except ValueError as exc:
            raise ViewError("媒体哈希无效") from exc
        if media["object_key"] != key.value:
            raise ViewError("媒体对象键与哈希不匹配")
        path = root.joinpath(*key.value.split("/"))
        with open_scoped(path, [root]) as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > limit or size != media["size_bytes"]:
                raise ViewError("媒体大小与记录不符或超过限额")
            total += size
            if total > MAX_VIEW_BYTES:
                raise ViewError("本次查看的媒体总量超过 256 MiB")
            data = handle.read(limit + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != media["sha256"]:
            raise ViewError("媒体字节校验失败")
        if sniff_mime(data) != media["mime_detected"]:
            raise ViewError("媒体魔数与记录类型不符")
        images.append((content_id, data, extension))
    return images


def html_page(result: dict[str, Any], names: dict[int, list[str]]) -> bytes:
    sections = []
    for part in result["parts"]:
        if part["text"]:
            sections.append(f"<pre>{html.escape(part['text'])}</pre>")
        for name in names.get(part["content_id"], []):
            sections.append(f'<img src="{html.escape(name, quote=True)}" alt="归档图片">')
    page = (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        "<title>归档内容</title><style>body{max-width:960px;margin:2rem auto;padding:0 1rem}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}img{display:block;"
        "max-width:100%;height:auto;margin:1rem 0}</style><body>"
        + "\n".join(sections)
        + "</body></html>"
    )
    return page.encode("utf-8")


def materialize(
    result: dict[str, Any], images: list[tuple[int, bytes, str]], *, cache_root: Path, kind: str
) -> ViewFiles:
    """全部准备好后写入缓存；已有副本每次对照预期字节校验。"""
    payloads: dict[str, bytes] = {}
    names: dict[int, list[str]] = {}
    for index, (content_id, data, extension) in enumerate(images, 1):
        name = f"{index:04d}-{content_id}{extension}"
        payloads[name] = data
        names.setdefault(content_id, []).append(name)
    if kind in {"text", "mixed"}:
        payloads["content.txt"] = text_body(result).encode("utf-8")
    if kind == "mixed":
        if any(extension == ".heic" for _, _, extension in images):
            raise ViewError("混合内容 HTML 暂不支持 HEIC 图片")
        payloads["index.html"] = html_page(result, names)
    identity = json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")
    fingerprint = hashlib.sha256(identity + b"view-format-v1").hexdigest()
    directory = absolute_path(cache_root) / str(result["message_id"]) / fingerprint
    # mkdir 前检查已有祖先，拒绝重定向；写入采用独占临时文件和原子替换。
    for component in (*reversed(directory.parents), directory):
        if component.exists() or component.is_symlink():
            _check_redirects(component)
    directory.mkdir(parents=True, exist_ok=True)
    _check_redirects(directory)
    for name, data in payloads.items():
        path = directory / name
        if path.exists() or path.is_symlink():
            with open_scoped(path, [directory]) as handle:
                info = os.fstat(handle.fileno())
                intact = (
                    info.st_nlink == 1
                    and info.st_size == len(data)
                    and handle.read(len(data) + 1) == data
                )
            if intact:
                continue
        temporary = directory / f".tmp-{uuid.uuid4().hex}"
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
            _check_redirects(directory)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    files = tuple(directory / name for name in payloads if name != "index.html")
    open_files = (directory / "index.html",) if kind == "mixed" else files
    return ViewFiles(directory, files, open_files)
