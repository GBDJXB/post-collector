"""独立组合、历史兼容、有序签名与升级回归。"""

import json
import shutil
import uuid

import pytest

from pc import cli
from pc.adapters import sqlite as db
from pc.domain import post_signature
from pc.services import compositions as co
from pc.services import tagging
from pc.services.search import SearchQuery, run_search


@pytest.fixture
def items(fresh_db):
    conn = fresh_db
    source = conn.execute(
        "INSERT INTO source(uuid,name,platform,kind) VALUES (?,'mine','local','manual') RETURNING source_id",
        (str(uuid.uuid4()),),
    ).fetchone()[0]
    ids = []
    for i in range(3):
        message = conn.execute(
            "INSERT INTO message(uuid,source_id,platform_msg_id) VALUES (?,?,?) RETURNING message_id",
            (str(uuid.uuid4()), source, str(i)),
        ).fetchone()[0]
        ids.append(
            conn.execute(
                "INSERT INTO content_item(uuid,message_id,item_type,text_content,content_hash,status) VALUES (?,?,'text',?,?,'active') RETURNING content_id",
                (str(uuid.uuid4()), message, f"正文{i}", f"hash{i}"),
            ).fetchone()[0]
        )
    return conn, ids


def make(conn, ids):
    return co.write(conn, "create", name="组合", contents=ids)["composition_id"]


def test_order_cross_message_persistence_and_delete(items):
    conn, ids = items
    ident = make(conn, [ids[1], ids[0], ids[1]])
    assert [r["content_id"] for r in co.show(conn, ident)["members"]] == [ids[1], ids[0], ids[1]]
    other = make(conn, [ids[0]])
    co.write(conn, "set-members", ident=ident, contents=[ids[2], ids[0]])
    co.write(conn, "update", ident=ident, name="新名称", description="原文\n说明")
    assert co.show(conn, ident)["description"] == "原文\n说明"
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    with db.open_db(path) as second:
        assert co.show(second, ident)["name"] == "新名称"
    co.write(conn, "delete", ident=ident)
    assert co.list_compositions(conn)["total"] == 1
    assert co.show(conn, other)["members"][0]["content_id"] == ids[0]
    assert conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 3
    assert (
        conn.execute("SELECT count(*) FROM audit_log WHERE action LIKE 'composition.%'").fetchone()[
            0
        ]
        == 5
    )


@pytest.mark.parametrize("change", ["status='quarantined'", "nsfw_level=1", "deleted_at='now'"])
def test_hidden_member_redacted_and_not_tag_matched(items, change):
    conn, ids = items
    ident = make(conn, [ids[0], ids[1], ids[0]])
    tagging.apply_classification(conn, tagging.manual_result(["私有标签"]), content_ids=[ids[0]])
    conn.execute(f"UPDATE content_item SET {change} WHERE content_id=?", (ids[0],))
    members = co.show(conn, ident)["members"]
    assert members[0] == {"position": 0, "available": False}
    assert members[2] == {"position": 2, "available": False}
    assert co.list_compositions(conn, names=["私有标签"], match_member_tags=True)["total"] == 0
    with pytest.raises(ValueError):
        co.write(conn, "set-members", ident=ident, contents=[ids[0]])


def test_parent_deleted_and_invalid_update_rollback(items):
    conn, ids = items
    ident = make(conn, [ids[0]])
    before = co.show(conn, ident)
    audits = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    for contents in [[], [ids[1], 999], [True], [2**63]]:
        with pytest.raises(ValueError):
            co.write(conn, "set-members", ident=ident, contents=contents)
        assert co.show(conn, ident) == before
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == audits
    conn.execute(
        "UPDATE message SET deleted_at='now' WHERE message_id=?",
        (before["members"][0]["message_id"],),
    )
    assert co.show(conn, ident)["members"] == [{"position": 0, "available": False}]


def test_root_check(items):
    conn, ids = items
    user = conn.execute(
        "INSERT INTO app_user(uuid,username,display_name,tier) VALUES (?,'reader','reader',1) RETURNING user_id",
        (str(uuid.uuid4()),),
    ).fetchone()[0]
    with pytest.raises(ValueError, match="root"):
        co.write(conn, "create", name="组合", contents=ids, actor_id=user)
    assert co.list_compositions(conn)["total"] == 0


