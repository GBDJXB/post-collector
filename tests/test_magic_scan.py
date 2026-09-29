"""魔数嗅探扫描器：白名单、拒绝清单、上限与 fail closed。

这是"内容进库"这条路上唯一的安全闸门，所以每个拒绝分支都要有测试——**放过一个
不该放过的类型，比拒掉一个正常图片严重得多**。反过来，"拒绝"也必须给出人能读懂
的理由：内容被隔离之后，人要知道为什么。
"""

from __future__ import annotations

import pytest

from pc.adapters.magic_scan import (
    MagicNumberScanner,
    declared_mime_for_name,
    item_type_for,
    sniff_mime,
)
from pc.config import IngestLimits, SecurityPolicy

# 真实文件头（后面跟点填充，免得看起来像被截断的样本）
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"p" * 32
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"p" * 32
GIF = b"GIF89a" + b"\x10\x00\x10\x00" + b"p" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"p" * 32
MP4 = b"\x00\x00\x00\x20ftypmp42\x00\x00\x00\x00" + b"p" * 32
WEBM = b"\x1a\x45\xdf\xa3" + b"p" * 32
AVIF = b"\x00\x00\x00\x20ftypavif\x00\x00\x00\x00" + b"p" * 32
HEIC = b"\x00\x00\x00\x20ftypheic\x00\x00\x00\x00" + b"p" * 32

ZIP = b"PK\x03\x04\x14\x00\x00\x00" + b"p" * 32
SEVENZ = b"7z\xbc\xaf\x27\x1c\x00\x04" + b"p" * 32
RAR = b"RAR!\x1a\x07\x01\x00" + b"p" * 32
GZIP = b"\x1f\x8b\x08\x00" + b"p" * 32
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"p" * 32
ELF = b"\x7fELF\x02\x01\x01\x00" + b"p" * 32
PE = b"MZ\x90\x00\x03\x00\x00\x00" + b"p" * 32
SCRIPT = b"#!/bin/sh\necho hello\n"
SVG = b'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
SVG_WITH_BOM = b"\xef\xbb\xbf" + SVG
UNKNOWN = bytes(range(1, 40))


@pytest.fixture
def scanner() -> MagicNumberScanner:
    return MagicNumberScanner()


class TestSniffMime:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (JPEG, "image/jpeg"),
            (PNG, "image/png"),
            (GIF, "image/gif"),
            (WEBP, "image/webp"),
            (MP4, "video/mp4"),
            (WEBM, "video/webm"),
            (AVIF, "image/avif"),
            (HEIC, "image/heic"),
            (PDF, "application/pdf"),
            (ZIP, "application/zip"),
            (ELF, "application/x-elf"),
            (PE, "application/x-dosexec"),
            (GZIP, "application/gzip"),
            (RAR, "application/vnd.rar"),
            (SEVENZ, "application/x-7z-compressed"),
            (SCRIPT, "text/x-shellscript"),
            (SVG, "image/svg+xml"),
        ],
    )
    def test_known_signatures(self, data: bytes, expected: str) -> None:
        assert sniff_mime(data) == expected

    def test_svg_with_bom_is_detected(self) -> None:
        assert sniff_mime(SVG_WITH_BOM) == "image/svg+xml"

    def test_unknown_bytes_have_no_mime(self) -> None:
        assert sniff_mime(UNKNOWN) is None
        assert sniff_mime(b"") is None

    def test_riff_without_webp_is_not_webp(self) -> None:
        assert sniff_mime(b"RIFF\x24\x00\x00\x00WAVEfmt ") is None

    def test_ftyp_with_unknown_brand_falls_back_to_mp4(self) -> None:
        assert sniff_mime(b"\x00\x00\x00\x20ftypqqqq") == "video/mp4"


class TestAcceptedTypes:
    @pytest.mark.parametrize(
        ("data", "expected_mime"),
        [(JPEG, "image/jpeg"), (PNG, "image/png"), (GIF, "image/gif"), (WEBP, "image/webp"), (MP4, "video/mp4")],
    )
    def test_media_passes(
        self, scanner: MagicNumberScanner, data: bytes, expected_mime: str
    ) -> None:
        verdict = scanner.scan(data)
        assert verdict.passed is True
        assert verdict.gray is False
        assert verdict.details["mime_detected"] == expected_mime
        assert verdict.details["category"] == "media"
        assert verdict.reasons == ()

    def test_engine_is_named(self, scanner: MagicNumberScanner) -> None:
        assert scanner.scan(JPEG).engine == "magic"


