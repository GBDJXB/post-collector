"""内容展示：真实摄入、可见性、独立副本和系统操作边界。"""

from __future__ import annotations

import base64
import json
import os
import struct
import uuid
from pathlib import Path

import pytest

from pc import cli
from pc.adapters import sqlite as db
from pc.adapters import view_files, windows_view
from pc.services import viewing
from pc.view_presentation import terminal_text

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII="
)
BODY = "正文第一行\n第二行 <script>alert(1)</script> & 中文"


@pytest.fixture
def workspace(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("PC_DATA_ROOT", str(data))
    path = str(data / "pc.sqlite3")
    assert cli.main(["db", "apply", "--path", path]) == 0
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "text.txt").write_text(BODY, encoding="utf-8")
    (inputs / "image.png").write_bytes(PNG)
    (inputs / "mixed.png").write_bytes(PNG)
    (inputs / "mixed.txt").write_text("混合说明", encoding="utf-8")
    assert cli.main(["upload", str(inputs), "--path", path]) == 0
    capsys.readouterr()
    with db.open_db(path) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT content_id,message_id,item_type,text_content,original_name FROM content_item"
            )
        ]
        mixed_messages = {r["message_id"] for r in rows if r["original_name"] == "mixed.png"}
        targets = {}
        for row in rows:
            text = row["text_content"] or ""
            if text.startswith("正文第一行"):
                targets["text"] = row
                # 详情展示读取数据库正文，保留其中原有换行。
                conn.execute(
                    "UPDATE content_item SET text_content=? WHERE content_id=?",
                    (BODY, row["content_id"]),
                )
            elif row["message_id"] in mixed_messages:
                targets["mixed"] = row
            elif row["item_type"] == "image":
                targets["image"] = row
        conn.commit()
    return {"path": path, "data": data, "targets": targets}


def run(workspace, capsys, command, kind, *options):
    code = cli.main(
        [
            command,
            "--message",
            str(workspace["targets"][kind]["message_id"]),
            "--path",
            workspace["path"],
            *options,
        ]
    )
    return code, capsys.readouterr()


@pytest.fixture
def system(monkeypatch):
    calls = []
    monkeypatch.setattr(windows_view, "require_windows", lambda: None)
    for name in ("open_files", "reveal_files", "copy_text", "copy_files"):
        monkeypatch.setattr(
            windows_view, name, lambda value, name=name: calls.append((name, value))
        )
    return calls


def test_show_json_and_body(workspace, capsys):
    code, output = run(workspace, capsys, "show", "text")
    assert code == 0 and output.out == BODY + "\n" and not output.err
    code, output = run(workspace, capsys, "show", "text", "--json")
    data = json.loads(output.out)
    assert code == 0 and data["result_type"] == "content" and data["result_version"] == 1
    assert data["parts"][0]["text"] == BODY
    assert not (workspace["data"] / "view-cache").exists()


@pytest.mark.parametrize("command", ["show", "open", "reveal"])
def test_text_copy(workspace, capsys, system, command):
    code, output = run(workspace, capsys, command, "text", "--copy")
    assert code == 0, output.err
    assert system[0] == ("copy_text", BODY)
    if command != "show":
        paths = system[1][1]
        assert paths[0].suffix == ".txt"
        assert paths[0].read_text(encoding="utf-8") == BODY


@pytest.mark.parametrize("command", ["open", "reveal"])
def test_image_copy_and_separate_file(workspace, capsys, system, command):
    code, output = run(workspace, capsys, command, "image", "--copy")
    assert code == 0, output.err
    name, copied = system[0]
    assert name == "copy_files" and copied[0].suffix == ".png" and copied[0].read_bytes() == PNG
    assert system[1][1] == copied
    with db.open_db(workspace["path"]) as conn:
        media = conn.execute("SELECT * FROM media LIMIT 1").fetchone()
        backend = conn.execute(
            "SELECT config FROM storage_backend WHERE backend_id=?", (media["storage_backend_id"],)
        ).fetchone()
    archived = Path(json.loads(backend[0])["root"]) / media["object_key"]
    assert copied[0] != archived and not os.path.samefile(copied[0], archived)
    copied[0].write_bytes(b"edited")
    assert archived.read_bytes() == PNG
    system.clear()
    code, output = run(workspace, capsys, command, "image")
    assert code == 0, output.err
    assert system[0][1][0].read_bytes() == PNG


