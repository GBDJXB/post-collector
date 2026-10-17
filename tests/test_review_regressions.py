"""安全审查与帖级检索契约的回归用例。"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest

from pc import cli
from pc.adapters import safe_files
from pc.adapters.local_files import LocalFilesConnector
from pc.adapters.local_storage import LocalStorageBackend
from pc.adapters.sqlite import now_iso
from pc.domain import SourceMessage, SourceNotAuthorizedError, normalize_text
from pc.services.ingest import ingest_messages
from pc.services.pipeline import Worker
from pc.services.search import SearchError, SearchQuery, run_search
from pc.services.sources import create_source, require_source


@pytest.fixture
def setup(fresh_db: Any, tmp_path: Path) -> tuple[Any, dict[str, Any], Worker, Path]:
    source_id = create_source(
        fresh_db, name="review", platform="local", kind="manual", config={"roots": [str(tmp_path)]}
    )["source_id"]
    source = require_source(fresh_db, source_id)
    worker = Worker(
        fresh_db,
        storage=LocalStorageBackend(tmp_path / "media"),
        quarantine=LocalStorageBackend(tmp_path / "quarantine"),
    )
    return fresh_db, source, worker, tmp_path


def enqueue(
    setup: Any, *, text: str = "caption", refs: tuple[str, ...] = (), sent: str | None = None
) -> int:
    conn, source, _worker, _root = setup
    return ingest_messages(
        conn,
        source,
        [
            SourceMessage(
                source_id=source["source_id"],
                platform_msg_id=str(uuid.uuid4()),
                message_type="mixed" if refs else "text",
                text_raw=text,
                media_refs=refs,
                sent_at=sent,
            )
        ],
    ).message_ids[0]


@pytest.mark.parametrize("change", ["revoked", "expired", "disabled", "deleted"])
def test_text_authorization_is_rechecked(setup: Any, change: str) -> None:
    conn, _source, worker, _root = setup
    enqueue(setup)
    if change in ("revoked", "expired"):
        column = "revoked_at" if change == "revoked" else "expires_at"
        conn.execute(f"UPDATE source_authorization SET {column}='2000-01-01T00:00:00.000Z'")
    elif change == "disabled":
        conn.execute("UPDATE source SET enabled=0")
    else:
        conn.execute("UPDATE source SET deleted_at=?", (now_iso(),))
    with pytest.raises(SourceNotAuthorizedError):
        enqueue(setup)
    assert worker.run_once().dead == 1
    assert conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 0
    assert not run_search(conn, SearchQuery(q="caption", limit=10))["hits"]


def test_authorization_changed_during_collection_is_rechecked(setup: Any, monkeypatch: Any) -> None:
    conn, _source, worker, _root = setup
    enqueue(setup)
    collect = worker._collect

    def revoke(*args: Any) -> Any:
        result = collect(*args)
        conn.execute("UPDATE source_authorization SET revoked_at=?", (now_iso(),))
        return result

    monkeypatch.setattr(worker, "_collect", revoke)
    assert worker.run_once().dead == 1
    assert conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 0


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_old_text_tombstone_does_not_block_message(setup: Any, expired: bool, mixed: bool) -> None:
    conn, _source, worker, root = setup
    digest = hashlib.sha256(normalize_text("caption").encode()).hexdigest()
    conn.execute(
        "INSERT INTO hash_tombstone(hash,alg,deleted_at,expires_at) VALUES (?,'content_hash',?,?)",
        (digest, now_iso(), "2000-01-01T00:00:00.000Z" if expired else None),
    )
    image = root / "cat.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"x" * 32)
    enqueue(setup, refs=(str(image),) if mixed else ())
    report = worker.run_once()
    assert report.succeeded == 1
    assert report.dead == 0
    assert conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == (2 if mixed else 1)
    assert conn.execute("SELECT count(*) FROM content_media").fetchone()[0] == int(mixed)
    assert conn.execute("SELECT count(*) FROM hash_tombstone").fetchone()[0] == 1


def test_last_attempt_crash_recovery_is_terminal_and_idempotent(setup: Any) -> None:
    conn, _source, worker, _root = setup
    enqueue(setup)
    conn.execute(
        "UPDATE job SET status='running', attempts=max_attempts, locked_at='2000-01-01T00:00:00.000Z'"
    )
    assert worker.recover_stale() == 1
    assert worker.recover_stale() == 0
    row = conn.execute("SELECT * FROM job").fetchone()
    assert row["status"] == "dead" and row["finished_at"] and row["dead_reason"]
    assert row["locked_at"] is None and row["locked_by"] is None
    assert conn.execute("SELECT count(*) FROM audit_log WHERE result='dead'").fetchone()[0] == 1


def test_manual_long_texts_with_same_stored_prefix_are_distinct_messages(setup: Any) -> None:
    conn, source, _worker, root = setup
    folder = root / "long-texts"
    folder.mkdir()
    (folder / "a.txt").write_text("x" * 100 + "A", encoding="utf-8")
    (folder / "b.txt").write_text("x" * 100 + "B", encoding="utf-8")
    connector = LocalFilesConnector(source, roots=[folder], identity="content", max_text_chars=100)
    report, _ = cli._ingest_loop(conn, source, connector, dry_run=False, limit=None)
    assert report.inserted == 2
    assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == 2
    assert {row[0] for row in conn.execute("SELECT text_raw FROM message")} == {"x" * 100}


def test_empty_batch_does_not_hide_later_files(setup: Any) -> None:
    conn, source, _worker, root = setup
    root = root / "files"
    root.mkdir()
    for i in range(201):
        (root / f"{i:03d}.txt").write_text("" if i < 200 else "valid last file")
    connector = LocalFilesConnector(source, roots=[root], identity="content")
    report, _ = cli._ingest_loop(conn, source, connector, dry_run=False, limit=None)
    assert report.inserted == 1
    assert len(connector.skipped) == 200


def test_nonadvancing_nonempty_batch_is_rejected(setup: Any) -> None:
    conn, source, _worker, _root = setup

    class Broken:
        def list_new(self, cursor: Any) -> Any:
            return [SourceMessage(source["source_id"], "broken", "text", text_raw="bad")], cursor

    with pytest.raises(RuntimeError, match="游标"):
        cli._ingest_loop(conn, source, Broken(), dry_run=False, limit=None)
    assert conn.execute("SELECT count(*) FROM message").fetchone()[0] == 0


def test_root_boundary_and_single_file_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    image = root / "cat.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    image.with_suffix(".txt").write_text("caption")
    connector = LocalFilesConnector({"source_id": 1}, roots=[image])
    with pytest.raises(safe_files.UnsafeLocalPathError):
        connector.fetch_media(str(outside))
    messages, _ = connector.list_new(None)
    assert len(messages) == 1 and messages[0].text_raw == "caption"
    assert Path(messages[0].media_refs[0]).is_absolute()


def test_opened_handle_path_is_checked(tmp_path: Path, monkeypatch: Any) -> None:
    path = tmp_path / "file.jpg"
    path.write_bytes(b"private")
    monkeypatch.setattr(safe_files, "_opened_path", lambda _handle: tmp_path.parent / "secret")
    with pytest.raises(safe_files.UnsafeLocalPathError, match="替换"):
        LocalFilesConnector({"source_id": 1}, roots=[tmp_path]).fetch_media(str(path))


def test_symlink_sidecar_is_skipped(tmp_path: Path) -> None:
    image = tmp_path / "cat.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    secret = tmp_path / "private.txt"
    secret.write_text("secret")
    try:
        image.with_suffix(".txt").symlink_to(secret)
    except OSError:
        pytest.skip("Creating symbolic links requires platform permission")
    connector = LocalFilesConnector({"source_id": 1}, roots=[image])
    messages, _ = connector.list_new(None)
    assert messages[0].text_raw is None and connector.skipped


@pytest.mark.parametrize("keyword", ["", "caption"])
def test_sent_null_paging_has_no_gaps(setup: Any, keyword: str) -> None:
    conn, _source, worker, _root = setup
    for i in range(7):
        enqueue(setup, text=f"caption {i}", sent="2026-01-01T00:00:00.000Z" if i < 3 else None)
    worker.run_once()
    seen = []
    cursor = None
    for _ in range(10):
        result = run_search(
            conn, SearchQuery(q=keyword, limit=2, sort="time", time_field="sent", cursor=cursor)
        )
        seen.extend(hit["message_id"] for hit in result["hits"])
        cursor = result["next_cursor"]
        if not result["has_more"]:
            break
        assert cursor
    assert len(seen) == 7 and len(set(seen)) == 7


def test_distinct_images_keep_captions_and_cross_part_queries(setup: Any) -> None:
    conn, _source, worker, root = setup
    for i in range(2):
        image = root / f"scene{i}.jpg"
        image.write_bytes(b"\xff\xd8\xff" + bytes([i]) * 32)
        enqueue(setup, refs=(str(image),))
    worker.run_once()
    hits = run_search(conn, SearchQuery(q="caption", limit=10))["hits"]
    assert len(hits) == 2 and all(len(hit["parts"]) == 2 for hit in hits)
    assert len(run_search(conn, SearchQuery(q="caption scene1", limit=10))["hits"]) == 1
    assert not run_search(conn, SearchQuery(item_types=("image",), limit=10))["hits"]
    assert len(run_search(conn, SearchQuery(item_types=("mixed",), limit=10))["hits"]) == 2
    image_id = hits[0]["parts"][1]["content_id"]
    conn.execute("UPDATE content_item SET nsfw_level=4 WHERE content_id=?", (image_id,))
    hits = run_search(conn, SearchQuery(q="caption", limit=10))["hits"]
    assert all(part["nsfw_level"] == 0 for hit in hits for part in hit["parts"])
    assert all(part["content_id"] != image_id for hit in hits for part in hit["parts"])


def test_old_cursor_is_rejected(setup: Any) -> None:
    import base64

    conn, _source, _worker, _root = setup
    old = base64.urlsafe_b64encode(b"sent|None|1").decode()
    with pytest.raises(SearchError, match="重新查询"):
        run_search(conn, SearchQuery(time_field="sent", cursor=old, limit=2))


def test_post_rebuild_respects_sql_variable_limit(setup: Any) -> None:
    import sqlite3

    from pc.adapters.sqlite import sync_post_rows

    conn, _source, worker, _root = setup
    for i in range(35):
        enqueue(setup, text=f"unique caption {i}")
    worker.run_once()
    conn.execute("UPDATE post SET content_signature=NULL, canonical_post_id=NULL")
    before = conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 20)
    try:
        assert sync_post_rows(conn)["posts"] == 35
        assert sync_post_rows(conn)["posts"] == 0
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, before)


@pytest.mark.parametrize("change", ["hidden", "deleted_message", "changed_signature"])
def test_canonical_post_guard_keeps_other_post(setup: Any, change: str) -> None:
    conn, _source, worker, _root = setup
    first, second = enqueue(setup), enqueue(setup)
    worker.run_once()
    assert [hit["message_id"] for hit in run_search(conn, SearchQuery(q="caption"))["hits"]] == [
        first
    ]
    if change == "hidden":
        conn.execute("UPDATE content_item SET nsfw_level=4 WHERE message_id=?", (first,))
    elif change == "deleted_message":
        conn.execute("UPDATE message SET deleted_at=? WHERE message_id=?", (now_iso(), first))
    else:
        conn.execute("UPDATE post SET content_signature='different' WHERE message_id=?", (first,))
    assert second in [
        hit["message_id"] for hit in run_search(conn, SearchQuery(q="caption"))["hits"]
    ]


def test_tags_can_match_different_visible_parts(setup: Any) -> None:
    conn, _source, worker, root = setup
    image = root / "scene.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"x" * 32)
    message_id = enqueue(setup, refs=(str(image),))
    worker.run_once()
    parts = conn.execute(
        "SELECT content_id FROM content_item WHERE message_id=? ORDER BY ordinal", (message_id,)
    ).fetchall()
    for name, part in zip(("caption_tag", "image_tag"), parts, strict=True):
        tag = conn.execute(
            "INSERT INTO tag(uuid,name,name_norm,namespace) VALUES (?,?,?,'general') RETURNING tag_id",
            (str(uuid.uuid4()), name, name),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO tag_assignment(content_id,tag_id,source) VALUES (?,?,'human')",
            (part[0], tag),
        )
    query = SearchQuery(tags=("caption_tag", "image_tag"), limit=10)
    assert len(run_search(conn, query)["hits"]) == 1
    conn.execute("UPDATE content_item SET nsfw_level=4 WHERE content_id=?", (parts[1][0],))
    assert not run_search(conn, query)["hits"]


def test_growth_during_read_is_bounded(tmp_path: Path, monkeypatch: Any) -> None:
    from contextlib import contextmanager

    from pc.adapters import local_files
    from pc.domain import MediaTooLargeError

    path = tmp_path / "growing.jpg"
    path.write_bytes(b"abc")
    original = local_files.open_scoped
    reads = []

    class GrowingHandle:
        def __init__(self, handle: Any) -> None:
            self.handle = handle

        def fileno(self) -> int:
            return self.handle.fileno()

        def read(self, size: int) -> bytes:
            reads.append(size)
            path.write_bytes(b"abcdefghij")
            return self.handle.read(size)

    @contextmanager
    def growing(*args: Any, **kwargs: Any) -> Any:
        with original(*args, **kwargs) as handle:
            yield GrowingHandle(handle)

    monkeypatch.setattr(local_files, "open_scoped", growing)
    with pytest.raises(MediaTooLargeError):
        LocalFilesConnector({"source_id": 1}, roots=[tmp_path], max_media_bytes=5).fetch_media(
            str(path)
        )
    assert reads == [6]


def test_windows_junction_is_rejected(tmp_path: Path) -> None:
    import os

    if os.name != "nt":
        pytest.skip("Windows junction test")
    import _winapi

    root = tmp_path / "allowed"
    secret = tmp_path / "private"
    root.mkdir()
    secret.mkdir()
    (secret / "secret.txt").write_text("private data")
    junction = root / "redirect"
    _winapi.CreateJunction(str(secret), str(junction))
    try:
        connector = LocalFilesConnector({"source_id": 1}, roots=[root])
        assert not connector.list_new(None)[0]
        with pytest.raises(safe_files.UnsafeLocalPathError):
            connector.fetch_media(str(junction / "secret.txt"))
    finally:
        junction.rmdir()


def test_legacy_relative_reference_and_config_are_rejected(tmp_path: Path) -> None:
    from pc.adapters.registry import SourceConfigError, connector_for

    connector = LocalFilesConnector({"source_id": 1}, roots=[tmp_path])
    with pytest.raises(safe_files.UnsafeLocalPathError, match="绝对路径"):
        connector.fetch_media("old.jpg")
    with pytest.raises(SourceConfigError, match="旧相对 roots"):
        connector_for({"source_id": 1, "kind": "manual", "config": {"roots": ["old"]}})


def test_post_query_keeps_caller_transaction(setup: Any) -> None:
    conn, _source, worker, _root = setup
    enqueue(setup)
    worker.run_once()
    conn.execute("BEGIN")
    conn.execute("UPDATE content_item SET nsfw_level=1")
    try:
        assert not run_search(conn, SearchQuery(q="caption"))["hits"]
        assert conn.in_transaction
    finally:
        conn.rollback()
    assert run_search(conn, SearchQuery(q="caption"))["hits"]


def test_candidate_scan_expands_past_many_parts_of_one_post(setup: Any) -> None:
    from pc.adapters.fts_search import SqliteFtsSearch, _plan_terms

    conn, _source, worker, _root = setup
    messages = []
    for _ in range(50):
        messages.append(enqueue(setup))
        worker.run_once()
    # 前 48 个文档属于同帖，超过初始预留空间，必须扩展才能凑出三个帖。
    for ordinal, message_id in enumerate(messages[1:48], 1):
        conn.execute(
            "UPDATE content_item SET message_id=?, ordinal=? WHERE message_id=?",
            (messages[0], ordinal, message_id),
        )
    candidates = SqliteFtsSearch(conn)._post_candidates([_plan_terms(["caption"])], {}, 2)
    assert {message_id for message_id, _ in candidates} == {
        messages[0], messages[48], messages[49]
    }
