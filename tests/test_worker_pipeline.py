"""L3 处理管线：抢占、重试与死信、去重、隔离、幂等。

这组测试守的是**三件容易悄悄出错、出错了又很难发现的事**：

* 抢占之后 ``attempts`` 必须已经加过——否则一个能把 worker 搞崩的任务会被无限重试；
* 同一份字节只能有一行 ``media``，多个内容项各自引用它（内容项与媒体是多对多）；
* 确定性失败进死信、偶发失败才退避重试（把前者反复重试只是把同一条错误抄五遍）。

外加一条红线邻居：**扫描不通过的字节落在隔离区**，与主媒体目录物理分离。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pc.adapters import sqlite as db
from pc.adapters.local_storage import LocalStorageBackend
from pc.adapters.magic_scan import MagicNumberScanner
from pc.config import IngestLimits, JobSettings
from pc.domain import (
    MediaTooLargeError,
    post_is_mixed,
    post_signature,
    post_type_mask,
)
from pc.services import ingest as ingest_service
from pc.services import pipeline as pipeline_module
from pc.services import sources as sources_service
from pc.services.pipeline import PermanentError, Worker

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"j" * 64
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"p" * 64
EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"e" * 32


@pytest.fixture
def env(tmp_path: Path, fresh_db: Any) -> SimpleNamespace:
    """迷你环境：已迁移的库 + 两个存储后端 + 扫描器 + worker。"""
    media = tmp_path / "media"
    quarantine = tmp_path / "quarantine"
    worker = Worker(
        fresh_db,
        storage=LocalStorageBackend(media),
        quarantine=LocalStorageBackend(quarantine),
        scanner=MagicNumberScanner(),
        limits=IngestLimits(),
        job_settings=JobSettings(),
        worker_id="test-worker",
    )
    return SimpleNamespace(
        conn=fresh_db, worker=worker, media=media, quarantine=quarantine, tmp=tmp_path
    )


def _source(conn: Any, root: Path, *, name: str = "mine", kind: str = "export") -> dict[str, Any]:
    info = sources_service.create_source(
        conn, name=name, platform="local", kind=kind, config={"roots": [str(root)]}
    )
    return sources_service.require_source(conn, info["source_id"])


def _seed(
    conn: Any,
    *,
    source_id: int,
    refs: tuple[str, ...] = (),
    text: str | None = None,
    msg_id: str = "m1",
    attempts: int = 0,
    max_attempts: int = 5,
    sender: str | None = None,
    note: str | None = None,
    sent_at: str | None = None,
) -> tuple[int, int]:
    """直接写一条消息 + 一个排队任务（绕开连接器，便于构造边界情况）。"""
    message_id = conn.execute(
        "INSERT INTO message (uuid, source_id, platform_msg_id, message_type, text_raw,"
        " sender_display_name, origin_note, media_refs, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING message_id",
        (
            str(uuid.uuid4()),
            source_id,
            msg_id,
            "media" if refs else "text",
            text,
            sender,
            note,
            json.dumps(list(refs)),
            sent_at,
        ),
    ).fetchone()[0]
    job_id = conn.execute(
        "INSERT INTO job (job_type, target_type, target_id, status, priority, attempts,"
        " max_attempts, run_after, idem_key) "
        "VALUES ('ingest.process', 'message', ?, 'queued', 100, ?, ?, ?, ?) RETURNING job_id",
        (
            int(message_id),
            attempts,
            max_attempts,
            db.now_iso(),
            ingest_service.job_idem_key(int(message_id)),
        ),
    ).fetchone()[0]
    return int(message_id), int(job_id)


class TestPostRows:
    """帖行（派生表 ``post``，检索的结果单位）：构成摘要 + 整帖签名 + 归组。"""

    def _parts(self, env: SimpleNamespace) -> dict[str, Any]:
        return {
            "text_hashes": [
                row[0]
                for row in env.conn.execute(
                    "SELECT content_hash FROM content_item WHERE item_type = 'text'"
                )
            ],
            "media_hashes": [row[0] for row in env.conn.execute("SELECT sha256 FROM media")],
        }

    def test_mixed_message_writes_one_post_row(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        first = _write(env.tmp / "a.jpg", JPEG)
        second = _write(env.tmp / "b.png", PNG)
        _seed(
            env.conn,
            source_id=source["source_id"],
            refs=(first, second),
            text="两图一说明",
            sent_at="2026-02-03T04:05:06.000Z",
        )
        env.worker.run_once()
        post = dict(env.conn.execute("SELECT * FROM post").fetchone())
        parts = self._parts(env)
        assert post["part_count"] == 3
        assert post["type_mask"] == post_type_mask(["text", "image"])
        assert post_is_mixed(post["type_mask"]), "文本 + 图片 = 跨类型内容"
        assert post["sent_at"] == "2026-02-03T04:05:06.000Z"
        assert post["content_signature"] == post_signature(
            [("text", parts["text_hashes"][0])]
            + [("media", digest) for digest in parts["media_hashes"]]
        )
        assert post["canonical_post_id"] == post["message_id"], "第一条帖的代表就是它自己"

    def test_images_only_post_is_not_mixed(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        first = _write(env.tmp / "a.jpg", JPEG)
        second = _write(env.tmp / "b.png", PNG)
        _seed(env.conn, source_id=source["source_id"], refs=(first, second))
        env.worker.run_once()
        post = dict(env.conn.execute("SELECT * FROM post").fetchone())
        assert post["part_count"] == 2
        assert post["type_mask"] == post_type_mask(["image"])
        assert not post_is_mixed(post["type_mask"]), "两张图是纯图片内容"

    def test_same_signature_points_at_the_first_post(self, env: SimpleNamespace) -> None:
        """整帖内容相同才折叠：代表是 message_id 较小的那条。"""
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text="同一段话", msg_id="m1")
        _seed(env.conn, source_id=source["source_id"], text="同一段话", msg_id="m2")
        env.worker.run_once()
        rows = [
            dict(row)
            for row in env.conn.execute(
                "SELECT message_id, content_signature, canonical_post_id, dup_group_id"
                " FROM post ORDER BY message_id"
            )
        ]
        assert len(rows) == 2
        assert rows[0]["content_signature"] == rows[1]["content_signature"]
        assert rows[0]["canonical_post_id"] == rows[0]["message_id"]
        assert rows[1]["canonical_post_id"] == rows[0]["message_id"]

    def test_message_without_parts_has_no_post_row(self, env: SimpleNamespace) -> None:
        """空消息不产生内容项，也不该在结果里留一张空帖。"""
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text=None, refs=())
        env.worker.run_once()
        assert env.conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 0
        assert env.conn.execute("SELECT count(*) FROM post").fetchone()[0] == 0


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path)


def _job(conn: Any, job_id: int) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM job WHERE job_id = ?", (job_id,)).fetchone())


class TestClaimAndRecover:
    def test_claim_marks_running_and_counts_the_attempt(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"])
        claimed = env.worker.claim()
        assert claimed is not None
        assert claimed["status"] == "running"
        assert claimed["locked_by"] == "test-worker"
        assert claimed["attempts"] == 1, "抢占即一次尝试：进程被杀也要算数"

    def test_claim_on_empty_queue_returns_none(self, env: SimpleNamespace) -> None:
        assert env.worker.claim() is None

    def test_exhausted_jobs_are_not_claimed(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], attempts=5, max_attempts=5)
        assert env.worker.claim() is None

    def test_future_run_after_is_not_claimed(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _, job_id = _seed(env.conn, source_id=source["source_id"])
        env.conn.execute(
            "UPDATE job SET run_after = '2099-01-01T00:00:00.000Z' WHERE job_id = ?", (job_id,)
        )
        assert env.worker.claim() is None

    def test_recover_stale_resets_old_running_jobs(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _, job_id = _seed(env.conn, source_id=source["source_id"])
        env.conn.execute(
            "UPDATE job SET status = 'running', locked_by = 'dead-worker', locked_at = ? "
            "WHERE job_id = ?",
            ("2000-01-01T00:00:00.000Z", job_id),
        )
        assert env.worker.recover_stale() == 1
        row = _job(env.conn, job_id)
        assert row["status"] == "queued"
        assert row["locked_by"] is None

    def test_recover_leaves_fresh_locks_alone(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _, job_id = _seed(env.conn, source_id=source["source_id"])
        env.conn.execute(
            "UPDATE job SET status = 'running', locked_at = ? WHERE job_id = ?",
            (db.now_iso(), job_id),
        )
        assert env.worker.recover_stale() == 0


class TestTextItems:
    def test_text_item_keeps_raw_and_tokenizes_into_text_search(
        self, env: SimpleNamespace
    ) -> None:
        """``text_search`` 是应用层分词结果（DESIGN §5.1），落库时就该算好。

        ``content_word`` 只索引这一列，所以它留空等于"1~2 字中文查询永远搜不到"。
        这里同时钉住两件事：正文保持原样（`text_content` 不改写），以及分词覆盖
        **与 content_fts 相同的四列**——`search_extra`（来源名/平台）也在词级索引里。
        """
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text="  Hello　World  ")
        report = env.worker.run_once()
        assert report.succeeded == 1
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert row["item_type"] == "text"
        assert row["text_content"] == "  Hello　World  ", "正文保持原样"
        assert row["text_search"] == "hello world mine local text 文 本 文本", (
            "分词结果 = 正文（NFKC 之后）+ search_extra（含类型值与中文标签）"
        )
        assert row["search_extra"] == "mine local text 文本"
        assert row["char_count"] == len("Hello World"), "字符数按规范化后的文本算"

    def test_chinese_text_gets_single_and_double_character_tokens(
        self, env: SimpleNamespace
    ) -> None:
        """中文要产出单字与双字：单字让 1 字查询可命中，双字让 2 字查询可命中。"""
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text="蓝发精灵在雨里")
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert "蓝发" in row["text_search"].split()
        assert "雨" in row["text_search"].split()
        assert "蓝发精" not in row["text_search"].split(), "三字交给 trigram，不进词级索引"

    def test_pipeline_written_row_is_searchable_by_two_characters(
        self, env: SimpleNamespace
    ) -> None:
        """端到端钉住设计里那条退化点：**真实管线写出的行**，2 字查询必须命中词级索引。

        《DESIGN §5.1》：``trigram`` 在查询串少于 3 字时命中 0；因此 2 字必须由
        `content_word` 兜住，而它只索引 `text_search`——这一列由 worker 在落库时算好。
        `tests/test_sqlite_behaviour.py` 用手工种的 `text_search` 断言过这条规则，
        这里断言的是"管线真的产出了满足它的那一列"。
        """
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text="蓝发精灵在雨里")
        assert env.worker.run_once().succeeded == 1
        word_hits = env.conn.execute(
            "SELECT count(*) FROM content_word WHERE content_word MATCH ?", ('"蓝发"',)
        ).fetchone()[0]
        trigram_hits = env.conn.execute(
            "SELECT count(*) FROM content_fts WHERE content_fts MATCH ?", ('"蓝发"',)
        ).fetchone()[0]
        assert word_hits == 1, "2 字查询没命中词级索引，说明 text_search 没写对"
        assert trigram_hits == 0, "trigram 的 2 字退化行为应该保持原样"

    def test_sender_name_enters_search_extra(self, env: SimpleNamespace) -> None:
        """发送者名是检索维度（DESIGN §5.1），拼进 search_extra 才搜得到。

        ``search_extra`` 的合成顺序是固定契约（``domain.search_extra_value`` 的 docstring）：
        来源名 → 平台 → 发送者名 → 类型 → 类型中文 → 文件名 → 标签名 → 备注。
        """
        source = _source(env.conn, env.tmp)
        _seed(
            env.conn,
            source_id=source["source_id"],
            text="一段话",
            sender="蓝发精灵",
        )
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert row["search_extra"] == "mine local 蓝发精灵 text 文本"
        assert "蓝发" in row["text_search"].split()

    def test_item_type_is_searchable_in_both_scripts(self, env: SimpleNamespace) -> None:
        """类型既进 search_extra（可搜"图片/image"），也仍是精确过滤器。"""
        source = _source(env.conn, env.tmp)
        image = env.tmp / "壁纸.jpg"
        image.write_bytes(JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(str(image),))
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert row["item_type"] == "image"
        assert "image" in row["search_extra"].split()
        assert "图片" in row["search_extra"].split()
        assert row["search_extra"].split()[-3:] == ["image", "图片", "壁纸.jpg"]

    def test_media_item_keeps_its_file_name(self, env: SimpleNamespace) -> None:
        """文件名落进 original_name，并进 search_extra（正是"按名字找图"的入口）。"""
        source = _source(env.conn, env.tmp)
        photo = env.tmp / "电脑壁纸.jpg"
        photo.write_bytes(JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(str(photo),), note="从某个群里存的")
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert row["original_name"] == "电脑壁纸.jpg"
        assert "电脑壁纸.jpg" in row["search_extra"]
        assert row["search_extra"].endswith("从某个群里存的"), "备注排在最后（合成顺序契约）"
        assert "壁纸" in row["text_search"].split(), "2 字查询走词级索引"
        assert "jpg" in row["text_search"].split(), "扩展名随文件名一起可搜"

    def test_file_name_never_keeps_a_directory(self, env: SimpleNamespace) -> None:
        """只存 basename：数据库里不存位置（AGENTS「不把媒体文件路径写进数据库」）。"""
        source = _source(env.conn, env.tmp)
        nested = env.tmp / "素材" / "某个群"
        nested.mkdir(parents=True)
        photo = nested / "猫.png"
        photo.write_bytes(PNG)
        _seed(env.conn, source_id=source["source_id"], refs=(str(photo),))
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert row["original_name"] == "猫.png"
        assert "素材" not in row["search_extra"] and "某个群" not in row["search_extra"]

    def test_message_time_is_copied_onto_the_item(self, env: SimpleNamespace) -> None:
        """``content_item.sent_at`` 是消息时间的派生副本（0006），落库时就要抄下来。

        缺了它，"按消息时间排序"会去 join 另一张表——十万条实测退化成一次整体排序
        （P50 354 ms）；有了单表副本与索引，同一条查询是亚毫秒级。
        """
        source = _source(env.conn, env.tmp)
        photo = env.tmp / "猫.png"
        photo.write_bytes(PNG)
        _seed(env.conn, source_id=source["source_id"], refs=(str(photo),), sent_at="2026-02-03T04:05:06.000Z")
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert row["sent_at"] == "2026-02-03T04:05:06.000Z"
        assert row["sent_at"] == env.conn.execute("SELECT sent_at FROM message").fetchone()[0]

    def test_manual_upload_has_no_message_time(self, env: SimpleNamespace) -> None:
        """手工上传没有消息时间：两侧都是空，这是正常状态而不是缺失数据。"""
        source = _source(env.conn, env.tmp)
        photo = env.tmp / "猫.png"
        photo.write_bytes(PNG)
        _seed(env.conn, source_id=source["source_id"], refs=(str(photo),), sent_at=None)
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert row["sent_at"] is None

    def test_text_item_has_no_file_name_yet(self, env: SimpleNamespace) -> None:
        """纯文本文件没有 media ref，名字在 L2 之后就丢了 → 这一刀留空（见 AGENTS）。

        它按内容仍然可搜；补齐文本文件名需要给信封加字段，是单独一刀的事。这里同时钉住
        "类型标签进得去、文件名进不去"，避免将来有人以为文本项已经有名字了。
        """
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text="文件名暂时搜不到，但内容可以")
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert row["item_type"] == "text"
        assert row["original_name"] is None
        assert row["search_extra"] == "mine local text 文本"
        word_hits = env.conn.execute(
            "SELECT count(*) FROM content_word WHERE content_word MATCH ?", ('"内容"',)
        ).fetchone()[0]
        assert word_hits == 1, "文本项按内容照样可搜"

    def test_same_text_in_two_messages_is_grouped_not_blocked(
        self, env: SimpleNamespace
    ) -> None:
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text="同一段话", msg_id="m1")
        _seed(env.conn, source_id=source["source_id"], text="同一段话", msg_id="m2")
        env.worker.run_once()
        rows = [
            dict(row)
            for row in env.conn.execute(
                "SELECT content_id, content_hash, dup_group_id, canonical_id FROM content_item "
                "ORDER BY content_id"
            )
        ]
        assert len(rows) == 2, "同文本跨消息必须能落两行（0002 迁移的语义）"
        assert rows[0]["content_hash"] == rows[1]["content_hash"]
        assert rows[0]["dup_group_id"] == rows[1]["dup_group_id"] == rows[0]["content_id"]
        assert rows[1]["canonical_id"] == rows[0]["content_id"]

    def test_whitespace_only_text_creates_no_item(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text="   \n  ")
        report = env.worker.run_once()
        assert report.succeeded == 1
        assert env.conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 0


class TestMediaItems:
    def test_media_item_links_media_and_lands_on_disk(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "cat.jpg", JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        assert env.worker.run_once().succeeded == 1

        item = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        media = dict(env.conn.execute("SELECT * FROM media").fetchone())
        link = dict(env.conn.execute("SELECT * FROM content_media").fetchone())
        assert item["item_type"] == "image"
        assert item["status"] == "active"
        assert item["byte_size"] == len(JPEG)
        assert media["sha256"] == hashlib.sha256(JPEG).hexdigest()
        assert media["mime_detected"] == "image/jpeg"
        assert media["scan_status"] == "clean"
        assert media["quarantine_key"] is None
        assert link["content_id"] == item["content_id"]
        assert link["media_id"] == media["media_id"]
        assert (env.media / media["object_key"]).is_file(), "字节必须落在数据根目录下的媒体目录"

    def test_two_messages_with_the_same_bytes_share_one_media_row(
        self, env: SimpleNamespace
    ) -> None:
        """§3.8 的多对多：内容项与媒体是两回事，一份字节只有一行 media。"""
        source = _source(env.conn, env.tmp)
        first = _write(env.tmp / "a.jpg", JPEG)
        second = _write(env.tmp / "b.jpg", JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(first,), msg_id="m1")
        _seed(env.conn, source_id=source["source_id"], refs=(second,), msg_id="m2")
        env.worker.run_once()

        assert env.conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 2
        assert env.conn.execute("SELECT count(*) FROM content_media").fetchone()[0] == 2
        assert env.conn.execute("SELECT count(*) FROM media").fetchone()[0] == 1, (
            "同一份字节只能占一份盘"
        )

    def test_multiple_refs_in_one_message(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        first = _write(env.tmp / "a.jpg", JPEG)
        second = _write(env.tmp / "b.png", PNG)
        _seed(env.conn, source_id=source["source_id"], refs=(first, second), text="两图一说明")
        assert env.worker.run_once().succeeded == 1
        items = [
            dict(row)
            for row in env.conn.execute("SELECT item_type, ordinal FROM content_item ORDER BY ordinal")
        ]
        assert [item["item_type"] for item in items] == ["text", "image", "image"]
        assert [item["ordinal"] for item in items] == [0, 1, 2]

    def test_backend_rows_are_created_and_used(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "cat.jpg", JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        env.worker.run_once()
        row = env.conn.execute(
            "SELECT sb.name FROM media m JOIN storage_backend sb "
            "ON sb.backend_id = m.storage_backend_id"
        ).fetchone()
        assert row[0] == "local"


class TestQuarantineAndReview:
    def test_rejected_type_is_quarantined_physically(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "tool.exe", EXE)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        assert env.worker.run_once().succeeded == 1

        item = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        media = dict(env.conn.execute("SELECT * FROM media").fetchone())
        backend = env.conn.execute(
            "SELECT name FROM storage_backend WHERE backend_id = ?", (media["storage_backend_id"],)
        ).fetchone()[0]
        assert item["status"] == "quarantined"
        assert media["scan_status"] == "suspicious"
        assert media["quarantine_key"] is not None
        assert backend == "local-quarantine"
        assert (env.quarantine / media["object_key"]).is_file()
        assert not (env.media / media["object_key"]).exists(), "隔离区的字节必须与主存储分离"

    def test_rejected_type_opens_a_review_task(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "tool.exe", EXE)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        env.worker.run_once()
        task = dict(env.conn.execute("SELECT * FROM review_task").fetchone())
        assert task["kind"] == "scan_gray"
        assert task["status"] == "pending"
        payload = json.loads(task["payload"])
        assert payload["passed"] is False
        assert payload["ref"].endswith("tool.exe")

    def test_extension_mismatch_stays_active_but_opens_a_review(
        self, env: SimpleNamespace
    ) -> None:
        """扩展名骗人时内容本身可用，所以放行——但要进复核，不能静默。"""
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "fake.png", JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        env.worker.run_once()
        item = dict(env.conn.execute("SELECT * FROM content_item").fetchone())
        assert item["status"] == "active"
        assert env.conn.execute("SELECT count(*) FROM review_task").fetchone()[0] == 1

    def test_clean_media_opens_no_review(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "cat.jpg", JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        env.worker.run_once()
        assert env.conn.execute("SELECT count(*) FROM review_task").fetchone()[0] == 0


class TestPermanentFailures:
    def test_missing_file_is_permanent(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _, job_id = _seed(
            env.conn, source_id=source["source_id"], refs=(str(env.tmp / "nope.jpg"),)
        )
        report = env.worker.run_once()
        assert report.dead == 1
        row = _job(env.conn, job_id)
        assert row["status"] == "dead"
        assert "不存在" in row["dead_reason"]

    def test_tombstoned_content_is_refused_and_nothing_is_stored(
        self, env: SimpleNamespace
    ) -> None:
        """硬删之后哈希墓地要能拦住重复导入，而且不该再把字节写回盘上。"""
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "cat.jpg", JPEG)
        digest = hashlib.sha256(JPEG).hexdigest()
        env.conn.execute(
            "INSERT INTO hash_tombstone (hash, alg, deleted_at) VALUES (?, 'sha256', ?)",
            (digest, db.now_iso()),
        )
        _, job_id = _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        assert env.worker.run_once().dead == 1
        assert "哈希墓地" in _job(env.conn, job_id)["dead_reason"]
        assert env.conn.execute("SELECT count(*) FROM media").fetchone()[0] == 0
        assert not env.media.exists() or not any(env.media.rglob("*.*"))

    def test_expired_tombstone_does_not_block(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "cat.jpg", JPEG)
        env.conn.execute(
            "INSERT INTO hash_tombstone (hash, alg, deleted_at, expires_at) "
            "VALUES (?, 'sha256', ?, ?)",
            (hashlib.sha256(JPEG).hexdigest(), db.now_iso(), "2000-01-01T00:00:00.000Z"),
        )
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        assert env.worker.run_once().succeeded == 1

    def test_oversized_media_is_permanent(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "big.jpg", JPEG * 100)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        env.worker._limits = IngestLimits(max_media_bytes=16)
        assert env.worker.run_once().dead == 1

    def test_revoked_authorization_is_permanent(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "cat.jpg", JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        env.conn.execute("UPDATE source_authorization SET revoked_at = ?", (db.now_iso(),))
        assert env.worker.run_once().dead == 1

    def test_missing_message_is_permanent(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _, job_id = _seed(env.conn, source_id=source["source_id"])
        env.conn.execute("DELETE FROM message")
        assert env.worker.run_once().dead == 1
        assert "不存在" in _job(env.conn, job_id)["dead_reason"]

    def test_unknown_job_type_is_permanent(self, env: SimpleNamespace) -> None:
        env.conn.execute(
            "INSERT INTO job (job_type, target_type, target_id, status, priority, attempts,"
            " max_attempts, run_after) VALUES ('mystery.job', 'message', 1, 'queued', 100, 0, 5, ?)",
            (db.now_iso(),),
        )
        assert env.worker.run_once().dead == 1


class TestRetryableFailures:
    def _boom(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        class BoomConnector:
            def authorize(self) -> bool:
                return True

            def capabilities(self) -> dict[str, Any]:
                return {"supports_media_fetch": True}

            def fetch_media(self, ref: str) -> bytes:
                raise exc

        monkeypatch.setattr(
            pipeline_module, "connector_for", lambda *args, **kwargs: BoomConnector()
        )

    def test_transient_error_schedules_a_backoff(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _source(env.conn, env.tmp)
        _, job_id = _seed(env.conn, source_id=source["source_id"], refs=("/tmp/x.jpg",))
        self._boom(monkeypatch, RuntimeError("网络抖动"))
        report = env.worker.run_once()
        assert report.failed == 1
        row = _job(env.conn, job_id)
        assert row["status"] == "failed"
        assert row["attempts"] == 1
        assert "网络抖动" in row["last_error"]
        assert row["run_after"] > db.now_iso(), "偶发失败要退避，不能立刻重试"
        assert row["finished_at"] is None

    def test_last_attempt_becomes_dead(
        self, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _source(env.conn, env.tmp)
        _, job_id = _seed(
            env.conn, source_id=source["source_id"], refs=("/tmp/x.jpg",), attempts=4, max_attempts=5
        )
        self._boom(monkeypatch, RuntimeError("又抖了"))
        assert env.worker.run_once().dead == 1
        row = _job(env.conn, job_id)
        assert row["status"] == "dead"
        assert "连续 5 次失败" in row["dead_reason"]


class TestIdempotencyAndAudit:
    def test_reprocessing_the_same_message_creates_no_second_item(
        self, env: SimpleNamespace
    ) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "cat.jpg", JPEG)
        _, job_id = _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        env.worker.run_once()
        # 模拟崩溃恢复：任务被重置为排队，于是又被处理一次
        env.conn.execute(
            "UPDATE job SET status = 'queued', attempts = 0, locked_by = NULL WHERE job_id = ?",
            (job_id,),
        )
        env.worker.run_once()
        assert env.conn.execute("SELECT count(*) FROM content_item").fetchone()[0] == 1
        assert env.conn.execute("SELECT count(*) FROM media").fetchone()[0] == 1
        assert _job(env.conn, job_id)["status"] == "succeeded"

    def test_audit_row_is_written_per_job(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        ref = _write(env.tmp / "cat.jpg", JPEG)
        _seed(env.conn, source_id=source["source_id"], refs=(ref,))
        env.worker.run_once()
        row = dict(env.conn.execute("SELECT * FROM audit_log").fetchone())
        assert row["actor_type"] == "system"
        assert row["action"] == "ingest.process"
        assert row["target_type"] == "message"
        assert row["result"] == "succeeded"

    def test_report_notes_carry_every_outcome(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        _seed(env.conn, source_id=source["source_id"], text="hi")
        report = env.worker.run_once()
        assert report.notes[-1][1] == "succeeded"
        assert "内容项 1 个" in report.notes[-1][2]

    def test_limit_stops_early(self, env: SimpleNamespace) -> None:
        source = _source(env.conn, env.tmp)
        for index in range(3):
            _seed(env.conn, source_id=source["source_id"], text=f"t{index}", msg_id=f"m{index}")
        report = env.worker.run_once(limit=2)
        assert report.claimed == 2
        assert env.conn.execute("SELECT count(*) FROM job WHERE status = 'succeeded'").fetchone()[0] == 2


class TestPermanentErrorType:
    def test_permanent_error_is_a_runtime_error(self) -> None:
        assert issubclass(PermanentError, RuntimeError)

    def test_media_too_large_is_permanent_by_construction(self) -> None:
        error = MediaTooLargeError("/tmp/big.jpg", 2048, 1024)
        assert error.size_bytes == 2048
        assert "上限" in str(error)

    def test_ref_name_helper_handles_urls_and_paths(self) -> None:
        assert pipeline_module._ref_name("/a/b/c.jpg") == "c.jpg"
        assert pipeline_module._ref_name("https://x.test/y/z.png?sig=1") == "z.png"
        assert pipeline_module._ref_name("C:\\a\\b\\d.gif") == "d.gif"
