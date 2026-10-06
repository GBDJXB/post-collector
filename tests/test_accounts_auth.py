"""受邀账号、认证独立入口及身份 JSON 的安全回归。"""

from __future__ import annotations

import json
import re
import shlex
from uuid import uuid4

import httpx
import pytest

from pc import cli
from pc.adapters import passwords
from pc.adapters import sqlite as db
from pc.api.app import Runtime, create_app
from pc.api.remote_auth import COOKIE, LOGIN_COOKIE, create_remote_app, public_origin
from pc.services import accounts

ORIGIN = "https://archive.example.com"
PASSWORD = "this is a long test password!"


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("PC_DATA_ROOT", str(tmp_path))
    path = tmp_path / "pc.sqlite3"
    assert cli.main(["db", "apply", "--path", str(path)]) == 0
    with db.open_db(path) as conn:
        accounts.create_user(conn, "reader", PASSWORD)
    return path


def client_for(app, origin=ORIGIN, peer="127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
                             base_url=origin, headers={"Origin": origin})


async def form(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["Referrer-Policy"] == "same-origin"
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


async def login(client, name="reader", password=PASSWORD):
    csrf = await form(client)
    return await client.post("/api/v1/session", data={"username": name, "password": password, "csrf": csrf})


def test_accounts_lifecycle_and_no_secrets(database):
    with db.open_db(database) as conn:
        row = accounts.authenticate(conn, "READER", PASSWORD)
        assert row["tier"] == 1 and row["password_hash"].startswith("$argon2id$v=19$m=19456,t=2,p=1$")
        version = accounts.fingerprint(row)
        accounts.change_user(conn, "reader", "disable")
        accounts.change_user(conn, "reader", "enable")
        assert accounts.active_session_user(conn, row["user_id"], version) is None
        assert accounts.authenticate(conn, "reader", PASSWORD)
        accounts.change_user(conn, "reader", "reset-password", PASSWORD + "new")
        assert accounts.authenticate(conn, "reader", PASSWORD) is None
        assert accounts.authenticate(conn, "reader", PASSWORD + "new")
        text = json.dumps(accounts.list_users(conn))
        audit = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
        for value in (text, audit):
            assert PASSWORD not in value and "$argon2" not in value and "password_hash" not in value
        assert "user.create" in audit and "auth.login" in audit


@pytest.mark.parametrize("name", ["root", "ab", "中文账号", "some name", "a/b", "x" * 65])
def test_invalid_usernames(database, name):
    with db.open_db(database) as conn, pytest.raises(ValueError):
        accounts.create_user(conn, name, PASSWORD)


@pytest.mark.parametrize("password", ["x" * 14, "x" * 129])
def test_invalid_password_lengths(database, password):
    with db.open_db(database) as conn, pytest.raises(ValueError):
        accounts.create_user(conn, "another", password)


def test_duplicate_and_exact_password(database):
    with db.open_db(database) as conn:
        with pytest.raises(ValueError):
            accounts.create_user(conn, "READER", PASSWORD)
        accounts.create_user(conn, "unicode", "  密码保持原样。全角Ａ与A不同  ")
        assert accounts.authenticate(conn, "unicode", "  密码保持原样。全角Ａ与A不同  ")
        assert accounts.authenticate(conn, "unicode", "密码保持原样。全角Ａ与A不同") is None


def test_user_cli(database, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt: PASSWORD)
    for args in (["create", "cli-reader"], ["list"], ["disable", "cli-reader"],
                 ["enable", "cli-reader"], ["reset-password", "cli-reader"]):
        assert cli.main(["user", *args, "--path", str(database), "--json"]) == 0
        output = capsys.readouterr().out
        # 首轮输出含 fixture 的迁移报告，取 JSON 部分。
        assert "password_hash" not in output and PASSWORD not in output
    values = iter([PASSWORD, "different"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(values))
    assert cli.main(["user", "create", "mismatch", "--path", str(database)]) == 2
    with db.open_db(database) as conn:
        assert conn.execute("SELECT 1 FROM app_user WHERE username='mismatch'").fetchone() is None


@pytest.mark.parametrize("name,error", [
    ("reader", "用户名已存在"),
    ("READER", "用户名已存在"),
    ("root", "保留名称"),
    ("ab", "用户名须为"),
    ("some name", "用户名须为"),
])
def test_create_cli_rejects_before_password(database, monkeypatch, capsys, name, error):
    def unexpected_prompt(prompt):
        pytest.fail("无效或重复用户名不应提示输入密码")

    monkeypatch.setattr("getpass.getpass", unexpected_prompt)
    with db.open_db(database) as conn:
        before = dict(conn.execute("SELECT * FROM app_user WHERE username='reader'").fetchone())
        audit_count = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    assert cli.main(["user", "create", name, "--path", str(database)]) == 2
    assert error in capsys.readouterr().err
    with db.open_db(database) as conn:
        assert dict(conn.execute("SELECT * FROM app_user WHERE username='reader'").fetchone()) == before
        assert conn.execute("SELECT count(*) FROM app_user").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == audit_count


def test_create_cli_concurrent_name_during_prompt(database, monkeypatch, capsys):
    calls = []

    def prompt_password(prompt):
        if not calls:
            with db.open_db(database) as conn:
                accounts.create_user(conn, "racing", PASSWORD + " winner")
        calls.append(prompt)
        return PASSWORD

    monkeypatch.setattr("getpass.getpass", prompt_password)
    assert cli.main(["user", "create", "racing", "--path", str(database)]) == 2
    assert "用户名已存在" in capsys.readouterr().err
    assert len(calls) == 2
    with db.open_db(database) as conn:
        row = conn.execute("SELECT password_hash FROM app_user WHERE username='racing'").fetchone()
        assert passwords.verify_password(row["password_hash"], PASSWORD + " winner")
        assert conn.execute("SELECT count(*) FROM app_user WHERE username='racing'").fetchone()[0] == 1


@pytest.mark.parametrize("value", ["http://archive.example.com", "https://x/", "https://x/path",
                                   "https://x?query", "https://x#token", "https://user@x", "https://x:99999", ""])
def test_invalid_public_origin(value):
    with pytest.raises(ValueError):
        public_origin(value)


@pytest.mark.asyncio
async def test_login_identity_and_logout(database):
    app = create_remote_app(database, ORIGIN)
    async with client_for(app) as client:
        assert (await client.get("/api/v1/session")).status_code == 401
        response = await login(client)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["identity_version"] == 1
        assert data["identity"]["role"] == "invited"
        assert data["scope"]["sources"] == "all"
        assert all(not x["executable"] and x["execution_context"] == "remote" for x in data["operations"])
        assert {x["code"] for x in data["operations"]} == {"content.search", "content.detail"}
        assert (await client.get("/api/v1/session")).json() == data
        cookies = response.headers.get_list("set-cookie")
        assert any(COOKIE in c and "Secure" in c and "HttpOnly" in c and "SameSite=strict" in c for c in cookies)
        assert LOGIN_COOKIE not in client.cookies
        assert (await client.post("/api/v1/logout")).status_code == 403
        assert (await client.post("/api/v1/logout", headers={"X-CSRF-Token": data["csrf"]})).status_code == 200
        assert (await client.get("/api/v1/session")).status_code == 401
        assert (await client.get("/healthz")).status_code == 200


@pytest.mark.asyncio
async def test_uniform_failures_and_root_rejection(database):
    with db.open_db(database) as conn, db.transaction(conn):
        conn.execute("INSERT INTO app_user(uuid,username,tier,password_hash,totp_secret_enc) VALUES (?,?,0,?,?)",
                     (str(uuid4()), "owner", passwords.hash_password(PASSWORD), b"real-test-secret"))
    app = create_remote_app(database, ORIGIN)
    async with client_for(app) as client:
        responses = [await login(client, "unknown"), await login(client, "reader", "bad"),
                     await login(client, "owner"), await login(client, "root")]
        with db.open_db(database) as conn:
            accounts.change_user(conn, "reader", "disable")
        responses.append(await login(client))
        assert all(r.status_code == 401 and r.json() == {"detail": "用户名或密码无效"} for r in responses)
        assert not app.state.auth.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["disable", "reset-password", "disable-enable"])
async def test_session_revocation(database, operation):
    app = create_remote_app(database, ORIGIN)
    async with client_for(app) as client:
        assert (await login(client)).status_code == 200
        with db.open_db(database) as conn:
            if operation == "disable-enable":
                accounts.change_user(conn, "reader", "disable")
                accounts.change_user(conn, "reader", "enable")
            else:
                accounts.change_user(conn, "reader", operation, PASSWORD + "new")
        assert (await client.get("/api/v1/session")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed", [1800, 28800])
async def test_timeouts(database, elapsed):
    app = create_remote_app(database, ORIGIN)
    now = [100.0]
    app.state.auth.clock = lambda: now[0]
    async with client_for(app) as client:
        await login(client)
        if elapsed == 28800:
            for _ in range(31):
                now[0] += 900
                assert (await client.get("/api/v1/session")).status_code == 200
            now[0] = 28900
        else:
            now[0] += elapsed
        assert (await client.get("/api/v1/session")).status_code == 401


@pytest.mark.asyncio
async def test_boundary_body_csrf_and_rate(database):
    app = create_remote_app(database, ORIGIN)
    async with client_for(app) as client:
        assert (await client.get("/", headers={"Host": "evil.example"})).status_code == 403
        assert (await client.post("/api/v1/session", headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.post("/api/v1/session", content=b"x" * 8193,
                                  headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "1"})).status_code == 413
        csrf = await form(client)
        assert (await client.post("/api/v1/session", data={"username": "reader", "password": PASSWORD, "csrf": "bad"})).status_code == 403
        assert (await client.post("/api/v1/session", data={"username": "reader", "password": PASSWORD, "csrf": csrf, "tier": "0"})).status_code == 400
        for _ in range(5):
            assert (await login(client, "reader", "wrong")).status_code == 401
        assert (await login(client, "READER", "wrong")).status_code == 429
    async with client_for(app, peer="192.0.2.1") as client:
        assert (await client.get("/", headers={"X-Forwarded-For": "127.0.0.1"})).status_code == 403


@pytest.mark.asyncio
async def test_global_limit_and_form_expiry(database):
    app = create_remote_app(database, ORIGIN)
    now = [100.0]
    app.state.auth.clock = lambda: now[0]
    async with client_for(app) as client:
        csrf = await form(client)
        now[0] += 300
        assert (await client.post("/api/v1/session", data={"username": "reader", "password": PASSWORD, "csrf": csrf})).status_code == 403
        for i in range(30):
            assert (await login(client, f"unknown{i}")).status_code == 401
        assert (await login(client, "another")).status_code == 429
        assert len(app.state.auth.attempts) <= 30
        now[0] += 60
        assert (await login(client)).status_code == 200


@pytest.mark.asyncio
async def test_route_allowlist_and_restart(database):
    remote = create_remote_app(database, ORIGIN)
    local = create_app(Runtime(database, database.parent / "view-cache", 1024))
    paths = [(route.path, method) for route in local.routes if hasattr(route, "methods")
             for method in route.methods if method in {"GET", "POST"}]
    async with client_for(remote) as client:
        for authenticated in (False, True):
            if authenticated:
                assert (await login(client)).status_code == 200
            for path, method in paths:
                if path in {"/", "/api/v1/session", "/api/v1/logout", "/healthz"}:
                    continue
                path = re.sub(r"\{[^}]+\}", "1", path)
                assert (await client.request(method, path)).status_code == 404, path
            assert (await client.get("/assets/app.js")).status_code == 404
        async with client_for(create_remote_app(database, ORIGIN)) as restarted:
            restarted.cookies.update(client.cookies)
            assert (await restarted.get("/api/v1/session")).status_code == 401
        async with client_for(local, "http://127.0.0.1:8080") as local_client:
            local_client.cookies.set("pc_session", client.cookies.get(COOKIE))
            assert (await local_client.get("/identity")).status_code == 401
            response = await local_client.post("/api/v1/session", json={"token": local.state.sessions.bootstrap})
            assert response.status_code == 200
            assert response.json()["identity"]["role"] == "root"
            assert (await local_client.get("/identity")).json()["identity"]["user_id"] is None
        # 本机令牌不能作为远程表单凭据。
        assert (await client.post("/api/v1/session", json={"token": local.state.sessions.bootstrap})).status_code == 415


def test_command_catalogue_matches_parser():
    parser = cli.build_parser()
    for remote in (False, True):
        data = accounts.identity(remote=remote)
        for operation in data["operations"]:
            command = re.sub(r"<ID>", "1", operation["command"])
            command = re.sub(r"<[^>]+>", "example", command)
            parser.parse_args(shlex.split(command)[1:])
            assert operation["comment"]


def test_remote_cli_factory(database, monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app, **kwargs))
    assert cli.main(["serve", "--mode", "remote", "--port", "8081", "--public-origin", ORIGIN,
                     "--path", str(database)]) == 0
    assert captured["host"] == "127.0.0.1" and not captured["proxy_headers"]
    assert not hasattr(captured["app"].state, "sessions")
    assert "#token=" not in capsys.readouterr().out
    assert cli.main(["serve", "--mode", "remote", "--path", str(database)]) == 2