def test_tags_independent_alias_and_filters(items):
    conn, ids = items
    ident = make(conn, ids[:2])
    tagging.apply_classification(conn, tagging.manual_result(["成员标签"]), content_ids=[ids[0]])
    co.tag_change(conn, ident, ["组合标签"])
    tagging.change_alias(conn, "别名", name="组合标签")
    assert co.tag_change(conn, ident, ["别名"])["changed_tag_ids"] == []
    assert conn.execute("SELECT count(*) FROM tag_assignment").fetchone()[0] == 1
    counts = {
        r["name"]: (r["usage_count"], r["composition_usage_count"])
        for r in tagging.list_tags(conn)["tags"]
    }
    assert counts["组合标签"] == (0, 1)
    assert counts["成员标签"] == (1, 0)
    assert co.list_compositions(conn, names=["组合标签", "成员标签"])["total"] == 0
    rows = co.list_compositions(conn, names=["别名", "成员标签"], match_member_tags=True)[
        "compositions"
    ]
    assert len(rows) == 1 and {r["target"] for r in rows[0]["matches"]} == {"member", "composition"}
    original = co.show_tags(conn, ident)["assignments"][0]["assign_id"]
    co.tag_change(conn, ident, ["组合标签"], remove=True)
    assert co.show(conn, ident)["tags"] == []
    co.tag_change(conn, ident, ["组合标签"])
    assert co.show_tags(conn, ident)["assignments"][0]["assign_id"] == original
    co.write(conn, "set-members", ident=ident, contents=[ids[2]])
    assert co.show(conn, ident)["tags"][0]["name"] == "组合标签"
    assert co.list_compositions(conn, names=["成员标签"], match_member_tags=True)["total"] == 0
    co.write(conn, "delete", ident=ident)
    assert (
        next(r for r in tagging.list_tags(conn)["tags"] if r["name"] == "组合标签")[
            "composition_usage_count"
        ]
        == 0
    )


def test_list_compositions_filters_literal_name_and_exact_id(items):
    conn, ids = items
    first = co.write(conn, 'create', name='午后_图文', contents=[ids[0]])['composition_id']
    second = co.write(conn, 'create', name='午后%作品', contents=[ids[1]])['composition_id']
    third = co.write(conn, 'create', name='其他\\内容', contents=[ids[2]])['composition_id']
    co.write(conn, 'delete', ident=third)
    assert [r['composition_id'] for r in co.list_compositions(conn)['compositions']] == [second, first]
    assert [r['composition_id'] for r in co.list_compositions(conn, name='午后_')['compositions']] == [first]
    assert [r['composition_id'] for r in co.list_compositions(conn, name='%')['compositions']] == [second]
    assert [r['composition_id'] for r in co.list_compositions(conn, name='\\')['compositions']] == []
    assert [r['composition_id'] for r in co.list_compositions(conn, composition_id=first)['compositions']] == [first]
    assert co.list_compositions(conn, composition_id=third)['total'] == 0
    assert co.list_compositions(conn, composition_id=999999)['total'] == 0
    assert co.list_compositions(conn, name='正文0')['total'] == 0
    with pytest.raises(ValueError):
        co.list_compositions(conn, name='x' * 256)
    with pytest.raises(ValueError):
        co.list_compositions(conn, composition_id=0)


def test_list_compositions_filters_before_pagination_and_combines_tags(items):
    conn, ids = items
    first = co.write(conn, 'create', name='Album One', contents=[ids[0]])['composition_id']
    second = co.write(conn, 'create', name='Album Two', contents=[ids[1]])['composition_id']
    co.write(conn, 'create', name='Other', contents=[ids[2]])
    co.tag_change(conn, first, ['自有'])
    tagging.apply_classification(conn, tagging.manual_result(['成员']), content_ids=[ids[1]])
    page = co.list_compositions(conn, name='album', limit=1, offset=1)
    assert page['total'] == 2 and not page['has_more']
    assert [row['composition_id'] for row in page['compositions']] == [first]
    assert [row['composition_id'] for row in co.list_compositions(conn, name='album', names=['自有'])['compositions']] == [first]
    assert co.list_compositions(conn, name='album', names=['成员'])['total'] == 0
    assert [row['composition_id'] for row in co.list_compositions(conn, name='album', names=['成员'], match_member_tags=True)['compositions']] == [second]
    assert co.list_compositions(conn, name='Album One', composition_id=second)['total'] == 0
    assert [row['composition_id'] for row in co.list_compositions(conn, name='ALBUM ONE', composition_id=first)['compositions']] == [first]