class TestRejections:
    @pytest.mark.parametrize(
        ("data", "needle"),
        [
            (ZIP, "压缩包容器"),
            (SEVENZ, "压缩包容器"),
            (RAR, "压缩包容器"),
            (GZIP, "压缩炸弹"),
            (PDF, "隔离渲染"),
            (ELF, "可执行文件"),
            (PE, "可执行文件"),
            (SCRIPT, "脚本"),
            (SVG, "SVG 是不可信 XML"),
        ],
    )
    def test_rejected_with_a_human_reason(
        self, scanner: MagicNumberScanner, data: bytes, needle: str
    ) -> None:
        verdict = scanner.scan(data)
        assert verdict.passed is False
        assert any(needle in reason for reason in verdict.reasons), verdict.reasons
        assert verdict.details["category"] in {"rejected", "svg"}

    def test_unknown_type_fails_closed(self, scanner: MagicNumberScanner) -> None:
        verdict = scanner.scan(UNKNOWN)
        assert verdict.passed is False
        assert verdict.gray is False, "未知类型是不放行，不是'可疑待定'"
        assert "无法识别" in verdict.reasons[0]
        assert verdict.details["mime_detected"] is None

    def test_empty_file_is_not_passed(self, scanner: MagicNumberScanner) -> None:
        verdict = scanner.scan(b"")
        assert verdict.passed is False
        assert verdict.gray is True
        assert "空文件" in verdict.reasons[0]

    def test_oversized_payload_is_refused_before_anything_else(self) -> None:
        scanner = MagicNumberScanner(limits=IngestLimits(max_media_bytes=1024))
        verdict = scanner.scan(JPEG + b"x" * 2048)
        assert verdict.passed is False
        assert verdict.details["category"] == "too_large"
        assert verdict.details["limit_bytes"] == 1024
        assert "超过单文件上限" in verdict.reasons[0]


class TestDeclaredTypeMismatch:
    def test_matching_declaration_is_not_gray(self, scanner: MagicNumberScanner) -> None:
        verdict = scanner.scan(JPEG, declared_mime="image/jpeg")
        assert verdict.passed is True
        assert verdict.gray is False

    def test_mismatch_is_gray_but_not_rejected(self, scanner: MagicNumberScanner) -> None:
        """扩展名骗人时内容仍然可用，所以放行——但要进复核队列，不能静默。"""
        verdict = scanner.scan(JPEG, declared_mime="image/png")
        assert verdict.passed is True
        assert verdict.gray is True
        assert any("不一致" in reason for reason in verdict.reasons)

    def test_mismatch_check_can_be_switched_off(self) -> None:
        scanner = MagicNumberScanner(policy=SecurityPolicy(sniff_mime_by_magic=False))
        verdict = scanner.scan(JPEG, declared_mime="image/png")
        assert verdict.gray is False


class TestSvgPolicy:
    def test_svg_is_rejected_by_default(self, scanner: MagicNumberScanner) -> None:
        assert scanner.policy.allow_svg is False
        assert scanner.scan(SVG).passed is False

    def test_allowing_svg_still_marks_it_suspicious(self) -> None:
        scanner = MagicNumberScanner(policy=SecurityPolicy(allow_svg=True))
        verdict = scanner.scan(SVG)
        assert verdict.passed is True, "显式打开开关后应当放行"
        assert verdict.gray is True, "放行不等于可信：仍需人工复核"
        assert verdict.details["category"] == "svg"


class TestHelpers:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("cat.jpg", "image/jpeg"),
            ("cat.JPEG", "image/jpeg"),
            ("cat.png", "image/png"),
            ("cat.mp4", "video/mp4"),
            ("cat.unknown-ext", None),
        ],
    )
    def test_declared_mime_for_name(self, name: str, expected: str | None) -> None:
        assert declared_mime_for_name(name) == expected

    @pytest.mark.parametrize(
        ("mime", "expected"),
        [
            ("image/jpeg", "image"),
            ("image/gif", "image"),
            ("video/mp4", "video"),
            ("audio/mpeg", "audio"),
            (None, "file"),
            ("application/zip", "file"),
        ],
    )
    def test_item_type_for(self, mime: str | None, expected: str) -> None:
        assert item_type_for(mime) == expected
