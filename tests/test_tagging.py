"""统一打标服务：建议隔离、root 权限、事务与索引一致性。"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from pc.adapters import sqlite as db
from pc.adapters.fts_search import SqliteFtsSearch
from pc.adapters.tag_normalizer import normalize_tag
from pc.domain import ClassificationResult, TagCandidate
from pc.services import tagging as tags
from pc.services.search import SearchQuery, run_search


@pytest.fixture
def post(fresh_db):
    conn = fresh_db
    source = conn.execute(
        "INSERT INTO source(uuid,name,platform,kind) VALUES (?,'mine','local','manual') RETURNING source_id",
        (str(uuid.uuid4()),),
    ).fetchone()[0]
    message = conn.execute(
        "INSERT INTO message(uuid,source_id,platform_msg_id) VALUES (?,?,'one') RETURNING message_id",
        (str(uuid.uuid4()), source),
    ).fetchone()[0]
    ids = []
    for i in range(2):
        ids.append(
            conn.execute(
                "INSERT INTO content_item(uuid,message_id,item_type,ordinal,text_content,status) VALUES (?,?,'text',?,?,'active') RETURNING content_id",
                (str(uuid.uuid4()), message, i, f"正文{i}"),
            ).fetchone()[0]
        )
    db.refresh_search_derived(conn)
    return conn, message, ids


def search(conn, word="", names=()):
    return run_search(conn, SearchQuery(q=word, tags=names))


@pytest.mark.parametrize(
    ("value", "expected"), [("ＡＢ_C", "ab_c"), (" AB  c ", "ab_c"), ("中文 标签", "中文_标签")]
)
def test_normalization(value, expected):
    assert normalize_tag(value) == expected
    assert normalize_tag(expected) == expected


@pytest.mark.parametrize("value", ["", "___", "x\n", "x" * 256])
def test_invalid_names(value):
    with pytest.raises(ValueError):
        normalize_tag(value)


def test_whole_post_idempotent_and_readd(post):
    conn, message, ids = post
    classification = tags.manual_result(["自选名称", "第二标签", "自选名称"])
    before = tuple(conn.execute("SELECT content_signature,canonical_post_id FROM post").fetchone())
    assert (
        tags.apply_classification(conn, classification, message_ids=[message, message])[
            "changed_content_ids"
        ]
        == ids
    )
    original = [r[0] for r in conn.execute("SELECT assign_id FROM tag_assignment")]
    assert conn.execute("SELECT count(*) FROM tag_assignment").fetchone()[0] == 4
    assert (
        tags.apply_classification(conn, classification, message_ids=[message])[
            "changed_content_ids"
        ]
        == []
    )
    assert all(t["usage_count"] == 2 for t in tags.list_tags(conn)["tags"])
    assert len(search(conn, "自选名称")["hits"]) == 1
    assert len(search(conn, names=("自选名称", "第二标签"))["hits"]) == 1
    tags.remove_tags(conn, ["自选名称"], message_ids=[message])
    assert not search(conn, "自选名称")["hits"]
    tags.apply_classification(conn, classification, message_ids=[message])
    assert [r[0] for r in conn.execute("SELECT assign_id FROM tag_assignment")] == original
    assert (
        tuple(conn.execute("SELECT content_signature,canonical_post_id FROM post").fetchone())
        == before
    )
    assert (
        conn.execute("SELECT count(*) FROM audit_log WHERE user_agent='local_root'").fetchone()[0]
        > 0
    )


def test_precise_part_and_quarantine(post):
    conn, _, ids = post
    conn.execute(
        "UPDATE content_item SET status='quarantined',nsfw_level=3 WHERE content_id=?", (ids[0],)
    )
    tags.apply_classification(conn, tags.manual_result(["待整理"]), content_ids=[ids[0]])
    assert tuple(
        conn.execute(
            "SELECT status,nsfw_level FROM content_item WHERE content_id=?", (ids[0],)
        ).fetchone()
    ) == ("quarantined", 3)
    assert not search(conn, "待整理")["hits"]
    assert conn.execute("SELECT count(*) FROM tag_assignment").fetchone()[0] == 1


def test_suggestion_confirm_and_alias_no_leak(post):
    conn, message, _ids = post
    proposed = ClassificationResult(
        tags=(TagCandidate("待定名称", 0.8, {"reason": "模型证据"}),), decided_by="future_model"
    )
    tags.apply_classification(conn, proposed, message_ids=[message])
    tags.change_alias(conn, "候选别名", name="待定名称")
    assert tags.list_tags(conn)["tags"][0]["status"] == "pending"
    for term in ("待定名称", "候选别名"):
        assert not search(conn, term)["hits"]
        assert not search(conn, names=(term,))["hits"]
    assert tags.list_tags(conn)["tags"][0]["usage_count"] == 0
    assignment = tags.show_tags(conn, message_ids=[message])["assignments"][0]["assign_id"]
    tags.review_suggestions(conn, [assignment], confirm=True)
    assert tags.list_tags(conn)["tags"][0]["usage_count"] == 1
    hit = search(conn, "候选别名")["hits"][0]
    assert all(t["source"] != "suggested" for t in hit["tags"])
    human = conn.execute("SELECT evidence FROM tag_assignment WHERE source='human'").fetchone()[0]
    assert json.loads(human)["suggestion_id"] == assignment
    assert len(search(conn, names=("候选别名",))["hits"]) == 1
    tags.review_suggestions(conn, [assignment], confirm=True)
    assert (
        conn.execute("SELECT count(*) FROM tag_assignment WHERE source='human'").fetchone()[0] == 1
    )
    remaining = conn.execute(
        "SELECT assign_id FROM tag_assignment WHERE source='suggested' AND revoked_at IS NULL"
    ).fetchone()[0]
    tags.review_suggestions(conn, [remaining], confirm=False)
    assert tags.list_tags(conn)["tags"][0]["usage_count"] == 1
    assert conn.execute("SELECT count(*) FROM classification_run").fetchone()[0] == 3


def test_suggested_on_active_tag_and_multi_source_count(post):
    conn, _, ids = post
    tags.apply_classification(conn, tags.manual_result(["普通名称"]), content_ids=[ids[0]])
    proposed = ClassificationResult(tags=(TagCandidate("普通名称", 0.9),))
    tags.apply_classification(conn, proposed, content_ids=ids)
    assert len(search(conn, "普通名称")["hits"][0]["tags"]) == 1
    rows = tags.show_tags(conn, content_ids=ids)["assignments"]
    tags.review_suggestions(
        conn, [r["assign_id"] for r in rows if r["source"] == "suggested"], confirm=True
    )
    assert tags.list_tags(conn)["tags"][0]["usage_count"] == 2
    tag_id = tags.list_tags(conn)["tags"][0]["tag_id"]
    conn.execute(
        "INSERT INTO tag_assignment(content_id,tag_id,source) VALUES (?,?,'system')",
        (ids[0], tag_id),
    )
    tags.remove_tags(conn, ["普通名称"], content_ids=ids)
    assert tags.list_tags(conn)["tags"][0]["usage_count"] == 1


def test_alias_add_remove_and_recompute(post):
    conn, message, _ = post
    tags.apply_classification(conn, tags.manual_result(["canonical"]), message_ids=[message])
    tags.change_alias(conn, "独特别名", name="canonical")
    assert len(search(conn, "独特别名")["hits"]) == 1
    before = [tuple(r) for r in conn.execute("SELECT text_search,search_extra FROM content_item")]
    db.refresh_search_derived(conn)
    assert [
        tuple(r) for r in conn.execute("SELECT text_search,search_extra FROM content_item")
    ] == before
    assert tags.change_alias(conn, "独特别名", name="canonical")["changed"] is False
    tags.change_alias(conn, "独特别名")
    assert not search(conn, "独特别名")["hits"]


def test_name_and_alias_collisions(post):
    conn, _, ids = post
    for name in ("one", "two"):
        tags.apply_classification(conn, tags.manual_result([name]), content_ids=[ids[0]])
    with pytest.raises(ValueError):
        tags.change_alias(conn, "ONE", name="two")
    tags.change_alias(conn, "alt", name="one")
    with pytest.raises(ValueError):
        tags.change_alias(conn, "ALT", name="two")
    with pytest.raises(ValueError):
        tags.apply_classification(conn, tags.manual_result(["one"], "character"), content_ids=ids)
    tags.apply_classification(conn, tags.manual_result(["ALT"]), content_ids=[ids[1]])
    assert len(tags.list_tags(conn)["tags"]) == 2


@pytest.mark.parametrize("failure", ["missing", "deleted", "root", "index", "audit"])
def test_atomic_failure(post, monkeypatch, failure):
    conn, message, ids = post
    targets = ids
    actor = None
    if failure == "missing":
        targets = [ids[0], 99999]
    elif failure == "deleted":
        conn.execute("UPDATE message SET deleted_at=? WHERE message_id=?", (db.now_iso(), message))
    elif failure == "root":
        actor = conn.execute(
            "INSERT INTO app_user(uuid,tier) VALUES (?,1) RETURNING user_id", (str(uuid.uuid4()),)
        ).fetchone()[0]
    elif failure == "index":
        monkeypatch.setattr(
            SqliteFtsSearch,
            "index_many",
            lambda *_: (_ for _ in ()).throw(RuntimeError("index failure")),
        )
    elif failure == "audit":
        conn.execute(
            "CREATE TEMP TRIGGER fail_audit BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT,'audit failure'); END"
        )
    with pytest.raises((ValueError, RuntimeError, sqlite3.Error)):
        tags.apply_classification(
            conn, tags.manual_result(["rollback"]), content_ids=targets, actor_id=actor
        )
    for table in ("tag", "tag_assignment", "classification_run", "audit_log"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert not conn.in_transaction


def test_empty_batch_index_is_readonly(post):
    conn, _, _ = post
    before = conn.total_changes
    SqliteFtsSearch(conn).index_many([])
    assert conn.total_changes == before


def test_names_with_colon_and_namespaced_alias(post):
    conn, message, _ = post
    tags.apply_classification(conn, tags.manual_result(["随选:名称"]), message_ids=[message])
    assert search(conn, names=("随选:名称",))["hits"]
    assert search(conn, names=("general:随选:名称",))["hits"]
    tags.apply_classification(
        conn, tags.manual_result(["人物名称"], "character"), message_ids=[message]
    )
    tags.change_alias(conn, "另一名称", name="人物名称")
    assert search(conn, names=("character:另一名称",))["hits"]


def test_alias_and_confirmation_failure_rollback(post, monkeypatch):
    conn, message, _ = post
    tags.apply_classification(
        conn, ClassificationResult(tags=(TagCandidate("suggestion", 0.8),)), message_ids=[message]
    )
    ident = tags.show_tags(conn, message_ids=[message])["assignments"][0]["assign_id"]
    before = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    with pytest.raises(ValueError):
        tags.review_suggestions(conn, [ident, 99999], confirm=True)
    assert conn.execute("SELECT status FROM tag").fetchone()[0] == "pending"
    monkeypatch.setattr(
        SqliteFtsSearch,
        "index_many",
        lambda *_: (_ for _ in ()).throw(RuntimeError("index failure")),
    )
    with pytest.raises(RuntimeError):
        tags.change_alias(conn, "rollback_alias", name="suggestion")
    with pytest.raises(RuntimeError):
        tags.review_suggestions(conn, [ident], confirm=True)
    assert conn.execute("SELECT count(*) FROM tag_synonym").fetchone()[0] == 0
    assert conn.execute("SELECT aliases,status,usage_count FROM tag").fetchone()[:] == (
        "[]",
        "pending",
        0,
    )
    assert (
        conn.execute("SELECT count(*) FROM tag_assignment WHERE source='human'").fetchone()[0] == 0
    )
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before


@pytest.mark.parametrize("status", ["active", "disabled"])
def test_explicit_root_actor(post, status):
    conn, message, _ = post
    ident = conn.execute(
        "INSERT INTO app_user(uuid,tier,status,password_hash,totp_secret_enc) "
        "VALUES (?,0,?,'test-hash',X'01') RETURNING user_id",
        (str(uuid.uuid4()), status),
    ).fetchone()[0]
    if status == "disabled":
        with pytest.raises(ValueError):
            tags.apply_classification(
                conn, tags.manual_result(["root标签"]), message_ids=[message], actor_id=ident
            )
    else:
        tags.apply_classification(
            conn, tags.manual_result(["root标签"]), message_ids=[message], actor_id=ident
        )
        assert conn.execute("SELECT assigned_by FROM tag_assignment LIMIT 1").fetchone()[0] == ident
        assert conn.execute("SELECT actor_id FROM audit_log LIMIT 1").fetchone()[0] == ident


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_model_confidence(post, confidence):
    conn, message, _ = post
    with pytest.raises(ValueError):
        tags.apply_classification(
            conn,
            ClassificationResult(tags=(TagCandidate("建议", confidence),)),
            message_ids=[message],
        )
    assert conn.execute("SELECT count(*) FROM tag").fetchone()[0] == 0
