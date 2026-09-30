"""本地文件连接器（DESIGN §1.3、§9.1：``Connector`` 的第一个实现）。

服务的是阶段 1 的两个入口：**官方导出导入**（``pc import``）与 **root 手工上传**
（``pc upload``）。两者的取字节逻辑完全一样，差别只有**身份怎么定**——所以这里是
同一个实现、两种身份策略，而不是两个类：

``identity="path"``（导出/目录导入）
  一个文件一条消息；``platform_msg_id`` 用**相对路径**（``根标签/相对路径``），所以同一个
  文件重复导入是幂等的，换个路径再导一次算另一条消息。``sent_at`` 取文件 mtime——导出包
  里的文件时间是有意义的线索。适合"把一个目录整体导进来"。

``identity="content"``（root 手工上传）
  一个文件一条消息；``platform_msg_id`` 由**内容**决定（见下），所以同一份内容重传是幂等的，
  改名、换目录都不影响。``sent_at`` **留空**：手工上传的文件 mtime 是下载/复制/解压的时间，
  拿它冒充"发布时间"是假数据。适合"我随手把几个文件放进来"。

两种策略共同的规则：

* 同名侧车文本（``cat.jpg`` + ``cat.txt``）合成一条 ``mixed`` 消息，这正是"图 + 配文"在
  平台上的样子，也让导入层能验证一条消息含多个内容项；
* 文本文件（``.txt`` / ``.md``）作为纯文本消息入库，媒体文件作为媒体消息；
* 其余一切都当作**待扫描的媒体**：类型对不对由扫描器按魔数判定，不由扩展名判定。

内容身份的构造规则（``upload:`` 前缀 + 冒号连接的组件）：

===================  ==========================================
单张图                ``upload:<字节的 sha256>``
纯文本                ``upload:<规范化文本的 sha256>``
图 + 配文             ``upload:<媒体 sha256>:<文本 sha256>``
===================  ==========================================

组件用的就是内容层已经在用的那两个哈希：媒体是 ``media.sha256``，文本是
``content_item.content_hash``（``sha256(normalize_text(原文))``）的口径。**两处用同一套
判据**，只差空白的文本文件因此会被认成同一条，而不是先分裂成两条再靠 dup_group 归组。

**内容为空的文件不产生消息**（空文件、只剩空白的文本文件）：它们没有可用的身份组件，而
"所有空文件的哈希都相同"会让它们互相顶掉——那是最难查的一类静默丢数据。跳过的文件记在
``skipped`` 里供命令行报告，不是静默丢弃。

刻意不做的事：解析 Telegram / QQ / 微信的导出格式。那是**同一端口上的另一个实现**
（``list_new`` 的游标语义换成"读到 JSON 数组的第几条"即可），不是本类的分支——把它塞进
来会让"一个文件一条消息"这条简单规则变成一堆格式判断。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pc.adapters.sqlite import now_iso
from pc.domain import MediaTooLargeError, SourceMessage, normalize_text

#: 视为"纯文本内容"的扩展名
TEXT_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".markdown"})

#: 侧车文本按这个顺序找（第一个存在的生效）
SIDECAR_SUFFIXES: tuple[str, ...] = (".txt", ".md", ".markdown")

#: 一次 ``list_new`` 最多返回多少条
DEFAULT_BATCH_SIZE = 200

#: 身份策略：``platform_msg_id`` 由路径还是由内容决定
IDENTITY_PATH = "path"
IDENTITY_CONTENT = "content"
IDENTITIES: frozenset[str] = frozenset({IDENTITY_PATH, IDENTITY_CONTENT})

#: 内容身份的前缀，让两种身份在库里一眼可辨（``upload:...`` vs ``根标签/相对路径``）
CONTENT_ID_PREFIX = "upload:"

#: 流式哈希的分块大小（内容身份要读一遍字节，但不该把整个文件读进内存）
HASH_CHUNK_BYTES = 1 << 20


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
        identity: str = IDENTITY_PATH,
    ) -> None:
        if not roots:
            raise ValueError(
                "本地文件来源至少要有一个目录；请在 source.config 的 roots 里写，"
                "或在命令行把路径传给 pc import。"
            )
        if identity not in IDENTITIES:
            raise ValueError(
                f"未知的身份策略 identity={identity!r}；"
                f"可用：{'、'.join(sorted(IDENTITIES))}"
            )
        self._source = source
        self._authorizations = tuple(authorizations)
        self._roots = tuple(Path(root).expanduser() for root in roots)
        self._batch_size = max(1, int(batch_size))
        self._max_media_bytes = int(max_media_bytes)
        self._max_text_chars = int(max_text_chars)
        self._identity = identity
        # 内容身份需要读一遍字节。``list_new`` 是按批调用的，而 ``discover()`` 每批都重扫
        # 整个目录——没有缓存的话，一个大目录会被反复哈希成 O(n²)。缓存键带 mtime_ns，
        # 文件被改动时自然失效。
        self._hash_cache: dict[tuple[str, int, int], str] = {}
        self._skipped: list[tuple[str, str]] = []
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
        """扫描全部根，返回**稳定排序**的文件列表。

        一个根**可以是一个文件**（``pc upload a.jpg`` / ``pc import a.jpg``）：那就只产出
        它自己，``relative`` 取文件名。这不是特例而是必要——把文件当根、再去 ``rglob``，
        一个文件都发现不了（``Path("a.jpg").rglob("*")`` 是空的），于是"导入单个文件"会
        静默地什么都不做。命令行因此可以原样把用户给的路径当根，而不必偷偷换成它们的
        父目录（换成父目录会把整个目录都导进来，那是另一种误伤）。

        跳过：隐藏文件与隐藏目录（``.`` 开头）、符号链接（防环，也防"导出包里指向
        ``/etc`` 的链接"）、非普通文件。跳过不是静默的：``capabilities()`` 里写明了规则。
        """
        found: list[DiscoveredFile] = []
        for label, root in zip(self._root_labels, self._roots, strict=True):
            if not root.exists() or root.is_symlink():
                continue
            if root.is_file():
                candidates = [(root, Path(root.name))]
            else:
                candidates = [
                    (path, path.relative_to(root)) for path in root.rglob("*")
                ]
            for path, relative in candidates:
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

        内容身份下，**内容为空的文件不会出现在返回值里**——它们没有可用的身份组件。
        跳过了哪些、为什么，看 :attr:`skipped`。
        """
        files = self.discover()
        try:
            start = max(0, int(cursor)) if cursor else 0
        except (TypeError, ValueError):
            start = 0
        if start >= len(files):
            return (), str(len(files))
        batch = files[start : start + self._batch_size]
        messages = [message for item in batch if (message := self._to_message(item)) is not None]
        return tuple(messages), str(start + len(batch))

    @property
    def skipped(self) -> tuple[tuple[str, str], ...]:
        """``list_new`` 跳过的文件（路径）与原因，去重、按路径排序。**只为报告。**"""
        seen: dict[str, str] = {}
        for relative, reason in self._skipped:
            seen.setdefault(relative, reason)
        return tuple(sorted(seen.items()))

    def capabilities(self) -> dict[str, Any]:
        skip_rules = ["隐藏文件与隐藏目录（. 开头）", "符号链接", "非普通文件"]
        if self._identity == IDENTITY_CONTENT:
            skip_rules.append("内容为空的文件（空文件、只剩空白的文本）")
        return {
            "platform": self._source.get("platform"),
            "kind": self._source.get("kind"),
            "supports_pull": True,
            "supports_push": False,
            "supports_media_fetch": True,
            # 凭证需求由实现自己声明：本地文件的授权来自"操作者在场"或"手工导出"，
            # 没有平台凭证这回事。平台型连接器必须声明 True（见 registry 的模块文档）。
            "requires_credential": False,
            "identity": self._identity,
            "history": "all-local",
            "message_types": ["text", "media", "mixed"],
            "max_attachment_bytes": self._max_media_bytes,
            "supports_edit_events": False,
            "supports_delete_events": False,
            "cursor": "文件在稳定排序（根目录标签 + 相对路径）后的序号",
            "skip_rules": skip_rules,
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
            "本地文件来源是拉取型：请用 list_new（命令行是 `pc import` 或 `pc upload`），"
            "不支持事件推送。"
        )

    # -- 内部 ---------------------------------------------------------------

    def _to_message(self, item: DiscoveredFile) -> SourceMessage | None:
        """把一个发现的文件变成信封；内容身份下无法成立身份时返回 ``None``（并记账跳过）。"""
        text = self._read_text(item)
        is_text_file = item.path.suffix.lower() in TEXT_SUFFIXES
        media_refs: tuple[str, ...] = () if is_text_file else (str(item.path),)

        if self._identity == IDENTITY_CONTENT:
            identity = self._content_identity(item, text=text, has_media=bool(media_refs))
            if identity is None:
                reason = (
                    "0 字节的文件：空字节无法构成内容身份"
                    if item.size_bytes == 0
                    else "只剩空白的文本：规范化后没有内容，无法构成内容身份"
                )
                # 报**路径**而不是 msg_id：内容身份模式下 msg_id 是一串哈希，对人没用；
                # 而且文件根时 msg_id 会长成 "a.jpg/a.jpg"（根标签与文件名相同）。
                self._skipped.append((str(item.path), reason))
                return None
            platform_msg_id = identity
            sent_at = None
            time_note = "内容身份模式下 sent_at 留空：mtime 只是文件系统线索，不是发布时间"
        else:
            platform_msg_id = item.msg_id
            # 本地文件的 mtime 是**唯一可用的时间线索**，但它不是"这条内容发布的时间"：
            # 复制、下载、解压都会改 mtime。写进 raw_payload 让来源可追溯。
            sent_at = _iso_from_timestamp(item.mtime)
            time_note = "sent_at 取自文件 mtime，不是真实发布时间"

        if media_refs and text:
            message_type = "mixed"
        elif media_refs:
            message_type = "media"
        else:
            message_type = "text"

        return SourceMessage(
            source_id=int(self._source["source_id"]),
            platform_msg_id=platform_msg_id,
            message_type=message_type,
            sent_at=sent_at,
            text_raw=text,
            media_refs=media_refs,
            raw_payload={
                "kind": "local_file",
                "identity": self._identity,
                "path": item.msg_id,
                "absolute_path": str(item.path),
                "size_bytes": item.size_bytes,
                "mtime": _iso_from_timestamp(item.mtime),
                "note": time_note,
            },
        )

    def _content_identity(
        self, item: DiscoveredFile, *, text: str | None, has_media: bool
    ) -> str | None:
        """按内容算出 ``platform_msg_id``；没有可用的身份组件时返回 ``None``。

        组件的取法与内容层一致：媒体用字节的 sha256（等于 ``media.sha256``），文本用
        ``sha256(normalize_text(原文))``（等于 ``content_item.content_hash``）。

        **0 字节的文件一律不产生消息**（文本或媒体都一样）：所有空文件的哈希都是同一个值，
        拿它当身份会让它们互相顶掉。这不会丢内容——媒体的同名侧车文本本身就是
        ``TEXT_SUFFIXES`` 里的文件，会被 ``discover()`` 单独发现、作为纯文本消息入库。
        """
        if item.size_bytes == 0:
            return None
        components: list[str] = []
        if has_media:
            components.append(self._hash_file(item.path))
        if text is not None:
            normalized = normalize_text(text)
            if normalized:
                components.append(hashlib.sha256(normalized.encode("utf-8")).hexdigest())
        if not components:
            return None
        return CONTENT_ID_PREFIX + ":".join(components)

    def _hash_file(self, path: Path) -> str:
        """流式算文件字节的 sha256（带缓存，见 ``__init__`` 里的说明）。"""
        stat = path.stat()
        key = (str(path), stat.st_size, int(stat.st_mtime_ns))
        cached = self._hash_cache.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(HASH_CHUNK_BYTES):
                digest.update(chunk)
        value = digest.hexdigest()
        self._hash_cache[key] = value
        return value

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


__all__ = [
    "CONTENT_ID_PREFIX",
    "DEFAULT_BATCH_SIZE",
    "IDENTITIES",
    "IDENTITY_CONTENT",
    "IDENTITY_PATH",
    "SIDECAR_SUFFIXES",
    "TEXT_SUFFIXES",
    "DiscoveredFile",
    "LocalFilesConnector",
]
