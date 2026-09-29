"""本地文件连接器（DESIGN §1.3、§9.1：``Connector`` 的第一个实现）。

服务的是阶段 1 的入口：**官方导出导入 + 手工上传**。语义刻意简单到没有歧义：

* 一个文件一条消息；同名侧车文本（``cat.jpg`` + ``cat.txt``）合成一条 ``mixed`` 消息，
  这正是"图 + 配文"在平台上的样子，也让导入层能验证一条消息含多个内容项；
* ``platform_msg_id`` 用**相对路径**，所以同一个文件重复导入是幂等的（唯一约束挡在
  摄入层），换个路径再导一次则被视为另一条消息——去重交给内容哈希，不靠路径；
* 文本文件（``.txt`` / ``.md``）作为纯文本消息入库，媒体文件作为媒体消息；
* 其余一切都当作**待扫描的媒体**：类型对不对由扫描器按魔数判定，不由扩展名判定。

刻意不做的事：解析 Telegram / QQ / 微信的导出格式。那是**同一端口上的另一个实现**
（``list_new`` 的游标语义换成"读到 JSON 数组的第几条"即可），不是本类的分支——把它塞进
来会让"一个文件一条消息"这条简单规则变成一堆格式判断。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pc.adapters.sqlite import now_iso
from pc.domain import MediaTooLargeError, SourceMessage

#: 视为"纯文本内容"的扩展名
TEXT_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".markdown"})

#: 侧车文本按这个顺序找（第一个存在的生效）
SIDECAR_SUFFIXES: tuple[str, ...] = (".txt", ".md", ".markdown")

#: 一次 ``list_new`` 最多返回多少条
DEFAULT_BATCH_SIZE = 200


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """一次目录扫描发现的文件（稳定排序后的一条）。"""

    root_label: str
    relative: str  # 相对来源根目录的 posix 路径
    path: Path
    size_bytes: int
    mtime: float

    @property
    def msg_id(self) -> str:
        return f"{self.root_label}/{self.relative}"


def _iso_from_timestamp(timestamp: float) -> str:
    """POSIX 时间戳 → 项目统一的时间戳格式（ISO-8601 UTC 毫秒，字典序即时间序）。"""
    moment = datetime.fromtimestamp(timestamp, tz=UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


class LocalFilesConnector:
    """把一个或多个本地目录当成"来源"。

    构造时注入来源行与它的授权行，**适配器不自己开数据库连接**（这是分层的硬要求：
    ``connector_for`` 是唯一解密凭证与读授权的地方）。目录每次 ``list_new`` 重新扫描，
    因此导入期间新放进来的文件会被下一轮发现。
    """

    def __init__(
        self,
        source: Mapping[str, Any],
        authorizations: Sequence[Mapping[str, Any]] = (),
        *,
        roots: Sequence[Path | str],
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_media_bytes: int = 52_428_800,
        max_text_chars: int = 100_000,
    ) -> None:
        if not roots:
            raise ValueError(
                "本地文件来源至少要有一个目录；请在 source.config 的 roots 里写，"
                "或在命令行把路径传给 pc import。"
            )
        self._source = source
        self._authorizations = tuple(authorizations)
        self._roots = tuple(Path(root).expanduser() for root in roots)
        self._batch_size = max(1, int(batch_size))
        self._max_media_bytes = int(max_media_bytes)
        self._max_text_chars = int(max_text_chars)
        self._root_labels = self._make_root_labels()

    def _make_root_labels(self) -> tuple[str, ...]:
        """给每个根目录一个稳定的短名字，用来拼 ``platform_msg_id``。

        重名时加序号前缀：``platform_msg_id`` 是幂等键的一半，不能因为两个目录同名就撞车。
        """
        names = [root.name or root.drive or "root" for root in self._roots]
        if len(set(names)) == len(names):
            return tuple(names)
        return tuple(f"{index}-{name}" for index, name in enumerate(names))

    # -- 发现的文件 ---------------------------------------------------------

    def discover(self) -> list[DiscoveredFile]:
        """扫描全部根目录，返回**稳定排序**的文件列表。

        跳过：隐藏文件与隐藏目录（``.`` 开头）、符号链接（防环，也防"导出包里指向
        ``/etc`` 的链接"）、非普通文件。跳过不是静默的：``capabilities()`` 里写明了规则。
        """
        found: list[DiscoveredFile] = []
        for label, root in zip(self._root_labels, self._roots, strict=True):
            if not root.exists():
                continue
            for path in root.rglob("*"):
                relative = path.relative_to(root)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    # 扫描期间文件消失（正在被移动/删除）：跳过，不打断整批导入
                    continue
                found.append(
                    DiscoveredFile(
                        root_label=label,
                        relative=relative.as_posix(),
                        path=path,
                        size_bytes=stat.st_size,
                        mtime=stat.st_mtime,
                    )
                )
        found.sort(key=lambda item: (item.root_label, item.relative))
        return found

    # -- Connector 契约 -----------------------------------------------------

    def list_new(self, cursor: str | None) -> tuple[Sequence[SourceMessage], str | None]:
        """按"稳定排序后的序号"增量产出。

        游标就是序号：它不承诺文件集合不变（新文件插到中间会让序号位移），所以**发现
        新消息的兜底始终是内容哈希去重**，而不是游标本身。这一点写在这里，免得后来者
        以为游标是可靠的增量水位。
        """
        files = self.discover()
        try:
            start = max(0, int(cursor)) if cursor else 0
        except (TypeError, ValueError):
            start = 0
        if start >= len(files):
            return (), str(len(files))
        batch = files[start : start + self._batch_size]
        return tuple(self._to_message(item) for item in batch), str(start + len(batch))

    def capabilities(self) -> dict[str, Any]:
        return {
            "platform": self._source.get("platform"),
            "kind": self._source.get("kind"),
            "supports_pull": True,
            "supports_push": False,
            "supports_media_fetch": True,
            "history": "all-local",
            "message_types": ["text", "media", "mixed"],
            "max_attachment_bytes": self._max_media_bytes,
            "supports_edit_events": False,
            "supports_delete_events": False,
            "cursor": "文件在稳定排序（根目录标签 + 相对路径）后的序号",
            "skip_rules": ["隐藏文件与隐藏目录（. 开头）", "符号链接", "非普通文件"],
            "roots": [str(root) for root in self._roots],
        }

    def authorize(self) -> bool:
        """三者齐备才返回 True：来源可用 + 有未撤销、未过期的授权记录。

        判据来自**真实记录**（``source_authorization`` 行），不是"本地目录应该没问题吧"。
        凭证明细（``evidence_ref``）只作留档，不参与判断。
        """
        if not self._source.get("enabled", 1):
            return False
        if self._source.get("deleted_at"):
            return False
        moment = now_iso()
        for authorization in self._authorizations:
            if authorization.get("revoked_at"):
                continue
            expires_at = authorization.get("expires_at")
            if expires_at and str(expires_at) <= moment:
                continue
            return True
        return False

    def rate_limit_hint(self) -> dict[str, Any]:
        return {
            "tokens_per_second": None,
            "bucket_size": None,
            "retry_after": None,
            "note": "本地文件没有速率限制；大批量导入的瓶颈是磁盘与写事务",
        }

    def fetch_media(self, ref: str) -> bytes:
        """读一个本地文件。

        先 ``stat`` 比对上限再读：把一个 2 GB 文件整个读进内存再判断"太大了"，
        等于让上限形同虚设（DESIGN §6.5 要求流式与字节上限）。
        """
        path = Path(ref)
        try:
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise KeyError(f"媒体文件不存在：{ref}") from exc
        if size > self._max_media_bytes:
            raise MediaTooLargeError(ref, size, self._max_media_bytes)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:  # pragma: no cover - stat 与 read 之间的竞态
            raise KeyError(f"媒体文件不存在：{ref}") from exc
        if len(data) > self._max_media_bytes:  # pragma: no cover - 读取期间被写大
            raise MediaTooLargeError(ref, len(data), self._max_media_bytes)
        return data

    def parse_event(self, payload: dict[str, Any]) -> Sequence[SourceMessage]:
        raise NotImplementedError(
            "本地文件来源是拉取型：请用 list_new（命令行是 `pc import`），不支持事件推送。"
        )

    # -- 内部 ---------------------------------------------------------------

    def _to_message(self, item: DiscoveredFile) -> SourceMessage:
        text = self._read_text(item)
        is_text_file = item.path.suffix.lower() in TEXT_SUFFIXES
        media_refs: tuple[str, ...] = () if is_text_file else (str(item.path),)

        if media_refs and text:
            message_type = "mixed"
        elif media_refs:
            message_type = "media"
        else:
            message_type = "text"

        return SourceMessage(
            source_id=int(self._source["source_id"]),
            platform_msg_id=item.msg_id,
            message_type=message_type,
            # 本地文件的 mtime 是**唯一可用的时间线索**，但它不是"这条内容发布的时间"：
            # 复制、下载、解压都会改 mtime。写进 raw_payload 让来源可追溯。
            sent_at=_iso_from_timestamp(item.mtime),
            text_raw=text,
            media_refs=media_refs,
            raw_payload={
                "kind": "local_file",
                "path": item.msg_id,
                "absolute_path": str(item.path),
                "size_bytes": item.size_bytes,
                "mtime": _iso_from_timestamp(item.mtime),
                "note": "sent_at 取自文件 mtime，不是真实发布时间",
            },
        )

    def _read_text(self, item: DiscoveredFile) -> str | None:
        """文本文件读自己；媒体文件找同名侧车（``cat.jpg`` + ``cat.txt``）。"""
        if item.path.suffix.lower() in TEXT_SUFFIXES:
            return self._read_text_file(item.path)
        for sidecar_suffix in SIDECAR_SUFFIXES:
            candidate = item.path.with_suffix(sidecar_suffix)
            if candidate != item.path and candidate.is_file():
                return self._read_text_file(candidate)
        return None

    def _read_text_file(self, path: Path) -> str | None:
        """读文本并截断到 ``ingest.max_text_chars``（按字节多读一点以容纳多字节字符）。"""
        limit_bytes = self._max_text_chars * 4
        try:
            with path.open("rb") as handle:
                raw = handle.read(limit_bytes)
        except OSError:
            return None
        text = raw.decode("utf-8", errors="replace")
        if len(text) > self._max_text_chars:
            text = text[: self._max_text_chars]
        return text


__all__ = ["DEFAULT_BATCH_SIZE", "SIDECAR_SUFFIXES", "TEXT_SUFFIXES", "DiscoveredFile", "LocalFilesConnector"]
