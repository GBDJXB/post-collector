"""浏览器上传暂存文件的有界写入与终态清理。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path

from pc.adapters.safe_files import _check_redirects, _opened_path, absolute_path, open_scoped

MAX_IMAGES = 19
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_REQUEST_BYTES = 258 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".heic", ".heif"}


def filename(value: str) -> str:
    if (
        not value or len(value) > 255 or value in {".", ".."}
        or any(ord(c) < 32 or ord(c) == 127 or c in '/\\:<>"|?*' for c in value)
        or value.endswith((" ", "."))
        or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}
    ):
        raise ValueError("图片文件名无效，请使用普通文件名")
    if Path(value).suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("请选择 PNG、JPEG、GIF、WebP、AVIF 或 HEIC 图片")
    return value


def make_directory(path: Path) -> Path:
    """逐层检查，避免 mkdir 穿过已有重定向。"""
    path = absolute_path(path)
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink():
            _check_redirects(current)
        missing.append(current)
        current = current.parent
    _check_redirects(current)
    for component in reversed(missing):
        component.mkdir(exist_ok=True)
        _check_redirects(component)
    _check_redirects(path)
    return path


def stage(root: Path, images, *, limit: int):
    """独占新目录，边写边校验；文件对象由 HTTP 负责关闭。"""
    root = make_directory(root)
    directory = root / uuid.uuid4().hex
    directory.mkdir()
    refs, digests = [], []
    total = 0
    try:
        for ordinal, image in enumerate(images):
            name = filename(image.filename or "")
            child = make_directory(directory / f"{ordinal:04d}")
            path = child / name
            digest, size = hashlib.sha256(), 0
            image.file.seek(0)
            with path.open("xb") as handle:
                _check_redirects(child)
                if _opened_path(handle) != path or os.fstat(handle.fileno()).st_nlink != 1:
                    raise ValueError("暂存文件在创建时发生重定向")
                while chunk := image.file.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > limit or total > MAX_TOTAL_BYTES:
                        raise ValueError("图片大小超过上传限额")
                    handle.write(chunk)
                    digest.update(chunk)
            if not size:
                raise ValueError("图片文件为空")
            with open_scoped(path, [root]) as handle:
                if os.fstat(handle.fileno()).st_nlink != 1:
                    raise ValueError("暂存文件存在硬链接")
            refs.append(str(path))
            digests.append(digest.hexdigest())
        return directory, tuple(refs), tuple(digests)
    except BaseException:
        cleanup(root, directory)
        raise


def cleanup(root: Path, directory: Path) -> None:
    """只删除已验证的直接 UUID 子目录，禁止清理其他运行时路径。"""
    root, directory = absolute_path(root), absolute_path(directory)
    if directory.parent != root or not re.fullmatch(r"[0-9a-f]{32}", directory.name):
        raise ValueError("上传清理目录不在预期范围内")
    if not directory.exists():
        return
    _check_redirects(directory)
    for child in directory.rglob("*"):
        _check_redirects(child)
        if child.is_file() and child.stat().st_nlink != 1:
            raise ValueError("拒绝清理包含硬链接的暂存目录")
    if directory.resolve().parent != root.resolve():
        raise ValueError("上传清理目录发生重定向")
    shutil.rmtree(directory)


def cleanup_refs(root: Path, refs) -> None:
    root = absolute_path(root)
    directories = set()
    for ref in refs:
        path = absolute_path(ref)
        if path.is_relative_to(root):
            relative = path.relative_to(root)
            if len(relative.parts) == 3 and re.fullmatch(r"[0-9a-f]{32}", relative.parts[0]):
                directories.add(root / relative.parts[0])
    for directory in directories:
        cleanup(root, directory)
