"""本机 Web 的真实服务链路、会话边界与系统副作用检查。"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from pc import cli
from pc.adapters import sqlite as db
from pc.adapters import windows_view
from pc.api.app import Runtime, create_app
from pc.domain import ClassificationResult, TagCandidate, TagSource
from pc.services import tagging

ORIGIN = "http://127.0.0.1:8080"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII="
)
BODY = "网页测试中文 <script>window.bad = true</script>\n第二行"


@pytest.fixture
def web(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("PC_DATA_ROOT", str(data))
    path = data / "pc.sqlite3"
    assert cli.main(["db", "apply", "--path", str(path)]) == 0
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "text.txt").write_text(BODY, encoding="utf-8")
    (inputs / "image.png").write_bytes(PNG)
    (inputs / "mixed.png").write_bytes(PNG)
    (inputs / "mixed.txt").write_text("混合说明", encoding="utf-8")
    assert cli.main(["upload", str(inputs), "--path", str(path)]) == 0
    capsys.readouterr()
    with db.open_db(path) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM content_item")]
    targets = {}
    for row in rows:
        name = row["original_name"] or ""
        if name in {"image.png", "mixed.png"}:
            targets[name.partition(".")[0]] = row
        elif row["item_type"] == "text":
            targets["text"] = row
    app = create_app(Runtime(path, data / "view-cache", 50 * 1024 * 1024))
    return {"app": app, "path": path, "data": data, "targets": targets}


@pytest_asyncio.fixture
async def client(web):
    transport = httpx.ASGITransport(app=web["app"], client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(
        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        yield client


async def login(client, web):
    response = await client.post(
        "/api/v1/session", json={"token": web["app"].state.sessions.bootstrap}
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf"]
    return response


def target(web, kind="text"):
    return {"message_id": web["targets"][kind]["message_id"]}


@pytest.mark.asyncio
async def test_authorization_and_public_page(client, web):
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/assets/app.js")).status_code == 200
    assert (await client.get("/healthz")).json()["status"] == "ok"
    for url in [
        "/api/v1/search",
        "/api/v1/tags",
        "/api/v1/aliases?name=x",
        "/api/v1/detail?message_id=1",
        "/api/v1/media/1/1",
        "/api/v1/assignments?message_ids=1",
    ]:
        assert (await client.get(url)).status_code == 401
    response = await client.post("/api/v1/tags/add", json={"message_ids": [1], "names": ["外来"]})
    assert response.status_code == 401
    with db.open_db(web["path"]) as conn:
        assert conn.execute("SELECT count(*) FROM tag").fetchone()[0] == 0
    response = await login(client, web)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert (await client.get("/api/v1/session")).status_code == 200
    assert (await client.get("/api/v1/search")).headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_host_origin_csrf_and_remote(client, web):
    await login(client, web)
    body = {"message_ids": [target(web)["message_id"]], "names": ["测试"]}
    assert (await client.get("/api/v1/search", headers={"Host": "evil.example"})).status_code == 403
    assert (
        await client.post("/api/v1/tags/add", json=body, headers={"Origin": "http://evil.example"})
    ).status_code == 403
    assert (
        await client.post("/api/v1/tags/add", json=body, headers={"X-CSRF-Token": "wrong"})
    ).status_code == 403
    transport = httpx.ASGITransport(app=web["app"], client=("192.0.2.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as remote:
        assert (await remote.get("/", headers={"X-Forwarded-For": "127.0.0.1"})).status_code == 403
    with db.open_db(web["path"]) as conn:
        assert conn.execute("SELECT count(*) FROM tag").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_bootstrap_expiration_and_replay(client, web):
    token = web["app"].state.sessions.bootstrap
    assert (await client.post("/api/v1/session", json={"token": "无效令牌"})).status_code == 401
    await login(client, web)
    assert (await client.post("/api/v1/session", json={"token": token})).status_code == 401
    web["app"].state.sessions.bootstrap = token
    web["app"].state.sessions.deadline = 0
    assert (await client.post("/api/v1/session", json={"token": token})).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["idle", "absolute", "logout", "restart"])
async def test_session_invalidation(client, web, reason):
    await login(client, web)
    sessions = web["app"].state.sessions
    entry = next(iter(sessions.entries.values()))
    if reason == "idle":
        entry["seen"] -= 1801
    elif reason == "absolute":
        entry["created"] -= 28801
    elif reason == "logout":
        assert (await client.post("/api/v1/logout", json={})).status_code == 200
    else:
        restarted = create_app(Runtime(web["path"], web["data"] / "view-cache", 10000))
        transport = httpx.ASGITransport(app=restarted, client=("127.0.0.1", 1))
        async with httpx.AsyncClient(
            transport=transport, base_url=ORIGIN, cookies=client.cookies
        ) as new:
            assert (await new.get("/api/v1/search")).status_code == 401
        return
    assert (await client.get("/api/v1/search")).status_code == 401


@pytest.mark.asyncio
async def test_search_pages_and_input_validation(client, web):
    await login(client, web)
    first = (await client.get("/api/v1/search", params={"sort": "time", "limit": 1})).json()
    assert first["result_type"] == "search" and first["has_more"]
    second = (
        await client.get(
            "/api/v1/search",
            params={"sort": "time", "limit": 1, "cursor": first["next_cursor"], "page": 2},
        )
    ).json()
    assert first["hits"][0]["message_id"] != second["hits"][0]["message_id"]
    for params in [{"limit": 51}, {"page": 0}, {"item_types": "unknown"}, {"actor_id": 1}]:
        assert (await client.get("/api/v1/search", params=params)).status_code == 422
    assert (
        await client.get("/api/v1/detail", params={"message_id": 1, "content_id": 1})
    ).status_code == 400
    assert (
        await client.post(
            "/api/v1/tags/add", json={"message_ids": [1], "names": ["x"], "actor_id": 1}
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_tags_aliases_and_immediate_search(client, web):
    await login(client, web)
    messages = [entry["message_id"] for entry in web["targets"].values()]
    body = {"message_ids": messages, "names": ["网页标签"]}
    added = await client.post("/api/v1/tags/add", json=body)
    assert added.status_code == 200, added.text
    count = len(added.json()["content_ids"])
    assert (await client.get("/api/v1/tags")).json()["tags"][0]["usage_count"] == count
    hits = (await client.get("/api/v1/search", params={"q": "网页标签"})).json()["hits"]
    assert len(hits) == len(messages)
    assert (
        await client.post("/api/v1/aliases/add", json={"name": "网页标签", "alias": "网页别名"})
    ).status_code == 200
    assert len(
        (await client.get("/api/v1/search", params={"tags": "网页别名"})).json()["hits"]
    ) == len(messages)
    assert (await client.get("/api/v1/aliases", params={"name": "网页标签"})).json()["aliases"][0][
        "alias"
    ] == "网页别名"
    assignments = (
        await client.get(
            "/api/v1/assignments", params=[("message_ids", ident) for ident in messages]
        )
    ).json()
    assert len(assignments["assignments"]) == count
    assert (
        await client.post("/api/v1/aliases/remove", json={"alias": "网页别名"})
    ).status_code == 200
    assert (await client.post("/api/v1/tags/remove", json=body)).status_code == 200
    assert not (await client.get("/api/v1/search", params={"q": "网页标签"})).json()["hits"]
    with db.open_db(web["path"]) as conn:
        assert (
            conn.execute("SELECT count(*) FROM audit_log WHERE user_agent='local_root'").fetchone()[
                0
            ]
            > 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["confirm", "reject"])
async def test_suggestions(client, web, operation):
    with db.open_db(web["path"]) as conn:
        classification = ClassificationResult(
            tags=(TagCandidate("候选标签", 0.8),), source=TagSource.SUGGESTED
        )
        tagging.apply_classification(conn, classification, message_ids=[target(web)["message_id"]])
        ident = conn.execute("SELECT assign_id FROM tag_assignment").fetchone()[0]
    await login(client, web)
    response = await client.post(f"/api/v1/suggestions/{operation}", json={"ids": [ident]})
    assert response.status_code == 200, response.text
    hits = (await client.get("/api/v1/search", params={"q": "候选标签"})).json()["hits"]
    assert bool(hits) == (operation == "confirm")


@pytest.mark.asyncio
async def test_detail_media_and_visibility(client, web):
    await login(client, web)
    data = (await client.get("/api/v1/detail", params=target(web, "image"))).json()
    part = data["parts"][0]
    ident = part["media"][0]["media_id"]
    url = f"/api/v1/media/{part['content_id']}/{ident}"
    image = await client.get(url)
    assert image.status_code == 200 and image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert (
        await client.get(f"/api/v1/media/{web['targets']['text']['content_id']}/{ident}")
    ).status_code == 404
    with db.open_db(web["path"]) as conn:
        conn.execute(
            "UPDATE content_item SET deleted_at=? WHERE content_id=?",
            (db.now_iso(), part["content_id"]),
        )
        conn.commit()
    assert (await client.get(url)).status_code == 400
    assert not (web["data"] / "view-cache").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "scan_status='pending'",
        "object_key='../escape'",
        "size_bytes=1",
        "mime_detected='image/svg+xml'",
    ],
)
async def test_media_rejects_unsafe_records(client, web, change):
    await login(client, web)
    data = (await client.get("/api/v1/detail", params=target(web, "image"))).json()
    part = data["parts"][0]
    ident = part["media"][0]["media_id"]
    with db.open_db(web["path"]) as conn:
        conn.execute(f"UPDATE media SET {change} WHERE media_id=?", (ident,))
        conn.commit()
    assert (await client.get(f"/api/v1/media/{part['content_id']}/{ident}")).status_code == 400


@pytest.mark.asyncio
async def test_native_actions_and_mixed_copy(client, web, monkeypatch):
    calls = []
    monkeypatch.setattr(windows_view, "require_windows", lambda: None)
    for name in ("copy_text", "copy_files", "open_files", "reveal_files"):
        monkeypatch.setattr(
            windows_view, name, lambda value, name=name: calls.append((name, value))
        )
    await login(client, web)
    assert (await client.post("/api/v1/actions/copy", json=target(web))).json()["copied"]
    assert calls[-1][0] == "copy_text"
    assert (
        await client.post("/api/v1/actions/open", json=target(web, "image") | {"copy": True})
    ).status_code == 200
    assert [call[0] for call in calls[-2:]] == ["copy_files", "open_files"]
    assert all(Path(path).is_file() for path in calls[-1][1])
    before = len(calls)
    assert (await client.post("/api/v1/actions/copy", json=target(web, "mixed"))).status_code == 400
    assert len(calls) == before
    assert (
        await client.post("/api/v1/actions/reveal", json=target(web, "mixed"))
    ).status_code == 200
    assert calls[-1][1][0].name == "index.html"
    assert (
        await client.post("/api/v1/actions/open", json=target(web) | {"path": "C:/outside"})
    ).status_code == 422


@pytest.mark.asyncio
async def test_partial_system_failure(client, web, monkeypatch):
    monkeypatch.setattr(windows_view, "require_windows", lambda: None)
    monkeypatch.setattr(windows_view, "copy_text", lambda value: None)

    def fail(paths):
        raise OSError("默认程序失败")

    monkeypatch.setattr(windows_view, "open_files", fail)
    await login(client, web)
    response = await client.post("/api/v1/actions/open", json=target(web) | {"copy": True})
    assert response.status_code == 400 and response.json()["copied"]


def test_serve_and_cli_defaults(web, monkeypatch, capsys):
    import uvicorn

    called = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: called.update(app=app, **options))
    assert cli.main(["serve", "--path", str(web["path"]), "--port", "8135"]) == 0
    assert called["host"] == "127.0.0.1" and called["proxy_headers"] is False
    assert "http://127.0.0.1:8135/#token=" in capsys.readouterr().out
    assert cli.main(["serve", "--path", str(web["path"]), "--port", "0"]) == 2


@pytest.mark.asyncio
async def test_page_csp_and_text_data(client, web):
    await login(client, web)
    page = await client.get("/")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    script = (await client.get("/assets/app.js")).text
    assert "innerHTML" not in script and "textContent" in script
    detail = (await client.get("/api/v1/detail", params=target(web))).json()
    assert "<script>" in detail["parts"][0]["text"]
    assert "bootstrap" not in (await client.get("/openapi.json")).text
