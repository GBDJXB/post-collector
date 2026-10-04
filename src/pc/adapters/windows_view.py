"""Windows 默认程序、文件定位与立即渲染的剪贴板格式。"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from pc.services.viewing import ViewError


def require_windows() -> None:
    if os.name != "nt":
        raise ViewError("open、reveal 与 --copy 当前支持 Windows")


def open_files(paths: tuple[Path, ...]) -> None:
    require_windows()
    for path in paths:
        os.startfile(str(path), "open")


def reveal_files(paths: tuple[Path, ...]) -> None:
    require_windows()
    if not paths or len({p.parent for p in paths}) != 1:
        raise ViewError("定位文件必须位于同一目录")
    ole = ctypes.WinDLL("ole32")
    shell = ctypes.WinDLL("shell32")
    ole.CoInitialize.argtypes = [ctypes.c_void_p]
    ole.CoInitialize.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    shell.SHParseDisplayName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    shell.SHParseDisplayName.restype = ctypes.c_long
    shell.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
    ]
    shell.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    shell.ILFindLastID.argtypes = [ctypes.c_void_p]
    shell.ILFindLastID.restype = ctypes.c_void_p
    status = ole.CoInitialize(None)
    if status < 0:
        raise OSError("无法初始化文件管理器 COM 接口")
    allocated = []
    try:

        def parse(path: Path) -> Any:
            pointer = ctypes.c_void_p()
            code = shell.SHParseDisplayName(str(path), None, ctypes.byref(pointer), 0, None)
            if code < 0:
                raise OSError(f"无法定位文件：{path}（HRESULT {code}）")
            allocated.append(pointer)
            return pointer

        folder = parse(paths[0].parent)
        children = (ctypes.c_void_p * len(paths))(
            *(shell.ILFindLastID(parse(path)) for path in paths)
        )
        code = shell.SHOpenFolderAndSelectItems(folder, len(paths), children, 0)
        if code < 0:
            raise OSError(f"文件管理器定位失败（HRESULT {code}）")
    finally:
        for pointer in allocated:
            ole.CoTaskMemFree(pointer)
        ole.CoUninitialize()


def file_drop_payload(paths: tuple[Path, ...]) -> bytes:
    if not paths or any(not path.is_absolute() for path in paths):
        raise ViewError("复制文件需要非空的绝对路径列表")
    # DROPFILES: pFiles, POINT(x,y), fNC, fWide；正文为双 NUL 终止的 UTF-16 列表。
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + (
        "\0".join(str(path) for path in paths) + "\0\0"
    ).encode("utf-16-le")


def copy_text(text: str) -> None:
    if "\0" in text:
        raise ViewError("正文含 NUL，无法完整复制到剪贴板")
    _set_clipboard(13, (text + "\0").encode("utf-16-le"))


def copy_files(paths: tuple[Path, ...]) -> None:
    _set_clipboard(15, file_drop_payload(paths))


def _set_clipboard(format_id: int, payload: bytes) -> None:
    require_windows()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.restype = wintypes.HGLOBAL
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE
    user.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DestroyWindow.argtypes = [wintypes.HWND]
    # EmptyClipboard 后需要有效 owner；创建 message-only 窗口，不显示 UI。
    owner = user.CreateWindowExW(0, "STATIC", "pc clipboard", 0, 0, 0, 0, 0, -3, None, None, None)
    if not owner:
        raise ctypes.WinError(ctypes.get_last_error())
    memory = None
    opened = False
    try:
        memory = kernel.GlobalAlloc(0x0002, len(payload))
        if not memory:
            raise ctypes.WinError(ctypes.get_last_error())
        pointer = kernel.GlobalLock(memory)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(pointer, payload, len(payload))
        kernel.GlobalUnlock(memory)
        for _ in range(10):
            if user.OpenClipboard(owner):
                opened = True
                break
            time.sleep(0.02)
        if not opened:
            raise ViewError("剪贴板被其他程序占用，请稍后重试")
        if not user.EmptyClipboard() or not user.SetClipboardData(format_id, memory):
            raise ctypes.WinError(ctypes.get_last_error())
        memory = None  # 所有权交给系统，内容在 pc 退出后仍可粘贴。
    finally:
        if opened:
            user.CloseClipboard()
        if memory:
            kernel.GlobalFree(memory)
        user.DestroyWindow(owner)
