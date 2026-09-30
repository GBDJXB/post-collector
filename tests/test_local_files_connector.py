"""本地文件连接器：发现顺序、游标分批、侧车文本、授权与取字节。

这个连接器是"把内容弄进库"的第一段路，它出错的方式都很安静：少发现一个文件、
把两张图的路径弄成同一个 ``platform_msg_id``、把 .txt 当成待扫描的媒体。所以这里
既测"看到了什么"，也测"生成的信封长什么样"。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from pc.adapters.local_files import (
    CONTENT_ID_PREFIX,
    IDENTITY_CONTENT,
    IDENTITY_PATH,
    LocalFilesConnector,
)
from pc.domain import MediaTooLargeError, normalize_text

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"j" * 64
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"p" * 64
GIF = b"GIF89a" + b"\x10\x00\x10\x00" + b"g" * 64


def _source(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_id": 7,
        "uuid": "u",
        "name": "samples",
        "platform": "local",
        "kind": "export",
        "config": "{}",
        "enabled": 1,
        "deleted_at": None,
    }
    base.update(overrides)
    return base


def _authz(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "authz_id": 1,
        "source_id": 7,
        "mode": "manual_export",
        "evidence_ref": None,
        "revoked_at": None,
        "expires_at": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    root = tmp_path / "samples"
    root.mkdir()
    (root / "cat.jpg").write_bytes(JPEG)
    # 用 write_bytes 而不是 write_text：后者在 Windows 上会把 \n 翻译成 \r\n，
    # 而"原文长什么样"正是这里要断言的东西
    (root / "cat.txt").write_bytes("一只猫".encode())
    (root / "dog.png").write_bytes(PNG)
    (root / "notes.md").write_bytes("# 笔记\n正文".encode())
    (root / ".hidden.jpg").write_bytes(JPEG)
    nested = root / "sub"
    nested.mkdir()
    (nested / "bird.gif").write_bytes(GIF)
    return root


def _connector(root: Path, **kwargs: Any) -> LocalFilesConnector:
    return LocalFilesConnector(
        _source(),
        [_authz()],
        roots=[root],
        **kwargs,
    )


class TestDiscovery:
    def test_finds_files_in_stable_order(self, samples: Path) -> None:
        names = [item.relative for item in _connector(samples).discover()]
        assert names == ["cat.jpg", "cat.txt", "dog.png", "notes.md", "sub/bird.gif"]

    def test_skips_hidden_files_and_dirs(self, samples: Path) -> None:
        found = {item.relative for item in _connector(samples).discover()}
        assert ".hidden.jpg" not in found

    def test_missing_root_is_not_an_error(self, tmp_path: Path) -> None:
        connector = _connector(tmp_path / "does-not-exist")
        assert connector.discover() == []

    def test_symlinks_are_skipped(self, samples: Path, tmp_path: Path) -> None:
        link = samples / "link.jpg"
        try:
            os.symlink(samples / "cat.jpg", link)
        except (OSError, NotImplementedError):
            pytest.skip("本机不允许创建符号链接（Windows 需要开发者模式）")
        found = {item.relative for item in _connector(samples).discover()}
        assert "link.jpg" not in found, "符号链接可能指到来源目录之外，一律跳过"


class TestListNew:
    def test_batches_follow_the_cursor(self, samples: Path) -> None:
        connector = _connector(samples, batch_size=2)
        first, cursor = connector.list_new(None)
        assert len(first) == 2
        assert cursor == "2"
        second, cursor = connector.list_new(cursor)
        assert len(second) == 2
        assert cursor == "4"
        third, cursor = connector.list_new(cursor)
        assert len(third) == 1
        assert cursor == "5"
        # 没有新的了：空列表 + 原样带回的游标，调用方据此停止
        empty, cursor = connector.list_new(cursor)
        assert empty == ()
        assert cursor == "5"

    def test_garbage_cursor_restarts_from_zero(self, samples: Path) -> None:
        messages, cursor = _connector(samples).list_new("not-a-number")
        assert len(messages) == 5
        assert cursor == "5"

    def test_message_ids_are_root_label_plus_relative_path(self, samples: Path) -> None:
        messages, _ = _connector(samples).list_new(None)
        assert [m.platform_msg_id for m in messages] == [
            "samples/cat.jpg",
            "samples/cat.txt",
            "samples/dog.png",
            "samples/notes.md",
            "samples/sub/bird.gif",
        ]

    def test_source_id_is_propagated(self, samples: Path) -> None:
        messages, _ = _connector(samples).list_new(None)
        assert {m.source_id for m in messages} == {7}

    def test_two_roots_with_the_same_name_get_distinct_labels(self, tmp_path: Path) -> None:
        first = tmp_path / "a" / "dump"
        second = tmp_path / "b" / "dump"
        for root, payload in ((first, JPEG), (second, PNG)):
            root.mkdir(parents=True)
            (root / "pic.bin").write_bytes(payload)
        connector = LocalFilesConnector(_source(), [_authz()], roots=[first, second])
        messages, _ = connector.list_new(None)
        ids = [m.platform_msg_id for m in messages]
        assert len(set(ids)) == 2, f"重名根目录必须被区分开，否则幂等键会撞车：{ids}"


class TestEnvelopeShape:
    def test_media_with_sidecar_becomes_a_mixed_message(self, samples: Path) -> None:
        messages, _ = _connector(samples).list_new(None)
        message = messages[0]
        assert message.message_type == "mixed"
        assert message.text_raw == "一只猫"
        assert len(message.media_refs) == 1
        assert message.media_refs[0].endswith("cat.jpg")

    def test_image_without_sidecar_is_a_media_message(self, samples: Path) -> None:
        messages, _ = _connector(samples).list_new(None)
        dog = next(m for m in messages if m.platform_msg_id.endswith("dog.png"))
        assert dog.message_type == "media"
        assert dog.text_raw is None

    def test_text_file_is_a_text_message_without_media(self, samples: Path) -> None:
        messages, _ = _connector(samples).list_new(None)
        notes = next(m for m in messages if m.platform_msg_id.endswith("notes.md"))
        assert notes.message_type == "text"
        assert notes.media_refs == ()
        assert notes.text_raw == "# 笔记\n正文"

    def test_crlf_is_preserved_in_text_raw(self, tmp_path: Path) -> None:
        """``text_raw`` 是**不可变原文**：换行原样保留，规范化是后面的步骤（DESIGN §4.2）。"""
        root = tmp_path / "crlf"
        root.mkdir()
        (root / "win.txt").write_bytes("第一行\r\n第二行\r\n".encode())
        messages, _ = _connector(root).list_new(None)
        assert messages[0].text_raw == "第一行\r\n第二行\r\n"

    def test_sent_at_is_iso_utc_and_documented_as_mtime(self, samples: Path) -> None:
        messages, _ = _connector(samples).list_new(None)
        message = messages[0]
        assert message.sent_at is not None and message.sent_at.endswith("Z")
        assert "mtime" in message.raw_payload["note"], "必须写明 sent_at 不是真实发布时间"

    def test_raw_payload_carries_provenance(self, samples: Path) -> None:
        messages, _ = _connector(samples).list_new(None)
        payload = messages[0].raw_payload
        assert payload["kind"] == "local_file"
        assert payload["path"] == "samples/cat.jpg"
        assert payload["size_bytes"] == len(JPEG)


class TestCapabilitiesAndAuth:
    def test_capabilities_declare_the_route(self, samples: Path) -> None:
        caps = _connector(samples).capabilities()
        assert caps["supports_pull"] is True
        assert caps["supports_push"] is False
        assert caps["supports_media_fetch"] is True
        assert caps["max_attachment_bytes"] > 0
        assert caps["roots"]

    def test_authorized_when_a_live_authorization_exists(self, samples: Path) -> None:
        assert _connector(samples).authorize() is True

    def test_no_authorization_row_means_not_authorized(self, samples: Path) -> None:
        """三者齐备是硬要求：没有授权记录就不许采集（DESIGN §1.3）。"""
        connector = LocalFilesConnector(_source(), [], roots=[samples])
        assert connector.authorize() is False

    def test_revoked_authorization_is_refused(self, samples: Path) -> None:
        connector = LocalFilesConnector(
            _source(), [_authz(revoked_at="2026-01-01T00:00:00.000Z")], roots=[samples]
        )
        assert connector.authorize() is False

    def test_expired_authorization_is_refused(self, samples: Path) -> None:
        connector = LocalFilesConnector(
            _source(), [_authz(expires_at="2000-01-01T00:00:00.000Z")], roots=[samples]
        )
        assert connector.authorize() is False

    def test_disabled_source_is_refused(self, samples: Path) -> None:
        connector = LocalFilesConnector(_source(enabled=0), [_authz()], roots=[samples])
        assert connector.authorize() is False

    def test_soft_deleted_source_is_refused(self, samples: Path) -> None:
        connector = LocalFilesConnector(
            _source(deleted_at="2026-01-01T00:00:00.000Z"), [_authz()], roots=[samples]
        )
        assert connector.authorize() is False

    def test_rate_limit_hint_is_explicit_about_having_none(self, samples: Path) -> None:
        hint = _connector(samples).rate_limit_hint()
        assert hint["tokens_per_second"] is None
        assert "没有速率限制" in hint["note"]


class TestFetchMedia:
    def test_reads_the_file(self, samples: Path) -> None:
        connector = _connector(samples)
        assert connector.fetch_media(str(samples / "cat.jpg")) == JPEG

    def test_missing_file_raises_keyerror(self, samples: Path) -> None:
        with pytest.raises(KeyError):
            _connector(samples).fetch_media(str(samples / "nope.jpg"))

    def test_oversized_file_raises_before_reading(self, samples: Path) -> None:
        connector = _connector(samples, max_media_bytes=8)
        with pytest.raises(MediaTooLargeError) as excinfo:
            connector.fetch_media(str(samples / "cat.jpg"))
        assert excinfo.value.limit_bytes == 8
        assert excinfo.value.size_bytes == len(JPEG)
        assert "上限" in str(excinfo.value)

    def test_push_route_is_explicitly_unsupported(self, samples: Path) -> None:
        with pytest.raises(NotImplementedError, match="拉取型"):
            _connector(samples).parse_event({"anything": True})


class TestTextLimits:
    def test_long_text_is_truncated(self, tmp_path: Path) -> None:
        root = tmp_path / "text"
        root.mkdir()
        (root / "long.txt").write_text("x" * 5000, encoding="utf-8")
        connector = _connector(root, max_text_chars=100)
        messages, _ = connector.list_new(None)
        assert len(messages[0].text_raw or "") == 100

    def test_binary_sidecar_does_not_crash(self, tmp_path: Path) -> None:
        """侧车文本是坏字节时用替换字符兜住，不能因为编码问题让整批导入失败。"""
        root = tmp_path / "mixed"
        root.mkdir()
        (root / "pic.jpg").write_bytes(JPEG)
        (root / "pic.txt").write_bytes(b"\xff\xfe\x00bad")
        messages, _ = _connector(root).list_new(None)
        assert messages[0].message_type == "mixed"
        assert messages[0].text_raw


def test_requires_at_least_one_root() -> None:
    with pytest.raises(ValueError, match="至少要有一个目录"):
        LocalFilesConnector(_source(), [_authz()], roots=[])


# ---------------------------------------------------------------------------
# 内容身份（pc upload 用的策略）
#
# 手工上传没有平台给的 ID，身份只能由内容决定。这里要钉住的恰恰是"安静出错"的几种：
# 重传同一张图变成两条、同一张图换个配文被当成同一条、空文件互相顶掉。
# ---------------------------------------------------------------------------


def _upload_dir(tmp_path: Path, name: str = "upload") -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _content_connector(root: Path, **kwargs: Any) -> LocalFilesConnector:
    return LocalFilesConnector(
        _source(kind="manual"),
        [_authz(mode="root_self")],
        roots=[root],
        identity=IDENTITY_CONTENT,
        **kwargs,
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestContentIdentity:
    def test_media_identity_is_the_byte_hash(self, tmp_path: Path) -> None:
        root = _upload_dir(tmp_path)
        (root / "whatever-name.jpg").write_bytes(JPEG)
        messages, _ = _content_connector(root).list_new(None)
        assert messages[0].platform_msg_id == f"{CONTENT_ID_PREFIX}{_sha(JPEG)}"

    def test_text_identity_uses_the_same_rule_as_content_hash(self, tmp_path: Path) -> None:
        """文本用 ``sha256(normalize_text(原文))``——与 ``content_item.content_hash`` 同一口径。"""
        root = _upload_dir(tmp_path)
        (root / "note.txt").write_bytes("一只猫".encode())
        messages, _ = _content_connector(root).list_new(None)
        expected = _sha(normalize_text("一只猫").encode("utf-8"))
        assert messages[0].platform_msg_id == f"{CONTENT_ID_PREFIX}{expected}"

    def test_media_with_sidecar_uses_both_components(self, tmp_path: Path) -> None:
        root = _upload_dir(tmp_path)
        (root / "cat.jpg").write_bytes(JPEG)
        (root / "cat.txt").write_bytes("一只猫".encode())
        messages, _ = _content_connector(root).list_new(None)
        text_part = _sha(normalize_text("一只猫").encode("utf-8"))
        assert messages[0].message_type == "mixed"
        assert messages[0].platform_msg_id == f"{CONTENT_ID_PREFIX}{_sha(JPEG)}:{text_part}"

    def test_reuploading_the_same_bytes_is_idempotent(self, tmp_path: Path) -> None:
        """换个文件名、换个目录都还是同一条：身份只认内容。"""
        first = _upload_dir(tmp_path, "a")
        second = _upload_dir(tmp_path, "b")
        (first / "cat.jpg").write_bytes(JPEG)
        (second / "renamed-copy.jpg").write_bytes(JPEG)
        id_one = _content_connector(first).list_new(None)[0][0].platform_msg_id
        id_two = _content_connector(second).list_new(None)[0][0].platform_msg_id
        assert id_one == id_two

    def test_same_media_with_a_different_caption_is_a_different_message(self, tmp_path: Path) -> None:
        """同一张图配不同文字是**两个条目**——那是两次不同的上传，不该互相顶掉。"""
        first = _upload_dir(tmp_path, "a")
        second = _upload_dir(tmp_path, "b")
        for root, caption in ((first, "一只猫"), (second, "其实是两只")):
            (root / "cat.jpg").write_bytes(JPEG)
            (root / "cat.txt").write_bytes(caption.encode())
        id_one = _content_connector(first).list_new(None)[0][0].platform_msg_id
        id_two = _content_connector(second).list_new(None)[0][0].platform_msg_id
        assert id_one != id_two

    def test_whitespace_only_text_files_share_one_identity(self, tmp_path: Path) -> None:
        """只差空白的文本文件算同一条：规范化之后它们本来就是同一份内容。"""
        first = _upload_dir(tmp_path, "a")
        second = _upload_dir(tmp_path, "b")
        (first / "one.txt").write_bytes("一只猫".encode())
        (second / "two.txt").write_bytes("  一只猫  \n".encode())
        id_one = _content_connector(first).list_new(None)[0][0].platform_msg_id
        id_two = _content_connector(second).list_new(None)[0][0].platform_msg_id
        assert id_one == id_two

    def test_sent_at_is_empty(self, tmp_path: Path) -> None:
        """文件 mtime 是下载/复制/解压的时间，拿它当发布时间是假数据。"""
        root = _upload_dir(tmp_path)
        (root / "cat.jpg").write_bytes(JPEG)
        messages, _ = _content_connector(root).list_new(None)
        assert messages[0].sent_at is None
        assert messages[0].raw_payload is not None
        assert "mtime" in messages[0].raw_payload, "时间线索仍要留档，只是不冒充发布时间"

    def test_path_identity_still_uses_mtime(self, samples: Path) -> None:
        messages, _ = _connector(samples).list_new(None)
        assert all(message.sent_at for message in messages)

    def test_images_with_the_same_bytes_keep_one_identity_across_names(self, tmp_path: Path) -> None:
        root = _upload_dir(tmp_path)
        (root / "one.jpg").write_bytes(JPEG)
        (root / "two.jpg").write_bytes(JPEG)
        messages, _ = _content_connector(root).list_new(None)
        assert len({message.platform_msg_id for message in messages}) == 1, (
            "两份字节相同的文件必须得到同一个身份，否则重传就会变成两条"
        )


class TestContentIdentitySkips:
    def test_empty_file_is_skipped_and_reported(self, tmp_path: Path) -> None:
        """空文件没有可用的身份组件；所有空文件的哈希相同，用它当身份会互相顶掉。"""
        root = _upload_dir(tmp_path)
        (root / "empty.txt").write_bytes(b"")
        connector = _content_connector(root)
        messages, _ = connector.list_new(None)
        assert messages == ()
        assert connector.skipped and connector.skipped[0][0].endswith("empty.txt")
        assert "0 字节" in connector.skipped[0][1]

    def test_whitespace_only_text_is_skipped(self, tmp_path: Path) -> None:
        root = _upload_dir(tmp_path)
        (root / "blank.txt").write_bytes(b"   \n\t ")
        connector = _content_connector(root)
        assert connector.list_new(None)[0] == ()

    def test_empty_media_is_skipped_and_its_sidecar_survives(self, tmp_path: Path) -> None:
        """0 字节的图不产生消息；同名侧车文本会被单独发现，所以内容没有丢。

        这条同时钉住一个不显眼的坑：如果让空媒体"退化成纯文本身份"，那张图的身份就会与
        侧车文本文件**撞成同一个**，于是其中一条被唯一约束静默吞掉（顺序还不确定）。
        """
        root = _upload_dir(tmp_path)
        (root / "broken.jpg").write_bytes(b"")
        (root / "broken.txt").write_bytes("正文还在".encode())
        connector = _content_connector(root)
        messages, _ = connector.list_new(None)
        assert len(messages) == 1
        assert messages[0].message_type == "text"
        assert messages[0].text_raw == "正文还在"
        expected = _sha(normalize_text("正文还在").encode("utf-8"))
        assert messages[0].platform_msg_id == f"{CONTENT_ID_PREFIX}{expected}"
        assert connector.skipped and "0 字节" in connector.skipped[0][1]

    def test_path_identity_does_not_skip_empty_files(self, tmp_path: Path) -> None:
        """导出目录通道的行为不变：空文件照样入库，由扫描器按魔数判定。"""
        root = _upload_dir(tmp_path)
        (root / "empty.jpg").write_bytes(b"")
        messages, _ = _connector(root).list_new(None)
        assert len(messages) == 1

    def test_skips_are_deduplicated_across_batches(self, tmp_path: Path) -> None:
        """``list_new`` 是按批调用的，每批都会重扫目录——报告不能随批次数翻倍。"""
        root = _upload_dir(tmp_path)
        (root / "empty-a.txt").write_bytes(b"")
        (root / "empty-b.txt").write_bytes(b"")
        (root / "cat.jpg").write_bytes(JPEG)
        connector = _content_connector(root, batch_size=1)
        cursor = None
        while True:
            _messages, cursor = connector.list_new(cursor)
            if cursor in (None, "3"):
                break
        assert [item[0].split("/")[-1] for item in connector.skipped] == [
            "empty-a.txt",
            "empty-b.txt",
        ]


class TestContentIdentityMechanics:
    def test_unknown_identity_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="未知的身份策略"):
            LocalFilesConnector(_source(), [_authz()], roots=[tmp_path], identity="vibes")

    def test_hashes_are_cached_across_list_new_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``list_new`` 每批都重扫目录：没有缓存的话一个大目录会被哈希成 O(n²)。"""
        root = _upload_dir(tmp_path)
        (root / "cat.jpg").write_bytes(JPEG)
        calls = {"n": 0}
        real = hashlib.sha256

        def counting(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(hashlib, "sha256", counting)
        connector = _content_connector(root)
        connector.list_new(None)
        first = calls["n"]
        connector.list_new(None)
        assert calls["n"] == first, "第二次 list_new 又读了一遍字节——缓存没生效"

    def test_capabilities_declare_content_identity_without_credentials(self, tmp_path: Path) -> None:
        caps = _content_connector(tmp_path).capabilities()
        assert caps["identity"] == IDENTITY_CONTENT
        assert caps["requires_credential"] is False
        assert any("内容为空" in rule for rule in caps["skip_rules"])

    def test_path_identity_is_the_default(self, samples: Path) -> None:
        caps = _connector(samples).capabilities()
        assert caps["identity"] == IDENTITY_PATH
        assert caps["requires_credential"] is False
        assert not any("内容为空" in rule for rule in caps["skip_rules"])
