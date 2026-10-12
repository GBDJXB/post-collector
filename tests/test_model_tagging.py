"""完整候选、最小原始输入与不可绕过的模型快照。"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid

import pytest

from pc.adapters import sqlite as db
from pc.domain import ClassificationResult, ObjectKey, TagCandidate
from pc.services import compositions as co
from pc.services import tagging


@pytest.fixture
def model_data(fresh_db):
    conn = fresh_db
    source = conn.execute(
        "INSERT INTO source(uuid,name,platform,kind) VALUES (?,'PRIVATE-SOURCE','local','manual') RETURNING source_id",
        (str(uuid.uuid4()),),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO source_authorization(source_id,mode,evidence_ref) VALUES (?,'manual_export','PRIVATE-AUTH')",
        (source,),
    )
    message = conn.execute(
        "INSERT INTO message(uuid,source_id,platform_msg_id,origin_note,raw_payload) VALUES (?,?,'PRIVATE-PLATFORM','PRIVATE-NOTE','{\"secret\":\"PRIVATE-RAW\"}') RETURNING message_id",
        (str(uuid.uuid4()), source),
    ).fetchone()[0]
    ids = [
        conn.execute(
            "INSERT INTO content_item(uuid,message_id,item_type,ordinal,text_content,status) VALUES (?,?,'text',?,?,'active') RETURNING content_id",
            (str(uuid.uuid4()), message, index, text),
        ).fetchone()[0]
        for index, text in enumerate(["正文一", "正文二"])
    ]
    tag_ids = [
        conn.execute(
            "INSERT INTO tag(uuid,name,name_norm,namespace,description,aliases) VALUES (?,?,?,'general','PRIVATE-DESCRIPTION','[\"PRIVATE-ALIAS\"]') RETURNING tag_id",
            (str(uuid.uuid4()), name, name),
        ).fetchone()[0]
        for name in ["已有甲", "已有乙"]
    ]
    db.refresh_search_derived(conn)
    return conn, ids, tag_ids, source


def test_catalog_and_wire_contain_only_approved_fields(model_data):
    from pc.services import model_tagging as mt

    conn, ids, _, _ = model_data
    for namespace, status in [
        ("general", "pending"),
        ("rating", "active"),
        ("source", "active"),
        ("system", "active"),
    ]:
        conn.execute(
            "INSERT INTO tag(uuid,name,name_norm,namespace,status) VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), namespace + status, namespace + status, namespace, status),
        )
    catalog = mt.load_catalog(conn)
    assert catalog.public == [
        {"candidate_id": 1, "name": "已有乙", "namespace": "general"},
        {"candidate_id": 2, "name": "已有甲", "namespace": "general"},
    ]
    prepared = mt.build_input(conn, content_id=ids[0])
    wire = mt.request_bytes(prepared, catalog)
    assert "正文一" in wire.decode()
    assert "正文二" not in wire.decode()
    assert "PRIVATE" not in wire.decode()
    assert "content_id" not in wire.decode() and "tag_id" not in wire.decode()
    assert json.loads(wire)["max_tokens"] == 2048


def test_complete_catalog_empty_or_over_limit_blocks(model_data):
    from pc.services import model_tagging as mt

    conn, _, _, _ = model_data
    conn.execute("DELETE FROM tag")
    with pytest.raises(ValueError):
        mt.load_catalog(conn)
    conn.executemany(
        "INSERT INTO tag(uuid,name,name_norm,namespace) VALUES (?,?,?,'general')",
        [(str(uuid.uuid4()), f"tag{i}", f"tag{i}") for i in range(501)],
    )
    with pytest.raises(ValueError):
        mt.load_catalog(conn)


def test_composition_order_repetitions_and_position_limit(model_data):
    from pc.services import model_tagging as mt

    conn, ids, _, _ = model_data
    ident = co.write(
        conn,
        "create",
        name="PRIVATE-TITLE",
        description="PRIVATE-DESC",
        contents=[ids[1], ids[0], ids[1]],
    )["composition_id"]
    prepared = mt.build_input(conn, composition_id=ident)
    assert [(p["position"], p["text"]) for p in prepared.parts] == [
        (0, "正文二"),
        (1, "正文一"),
        (2, "正文二"),
    ]
    assert prepared.text_bytes == 27
    assert "PRIVATE" not in mt.request_bytes(prepared, mt.load_catalog(conn)).decode()
    co.write(conn, "set-members", ident=ident, contents=[ids[0]] * 21)
    with pytest.raises(ValueError):
        mt.build_input(conn, composition_id=ident)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE content_item SET nsfw_level=1",
        "UPDATE content_item SET deleted_at='gone'",
        "UPDATE message SET deleted_at='gone'",
        "UPDATE source SET deleted_at='gone'",
        "UPDATE source SET enabled=0",
        "UPDATE source_authorization SET revoked_at='gone'",
        "UPDATE source_authorization SET expires_at='2000-01-01T00:00:00Z'",
    ],
)
def test_one_invalid_member_rejects_entire_composition(model_data, change):
    from pc.services import model_tagging as mt

    conn, ids, _, _ = model_data
    ident = co.write(conn, "create", name="组合", contents=ids)["composition_id"]
    conn.execute(change)
    with pytest.raises(ValueError):
        mt.build_input(conn, composition_id=ident)


def test_text_limit_is_utf8_and_never_truncates(model_data):
    from pc.services import model_tagging as mt

    conn, ids, _, _ = model_data
    conn.execute(
        "UPDATE content_item SET text_content=? WHERE content_id=?", ("中" * 10923, ids[0])
    )
    with pytest.raises(ValueError):
        mt.build_input(conn, content_id=ids[0])


def add_image(conn, content, root, data):
    key = ObjectKey(hashlib.sha256(data).hexdigest())
    path = root.joinpath(*key.value.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    backend = conn.execute(
        "INSERT INTO storage_backend(name,kind,config) VALUES ('test-model-media','local',?) RETURNING backend_id",
        (json.dumps({"root": str(root)}),),
    ).fetchone()[0]
    media = conn.execute(
        "INSERT INTO media(uuid,sha256,size_bytes,mime_detected,storage_backend_id,object_key,scan_status) VALUES (?,?,?,'image/png',?,?,'clean') RETURNING media_id",
        (str(uuid.uuid4()), key.sha256, len(data), backend, key.value),
    ).fetchone()[0]
    conn.execute("INSERT INTO content_media(content_id,media_id) VALUES (?,?)", (content, media))
    conn.execute(
        "UPDATE content_item SET item_type='image',text_content=NULL WHERE content_id=?", (content,)
    )
    return media


def test_original_image_bytes_metadata_warning_and_repetition_limits(model_data, tmp_path):
    from pc.services import model_tagging as mt

    conn, ids, _, _ = model_data
    data = b"\x89PNG\r\n\x1a\nORIGINAL-EXIF-DO-NOT-STRIP"
    media = add_image(conn, ids[0], tmp_path / "media", data)
    prepared = mt.build_input(conn, content_id=ids[0])
    wire = json.loads(mt.request_bytes(prepared, mt.load_catalog(conn)))
    image = next(p for p in wire["messages"][1]["content"] if p["type"] == "image_url")
    assert base64.b64decode(image["image_url"]["url"].split(",", 1)[1]) == data
    assert prepared.parts[0]["images"][0]["dimensions_verified"] is False
    assert prepared.image_count == 1 and prepared.image_bytes == len(data)
    ident = co.write(conn, "create", name="重复图", contents=[ids[0]] * 5)["composition_id"]
    with pytest.raises(ValueError):
        mt.build_input(conn, composition_id=ident)
    conn.execute("UPDATE media SET width=100001,height=100001 WHERE media_id=?", (media,))
    with pytest.raises(ValueError):
        mt.build_input(conn, content_id=ids[0])


@pytest.mark.parametrize('scan_status', ['clean', 'skipped', 'pending', 'infected', 'suspicious'])
def test_model_images_preserve_existing_scan_allowlist(model_data, tmp_path, scan_status):
    from pc.services import model_tagging as mt

    conn, ids, _, _ = model_data
    data = b'\x89PNG\r\n\x1a\nSCAN-ALLOWLIST'
    media = add_image(conn, ids[0], tmp_path / 'media', data)
    conn.execute('UPDATE media SET scan_status=? WHERE media_id=?', (scan_status, media))
    if scan_status in {'clean', 'skipped'}:
        prepared = mt.build_input(conn, content_id=ids[0])
        assert prepared.parts[0]['images'][0]['data'] == data
    else:
        with pytest.raises(ValueError, match='扫描'):
            mt.build_input(conn, content_id=ids[0])


def bound_proposal(model_data):
    from pc.services import model_tagging as mt
    from pc.services.model_access import ModelAccess

    conn, ids, tags, _ = model_data
    access = ModelAccess()
    access.set_key("session", "TEST-ONLY-KEY")
    access.set_access(conn, "session", True, confirmed=True)
    prepared, catalog = mt.build_input(conn, content_id=ids[0]), mt.load_catalog(conn)
    snapshot = mt.make_snapshot(prepared, catalog, [tags[0]], access.config_digest(conn))
    model = conn.execute(
        "INSERT INTO model_version(capability,model_name) VALUES ('llm','test') RETURNING model_version_id"
    ).fetchone()[0]
    proposal = ClassificationResult(tags=(TagCandidate("已有甲", 0.8),), model_version_id=model)
    args = {
        "content_ids": [ids[0]],
        "existing_tag_ids": [tags[0]],
        "input_ref": mt.encoded(snapshot).decode(),
    }
    return access, proposal, args


def proposal_row(conn):
    row = conn.execute("SELECT * FROM tag_assignment WHERE source='suggested'").fetchone()
    return row["assign_id"], {row["assign_id"]: json.loads(row["evidence"])["model_proposal"]}


@pytest.mark.parametrize("bad", ["{}", "null"])
def test_review_cannot_bypass_invalid_durable_snapshot(model_data, bad):
    conn, _, _, _ = model_data
    _, proposal, args = bound_proposal(model_data)
    tagging.apply_classification(conn, proposal, **args)
    ident, expected = proposal_row(conn)
    conn.execute("UPDATE classification_run SET input_ref=?", (bad,))
    with pytest.raises(ValueError, match="快照"):
        tagging.review_suggestions(conn, [ident], confirm=True, expected_runs=expected)
    assert (
        conn.execute("SELECT count(*) FROM tag_assignment WHERE source='human'").fetchone()[0] == 0
    )


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE content_item SET text_content='正文变了'",
        "UPDATE source_authorization SET revoked_at='gone'",
        "UPDATE tag SET name='标签变了',name_norm='标签变了' WHERE name='已有甲'",
    ],
)
def test_adoption_rechecks_target_authorization_and_selected_tag(model_data, change):
    conn, _, _, _ = model_data
    access, proposal, args = bound_proposal(model_data)
    tagging.apply_classification(conn, proposal, **args)
    ident, expected = proposal_row(conn)
    conn.execute(change)
    with pytest.raises(ValueError, match="快照"):
        tagging.review_suggestions(
            conn, [ident], confirm=True, expected_runs=expected, model_access=access
        )
    tagging.review_suggestions(conn, [ident], confirm=False)
    assert conn.execute("SELECT revoked_at FROM tag_assignment").fetchone()[0] is not None


def test_adoption_allows_unrelated_new_tag_but_requires_policy_context(model_data):
    conn, _, _, _ = model_data
    access, proposal, args = bound_proposal(model_data)
    tagging.apply_classification(conn, proposal, **args)
    ident, expected = proposal_row(conn)
    conn.execute(
        "INSERT INTO tag(uuid,name,name_norm,namespace) VALUES (?,'无关新增','无关新增','general')",
        (str(uuid.uuid4()),),
    )
    with pytest.raises(ValueError, match="审核入口"):
        tagging.review_suggestions(conn, [ident], confirm=True, expected_runs=expected)
    tagging.review_suggestions(
        conn, [ident], confirm=True, expected_runs=expected, model_access=access
    )
    assert (
        conn.execute("SELECT count(*) FROM tag_assignment WHERE source='human'").fetchone()[0] == 1
    )


def test_adoption_rechecks_effective_environment_policy(model_data, monkeypatch):
    conn, _, _, _ = model_data
    access, proposal, args = bound_proposal(model_data)
    tagging.apply_classification(conn, proposal, **args)
    ident, expected = proposal_row(conn)
    monkeypatch.setenv("PC__PRIVACY__LOCAL_ONLY_MODE", "true")
    with pytest.raises(ValueError, match="策略"):
        tagging.review_suggestions(
            conn, [ident], confirm=True, expected_runs=expected, model_access=access
        )


def test_adoption_uses_current_runtime_pixel_limit(model_data, tmp_path):
    from pc.services import model_tagging as mt

    conn, ids, _, _ = model_data
    media = add_image(conn, ids[0], tmp_path / 'media', b'\x89PNG\r\n\x1a\noriginal')
    conn.execute('UPDATE media SET width=10,height=10 WHERE media_id=?', (media,))
    access, proposal, args = bound_proposal(model_data)
    access.max_image_pixels = 99
    # 模拟同策略下被错误保存的旧建议；配置摘要相同也必须重新验证像素。
    snapshot = mt.parse_snapshot(args['input_ref'])
    snapshot['config_digest'] = access.config_digest(conn)
    args['input_ref'] = mt.encoded(snapshot).decode()
    tagging.apply_classification(conn, proposal, **args)
    ident, expected = proposal_row(conn)
    with pytest.raises(ValueError, match='快照'):
        tagging.review_suggestions(conn, [ident], confirm=True, expected_runs=expected, model_access=access)
    assert conn.execute("SELECT count(*) FROM tag_assignment WHERE source='human'").fetchone()[0] == 0


def test_bound_suggestions_participate_in_atomic_outer_transaction(model_data):
    conn, _, _, _ = model_data
    _, proposal, args = bound_proposal(model_data)
    with pytest.raises(RuntimeError, match="outer rollback"), db.transaction(conn):
        tagging.apply_classification(conn, proposal, **args)
        assert conn.in_transaction
        raise RuntimeError("outer rollback")
    assert conn.execute("SELECT count(*) FROM classification_run").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM tag_assignment").fetchone()[0] == 0


@pytest.mark.parametrize("input_ref", ["{}", "null", '{"version":1}'])
def test_empty_or_incomplete_bound_snapshot_writes_nothing(model_data, input_ref):
    conn, ids, tags, _ = model_data
    model = conn.execute(
        "INSERT INTO model_version(capability,model_name) VALUES ('llm','test') RETURNING model_version_id"
    ).fetchone()[0]
    proposal = ClassificationResult(tags=(TagCandidate("已有甲", 0.8),), model_version_id=model)
    with pytest.raises(ValueError, match="快照"):
        tagging.apply_classification(
            conn, proposal, content_ids=[ids[0]], existing_tag_ids=[tags[0]], input_ref=input_ref
        )
    assert conn.execute("SELECT count(*) FROM classification_run").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM tag_assignment").fetchone()[0] == 0
