"""本机 Web 的真实服务链路、会话边界与系统副作用检查。"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from pc import cli
from pc.adapters import sqlite as db
from pc.adapters import windows_view
from pc.api.app import Runtime, create_app

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
    runtime = Runtime(path, data / "view-cache", 50 * 1024 * 1024)
    app = create_app(runtime)
    return {"app": app, "path": path, "data": data, "targets": targets, "runtime": runtime}


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
    assert (await client.get("/compositions")).status_code == 200
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
        # 升级前已持久化的无证明建议仍可审核；现行生产入口禁止制造此类行。
        tag = conn.execute("INSERT INTO tag(uuid,name,name_norm,namespace,status) VALUES (?,'候选标签','候选标签','general','pending') RETURNING tag_id", (str(uuid.uuid4()),)).fetchone()[0]
        content = web["targets"]["text"]["content_id"]
        conn.execute("INSERT INTO classification_run(content_id,capability) VALUES (?,'llm')", (content,))
        ident = conn.execute("INSERT INTO tag_assignment(content_id,tag_id,source,confidence,evidence) VALUES (?,?,'suggested',0.8,'{}') RETURNING assign_id", (content, tag)).fetchone()[0]
        conn.commit()
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
    assert "textContent" in script
    assert 'type="module"' in page.text
    detail = (await client.get("/api/v1/detail", params=target(web))).json()
    assert "<script>" in detail["parts"][0]["text"]
    assert "bootstrap" not in (await client.get("/openapi.json")).text


@pytest.mark.asyncio
async def test_spa_routes_and_missing_resources(client):
    home = await client.get("/")
    for route in ("/add", "/manage", "/model"):
        assert (await client.get(route)).text == home.text
    assert (await client.get("/api/v1/missing")).status_code == 404
    assert (await client.get("/assets/missing.js")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("csrf", [False, True])
async def test_upload_auth_before_reading(client, web, csrf):
    if csrf:
        await login(client, web)
        client.headers["X-CSRF-Token"] = "wrong"

    async def unread():
        raise AssertionError("鉴权失败应当先于读取请求体")
        yield b""

    response = await client.post("/api/v1/uploads", content=unread(), headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert response.status_code == (403 if csrf else 401)


@pytest.mark.parametrize("part", [
    {"kind": "text", "text": None},
    {"kind": "link", "url": "https://example.org/"},
    {"kind": "image"},
])
def test_malformed_persisted_ordered_parts_are_permanent_failures(part):
    import json
    from types import SimpleNamespace

    from pc.services.pipeline import PermanentError, Worker

    fake = SimpleNamespace(_require_authorized=lambda source_id: None,
                           _refs=lambda message: [], _tombstoned=lambda *args, **kwargs: False)
    with pytest.raises(PermanentError, match="有序"):
        Worker._collect(fake, {"ordered_parts": json.dumps([part]), "media_refs": "[]"}, {"source_id": 1})


@pytest.mark.asyncio
async def test_ordered_upload_rejects_mixed_legacy_text_and_duplicate_image_indices(client, web):
    import json

    await login(client, web)
    both = await client.post("/api/v1/uploads", files={
        "parts": (None, json.dumps([{"kind": "text", "text": "一"}])), "text": (None, "旧文本"),
    })
    assert both.status_code in {400, 422}, both.text
    duplicate = await client.post("/api/v1/uploads", files=[
        ("parts", (None, json.dumps([{"kind": "image", "index": 0}, {"kind": "image", "index": 0}]))),
        ("images", ("once.png", PNG)),
    ])
    assert duplicate.status_code in {400, 422}, duplicate.text


@pytest.mark.asyncio
async def test_ordered_upload_interleaves_text_image_and_link(client, web):
    import json

    await login(client, web)
    parts = [
        {"kind": "text", "text": "第一段"},
        {"kind": "image", "index": 0},
        {"kind": "text", "text": "第二段 https://example.org/a"},
        {"kind": "link", "url": "HTTPS://Example.ORG:443/page#one", "description": "人工说明"},
    ]
    response = await client.post("/api/v1/uploads", files=[
        ("parts", (None, json.dumps(parts))), ("images", ("ordered.png", PNG)),
    ])
    assert response.status_code == 200, response.text
    detail = (await client.get("/api/v1/detail", params={"message_id": response.json()["message_id"]})).json()
    assert [(part["ordinal"], part["item_type"], part["text"]) for part in detail["parts"]] == [
        (0, "text", "第一段"), (1, "image", ""),
        (2, "text", "第二段 https://example.org/a"), (3, "link", "人工说明"),
    ]
    assert detail["parts"][3]["url"] == "https://example.org/page#one"
    link_id = detail["parts"][3]["content_id"]
    with db.open_db(web["path"]) as conn:
        db.refresh_search_derived(conn, content_ids=[link_id])
        link_row = conn.execute("SELECT original_name,search_extra FROM content_item WHERE content_id=?",
                                (link_id,)).fetchone()
        assert link_row["original_name"] is None
        assert "ordered.png" not in link_row["search_extra"]
        conn.execute("UPDATE content_item SET original_name='ordered.png' WHERE content_id=?", (link_id,))
        db.refresh_search_derived(conn, content_ids=[link_id])
        repaired = conn.execute("SELECT original_name,search_extra FROM content_item WHERE content_id=?",
                                (link_id,)).fetchone()
        assert repaired["original_name"] is None
        assert "ordered.png" not in repaired["search_extra"]


@pytest.mark.asyncio
async def test_unified_search_returns_items_and_matching_compositions_once(client, web):
    import json

    await login(client, web)
    uploaded = await client.post("/api/v1/uploads", files={
        "parts": (None, json.dumps([
            {"kind": "text", "text": "红杉独特词"},
            {"kind": "text", "text": "红杉独特词 再现"},
        ])),
        "create_composition": (None, "true"),
        "composition_name": (None, "集合名称"),
        "composition_description": (None, "专属组合说明"),
    })
    assert uploaded.status_code == 200, uploaded.text
    ids = uploaded.json()["content_ids"]
    found = (await client.get("/api/v1/search", params={"q": "红杉独特词", "result_version": 2})).json()
    assert found["result_version"] == 2, found
    assert {hit["content_id"] for hit in found["hits"] if hit["kind"] == "content"} == set(ids)
    assert all(hit["source_name"] == "manual" for hit in found["hits"] if hit["kind"] == "content")
    assert [hit["composition_id"] for hit in found["hits"] if hit["kind"] == "composition"] == [uploaded.json()["composition_id"]]
    only = (await client.get("/api/v1/search", params={"q": "专属组合说明", "result_version": 2, "kind": "composition"})).json()
    assert [hit["composition_id"] for hit in only["hits"]] == [uploaded.json()["composition_id"]]
    assert all(hit["kind"] == "composition" for hit in only["hits"])
    spanning = (await client.get("/api/v1/search", params={
        "q": "集合名称 再现", "result_version": 2, "kind": "composition",
    })).json()
    assert [hit["composition_id"] for hit in spanning["hits"]] == [uploaded.json()["composition_id"]]
    filtered = (await client.get("/api/v1/search", params={
        "q": "红杉独特词", "result_version": 2, "kind": "composition",
        "source": "manual", "item_types": "text", "statuses": "active",
    })).json()
    assert [hit["composition_id"] for hit in filtered["hits"]] == [uploaded.json()["composition_id"]]
    wrong_type = (await client.get("/api/v1/search", params={
        "q": "红杉独特词", "result_version": 2, "kind": "composition", "item_types": "image",
    })).json()
    assert wrong_type["hits"] == []
    first_page = (await client.get("/api/v1/search", params={
        "result_version": 2, "q": "红杉独特词", "limit": 1, "sort": "time",
    })).json()
    second_page_response = await client.get("/api/v1/search", params={
        "result_version": 2, "q": "红杉独特词", "limit": 1, "sort": "time",
        "cursor": first_page["next_cursor"], "page": 2,
    })
    assert second_page_response.status_code == 200, second_page_response.text
    second_page = second_page_response.json()
    assert (first_page["hits"][0]["kind"], first_page["hits"][0]["entity_id"]) != (
        second_page["hits"][0]["kind"], second_page["hits"][0]["entity_id"])
    malformed = await client.get("/api/v1/search", params={
        "result_version": 2, "cursor": "a", "sort": "time",
    })
    assert malformed.status_code == 400, malformed.text


@pytest.mark.asyncio
async def test_unified_keywords_match_across_distinct_visible_members(client, web):
    import json

    await login(client, web)
    uploaded = await client.post("/api/v1/uploads", files={
        "parts": (None, json.dumps([{"kind": "text", "text": "甲成员专用字"},
                                    {"kind": "text", "text": "乙成员独有字"}])),
        "create_composition": (None, "true"), "composition_name": (None, "两件资料"),
    })
    assert uploaded.status_code == 200, uploaded.text
    from pc.services import compositions

    with db.open_db(web["path"]) as conn:
        compositions.write(conn, "create", name="只含第一件", contents=[uploaded.json()["content_ids"][0]])
    found = await client.get("/api/v1/search", params={
        "result_version": 2, "q": "甲成员专用字 乙成员独有字",
    })
    assert found.status_code == 200, found.text
    assert [(hit["kind"], hit["composition_id"]) for hit in found.json()["hits"]] == [
        ("composition", uploaded.json()["composition_id"]),
    ]


@pytest.mark.asyncio
async def test_unified_composition_filters_require_same_visible_member(client, web):
    from pc.services import compositions

    await login(client, web)
    with db.open_db(web["path"]) as conn:
        other_source = conn.execute(
            "INSERT INTO source(uuid,name,platform,kind,config) VALUES (?, 'other-source', 'local', 'export', '{}') "
            "RETURNING source_id", (str(uuid.uuid4()),),
        ).fetchone()[0]
        conn.execute("UPDATE message SET source_id=? WHERE message_id=?",
                     (other_source, web["targets"]["image"]["message_id"]))
        ident = compositions.write(conn, "create", name="源类型同人", contents=[
            web["targets"]["text"]["content_id"], web["targets"]["image"]["content_id"],
        ])["composition_id"]
    base = {"result_version": 2, "q": "源类型同人", "kind": "composition", "item_types": "image"}
    mismatch = await client.get("/api/v1/search", params={**base, "source": "manual"})
    assert mismatch.status_code == 200, mismatch.text
    assert mismatch.json()["hits"] == []
    match = await client.get("/api/v1/search", params={**base, "source": "other-source"})
    assert match.status_code == 200, match.text
    assert [row["composition_id"] for row in match.json()["hits"]] == [ident]


@pytest.mark.asyncio
async def test_unified_member_match_returns_each_containing_composition_once(client, web):
    from pc.services import compositions

    await login(client, web)
    uploaded = await client.post("/api/v1/uploads", files={"text": (None, "同一内容命中多个组合")})
    assert uploaded.status_code == 200, uploaded.text
    content_id = uploaded.json()["content_ids"][0]
    with db.open_db(web["path"]) as conn:
        first = compositions.write(conn, "create", name="甲组合", contents=[content_id, content_id])["composition_id"]
        second = compositions.write(conn, "create", name="乙组合", contents=[content_id])["composition_id"]
    found = await client.get("/api/v1/search", params={
        "result_version": 2, "q": "同一内容命中多个组合", "kind": "composition",
    })
    assert found.status_code == 200, found.text
    assert sorted(row["composition_id"] for row in found.json()["hits"]) == sorted([first, second])


def test_unified_keyword_materializes_fts_once_for_items_and_compositions(web):
    from pc.services.web_search import run_web_search

    with db.open_db(web["path"]) as conn:
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            run_web_search(conn, q="蓝发", limit=1)
        finally:
            conn.set_trace_callback(None)
    statement = next(sql for sql in statements if "entities AS" in sql)
    assert "AS MATERIALIZED" in statement, statement
    assert "term_0_compositions AS MATERIALIZED" in statement, statement
    assert "FROM composition_member cm CROSS JOIN content_item c" in statement, statement
    assert "FROM content_item c INDEXED BY content_item_time_idx" in statement, statement
    assert statement.count("content_word MATCH") == 1, statement


def test_unified_search_limits_each_entity_kind_before_global_merge(web):
    from pc.services.web_search import run_web_search

    with db.open_db(web["path"]) as conn:
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            result = run_web_search(conn, limit=1)
        finally:
            conn.set_trace_callback(None)
    statement = next(sql for sql in statements if sql.startswith("WITH entities AS"))
    assert statement.count(" LIMIT ") >= 3, statement
    assert len(result["hits"]) == 1


@pytest.mark.asyncio
async def test_unified_mixed_type_filter_matches_heterogeneous_composition(client, web):
    from pc.services import compositions

    await login(client, web)
    with db.open_db(web["path"]) as conn:
        ident = compositions.write(conn, "create", name="文图合一", contents=[
            web["targets"]["text"]["content_id"], web["targets"]["image"]["content_id"],
        ])["composition_id"]
    response = await client.get("/api/v1/search", params={
        "result_version": 2, "kind": "composition", "q": "文图合一", "item_types": "mixed",
    })
    assert response.status_code == 200, response.text
    assert [entry["composition_id"] for entry in response.json()["hits"]] == [ident]


@pytest.mark.asyncio
async def test_unified_time_search_does_not_lose_composition_after_many_member_hits(client, web):
    from pc.services import compositions

    await login(client, web)
    with db.open_db(web["path"]) as conn:
        message_id = web["targets"]["text"]["message_id"]
        conn.executemany(
            "INSERT INTO content_item(uuid,message_id,item_type,ordinal,text_content,text_search,status) "
            "VALUES (?,?, 'text',?,?,?, 'active')",
            [(str(uuid.uuid4()), message_id, index + 100, "稀有三字词", "稀有三字词") for index in range(2003)],
        )
        last_id = conn.execute("SELECT max(content_id) FROM content_item").fetchone()[0]
        ident = compositions.write(conn, "create", name="不含关键词的组合", contents=[last_id])["composition_id"]
    result = await client.get("/api/v1/search", params={
        "result_version": 2, "q": "稀有三字词", "kind": "composition", "sort": "time",
    })
    assert result.status_code == 200, result.text
    assert [hit["composition_id"] for hit in result.json()["hits"]] == [ident]
    assert result.json()["incomplete"] is False


@pytest.mark.asyncio
async def test_unified_composition_sent_time_uses_latest_visible_member(client, web):
    from pc.services import compositions

    await login(client, web)
    older = web["targets"]["text"]
    newer = web["targets"]["image"]
    with db.open_db(web["path"]) as conn:
        conn.execute("UPDATE message SET sent_at=? WHERE message_id=?", ("2025-01-01T00:00:00.000Z", older["message_id"]))
        conn.execute("UPDATE message SET sent_at=? WHERE message_id=?", ("2025-02-01T00:00:00.000Z", newer["message_id"]))
        composition_id = compositions.write(conn, "create", name="可见发送时间", contents=[older["content_id"], newer["content_id"]])["composition_id"]
    params = {"result_version": 2, "kind": "composition", "q": "可见发送时间", "time_field": "sent",
              "since": "2025-02-01", "until": "2025-02-01"}
    response = await client.get("/api/v1/search", params=params)
    assert response.status_code == 200, response.text
    assert [(hit["composition_id"], hit["sort_time"]) for hit in response.json()["hits"]] == [
        (composition_id, "2025-02-01T00:00:00.000Z"),
    ]
    with db.open_db(web["path"]) as conn:
        conn.execute("UPDATE content_item SET status='quarantined' WHERE content_id=?", (newer["content_id"],))
    hidden = await client.get("/api/v1/search", params=params)
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["hits"] == []


@pytest.mark.asyncio
async def test_ordered_link_duplicate_ignores_fragment_but_preserves_positions(client, web):
    import json

    await login(client, web)
    async def submit(url):
        return await client.post("/api/v1/uploads", files={"parts": (None, json.dumps([
            {"kind": "link", "url": url, "description": "说明"},
            {"kind": "link", "url": url, "description": "说明"},
        ]))})
    first = await submit("HTTPS://Example.org:443/path#one")
    assert first.status_code == 200, first.text
    assert len(first.json()["content_ids"]) == 2
    with db.open_db(web["path"]) as conn:
        signature = conn.execute("SELECT content_signature FROM post WHERE message_id=?", (first.json()["message_id"],)).fetchone()
        assert signature is not None and signature[0]
    again = await submit("https://example.org/path#two")
    assert again.status_code == 200, again.text
    assert again.json()["duplicate"] is True
    assert again.json()["content_ids"] == first.json()["content_ids"]


@pytest.mark.asyncio
async def test_upload_creates_composition_with_independent_tags(client, web):
    import json

    await login(client, web)
    parts = [{"kind": "text", "text": "先看文字"}, {"kind": "image", "index": 0},
             {"kind": "text", "text": "再读后文"},
             {"kind": "link", "url": "HTTPS://Example.org:443/archive#one", "description": "后续链接"}]
    response = await client.post("/api/v1/uploads", files=[
        ("parts", (None, json.dumps(parts))), ("images", ("atomic.png", PNG)),
        ("create_composition", (None, "true")), ("composition_name", (None, "顺序组合")),
        ("composition_description", (None, "保留顺序")), ("composition_tags", (None, "仅组合")),
    ])
    assert response.status_code == 200, response.text
    data = response.json()
    detail = (await client.get(f'/api/v1/compositions/{data["composition_id"]}')).json()
    assert [member["content_id"] for member in detail["members"]] == data["content_ids"]
    assert [tag["name"] for tag in detail["tags"]] == ["仅组合"]
    assert all(not member["tags"] for member in detail["members"])
    assert detail["members"][3]["url"] == "https://example.org/archive#one"
    found = await client.get("/api/v1/search", params={
        "result_version": 2, "q": "顺序组合", "kind": "composition",
    })
    assert found.status_code == 200, found.text
    assert [tag["name"] for tag in found.json()["hits"][0]["tags"]] == ["仅组合"]
    with db.open_db(web["path"]) as conn:
        model_id = conn.execute("INSERT INTO model_version(capability,model_name) VALUES ('llm','test') "
                                "RETURNING model_version_id").fetchone()[0]
        conn.execute("UPDATE composition_tag_assignment SET source='suggested', confidence=0.8, "
                     "model_version_id=? WHERE composition_id=?", (model_id, data["composition_id"]))
    filtered = await client.get("/api/v1/search", params={
        "result_version": 2, "kind": "composition", "tags": "仅组合",
    })
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["hits"] == []


@pytest.mark.asyncio
async def test_upload_with_composition_scan_rejects_all_archive_parts(client, web):
    await login(client, web)
    with db.open_db(web["path"]) as conn:
        before = tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("message", "content_item", "composition"))
    gif = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 29
    response = await client.post("/api/v1/uploads", files=[
        ("text", (None, "不应部分保存")),
        ("images", ("first.gif", gif)),
        ("images", ("fake.png", b"MZ" + b"bad executable" * 4)),
        ("create_composition", (None, "true")), ("composition_name", (None, "应回滚")),
    ])
    assert response.status_code in {400, 422}, response.text
    with db.open_db(web["path"]) as conn:
        assert tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("message", "content_item", "composition")) == before
    assert list((web["data"] / "quarantine").rglob("*"))
    import hashlib
    sha = hashlib.sha256(gif).hexdigest()
    assert not (web["data"] / "media" / sha[:2] / sha[2:4] / sha).exists()


@pytest.mark.asyncio
async def test_upload_with_composition_rolls_back_new_archive_when_tag_invalid(client, web):
    await login(client, web)
    with db.open_db(web["path"]) as conn:
        before = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("message", "content_item", "post", "composition", "tag")}
    import hashlib
    gif = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 33
    response = await client.post("/api/v1/uploads", files=[
        ("text", (None, "原子提交不能留下消息")),
        ("images", ("new.gif", gif)),
        ("create_composition", (None, "true")),
        ("composition_name", (None, "新组合")),
        ("composition_tags", (None, "\u0001非法")),
    ])
    assert response.status_code in {400, 422}, response.text
    with db.open_db(web["path"]) as conn:
        assert {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before} == before
    sha = hashlib.sha256(gif).hexdigest()
    assert not (web["data"] / "media" / sha[:2] / sha[2:4] / sha).exists()


@pytest.mark.asyncio
async def test_atomic_failure_preserves_preexisting_shared_media(client, web):
    import hashlib

    await login(client, web)
    digest = hashlib.sha256(PNG).hexdigest()
    with db.open_db(web["path"]) as conn:
        old = conn.execute("SELECT media_id FROM media WHERE sha256=?", (digest,)).fetchone()
        assert old is not None
        before = tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                       for table in ("media", "message", "content_item", "composition", "tag"))
    response = await client.post("/api/v1/uploads", files=[
        ("text", (None, "含共享图片的失败组合")), ("images", ("already.png", PNG)),
        ("create_composition", (None, "true")), ("composition_name", (None, "失败组合")),
        ("composition_tags", (None, "\u0001非法")),
    ])
    assert response.status_code in {400, 422}, response.text
    with db.open_db(web["path"]) as conn:
        assert tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("media", "message", "content_item", "composition", "tag")) == before
        assert conn.execute("SELECT media_id FROM media WHERE sha256=?", (digest,)).fetchone()[0] == old[0]
    assert (web["data"] / "media" / digest[:2] / digest[2:4] / digest).read_bytes() == PNG


@pytest.mark.asyncio
async def test_atomic_composition_over_existing_duplicate_message(client, web):
    await login(client, web)
    first = await client.post("/api/v1/uploads", files={"text": (None, "复用旧消息的组合")})
    assert first.status_code == 200, first.text
    with db.open_db(web["path"]) as conn:
        before = conn.execute("SELECT count(*) FROM message").fetchone()[0]
    response = await client.post("/api/v1/uploads", files={
        "text": (None, "复用旧消息的组合"), "create_composition": (None, "true"),
        "composition_name": (None, "只新增组合"),
    })
    assert response.status_code == 200, response.text
    assert response.json()["duplicate"] is True
    assert response.json()["message_id"] == first.json()["message_id"]
    assert response.json()["content_ids"] == first.json()["content_ids"]
    with db.open_db(web["path"]) as conn:
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == before
        assert conn.execute("SELECT count(*) FROM composition WHERE composition_id=?",
                            (response.json()["composition_id"],)).fetchone()[0] == 1
        composition_count = conn.execute("SELECT count(*) FROM composition").fetchone()[0]
    rejected = await client.post("/api/v1/uploads", files={
        "text": (None, "复用旧消息的组合"), "create_composition": (None, "true"),
        "composition_name": (None, "失败不能删旧消息"), "composition_tags": (None, "\u0001非法"),
    })
    assert rejected.status_code in {400, 422}, rejected.text
    with db.open_db(web["path"]) as conn:
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == before
        assert conn.execute("SELECT count(*) FROM composition").fetchone()[0] == composition_count
        assert conn.execute("SELECT count(*) FROM content_item WHERE message_id=?",
                            (first.json()["message_id"],)).fetchone()[0] == len(first.json()["content_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize("ordered", [False, True])
async def test_ordinary_duplicate_after_atomic_upload_reports_success(client, web, ordered):
    import json

    await login(client, web)
    payload = {"parts": (None, json.dumps([{"kind": "text", "text": "原子上传后重复"}]))} if ordered else {
        "text": (None, "原子上传后重复"),
    }
    first = await client.post("/api/v1/uploads", files={
        **payload, "create_composition": (None, "true"), "composition_name": (None, "原子组合"),
    })
    assert first.status_code == 200, first.text
    saved = first.json()
    with db.open_db(web["path"]) as conn:
        assert conn.execute("SELECT 1 FROM job WHERE target_type='message' AND target_id=?",
                            (saved["message_id"],)).fetchone() is None
    for _ in range(2):
        again = await client.post("/api/v1/uploads", files=payload)
        assert again.status_code == 200, again.text
        result = again.json()
        assert result["duplicate"] is True
        assert result["message_id"] == saved["message_id"]
        assert result["content_ids"] == saved["content_ids"]
        assert result["status"] == result["job_status"] == "succeeded"
    with db.open_db(web["path"]) as conn:
        assert conn.execute("SELECT count(*) FROM composition WHERE composition_id=?",
                            (saved["composition_id"],)).fetchone()[0] == 1
        # 没有任务且没有成功归档证据时，不能仅凭现存内容误报成功。
        conn.execute("DELETE FROM audit_log WHERE action='upload.submit' AND target_type='message' AND target_id=?",
                     (str(saved["message_id"]),))
    unknown = await client.post("/api/v1/uploads", files=payload)
    assert unknown.json()["status"] == unknown.json()["job_status"] == "dead"


@pytest.mark.asyncio
async def test_atomic_upload_permanent_worker_failure_returns_client_error(client, web, monkeypatch):
    from pc.services.pipeline import PermanentError, Worker

    await login(client, web)
    monkeypatch.setattr(Worker, "_persist", lambda *args, **kwargs: (_ for _ in ()).throw(PermanentError("墓地拒绝")))
    with db.open_db(web["path"]) as conn:
        before = conn.execute("SELECT count(*) FROM message").fetchone()[0]
    response = await client.post("/api/v1/uploads", files={
        "text": (None, "永久拒绝的样本"), "create_composition": (None, "true"),
        "composition_name": (None, "失败组合"),
    })
    assert response.status_code in {400, 422}, response.text
    with db.open_db(web["path"]) as conn:
        assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == before


@pytest.mark.asyncio
async def test_upload_text_and_duplicate(client, web):
    await login(client, web)
    async def post(text, note):
        return await client.post("/api/v1/uploads", files={"text": (None, text), "note": (None, note)})
    first = await post("新内容 中文\n原始格式", "第一份备注")
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "succeeded" and not data["duplicate"] and len(data["content_ids"]) == 1
    second = (await post("新内容 中文 原始格式", "后来的备注")).json()
    assert second["duplicate"] and second["message_id"] == data["message_id"]
    detail = (await client.get("/api/v1/detail", params={"message_id": data["message_id"]})).json()
    assert detail["parts"][0]["text"] == "新内容 中文\n原始格式"
    assert detail["origin_note"] == "第一份备注"


@pytest.mark.asyncio
async def test_upload_single_image_cli_compatibility(client, web):
    await login(client, web)
    response = await client.post("/api/v1/uploads", files={"images": ("更名.png", PNG)})
    assert response.status_code == 200, response.text
    assert response.json()["duplicate"]
    assert response.json()["message_id"] == web["targets"]["image"]["message_id"]
    assert not list((web["data"] / "upload-staging").iterdir())


@pytest.mark.asyncio
async def test_upload_multi_image_order_names_and_mixed(client, web):
    await login(client, web)
    gif = b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 20
    files = [("images", ("同名.png", PNG)), ("images", ("同名.gif", gif)), ("text", (None, "多图配文"))]
    response = await client.post("/api/v1/uploads", files=files)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "succeeded" and len(data["content_ids"]) == 3
    detail = (await client.get("/api/v1/detail", params={"message_id": data["message_id"]})).json()
    assert [p["item_type"] for p in detail["parts"]] == ["text", "image", "image"]
    assert [p["original_name"] for p in detail["parts"][1:]] == ["同名.png", "同名.gif"]
    assert not list((web["data"] / "upload-staging").iterdir())
    again = (await client.post("/api/v1/uploads", files=files)).json()
    assert again["duplicate"] and again["message_id"] == data["message_id"]
    reversed_files = [files[1], files[0], files[2]]
    assert (await client.post("/api/v1/uploads", files=reversed_files)).json()["message_id"] != data["message_id"]


@pytest.mark.asyncio
async def test_upload_same_filenames_are_separate(client, web):
    await login(client, web)
    response = await client.post("/api/v1/uploads", files=[("images", ("same.png", PNG)), ("images", ("same.png", PNG)), ("text", (None, "同名多图"))])
    assert response.status_code == 200, response.text
    assert len(response.json()["content_ids"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [
    [("text", (None, "  \n"))], [("actor_id", (None, "1"))],
    [("text", (None, "a")), ("text", (None, "b"))],
    [("images", ("../outside.png", PNG))], [("images", ("a.png", b""))],
    [("images", ("a.txt", b"text"))], [("images", ("NUL.png", PNG))],
    [("images", (None, "fake"))], [("text", ("x.txt", b"text"))],
    [("note", (None, "a" * 4097)), ("text", (None, "text"))],
])
async def test_upload_invalid_form(client, web, files):
    await login(client, web)
    response = await client.post("/api/v1/uploads", files=files)
    assert response.status_code in {400, 422}, response.text
    root = web["data"] / "upload-staging"
    assert not root.exists() or not list(root.iterdir())


@pytest.mark.asyncio
async def test_upload_length_and_count_limits(client, web):
    from dataclasses import replace

    await login(client, web)
    web["runtime"].limits = replace(web["runtime"].limits, max_text_chars=3)
    assert (await client.post("/api/v1/uploads", files={"text": (None, "四个字符")})).status_code == 400
    web["runtime"].media_limit = 10
    assert (await client.post("/api/v1/uploads", files={"images": ("x.png", PNG)})).status_code == 413
    web["runtime"].media_limit = 100000
    assert (await client.post("/api/v1/uploads", files=[("images", (f"{i}.png", PNG)) for i in range(20)])).status_code == 400


@pytest.mark.asyncio
async def test_upload_actual_body_and_total_limit(client, web, monkeypatch):
    from pc.adapters import upload_files

    await login(client, web)
    request = client.build_request("POST", "/api/v1/uploads", files={"images": ("x.png", PNG)})
    body = request.read()
    monkeypatch.setattr(upload_files, "MAX_REQUEST_BYTES", len(body) - 1)

    async def stream():
        yield body[:40]
        yield body[40:]

    response = await client.post("/api/v1/uploads", content=stream(), headers={"Content-Type": request.headers["Content-Type"], "Content-Length": "1"})
    assert response.status_code == 413
    monkeypatch.setattr(upload_files, "MAX_REQUEST_BYTES", 1000000)
    monkeypatch.setattr(upload_files, "MAX_TOTAL_BYTES", len(PNG) + 1)
    assert (await client.post("/api/v1/uploads", files=[("images", ("a.png", PNG)), ("images", ("b.png", PNG))])).status_code == 413


@pytest.mark.asyncio
async def test_upload_quarantine(client, web):
    await login(client, web)
    response = await client.post("/api/v1/uploads", files={"images": ("fake.png", b"MZ" + b"bad executable" * 4)})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "quarantined" and not response.json()["content_ids"]
    assert not list((web["data"] / "upload-staging").iterdir())
    assert list((web["data"] / "quarantine").rglob("*"))


@pytest.mark.asyncio
async def test_upload_non_image_with_image_name_is_quarantined(client, web):
    await login(client, web)
    # MP4 是普通管线支持的媒体，但浏览器入口只接收图片。
    video = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 30
    response = await client.post("/api/v1/uploads", files={"images": ("video.png", video)})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "quarantined"
    assert not response.json()["content_ids"]


@pytest.mark.asyncio
async def test_upload_revoked_authorization(client, web):
    await login(client, web)
    with db.open_db(web["path"]) as conn:
        conn.execute("UPDATE source_authorization SET revoked_at=?", (db.now_iso(),))
    response = await client.post("/api/v1/uploads", files={"images": ("x.png", PNG)})
    assert response.status_code == 403
    assert not (web["data"] / "upload-staging").exists()


@pytest.mark.asyncio
async def test_upload_retry_retains_files_and_work_cleans(client, web, monkeypatch):
    from pc.services.pipeline import Worker

    await login(client, web)
    original = Worker._collect_one
    monkeypatch.setattr(Worker, "_collect_one", lambda *args: (_ for _ in ()).throw(OSError("暂时失败")))
    response = await client.post("/api/v1/uploads", files=[("images", ("x.png", PNG)), ("text", (None, "重试样本"))])
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "failed"
    assert list((web["data"] / "upload-staging").rglob("x.png"))
    monkeypatch.setattr(Worker, "_collect_one", original)
    with db.open_db(web["path"]) as conn:
        conn.execute("UPDATE job SET run_after='2000-01-01T00:00:00.000Z' WHERE target_id=?", (data["message_id"],))
    assert cli.main(["work", "--path", str(web["path"])]) == 0
    assert not list((web["data"] / "upload-staging").iterdir())


@pytest.mark.asyncio
async def test_upload_does_not_run_other_jobs(client, web):
    path = web["data"] / "later.txt"
    path.write_text("排队但不执行", encoding="utf-8")
    assert cli.main(["upload", str(path), "--no-process", "--path", str(web["path"])]) == 0
    with db.open_db(web["path"]) as conn:
        old = dict(conn.execute("SELECT * FROM job WHERE status='queued'").fetchone())
    await login(client, web)
    assert (await client.post("/api/v1/uploads", files={"text": (None, "定向处理")})).json()["status"] == "succeeded"
    with db.open_db(web["path"]) as conn:
        assert dict(conn.execute("SELECT * FROM job WHERE job_id=?", (old["job_id"],)).fetchone()) == old


@pytest.mark.asyncio
async def test_upload_staging_redirect_rejected(client, web):
    import os

    destination = web["data"] / "outside"
    destination.mkdir()
    root = web["data"] / "upload-staging"
    try:
        os.symlink(destination, root, target_is_directory=True)
    except OSError:
        pytest.skip("系统不允许创建符号链接")
    await login(client, web)
    assert (await client.post("/api/v1/uploads", files={"images": ("x.png", PNG)})).status_code == 400
    assert not list(destination.iterdir())


@pytest.mark.asyncio
async def test_upload_dead_cleans_and_enqueue_failure_cleans(client, web, monkeypatch):
    from pc.services import ingest
    from pc.services.pipeline import PermanentError, Worker

    await login(client, web)
    monkeypatch.setattr(Worker, "_collect_one", lambda *args: (_ for _ in ()).throw(PermanentError("终态失败")))
    response = await client.post("/api/v1/uploads", files=[("images", ("x.png", PNG)), ("text", (None, "死信样本"))])
    assert response.json()["status"] == "dead"
    assert not list((web["data"] / "upload-staging").iterdir())
    monkeypatch.setattr(ingest, "ingest_messages", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("入队失败")))
    response = await client.post("/api/v1/uploads", files={"images": ("x.png", PNG)})
    assert response.status_code == 503
    assert not list((web["data"] / "upload-staging").iterdir())


@pytest.mark.asyncio
async def test_upload_targeted_recovery_leaves_other_running_jobs(client, web):
    path = web["data"] / "stale.txt"
    path.write_text("独立超时任务", encoding="utf-8")
    assert cli.main(["upload", str(path), "--no-process", "--path", str(web["path"])]) == 0
    with db.open_db(web["path"]) as conn:
        conn.execute("UPDATE job SET status='running',attempts=1,locked_at='2000-01-01T00:00:00.000Z',locked_by='other' WHERE status='queued'")
        old = dict(conn.execute("SELECT * FROM job WHERE status='running'").fetchone())
    await login(client, web)
    response = await client.post("/api/v1/uploads", files={"text": (None, "另一个定向消息")})
    assert response.json()["status"] == "succeeded"
    with db.open_db(web["path"]) as conn:
        assert dict(conn.execute("SELECT * FROM job WHERE job_id=?", (old["job_id"],)).fetchone()) == old


def test_staging_cleanup_scope_and_hardlink(tmp_path):
    import os

    from pc.adapters import upload_files

    root = tmp_path / "uploads"
    root.mkdir()
    outside = tmp_path / "keep.txt"
    outside.write_text("保留", encoding="utf-8")
    with pytest.raises(ValueError, match="范围"):
        upload_files.cleanup(root, outside)
    directory = root / ("a" * 32)
    directory.mkdir()
    os.link(outside, directory / "linked.txt")
    with pytest.raises(ValueError, match="硬链接"):
        upload_files.cleanup(root, directory)
    assert outside.read_text(encoding="utf-8") == "保留"