@pytest.mark.parametrize("command", ["show", "open", "reveal"])
def test_mixed_copy_fails_without_effects(workspace, capsys, system, command):
    code, output = run(workspace, capsys, command, "mixed", "--copy")
    assert code == 2 and "混合内容暂不支持 --copy" in output.err and not output.out
    assert not system and not (workspace["data"] / "view-cache").exists()


def test_mixed_html_and_reveal(workspace, capsys, system):
    with db.open_db(workspace["path"]) as conn:
        conn.execute(
            "UPDATE content_item SET text_content=? WHERE message_id=?",
            (BODY, workspace["targets"]["mixed"]["message_id"]),
        )
        conn.commit()
    code, output = run(workspace, capsys, "open", "mixed")
    assert code == 0, output.err
    page = system[0][1][0]
    raw = page.read_text(encoding="utf-8")
    assert page.name == "index.html"
    assert "&lt;script&gt;" in raw and "<script>" not in raw
    assert raw.index("<pre>") < raw.index("<img ")
    assert "default-src 'none'" in raw and "http://" not in raw and "https://" not in raw
    assert next(page.parent.glob("*.png")).read_bytes() == PNG
    system.clear()
    code, output = run(workspace, capsys, "reveal", "mixed")
    assert code == 0 and system == [("reveal_files", (page,))]


@pytest.mark.parametrize("kind", ["image", "mixed"])
def test_show_images_unavailable_without_side_effect(workspace, capsys, system, kind):
    code, output = run(workspace, capsys, "show", kind)
    assert code == 2 and "终端图片展示" in output.err and not output.out
    assert not system and not (workspace["data"] / "view-cache").exists()


@pytest.mark.parametrize(
    "change",
    [
        "c.status='quarantined'",
        "c.nsfw_level=1",
        "c.deleted_at='2026-10-03T00:00:00.000Z'",
        "m.deleted_at='2026-10-03T00:00:00.000Z'",
    ],
)
def test_hidden_target_cannot_reuse_cache(workspace, capsys, system, change):
    code, _ = run(workspace, capsys, "open", "text")
    assert code == 0
    table = "content_item" if change.startswith("c.") else "message"
    with db.open_db(workspace["path"]) as conn:
        conn.execute(
            f"UPDATE {table} SET {change[2:]} WHERE message_id=?",
            (workspace["targets"]["text"]["message_id"],),
        )
        conn.commit()
    system.clear()
    code, output = run(workspace, capsys, "open", "text")
    assert code == 2 and "没有可见内容" in output.err and not system


@pytest.mark.parametrize(
    "change",
    [
        "object_key='../outside'",
        "scan_status='pending'",
        "scan_status='infected'",
        "quarantine_key='q/object'",
        "size_bytes=1",
        "deleted_at='2026-10-03T00:00:00.000Z'",
        "mime_detected='image/svg+xml'",
        "storage_backend_id=NULL",
    ],
)
def test_bad_media_rejected_before_system(workspace, capsys, system, change):
    with db.open_db(workspace["path"]) as conn:
        conn.execute(f"UPDATE media SET {change}")
        conn.commit()
    code, output = run(workspace, capsys, "open", "image")
    assert code == 2 and output.err and not system
    assert not (workspace["data"] / "view-cache").exists()


def test_archive_hash_and_magic(workspace, capsys, system):
    with db.open_db(workspace["path"]) as conn:
        row = conn.execute(
            "SELECT m.object_key,s.config FROM media m JOIN storage_backend s ON s.backend_id=m.storage_backend_id"
        ).fetchone()
    source = Path(json.loads(row["config"])["root"]) / row["object_key"]
    source.write_bytes(b"x" * len(PNG))
    code, output = run(workspace, capsys, "open", "image")
    assert code == 2 and "字节校验失败" in output.err and not system


@pytest.mark.parametrize("configuration", [{"root": "relative"}, [], {"root": 123}])
def test_invalid_backend_root(workspace, capsys, system, configuration):
    with db.open_db(workspace["path"]) as conn:
        conn.execute("UPDATE storage_backend SET config=?", (json.dumps(configuration),))
        conn.commit()
    code, output = run(workspace, capsys, "open", "image")
    assert code == 2 and "绝对路径" in output.err and not system


