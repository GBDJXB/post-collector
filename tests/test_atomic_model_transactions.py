"""模型日志/建议与双设置审计必须参与同一外层事务。"""

import sqlite3

import pytest

from pc.adapters import sqlite as db


def test_explicit_nested_transaction_does_not_commit_outer(fresh_db):
    conn = fresh_db
    with pytest.raises(RuntimeError), db.transaction(conn):
        with db.transaction(conn, nested=True):
            conn.execute(
                "INSERT INTO setting(key,value,value_type,source) VALUES ('one','true','bool','db')"
            )
        assert conn.in_transaction
        raise RuntimeError("rollback outer")
    assert conn.execute("SELECT count(*) FROM setting").fetchone()[0] == 0


def test_nested_failure_is_atomic_even_when_caller_catches(fresh_db):
    conn = fresh_db
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO setting(key,value,value_type,source) VALUES ('outer','true','bool','db')"
        )
        with pytest.raises(RuntimeError), db.transaction(conn, nested=True):
            conn.execute(
                "INSERT INTO setting(key,value,value_type,source) VALUES ('inner','true','bool','db')"
            )
            raise RuntimeError("rollback savepoint")
        assert conn.in_transaction
    assert [r[0] for r in conn.execute("SELECT key FROM setting")] == ["outer"]


def test_sqlite_rollback_preserves_original_exception(fresh_db):
    fresh_db.execute(
        "CREATE TRIGGER abort_settings BEFORE INSERT ON setting BEGIN SELECT RAISE(ROLLBACK,'original failure'); END"
    )
    with (
        pytest.raises(sqlite3.IntegrityError, match="original failure"),
        db.transaction(fresh_db),
        db.transaction(fresh_db, nested=True),
    ):
        fresh_db.execute(
            "INSERT INTO setting(key,value,value_type,source) VALUES ('inner','true','bool','db')"
        )
    assert not fresh_db.in_transaction


def test_default_still_rejects_accidental_nesting(fresh_db):
    with (
        db.transaction(fresh_db),
        pytest.raises(db.DatabaseError, match="不能嵌套"),
        db.transaction(fresh_db),
    ):
        pytest.fail("accidental nested transaction entered")