def test_cli_and_old_message_tags(items, capsys):
    conn, ids = items
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert (
        cli.main(
            [
                "composition",
                "create",
                "--name",
                "测试",
                "--content",
                str(ids[1]),
                "--content",
                str(ids[0]),
                "--path",
                path,
                "--json",
            ]
        )
        == 0
    )
    ident = json.loads(capsys.readouterr().out)["composition_id"]
    assert (
        cli.main(
            ["tag", "add", "--composition", str(ident), "--tag", "组合", "--path", path, "--json"]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["composition", "list", "--tag", "组合", "--path", path, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    for args in [
        ["composition", "show", str(ident)],
        ["composition", "list"],
        ["tag", "show", "--composition", str(ident)],
    ]:
        assert cli.main([*args, "--path", path]) == 0
        capsys.readouterr()
    message = co.show(conn, ident)["members"][0]["message_id"]
    tagging.apply_classification(conn, tagging.manual_result(["旧标签"]), message_ids=[message])
    assert co.show(conn, ident)["tags"][0]["name"] == "组合"


def test_signature_order_rebuild_and_missing(items):
    conn, _ids = items
    source = conn.execute("SELECT source_id FROM message LIMIT 1").fetchone()[0]
    messages = []
    for sequence in [["a", "b"], ["b", "a"], ["a", "b", "a"], ["a", "b"]]:
        mid = conn.execute(
            "INSERT INTO message(uuid,source_id,platform_msg_id) VALUES (?,?,?) RETURNING message_id",
            (str(uuid.uuid4()), source, str(uuid.uuid4())),
        ).fetchone()[0]
        messages.append(mid)
        # 插入顺序故意与 ordinal 相反，重算必须按语义顺序。
        for ordinal, digest in reversed(list(enumerate(sequence))):
            conn.execute(
                "INSERT INTO content_item(uuid,message_id,item_type,ordinal,content_hash,status) VALUES (?,?,'text',?,?,'active')",
                (str(uuid.uuid4()), mid, ordinal, digest),
            )
    db.sync_post_rows(conn)
    rows = [
        dict(conn.execute("SELECT * FROM post WHERE message_id=?", (mid,)).fetchone())
        for mid in messages
    ]
    assert len({r["content_signature"] for r in rows}) == 3
    assert rows[-1]["canonical_post_id"] == messages[0]
    db.refresh_search_derived(conn)
    hits = run_search(conn, SearchQuery())["hits"]
    assert {r["message_id"] for r in hits}.intersection(messages) == set(messages[:3])
    all_hits = run_search(conn, SearchQuery(include_duplicates=True))["hits"]
    assert {r["message_id"] for r in all_hits}.intersection(messages) == set(messages)
    assert db.sync_post_rows(conn)["posts"] == 0
    assert post_signature([("text", "a"), ("text", None)]) is None


def test_upgrade_clears_old_signatures(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in db.DEFAULT_MIGRATIONS_DIR.glob("*.sql"):
        if int(path.name[:4]) < 10:
            shutil.copy2(path, migrations / path.name)
    with db.open_db(tmp_path / "upgrade.sqlite3") as conn:
        db.apply_migration(conn, migrations)
        source = conn.execute(
            "INSERT INTO source(uuid,name,platform,kind) VALUES (?,'upgrade','local','manual') RETURNING source_id",
            (str(uuid.uuid4()),),
        ).fetchone()[0]
        mid = conn.execute(
            "INSERT INTO message(uuid,source_id,platform_msg_id) VALUES (?,?,'old') RETURNING message_id",
            (str(uuid.uuid4()), source),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO post(message_id,part_count,type_mask,first_seen_at,content_signature,canonical_post_id,dup_group_id) VALUES (?,1,1,'now','old',?,?)",
            (mid, mid, mid),
        )
        db.apply_migration(conn)
        assert tuple(
            conn.execute(
                "SELECT content_signature,canonical_post_id,dup_group_id FROM post"
            ).fetchone()
        ) == (None, None, None)
        assert conn.execute("SELECT count(*) FROM composition").fetchone()[0] == 0


def test_media_part_boundaries_and_association_order(items):
    conn, _ids = items
    source = conn.execute("SELECT source_id FROM message LIMIT 1").fetchone()[0]
    media = []
    for digest in ["a" * 64, "b" * 64]:
        media.append(
            conn.execute(
                "INSERT INTO media(uuid,sha256,size_bytes,object_key) VALUES (?,?,1,?) RETURNING media_id",
                (str(uuid.uuid4()), digest, digest),
            ).fetchone()[0]
        )
    signatures = []
    for sequences in [[[media[0], media[1]]], [[media[1], media[0]]], [[media[0]], [media[1]]]]:
        mid = conn.execute(
            "INSERT INTO message(uuid,source_id,platform_msg_id) VALUES (?,?,?) RETURNING message_id",
            (str(uuid.uuid4()), source, str(uuid.uuid4())),
        ).fetchone()[0]
        for ordinal, attachments in enumerate(sequences):
            content = conn.execute(
                "INSERT INTO content_item(uuid,message_id,item_type,ordinal,status) VALUES (?,?,'image',?,'active') RETURNING content_id",
                (str(uuid.uuid4()), mid, ordinal),
            ).fetchone()[0]
            for position, media_id in reversed(list(enumerate(attachments))):
                conn.execute(
                    "INSERT INTO content_media(content_id,media_id,role,ordinal) VALUES (?,?,'primary',?)",
                    (content, media_id, position),
                )
        db.sync_post_rows(conn, message_ids=[mid])
        signatures.append(
            conn.execute(
                "SELECT content_signature FROM post WHERE message_id=?", (mid,)
            ).fetchone()[0]
        )
    assert len(set(signatures)) == 3


def test_audit_failure_rolls_back_create_and_tag(items, monkeypatch):
    conn, ids = items
    ident = make(conn, ids)

    def fail(*args, **kwargs):
        raise RuntimeError("审计失败")

    monkeypatch.setattr(tagging, "_audit", fail)
    with pytest.raises(RuntimeError):
        co.write(conn, "create", name="失败", contents=ids)
    assert co.list_compositions(conn)["total"] == 1
    with pytest.raises(RuntimeError):
        co.tag_change(conn, ident, ["失败标签"])
    assert conn.execute("SELECT count(*) FROM tag").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM composition_tag_assignment").fetchone()[0] == 0


def test_remove_member_unlinks_one_occurrence_only(items):
    conn, ids = items
    ident = make(conn, [ids[0], ids[1], ids[0]])
    other = make(conn, [ids[0]])
    before_audits = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    co.write(conn, "remove-member", ident=ident, position=0)
    assert [(m["position"], m["content_id"]) for m in co.show(conn, ident)["members"]] == [
        (0, ids[1]), (1, ids[0])
    ]
    assert co.show(conn, other)["members"][0]["content_id"] == ids[0]
    assert conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before_audits + 1


def test_member_moves_and_addition_preserve_hidden_positions(items):
    conn, ids = items
    ident = make(conn, [ids[0], ids[1], ids[0]])
    conn.execute("UPDATE content_item SET deleted_at='now' WHERE content_id=?", (ids[1],))
    co.write(conn, "move-member", ident=ident, position=1, to_position=0)
    assert co.show(conn, ident)["members"][0] == {"position": 0, "available": False}
    co.write(conn, "remove-member", ident=ident, position=1)
    co.write(conn, "add-member", ident=ident, content_id=ids[0])
    members = co.show(conn, ident)["members"]
    assert members[0] == {"position": 0, "available": False}
    assert [(m["position"], m["content_id"]) for m in members[1:]] == [
        (1, ids[0]), (2, ids[0])
    ]
    assert [r["content_id"] for r in conn.execute(
        "SELECT content_id FROM composition_member WHERE composition_id=? ORDER BY position", (ident,)
    )] == [ids[1], ids[0], ids[0]]
    with pytest.raises(ValueError, match="不可见"):
        co.write(conn, "add-member", ident=ident, content_id=ids[1])


def test_last_member_requires_force_then_can_be_added_again(items):
    conn, ids = items
    ident = make(conn, [ids[0]])
    before = co.show(conn, ident)
    count = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    with pytest.raises(ValueError, match="最后"):
        co.write(conn, "remove-member", ident=ident, position=0)
    assert co.show(conn, ident) == before
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == count
    co.write(conn, "remove-member", ident=ident, position=0, force_empty=True)
    assert co.show(conn, ident)["members"] == []
    co.write(conn, "add-member", ident=ident, content_id=ids[1])
    assert co.show(conn, ident)["members"][0]["content_id"] == ids[1]


def test_member_position_validation_stale_revision_and_audit_rollback(items, monkeypatch):
    conn, ids = items
    ident = make(conn, [ids[0], ids[1]])
    initial = co.show(conn, ident)
    for position in (-1, 2, True):
        with pytest.raises(ValueError):
            co.write(conn, "remove-member", ident=ident, position=position)
    co.write(conn, "move-member", ident=ident, position=0, to_position=1)
    assert co.show(conn, ident)["updated_at"] != initial["updated_at"]
    with pytest.raises(ValueError, match="变化"):
        co.write(conn, "remove-member", ident=ident, position=0, expected_updated_at=initial["updated_at"])
    after = co.show(conn, ident)
    count = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    monkeypatch.setattr(tagging, "_audit", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("审计失败")))
    with pytest.raises(RuntimeError, match="审计失败"):
        co.write(conn, "remove-member", ident=ident, position=0)
    assert co.show(conn, ident) == after
    assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == count


def test_cli_remove_member_force_empty(items, capsys):
    conn, ids = items
    ident = make(conn, [ids[0]])
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    args = ["composition", "remove-member", str(ident), "--position", "0", "--path", path]
    assert cli.main(args) == 2
    capsys.readouterr()
    assert cli.main([*args, "-f", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "remove-member"
    assert co.show(conn, ident)["members"] == []
