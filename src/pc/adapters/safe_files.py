"""按来源 roots 限定本地文件读取，并校验实际打开的句柄。"""

from __future__ import annotations

import ctypes
import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


class UnsafeLocalPathError(RuntimeError):
    """本地引用超出来源范围，或经过文件系统链接。"""


def absolute_path(path: Path | str) -> Path:
    # abspath 清理点路径，不跟随链接。
    return Path(os.path.abspath(Path(path).expanduser()))


def _check_redirects(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UnsafeLocalPathError(f"拒绝链接或 reparse 路径：{component}")


def _allowed(path: Path, roots: Sequence[Path], *, sidecar: bool) -> bool:
    for root in roots:
        if root.is_dir() and path.is_relative_to(root):
            return True
        if path == root:
            return True
        if (
            sidecar
            and root.is_file()
            and path in [root.with_suffix(s) for s in (".txt", ".md", ".markdown")]
        ):
            return True
    return False


def _opened_path(handle: BinaryIO) -> Path:
    if os.name == "nt":
        import msvcrt
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        api.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        api.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = api(msvcrt.get_osfhandle(handle.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise UnsafeLocalPathError("无法校验已打开文件的实际路径")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return absolute_path(value)
    descriptor = Path(f"/proc/self/fd/{handle.fileno()}")
    if descriptor.exists():
        return absolute_path(os.readlink(descriptor))
    # macOS 用 F_GETPATH 读取句柄路径。
    import fcntl

    value = fcntl.fcntl(handle.fileno(), 50, bytes(1024))
    return absolute_path(os.fsdecode(value.split(b"\0", 1)[0]))


@contextmanager
def open_scoped(
    path: Path | str, roots: Sequence[Path], *, sidecar: bool = False
) -> Iterator[BinaryIO]:
    candidate = absolute_path(path)
    if not _allowed(candidate, roots, sidecar=sidecar):
        raise UnsafeLocalPathError(f"文件不在来源允许范围内：{candidate}；请重新指定导入路径")
    for root in roots:
        # 旧文件根已不存在时，仍允许读取其他登记根。
        if root.exists():
            _check_redirects(root)
    _check_redirects(candidate)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(candidate, flags)
    with os.fdopen(fd, "rb") as handle:
        opened = _opened_path(handle)
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or opened != candidate:
            raise UnsafeLocalPathError(f"文件在打开期间发生路径替换：{candidate}")
        _check_redirects(candidate)
        if not _allowed(opened, roots, sidecar=sidecar):
            raise UnsafeLocalPathError(f"已打开文件不在允许范围内：{opened}")
        current = candidate.stat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise UnsafeLocalPathError(f"文件在打开期间被替换：{candidate}")
        yield handle