def test_media_limit(workspace):
    with db.open_db(workspace["path"]) as conn:
        result = viewing.detail(
            conn, message_id=workspace["targets"]["image"]["message_id"], content_id=None
        )
        with pytest.raises(viewing.ViewError, match="超过限额"):
            view_files.read_images(conn, result, limit=1)


def test_multi_image_order_and_batch_copy(workspace, capsys, system):
    message = workspace["targets"]["image"]["message_id"]
    with db.open_db(workspace["path"]) as conn:
        original = workspace["targets"]["image"]["content_id"]
        new_id = conn.execute(
            "INSERT INTO content_item(uuid,message_id,item_type,ordinal) VALUES(?,?,'image',-1) RETURNING content_id",
            (str(uuid.uuid4()), message),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO content_media(content_id,media_id) SELECT ?,media_id FROM content_media WHERE content_id=?",
            (new_id, original),
        )
        conn.commit()
    code, output = run(workspace, capsys, "open", "image", "--copy")
    assert code == 0, output.err
    assert len(system[0][1]) == 2
    assert system[0][1][0].name == f"0001-{new_id}.png"
    assert system[1][1] == system[0][1]


def test_content_selector_does_not_expand_message(workspace, capsys):
    message = workspace["targets"]["text"]["message_id"]
    with db.open_db(workspace["path"]) as conn:
        conn.execute(
            "INSERT INTO content_item(uuid,message_id,item_type,ordinal,text_content) VALUES(?,?,'text',-1,'another')",
            (str(uuid.uuid4()), message),
        )
        conn.commit()
    assert (
        cli.main(
            [
                "show",
                "--content",
                str(workspace["targets"]["text"]["content_id"]),
                "--path",
                workspace["path"],
                "--json",
            ]
        )
        == 0
    )
    assert len(json.loads(capsys.readouterr().out)["parts"]) == 1
    code, output = run(workspace, capsys, "show", "text", "--json")
    assert code == 0
    assert [p["text"] for p in json.loads(output.out)["parts"]] == ["another", BODY]


def test_partial_failure_reports_completed_copy(workspace, capsys, system, monkeypatch):
    def fail(_):
        raise OSError("无默认程序")

    monkeypatch.setattr(windows_view, "open_files", fail)
    code, output = run(workspace, capsys, "open", "text", "--copy")
    assert code == 2 and "无默认程序" in output.err and "剪贴板复制已完成" in output.err
    assert system == [("copy_text", BODY)]


def test_target_changes_during_materialization(workspace, capsys, system, monkeypatch):
    original = view_files.materialize

    def hide(*args, **kwargs):
        files = original(*args, **kwargs)
        with db.open_db(workspace["path"]) as conn:
            conn.execute("UPDATE content_item SET status='quarantined'")
            conn.commit()
        return files

    monkeypatch.setattr(view_files, "materialize", hide)
    code, output = run(workspace, capsys, "open", "text", "--copy")
    assert code == 2 and output.err and not system


def test_dropfiles_payload(tmp_path):
    paths = (tmp_path / "中文.png", tmp_path / "two.png")
    data = windows_view.file_drop_payload(paths)
    assert struct.unpack("<IiiII", data[:20]) == (20, 0, 0, 0, 1)
    assert data[20:].decode("utf-16-le") == "\0".join(map(str, paths)) + "\0\0"


def test_terminal_control_filter():
    assert terminal_text("正常\x1b[31m\x07\x9b\r\n\t文") == "正常[31m\n\t文"


