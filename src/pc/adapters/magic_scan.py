"""魔数嗅探扫描器（DESIGN §6.5、§9.1：``Scanner`` 的确定性实现）。

这一版只做**不依赖任何第三方库、也不解码**的那一半安全工作，但它已经能挡住最常见的
几类投毒：

* **以魔数为准**，扩展名与声明的 Content-Type 只作参考——``.jpg`` 里装的是 zip 或
  ELF，照样按真实类型处置；
* **白名单放行**：只有图像（jpeg/png/gif/webp/avif/heif）与视频容器（mp4/webm）
  算"受支持的媒体"，压缩包、可执行文件、PDF、SVG、脚本一律拒绝；
* **未知一律不放行**（fail closed）：嗅不出类型就是不放行，交给人工复核。

刻意**不做**（随媒体管线加入，见 DESIGN §6.5）：病毒扫描（ClamAV）、解码隔离与
像素上限（``ingest.max_image_pixels`` 需要真正解码，本版还没有解码器）、SVG/PDF 的
隔离渲染。这些的缺失在 ``ScanVerdict.reasons`` 里不会伪装成"已通过"——本版对 SVG
与 PDF 的态度就是拒绝。

**它不判断内容是否合规**（NSFW 等），那是分类器的事；扫描器的输出只有"能不能安全
落盘"这一个问题。
"""

from __future__ import annotations

import mimetypes
from typing import Any

from pc.config import IngestLimits, SecurityPolicy
from pc.domain import ScanVerdict

#: 受支持的媒体类型 → 内容项类型（``content_item.item_type``）
MIME_TO_ITEM_TYPE: dict[str, str] = {
    "image/jpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "image/webp": "image",
    "image/avif": "image",
    "image/heic": "image",
    "video/mp4": "video",
    "video/webm": "video",
}

#: 明确拒绝的类型（魔数命中即拒），附人话理由
_REJECTED_BY_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"PK\x03\x04", "application/zip", "压缩包容器（zip / docx / apk 等，可藏嵌套炸弹）"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed", "压缩包容器"),
    (b"Rar!\x1a\x07", "application/vnd.rar", "压缩包容器"),
    (b"\x1f\x8b", "application/gzip", "压缩文件（可藏压缩炸弹）"),
    (b"%PDF", "application/pdf", "PDF 文档（必须走隔离渲染，本阶段不接受）"),
    (b"\x7fELF", "application/x-elf", "可执行文件"),
    (b"MZ", "application/x-dosexec", "Windows 可执行文件"),
    (b"#!", "text/x-shellscript", "脚本文件"),
)

#: ftyp 盒子的 brand → 类型（ISO-BMFF 家族：avif/heic/mp4 都长这样）
_FTYP_BRANDS: dict[bytes, str] = {
    b"avif": "image/avif",
    b"avis": "image/avif",
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"hevc": "image/heic",
    b"heim": "image/heic",
    b"heis": "image/heic",
    b"mif1": "image/heic",
    b"msf1": "image/heic",
    b"mp41": "video/mp4",
    b"mp42": "video/mp4",
    b"isom": "video/mp4",
    b"iso2": "video/mp4",
    b"avc1": "video/mp4",
    b"dash": "video/mp4",
}

#: SVG 的特征（视为不可信 XML，DESIGN §6.5）
_SVG_MARKERS = (b"<svg", b"<?xml")


def sniff_mime(data: bytes) -> str | None:
    """按魔数判断类型；判断不出时返回 ``None``（调用方按 fail closed 处理）。

    只看文件头，不解析结构：它要回答的是"这究竟是不是我以为的那种文件"，
    而不是"这个文件是否完整"。
    """
    head = data[:64]
    for magic, mime, _ in _REJECTED_BY_MAGIC:
        if head.startswith(magic):
            return mime
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12])
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if _looks_like_svg(data):
        return "image/svg+xml"
    return None


def _looks_like_svg(data: bytes) -> bool:
    """SVG 是文本，没有固定魔数：看头部里有没有 svg 标记（容忍 BOM 与前导空白）。"""
    sample = data[:1024].lstrip(b"\xef\xbb\xbf").lstrip()
    lowered = sample.lower()
    return any(marker in lowered for marker in _SVG_MARKERS) and b"<svg" in lowered


def declared_mime_for_name(name: str) -> str | None:
    """按文件名猜"声明的"类型，用于与魔数做一致性比对。

    刻意用标准库的 ``mimetypes`` 而不是自己维护一张扩展名表：这张表只需要覆盖
    "常见的骗人扩展名"，猜错也没什么损失——**魔数才是判据**。
    """
    guessed, _ = mimetypes.guess_type(name, strict=False)
    if guessed:
        return guessed
    return None


