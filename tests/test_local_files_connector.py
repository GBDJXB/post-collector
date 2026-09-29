"""本地文件连接器：发现顺序、游标分批、侧车文本、授权与取字节。

这个连接器是"把内容弄进库"的第一段路，它出错的方式都很安静：少发现一个文件、
把两张图的路径弄成同一个 ``platform_msg_id``、把 .txt 当成待扫描的媒体。所以这里
既测"看到了什么"，也测"生成的信封长什么样"。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from pc.adapters.local_files import LocalFilesConnector
from pc.domain import MediaTooLargeError

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
