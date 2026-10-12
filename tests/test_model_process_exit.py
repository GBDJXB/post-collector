"""真实本机进程退出与同库重启；只用假密钥，不调用外部模型。"""
from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from pc import cli
from pc.adapters import sqlite as db

ROOT = Path(__file__).resolve().parents[1]
KEY = "TEST-ONLY-PROCESS-EXIT-NOT-A-REAL-API-KEY"

# 辅助进程只附着自有测试服务器的独立控制台，不能广播到 pytest/PowerShell。
WINDOWS_CTRL_C = """
import ctypes
import sys
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.AttachConsole.argtypes = [wintypes.DWORD]
kernel.AttachConsole.restype = wintypes.BOOL
kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
kernel.FreeConsole()
if not kernel.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    if not kernel.SetConsoleCtrlHandler(None, True):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.GenerateConsoleCtrlEvent(0, 0):
        raise ctypes.WinError(ctypes.get_last_error())
finally:
    kernel.FreeConsole()
"""


def _log(path):
    return path.read_text(encoding="utf-8", errors="replace").replace(KEY, "[已脱敏]")


@contextmanager
def _server(path, data, log_path, port):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PC__")}
    env.update(PC_DATA_ROOT=str(data), PYTHONIOENCODING="utf-8")
    options = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options.update(creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "pc.cli", "serve", "--root", str(ROOT),
             "--path", str(path), "--port", str(port)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, **options,
        )
        try:
            origin = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=origin, trust_env=False, timeout=2,
                              headers={"Origin": origin}) as client:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    assert process.poll() is None, f"服务启动提前退出：{_log(log_path)}"
                    text = _log(log_path)
                    token = re.search(r"#token=([A-Za-z0-9_-]+)", text)
                    # 先确认自有进程绑定成功，再发请求，避免触及占用端口的其他服务。
                    if token and "Uvicorn running on" in text:
                        try:
                            response = client.get("/healthz")
                        except httpx.TransportError:
                            pass
                        else:
                            assert response.status_code == 200
                            assert response.json()["status"] == "ok"
                            break
                    time.sleep(0.05)
                else:
                    pytest.fail(f"服务启动超时：{_log(log_path)}")
                yield process, client, token.group(1)
        finally:
            # 仅终止本上下文创建的子进程；失败时也不遗留测试服务器。
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)


def _login(client, token):
    response = client.post("/api/v1/session", json={"token": token})
    assert response.status_code == 200, response.text.replace(KEY, "[已脱敏]")
    client.headers["X-CSRF-Token"] = response.json()["csrf"]


def _ctrl_c(process, log_path):
    if os.name != "nt":
        process.send_signal(signal.SIGINT)
        return
    with log_path.open("wb") as output:
        helper = subprocess.run(
            [sys.executable, "-c", WINDOWS_CTRL_C, str(process.pid)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False,
        )
    assert helper.returncode == 0, f"真实 CTRL_C_EVENT 发送失败：{_log(log_path)}"


def _assert_no_persisted_key(path, data, logs):
    # 只验证应用数据库与持久文件，不构成物理内存擦除证明。
    with db.open_db(path, expect_exists=True) as conn:
        assert KEY not in "\n".join(conn.iterdump())
        assert conn.execute("SELECT count(*) FROM egress_log").fetchone()[0] == 0
        assert db.read_settings(conn)["privacy.local_only_mode"].value is False
    for artifact in [*data.rglob("*"), *logs]:
        if artifact.is_file():
            raw = artifact.read_bytes()
            assert KEY.encode() not in raw, f"测试密钥进入持久文件：{artifact.name}"
            assert KEY.encode("utf-16-le") not in raw, artifact.name


@pytest.mark.parametrize("exit_mode", ["ctrl_c", "force"])
def test_process_exit_without_key_delete_cannot_restore_credentials(tmp_path, monkeypatch, exit_mode):
    # 隔离部署环境覆盖；仅修改当前测试进程，由 monkeypatch 自动恢复。
    for name in list(os.environ):
        if name.startswith("PC__"):
            monkeypatch.delenv(name)
    data = tmp_path / "data"
    monkeypatch.setenv("PC_DATA_ROOT", str(data))
    path = data / "pc.sqlite3"
    assert cli.main(["db", "apply", "--path", str(path), "--root", str(ROOT)]) == 0
    with socket.socket() as lease:
        lease.bind(("127.0.0.1", 0))
        port = lease.getsockname()[1]
    first_log, second_log = tmp_path / "first.log", tmp_path / "restart.log"
    signal_log = tmp_path / "ctrl-c.log"

    with _server(path, data, first_log, port) as (process, client, token):
        _login(client, token)
        response = client.post("/api/v1/model/key/set", json={"key": KEY})
        assert response.status_code == 200
        assert response.json()["key_present"] is True
        assert response.json()["enabled"] is False
        response = client.post("/api/v1/model/access", json={"enabled": True, "confirmed": True})
        assert response.status_code == 200, response.text.replace(KEY, "[已脱敏]")
        assert response.json()["key_present"] is True and response.json()["enabled"] is True
        old_cookie = client.cookies.get("pc_session")
        _assert_no_persisted_key(path, data, [first_log])
        # 故意不调用 /model/key/delete 或 /logout。
        if exit_mode == "ctrl_c":
            _ctrl_c(process, signal_log)
        else:
            process.kill()
        try:
            code = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{exit_mode} 后服务未结束：{_log(first_log)}")
        if exit_mode == "ctrl_c":
            assert code == 0, f"Ctrl+C 异常退出（{code}）：{_log(first_log)}"
            assert "Application shutdown complete" in _log(first_log)
        else:
            assert code != 0, "强制终止应返回非正常退出状态"
    # Windows 强杀返回后 TCP 清理可能稍晚完成；等待真实关闭条件而非固定 sleep。
    started = time.monotonic()
    deadline = started + 10
    probes = 0
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                break
        probes += 1
        time.sleep(0.05)
    else:
        pytest.fail(f"进程 {process.pid} 已退出（{code}），端口 {port} 在 10 秒后仍可连接")
    print(f"退出端口验证：pid={process.pid}、exit={code}、残留连接探测={probes}、耗时={time.monotonic() - started:.3f}s")

    # 复用库和数据环境，不依赖旧端口立即可重新绑定。
    with socket.socket() as lease:
        lease.bind(("127.0.0.1", 0))
        restart_port = lease.getsockname()[1]
    with _server(path, data, second_log, restart_port) as (process, client, token):
        client.cookies.set("pc_session", old_cookie)
        assert client.get("/api/v1/model/status").status_code == 401
        client.cookies.clear()
        _login(client, token)
        response = client.get("/api/v1/model/status")
        assert response.status_code == 200
        assert response.json()["key_present"] is False and response.json()["enabled"] is False
        response = client.post("/api/v1/model/access", json={"enabled": True, "confirmed": True})
        assert response.status_code == 400 and "先设置" in response.text
        response = client.post("/api/v1/model/preview", json={"content_id": 1})
        assert response.status_code == 400 and "未放行" in response.text
        _assert_no_persisted_key(path, data, [first_log, second_log, signal_log])
    print(f"PASS: {exit_mode}；同环境重启 key_present=false、enabled=false；旧会话拒绝；无密钥持久化或外发")