def item_type_for(mime: str | None) -> str:
    """媒体类型 → ``content_item.item_type``；未知类型按 ``file`` 记。"""
    if mime is None:
        return "file"
    if mime in MIME_TO_ITEM_TYPE:
        return MIME_TO_ITEM_TYPE[mime]
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


class MagicNumberScanner:
    """``Scanner`` 的确定性实现：魔数嗅探 + 白名单 + 字节上限 + fail closed。

    构造时注入 :class:`~pc.config.SecurityPolicy` 与 :class:`~pc.config.IngestLimits`，
    所以"允许 SVG""扫描失败是否放行"这类决定来自配置而不是散落在代码里的字面量。
    """

    engine = "magic"

    def __init__(
        self,
        *,
        policy: SecurityPolicy | None = None,
        limits: IngestLimits | None = None,
    ) -> None:
        self._policy = policy or SecurityPolicy()
        self._limits = limits or IngestLimits()

    @property
    def policy(self) -> SecurityPolicy:
        return self._policy

    @property
    def limits(self) -> IngestLimits:
        return self._limits

    def scan(self, data: bytes, *, declared_mime: str | None = None) -> ScanVerdict:
        """扫描一段字节。

        ``passed=False`` 的含义是"不许直接落盘"，不是"内容是坏的"；调用方应当据此把
        内容项置为隔离并开一条复核任务，而不是丢弃它——**丢弃会让人永远不知道
        自己丢了什么**。
        """
        size = len(data)
        details: dict[str, Any] = {"size_bytes": size, "mime_declared": declared_mime}

        if size == 0:
            details["category"] = "empty"
            return ScanVerdict(
                passed=False,
                engine=self.engine,
                reasons=("空文件（0 字节）：没有任何可判定的内容",),
                gray=True,
                details=details,
            )

        if size > self._limits.max_media_bytes:
            details["category"] = "too_large"
            details["limit_bytes"] = self._limits.max_media_bytes
            return ScanVerdict(
                passed=False,
                engine=self.engine,
                reasons=(
                    f"超过单文件上限：{size} 字节 > {self._limits.max_media_bytes} 字节"
                    f"（{size / 1048576:.1f} MB > {self._limits.max_media_bytes / 1048576:.1f} MB）",
                ),
                gray=False,
                details=details,
            )

        mime = sniff_mime(data)
        details["mime_detected"] = mime
        if mime is None:
            details["category"] = "unknown"
            return ScanVerdict(
                passed=False,
                engine=self.engine,
                reasons=("类型无法识别：魔数不匹配任何受支持的类型（未知一律不放行）",),
                gray=False,
                details=details,
            )

        rejection = self._rejection_reason(mime)
        if rejection is not None:
            details["category"] = "rejected"
            return ScanVerdict(
                passed=False,
                engine=self.engine,
                reasons=(rejection,),
                gray=False,
                details=details,
            )

        # 到这里类型本身是可接受的；只剩"声明的类型与真实类型是否一致"这一项
        reasons: list[str] = []
        gray = False
        if mime == "image/svg+xml":
            details["category"] = "svg"
            gray = True
            reasons.append("SVG 是不可信 XML：已按配置放行，但仍标记为可疑，进人工复核")
        else:
            details["category"] = "media"
        if declared_mime and self._policy.sniff_mime_by_magic and declared_mime != mime:
            gray = True
            reasons.append(f"声明的类型（{declared_mime}）与魔数判定（{mime}）不一致")
        return ScanVerdict(
            passed=True,
            engine=self.engine,
            reasons=tuple(reasons),
            gray=gray,
            details=details,
        )

    def _rejection_reason(self, mime: str) -> str | None:
        """拒绝理由；``None`` 表示这个类型可以接受。"""
        for _, magic_mime, why in _REJECTED_BY_MAGIC:
            if mime == magic_mime:
                return f"拒绝的类型：{why}（魔数判定为 {mime}）"
        if mime == "image/svg+xml" and not self._policy.allow_svg:
            return (
                "SVG 是不可信 XML，默认拒绝（config/app.yaml 的 security.allow_svg=false）；"
                "要接受它必须显式打开该开关，并且仍会走人工复核"
            )
        return None


__all__ = [
    "MIME_TO_ITEM_TYPE",
    "MagicNumberScanner",
    "declared_mime_for_name",
    "item_type_for",
    "sniff_mime",
]
