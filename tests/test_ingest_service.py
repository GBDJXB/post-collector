"""L2 摄入：幂等、批次、原始载荷策略、入队。

摄入层唯一允许"什么都不做"的场合是**重复投递**——这正是 ``(source_id, platform_msg_id)``
唯一约束的用途。测试必须把"重复"与"新消息"区分开，否则一条 SQL 写错就会静默地
把重复内容算成新内容，而行数看不出来（因为唯一约束会拒绝）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pc.domain import SourceMessage
from pc.services import ingest as svc
from pc.services import sources as sources_service


@pytest.fixture
def source(fresh_db: Any) -> dict[str, Any]:
    info = sources_service.create_source(
        fresh_db, name="mine", platform="local", kind="export", trust_level=2
    )
    return sources_service.require_source(fresh_db, info["source_id"])


def _message(msg_id: str = "m1", **overrides: Any) -> SourceMessage:
    payload: dict[str, Any] = {
        "source_id": 1,
        "platform_msg_id": msg_id,
        "message_type": "media",
        "text_raw": "配文",
        "media_refs": ("/tmp/a.jpg",),
        "raw_payload": {"kind": "local_file", "path": msg_id},
    }
    payload.update(overrides)
    return SourceMessage(**payload)


class TestInsert:
    def test_message_and_job_are_written(self, fresh_db: Any, source: dict[str, Any]) -> None:
        report = svc.ingest_messages(fresh_db, source, [_message()])
        assert (report.seen, report.inserted, report.duplicates, report.enqueued) == (1, 1, 0, 1)
        assert fresh_db.execute("SELECT count(*) FROM message").fetchone()[0] == 1
        assert fresh_db.execute("SELECT count(*) FROM job").fetchone()[0] == 1

    def test_source_trust_is_copied_at_ingest_time(
        self, fresh_db: Any, source: dict[str, Any]
    ) -> None:
        svc.ingest_messages(fresh_db, source, [_message()])
        row = fresh_db.execute("SELECT source_trust FROM message").fetchone()
        assert row[0] == 2, "来源的信任级别在摄入当时就要留副本，事后改来源不该改写历史"

    def test_media_refs_are_persisted(self, fresh_db: Any, source: dict[str, Any]) -> None:
        """少了这一列，worker 拿不到引用，消息就无法重放。"""
        svc.ingest_messages(fresh_db, source, [_message(media_refs=("/a.jpg", "/b.jpg"))])
        row = fresh_db.execute("SELECT media_refs FROM message").fetchone()
        assert json.loads(row[0]) == ["/a.jpg", "/b.jpg"]

    def test_forward_chain_defaults_to_empty_array(
        self, fresh_db: Any, source: dict[str, Any]
    ) -> None:
        svc.ingest_messages(fresh_db, source, [_message()])
        row = fresh_db.execute("SELECT forward_chain FROM message").fetchone()
        assert json.loads(row[0]) == []

    def test_job_idem_key_matches_the_design_format(
        self, fresh_db: Any, source: dict[str, Any]
    ) -> None:
        svc.ingest_messages(fresh_db, source, [_message()])
        row = fresh_db.execute("SELECT idem_key FROM job").fetchone()
        message_id = fresh_db.execute("SELECT message_id FROM message").fetchone()[0]
        assert row[0] == f"ingest.process:message:{message_id}:"


class TestIdempotency:
    def test_same_message_twice_is_a_duplicate(self, fresh_db: Any, source: dict[str, Any]) -> None:
        first = svc.ingest_messages(fresh_db, source, [_message()])
        second = svc.ingest_messages(fresh_db, source, [_message()])
        assert first.inserted == 1
        assert second.inserted == 0
        assert second.duplicates == 1
        assert fresh_db.execute("SELECT count(*) FROM message").fetchone()[0] == 1
        assert fresh_db.execute("SELECT count(*) FROM job").fetchone()[0] == 1, (
            "重复投递不该产生第二个任务"
        )

    def test_same_msg_id_from_another_source_is_a_new_message(
        self, fresh_db: Any, source: dict[str, Any]
    ) -> None:
        other_info = sources_service.create_source(
            fresh_db, name="other", platform="local", kind="export"
        )
        other = sources_service.require_source(fresh_db, other_info["source_id"])
        svc.ingest_messages(fresh_db, source, [_message("same-id")])
        report = svc.ingest_messages(fresh_db, other, [_message("same-id")])
        assert report.inserted == 1, "幂等键是（来源, 平台消息号），不同来源不该互相顶掉"
        assert fresh_db.execute("SELECT count(*) FROM message").fetchone()[0] == 2

    def test_reingesting_after_enqueue_does_not_enqueue_again(
        self, fresh_db: Any, source: dict[str, Any]
    ) -> None:
        svc.ingest_messages(fresh_db, source, [_message()])
        fresh_db.execute("DELETE FROM message")
        report = svc.ingest_messages(fresh_db, source, [_message()])
        assert report.inserted == 1
        assert report.enqueued == 0, "任务幂等键独立于消息行，重复入队要被吞掉"


class TestRawPayloadPolicy:
    def test_only_the_hash_is_stored_by_default(
        self, fresh_db: Any, source: dict[str, Any]
    ) -> None:
        svc.ingest_messages(fresh_db, source, [_message()])
        row = fresh_db.execute("SELECT raw_payload, raw_payload_hash FROM message").fetchone()
        assert row[0] is None, "默认档位只留结构化信封 + 哈希，不留原始载荷"
        assert row[1] is not None and len(str(row[1])) == 64

    def test_hash_is_stable_regardless_of_key_order(self) -> None:
        first = svc.raw_payload_hash({"a": 1, "b": 2})
        second = svc.raw_payload_hash({"b": 2, "a": 1})
        assert first == second

    def test_empty_payload_has_no_hash(self) -> None:
        assert svc.raw_payload_hash({}) is None
        assert svc.raw_payload_hash(None) is None


class TestBatching:
    def test_batches_commit_independently(self, fresh_db: Any, source: dict[str, Any]) -> None:
        messages = [_message(f"m{index}") for index in range(5)]
        report = svc.ingest_messages(fresh_db, source, messages, batch_size=2)
        assert report.inserted == 5
        assert fresh_db.execute("SELECT count(*) FROM message").fetchone()[0] == 5

    def test_batch_size_of_one_still_works(self, fresh_db: Any, source: dict[str, Any]) -> None:
        report = svc.ingest_messages(fresh_db, source, [_message("a"), _message("b")], batch_size=1)
        assert report.inserted == 2

    def test_report_merge_adds_up(self) -> None:
        left = svc.IngestReport(seen=1, inserted=1, enqueued=1, message_ids=(1,))
        right = svc.IngestReport(seen=2, inserted=1, duplicates=1, message_ids=(2,))
        merged = left.merge(right)
        assert (merged.seen, merged.inserted, merged.duplicates, merged.enqueued) == (3, 2, 1, 1)
        assert merged.message_ids == (1, 2)


class TestJobIdemKeyHelper:
    def test_format_is_stable(self) -> None:
        assert svc.job_idem_key(7) == "ingest.process:message:7:"
        assert svc.job_idem_key(7, "other.job") == "other.job:message:7:"


class TestOriginNote:
    """来源备注：root 手工上传时"我记不清这东西从哪来的"的那个钩子。

    它是**调用方注解**，不是平台信封的一部分（平台的"转自哪里"走 ``forward_chain``），
    所以整批套用同一个值，且**绝不能混进 raw_payload**——那是原始载荷的自证哈希。
    """

    def test_note_is_written_to_every_message(self, fresh_db: Any, source: dict[str, Any]) -> None:
        svc.ingest_messages(
            fresh_db, source, [_message("a"), _message("b")], origin_note="好像是某个群里的"
        )
        rows = fresh_db.execute("SELECT origin_note FROM message ORDER BY message_id").fetchall()
        assert [row[0] for row in rows] == ["好像是某个群里的", "好像是某个群里的"]

    def test_note_is_optional(self, fresh_db: Any, source: dict[str, Any]) -> None:
        svc.ingest_messages(fresh_db, source, [_message()])
        assert fresh_db.execute("SELECT origin_note FROM message").fetchone()[0] is None

    def test_blank_note_becomes_null(self, fresh_db: Any, source: dict[str, Any]) -> None:
        svc.ingest_messages(fresh_db, source, [_message()], origin_note="   ")
        assert fresh_db.execute("SELECT origin_note FROM message").fetchone()[0] is None, (
            "空备注要存成 NULL，不要让'什么都没写'和'写了空格'在库里长得不一样"
        )

    def test_note_does_not_leak_into_the_payload_hash(
        self, fresh_db: Any, source: dict[str, Any]
    ) -> None:
        """备注是人写的，原始载荷哈希必须只反映平台给的东西。"""
        payload = {"kind": "local_file", "path": "x.jpg"}
        svc.ingest_messages(
            fresh_db, source, [_message("a", raw_payload=payload)], origin_note="有备注"
        )
        svc.ingest_messages(fresh_db, source, [_message("b", raw_payload=payload)])
        hashes = {
            row[0]
            for row in fresh_db.execute("SELECT raw_payload_hash FROM message ORDER BY message_id")
        }
        assert len(hashes) == 1, "同一份原始载荷，备注不同不该算出两个哈希"