def test_json_copy_and_missing_database(tmp_path, capsys):
    assert cli.main(["show", "--message", "1", "--json", "--copy"]) == 2
    assert "--json 与 --copy" in capsys.readouterr().err
    path = tmp_path / "missing.sqlite3"
    assert cli.main(["show", "--message", "1", "--json", "--path", str(path)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and not path.exists()


def test_cache_symlink_rejected(workspace, capsys, system, tmp_path):
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    link = workspace["data"] / "view-cache"
    try:
        link.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("系统未授予符号链接权限")
    code, output = run(workspace, capsys, "open", "text")
    assert code == 2 and "reparse" in output.err and not system
    assert not list(destination.iterdir())


def test_cache_hardlink_replaced(workspace, capsys, system, tmp_path):
    code, output = run(workspace, capsys, "open", "image")
    assert code == 0, output.err
    copied = system[0][1][0]
    archived_copy = tmp_path / "archive-copy.png"
    archived_copy.write_bytes(PNG)
    copied.unlink()
    os.link(archived_copy, copied)
    system.clear()
    code, output = run(workspace, capsys, "open", "image")
    assert code == 0, output.err
    copied.write_bytes(b"changed")
    assert archived_copy.read_bytes() == PNG


def test_detail_transaction_restored_on_error(workspace):
    with db.open_db(workspace["path"]) as conn:
        with pytest.raises(viewing.ViewError):
            viewing.detail(conn, message_id=99999, content_id=None)
        assert not conn.in_transaction


def test_cache_read_link_rejected(workspace, capsys, system, tmp_path):
    code, output = run(workspace, capsys, "open", "text")
    assert code == 0, output.err
    copied = system[0][1][0]
    external = tmp_path / "external.txt"
    external.write_text(BODY, encoding="utf-8")
    copied.unlink()
    try:
        copied.symlink_to(external)
    except OSError:
        pytest.skip("系统未授予符号链接权限")
    system.clear()
    code, output = run(workspace, capsys, "open", "text")
    assert code == 2 and "reparse" in output.err and not system
    assert external.read_text(encoding="utf-8") == BODY


def test_native_windows_pidl_selection(tmp_path, monkeypatch):
    """真实解析 Windows PIDL，拦截最终打开动作，避免测试启动窗口。"""
    if os.name != "nt":
        pytest.skip("Windows 原生接口测试")
    import ctypes
    from ctypes import wintypes

    real_loader = ctypes.WinDLL
    shell = real_loader("shell32")
    ole = real_loader("ole32")
    shell.ILCombine.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    shell.ILCombine.restype = ctypes.c_void_p
    shell.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    paths = (tmp_path / "中文.txt", tmp_path / "second.png")
    for path in paths:
        path.write_bytes(b"sample")
    selected = []

    class Select:
        def __call__(self, folder, count, children, flags):
            assert flags == 0
            for index in range(count):
                combined = shell.ILCombine(folder, children[index])
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    assert shell.SHGetPathFromIDListW(combined, buffer)
                    selected.append(Path(buffer.value))
                finally:
                    ole.CoTaskMemFree(combined)
            return 0

    shell.SHOpenFolderAndSelectItems = Select()
    monkeypatch.setattr(ctypes, "WinDLL", lambda name: shell if name == "shell32" else ole)
    windows_view.reveal_files(paths)
    assert selected == list(paths)


@pytest.mark.parametrize("busy,fail_set", [(False, False), (True, False), (False, True)])
def test_native_windows_clipboard_memory(monkeypatch, busy, fail_set):
    """真实分配 Win32 内存与 owner 窗口；替换剪贴板调用以保留用户剪贴板。"""
    if os.name != "nt":
        pytest.skip("Windows 原生接口测试")
    import ctypes
    from ctypes import wintypes

    real_loader = ctypes.WinDLL
    kernel = real_loader("kernel32", use_last_error=True)
    user = real_loader("user32", use_last_error=True)
    kernel.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalSize.restype = ctypes.c_size_t
    kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.restype = wintypes.HGLOBAL
    captured = []
    closed = []

    class Api:
        def __init__(self, action):
            self.action = action

        def __call__(self, *args):
            return self.action(*args)

    def set_data(format_id, handle):
        pointer = kernel.GlobalLock(handle)
        try:
            data = ctypes.string_at(pointer, kernel.GlobalSize(handle))
        finally:
            kernel.GlobalUnlock(handle)
        captured.append((format_id, handle, data))
        return 0 if fail_set else handle

    user.OpenClipboard = Api(lambda _: int(not busy))
    user.EmptyClipboard = Api(lambda: 1)
    user.SetClipboardData = Api(set_data)
    user.CloseClipboard = Api(lambda: closed.append(True))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_: kernel if name == "kernel32" else user)
    monkeypatch.setattr(windows_view.time, "sleep", lambda _: None)
    if busy or fail_set:
        with pytest.raises((viewing.ViewError, OSError)):
            windows_view.copy_text("中文")
    else:
        windows_view.copy_text("中文")
        format_id, handle, data = captured[0]
        assert format_id == 13 and data.decode("utf-16-le") == "中文\0"
        assert closed == [True]
        # 模拟系统取得所有权：适配器返回时内存仍存在，由测试释放。
        assert kernel.GlobalSize(handle) == len(data)
        kernel.GlobalFree(handle)
    if busy:
        assert not captured and not closed
    if fail_set:
        assert closed == [True]
        assert kernel.GlobalSize(captured[0][1]) == 0


def test_unsupported_type_and_invalid_ids(workspace, capsys, system):
    with db.open_db(workspace["path"]) as conn:
        conn.execute("UPDATE content_item SET item_type='link'")
        conn.commit()
    code, output = run(workspace, capsys, "open", "text")
    assert code == 2 and "暂不支持展示内容类型" in output.err and not system
    assert cli.main(["show", "--message", "0", "--path", workspace["path"]]) == 2
    assert "正整数" in capsys.readouterr().err


def test_mixed_heic_stops_before_file_generation(tmp_path):
    result = {"message_id": 1, "parts": [{"content_id": 1, "text": "说明"}]}
    with pytest.raises(viewing.ViewError, match="HEIC"):
        view_files.materialize(
            result, [(1, b"heic", ".heic")], cache_root=tmp_path / "cache", kind="mixed"
        )
    assert not (tmp_path / "cache").exists()


def test_selected_image_with_body_is_mixed():
    result = {"parts": [{"item_type": "image", "text": "说明", "media": [{}]}]}
    with pytest.raises(viewing.ViewError, match="混合内容"):
        viewing.preflight(result, copy=True)


def test_missing_archive_cannot_use_existing_copy(workspace, capsys, system):
    code, output = run(workspace, capsys, "open", "image")
    assert code == 0, output.err
    with db.open_db(workspace["path"]) as conn:
        row = conn.execute(
            "SELECT m.object_key,s.config FROM media m JOIN storage_backend s ON s.backend_id=m.storage_backend_id"
        ).fetchone()
    archived = Path(json.loads(row["config"])["root"]) / row["object_key"]
    archived.unlink()
    system.clear()
    code, output = run(workspace, capsys, "open", "image")
    assert code == 2 and output.err and not system


def test_media_uses_recorded_backend(workspace, capsys, system, tmp_path):
    with db.open_db(workspace["path"]) as conn:
        row = conn.execute("SELECT object_key,storage_backend_id FROM media").fetchone()
        other = tmp_path / "other-backend"
        archived = other / row["object_key"]
        archived.parent.mkdir(parents=True)
        archived.write_bytes(PNG)
        conn.execute(
            "UPDATE storage_backend SET config=? WHERE backend_id=?",
            (json.dumps({"root": str(other)}), row["storage_backend_id"]),
        )
        conn.commit()
    code, output = run(workspace, capsys, "open", "image")
    assert code == 0, output.err
    assert system[0][1][0].read_bytes() == PNG


def test_view_media_total_limit(workspace, monkeypatch):
    monkeypatch.setattr(view_files, "MAX_VIEW_BYTES", 1)
    with db.open_db(workspace["path"]) as conn:
        result = viewing.detail(
            conn, message_id=workspace["targets"]["image"]["message_id"], content_id=None
        )
        with pytest.raises(viewing.ViewError, match="媒体总量"):
            view_files.read_images(conn, result, limit=1000)


def test_cache_junction_rejected(workspace, capsys, system, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction 测试")
    import subprocess

    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace["data"] / "view-cache"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
    )
    assert created.returncode == 0, created.stderr
    code, output = run(workspace, capsys, "open", "text")
    assert code == 2 and "reparse" in output.err and not system
    assert not list(outside.iterdir())


def test_clipboard_nul_error_before_side_effects(workspace, capsys, system):
    with db.open_db(workspace["path"]) as conn:
        conn.execute(
            "UPDATE content_item SET text_content=? WHERE message_id=?",
            ("前\0后", workspace["targets"]["text"]["message_id"]),
        )
        conn.commit()
    code, output = run(workspace, capsys, "open", "text", "--copy")
    assert code == 2 and "NUL" in output.err and not system
    assert not (workspace["data"] / "view-cache").exists()


def test_image_copy_failure_prevents_open(workspace, capsys, system, monkeypatch):
    def fail(_):
        raise viewing.ViewError("剪贴板被占用")

    monkeypatch.setattr(windows_view, "copy_files", fail)
    code, output = run(workspace, capsys, "open", "image", "--copy")
    assert code == 2 and "剪贴板被占用" in output.err and not system


def test_oversized_target_id(workspace, capsys):
    assert cli.main(["show", "--message", str(2**63), "--path", workspace["path"]]) == 2
    assert "正整数" in capsys.readouterr().err
