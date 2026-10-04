"""领域类型：状态取值与跨层传输的数据结构。

本模块**只依赖标准库**：它被 CLI（要能在没装任何依赖时运行）与所有适配器共同引用。
业务规则、校验与数据库访问都不在这里，见 DESIGN §3、§4、§9.1。

命名约定：标识符英文，注释中文；每个枚举的取值顺序与 DESIGN 表格顺序一致。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any

# ---------------------------------------------------------------------------
# 状态与取值枚举
# ---------------------------------------------------------------------------


class ContentStatus(StrEnum):
    """内容项状态；可见性矩阵见 DESIGN §3.3。"""

    ACTIVE = "active"
    QUARANTINED = "quarantined"  # 命中受限内容：不检索、不缩略图、永不外发
    ARCHIVED = "archived"  # 本体已归档，元数据仍可检索
    DELETED = "deleted"  # 软删（deleted_at 已置位）


class InboxStatus(StrEnum):
    """L2 匿名提报在隔离区内的状态。"""

    PENDING = "pending"
    REVIEWING = "reviewing"
    PROMOTED = "promoted"  # 通过审查，展开为 message + content_item
    REJECTED = "rejected"
    EXPIRED = "expired"  # 超期未处理


class EgressState(StrEnum):
    """AI 外发资格。

    骨架与 MVP 阶段恒为 ``LOCAL_ONLY``：没有任何代码消费该字段，
    也就不存在误发的可能（DESIGN §3.3、§8.1）。
    """

    LOCAL_ONLY = "local_only"  # 永不外发（默认；受限内容与全局仅本地开关强制）
    PENDING = "pending"  # 待 root 审阅
    CLEARED = "cleared"  # 已放行，携带 egress_scope 限定服务商与用途，且有有效期
    DENIED = "denied"
    EXPIRED = "expired"  # 放行失效（超期或服务商策略变更）


class JobStatus(StrEnum):
    """任务状态机（DESIGN §2.3、§7.1）。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # 可重试
    DEAD = "dead"  # 死信：不做自动清理，进复核台
    CANCELED = "canceled"


class ReviewKind(StrEnum):
    """复核任务类型。

    三类队列在 UI 上分开呈现：提报审查、外发放行、质量纠错（DESIGN §2.3、§3.3）。
    骨架阶段只创建 pending 队列的概念，不实现任何审查动作。
    """

    INBOX_SUBMISSION = "inbox_submission"
    EGRESS_REVIEW = "egress_review"
    LOW_CONFIDENCE = "low_confidence"
    NSFW_UNCERTAIN = "nsfw_uncertain"
    DEDUP_CONFLICT = "dedup_conflict"
    SCAN_GRAY = "scan_gray"
    TAG_CONFLICT = "tag_conflict"
    LINK_RISK = "link_risk"
    CHAR_AMBIGUOUS = "char_ambiguous"


class SubmissionTier(IntEnum):
    """提交来源信任层（DESIGN §6.1）。

    数值与 ``content_item.submission_tier`` 列一致，便于直接比较与过滤。
    """

    ROOT = 0
    DESIGNATED = 1  # L1 指定转发用户
    ANONYMOUS = 2  # L2 匿名提报，不产生 user 行


class TagNamespace(StrEnum):
    """标签命名空间（DESIGN §4.8）。"""

    CHARACTER = "character"
    FEATURE = "feature"
    UNCLASSIFIED = "unclassified"  # 角色识别失败时的可浏览归档桶，不是垃圾堆
    WORK = "work"
    ARTIST = "artist"
    GENERAL = "general"
    META = "meta"
    RATING = "rating"
    SOURCE = "source"
    SYSTEM = "system"


class TagSource(StrEnum):
    """一条标签判定的产出方式；决定是否进检索、是否需要确认（DESIGN §4.7.6）。"""

    HUMAN = "human"  # 人工，置信度视为 1.0，优先级最高
    SYSTEM = "system"  # 确定性规则
    AUTO = "auto"  # 本地模型，必须带 model_version_id 与 confidence
    SUGGESTED = "suggested"  # LLM 建议：不进检索、不进统计、不参与自动决策
    MERGED = "merged"  # 标签归并后的重定向产物


#: ``content_item.item_type`` 的取值（与 0001 的 CHECK 约束逐字一致）。
#: CLI 的 ``--type`` 选项与检索的"类型可搜"都从这里取，避免两处各写一份而漂移。
ITEM_TYPES: tuple[str, ...] = ("text", "image", "video", "audio", "link", "sticker", "file")

#: 类型的中文说法：进 ``search_extra`` 之后，"图片/视频"这类词也能搜到对应类型的内容。
#: 这不是查询期的意图识别（那需要维护词表、行为难解释），只是一段固定的可搜文本。
ITEM_TYPE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "text": "文本",
        "image": "图片",
        "video": "视频",
        "audio": "音频",
        "link": "链接",
        "sticker": "贴纸",
        "file": "文件",
    }
)

#: 检索结果单位是**帖子**（消息）时，"跨类型"这个构成取值的名字（DESIGN §5.1）。
#: 它不是 content_item 的类型，只是 ``--type`` 的一个可选取值——所以不进
#: ``ITEM_TYPE_LABELS``，否则按部件合成的 search_extra 里会混进一个永远不会出现的类型名。
MIXED_TYPE = "mixed"

#: ``--type`` 的允许取值：具体类型 + 跨类型。CLI 的 choices 与校验共用。
TYPE_FILTER_CHOICES: tuple[str, ...] = (*ITEM_TYPES, MIXED_TYPE)

#: 单帖最多返回多少个部件；超出时载荷里截断，``part_count`` 仍是真实值。
POST_PARTS_LIMIT = 20

#: 整帖去重签名的长度上限（字符）；超长则留空 = 不参与折叠。
POST_SIGNATURE_LIMIT = 2000


def post_type_mask(item_types: Iterable[str]) -> int:
    """把类型集合翻成位图，位序就是 :data:`ITEM_TYPES` 的顺序。

    帖子的构成用一个整数表达：单比特 = "纯内容"（构成全是这一种类型），多比特 = 跨类型。
    这样"只看图片内容"就是一次等值比较，"跨类型"就是一次位运算，见 :func:`post_matches_type`。
    """
    wanted = set(item_types)
    mask = 0
    for index, name in enumerate(ITEM_TYPES):
        if name in wanted:
            mask |= 1 << index
    return mask


def post_is_mixed(mask: int) -> bool:
    """构成是否跨类型：位图里不止一位（``mask & (mask - 1)`` 抹掉最低位后还非零）。"""
    return mask & (mask - 1) != 0


def post_matches_type(mask: int, bits: int, *, include_mixed: bool) -> bool:
    """帖子的构成是否命中 ``--type`` 的选择（DESIGN §5.1 的三种口径）。

    Args:
        mask: 帖子的类型位图（:func:`post_type_mask` 的结果）。
        bits: 所选类型的位图（多个 ``--type`` 取值取并集）。
        include_mixed: 是否把"跨类型内容里含所选类型部件"的帖子也算进来
            （``--include-mixed``）。注意它**不会**放进"视频 + 文本"这种不含所选类型的
            跨类型内容——``--type`` 说的是"返回什么内容"，不是集合求并。

    Examples:
        >>> post_matches_type(post_type_mask(["image"]), post_type_mask(["image"]), include_mixed=False)
        True
        >>> post_matches_type(post_type_mask(["image", "text"]), post_type_mask(["image"]), include_mixed=False)
        False
        >>> post_matches_type(post_type_mask(["image", "text"]), post_type_mask(["image"]), include_mixed=True)
        True
        >>> post_matches_type(post_type_mask(["video", "text"]), post_type_mask(["image"]), include_mixed=True)
        False
    """
    if post_is_mixed(mask):
        return include_mixed and (mask & bits) != 0
    return mask & bits != 0


def post_signature(parts: Iterable[tuple[str, str | None]]) -> str | None:
    """整帖去重签名：部件摘要去重、排序后拼接；没有摘要或超长则返回 ``None``。

    ``parts`` 是 ``(kind, digest)`` 序列——文本项 ``("text", content_hash)``、
    媒体项 ``("media", sha256)``。**拼接而不是哈希**：SQLite 没有 ``sha256()``，而这条
    规则要在落库与批量重算两条路径上算出同一个串；排序让签名与部件的到达顺序无关。

    ``None`` 的含义是"不参与折叠"：宁可多显示一条重复内容，也绝不误折叠两份不同的内容。
    """
    digests = sorted({f"{kind}:{digest}" for kind, digest in parts if digest})
    if not digests:
        return None
    joined = "|".join(digests)
    return joined if len(joined) <= POST_SIGNATURE_LIMIT else None


def post_composition_label(item_types: Sequence[str]) -> str:
    """把帖子的部件类型序列（按 ordinal）渲染成给人看的构成说明。

    连续的同类型合并成 ``图片x2``，不同类型用 ``+`` 连接：``图片+文本``、``图片x2``；
    单部件帖只显示一个类型名（与引入帖子概念之前的观感一致）。未知类型原样显示（不猜）。
    """
    labels: list[str] = []
    counts: list[int] = []
    for name in item_types:
        label = ITEM_TYPE_LABELS.get(name, name)
        if labels and labels[-1] == label:
            counts[-1] += 1
        else:
            labels.append(label)
            counts.append(1)
    return "+".join(
        label if count == 1 else f"{label}x{count}"
        for label, count in zip(labels, counts, strict=True)
    )


class AiInvolvement(StrEnum):
    """AI 参与度四档，默认第 1 档（DESIGN §4.7.3）。"""

    OFF = "off"  # 完全不参与：不加载任何模型，内容进待人工分类队列
    LOCAL_ONLY = "local_only"  # 仅本地模型（默认）
    ASSIST = "assist"  # 本地 + LLM 建议（全部落 suggested）
    ADVISORY_AUTO = "advisory_auto"  # 严格条件下的建议自动采信


class StorageClass(StrEnum):
    """媒体分层（DESIGN §7.5）。"""

    HOT = "hot"
    COLD = "cold"  # NAS / HDD，取回时有延迟提示
    ARCHIVE = "archive"  # 本体已删除，只剩元数据与哈希


class AuthzMode(StrEnum):
    """授权的取得方式；每个来源必须三者齐备才可入库（DESIGN §1.3）。

    ``ROOT_SELF`` 与 ``MANUAL_EXPORT`` 的区别是刻意的：前者是"授权来自 root **本人**
    在操作"（手工上传自己的文件），后者是"我导出了平台数据"。审计要能分清这两件事，
    所以不用一张占位凭证把前者糊过去。
    """

    OWNER_TOKEN = "owner_token"
    ADMIN_CONSENT = "admin_consent"
    PUBLIC_FEED = "public_feed"
    MANUAL_EXPORT = "manual_export"
    ROOT_SELF = "root_self"


class SettingsSource(StrEnum):
    """一个生效值来自哪一层。

    对应 ``pc config show --explain``：「这个值为什么是 8」必须能被回答
    （DESIGN §6.2.4）。解析顺序：env > db > yaml > default。
    """

    DEFAULT = "default"
    YAML = "yaml"
    DB = "db"
    ENV = "env"


# ---------------------------------------------------------------------------
# 跨层数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceMessage:
    """连接器归一化后的统一信封（DESIGN §2.1）。

    连接器**不得直接写库**：它只负责产出本结构，落库与入队由摄入层负责。
    """

    source_id: int
    platform_msg_id: str
    message_type: str  # text|media|mixed|link|system|repost
    sent_at: str | None = None  # ISO 8601 字符串，DA 层解析为 timestamptz
    platform_thread_id: str | None = None
    sender_platform_id: str | None = None
    sender_display_name: str | None = None
    sender_is_bot: bool = False
    text_raw: str | None = None
    reply_to_platform_id: str | None = None
    permalink: str | None = None
    media_refs: tuple[str, ...] = ()  # 平台侧媒体引用，下载由 fetch 阶段负责
    forward_chain: tuple[dict[str, Any], ...] = ()
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScanVerdict:
    """安全扫描结论。

    铁律：扫描失败按「未通过」处理（fail closed），进人工复核而不是放行
    （DESIGN §6.5）。
    """

    passed: bool
    engine: str
    reasons: tuple[str, ...] = ()
    gray: bool = False  # 可疑但未确认恶意 → scan_gray 复核队列
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TagCandidate:
    """单条候选标签及其证据；UI 必须能显示「哪个模型、几分、为什么」。"""

    tag: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)
    namespace: TagNamespace = TagNamespace.GENERAL


@dataclass(frozen=True, slots=True)
class RatingVerdict:
    """敏感度判定结果。

    敏感度一律**向上取整**，且外部模型永远不能降低本地给出的级别
    （DESIGN §4.7.2）。这条规则要写死在代码里，不靠 prompt。
    """

    level: int  # 0 safe … 4 restricted
    confidence: float


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """``Classifier`` 接口的统一输出（DESIGN §4.7.5）。

    界面与工作流必须建立在这个**判定结果**之上，而不是「哪个模型产出的」之上，
    否则「LLM 可关闭」就不成立。人工、本机模型、LLM 建议三种实现产出同一形状，
    只有 ``decided_by`` 与 ``source`` 不同。
    """

    tags: tuple[TagCandidate, ...] = ()
    rating: RatingVerdict | None = None
    flags: tuple[str, ...] = ()
    decided_by: str = ""  # 模型名或 'human' / 'rules'
    model_version_id: int | None = None
    source: TagSource = TagSource.SUGGESTED  # 默认最保守：只当建议，不进检索


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """媒体对象键：内容寻址 ``ab/cd/<sha256>``（DESIGN §3.6）。

    对象键是媒体位置的唯一真相；数据库里不存文件系统路径，
    路径由存储后端解析，这样换后端（本地 → NAS → S3）不需要数据搬迁。
    """

    sha256: str

    def __post_init__(self) -> None:
        if len(self.sha256) != 64 or not all(c in "0123456789abcdef" for c in self.sha256.lower()):
            raise ValueError("sha256 必须是 64 位十六进制字符串")

    @property
    def value(self) -> str:
        s = self.sha256.lower()
        return f"{s[0:2]}/{s[2:4]}/{s}"

    def __str__(self) -> str:  # pragma: no cover - 直通属性
        return self.value


# ---------------------------------------------------------------------------
# 跨层错误
#
# 放在领域层（而不是某个适配器里）：抛出方是连接器实现，捕获方是摄入层，
# 两者都不该 import 对方的模块。
# ---------------------------------------------------------------------------


class MediaTooLargeError(RuntimeError):
    """媒体本体超过 ``ingest.max_media_bytes``（DESIGN §6.5 的硬上限）。

    这是**永久失败**：重试同样的字节只会得到同样的结论，所以摄入层会直接把它
    记成死信而不是退避重试。
    """

    def __init__(self, ref: str, size_bytes: int, limit_bytes: int) -> None:
        self.ref = ref
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"媒体超过上限：{ref} 有 {size_bytes} 字节，上限是 {limit_bytes} 字节"
            f"（约 {size_bytes / 1048576:.1f} MB / {limit_bytes / 1048576:.1f} MB）。"
            "上限由 config/app.yaml 的 ingest.max_media_bytes 决定。"
        )


class SourceNotAuthorizedError(RuntimeError):
    """来源的「凭证 + 授权范围 + 授权证明」三者不齐备（DESIGN §1.3）。

    三者缺一不可采集：不齐备时**不得**继续，也不该"先收下来再说"。
    """


# ---------------------------------------------------------------------------
# 文本规范化
# ---------------------------------------------------------------------------


def local_source_authorized(source: Mapping[str, Any], authorizations: Sequence[Mapping[str, Any]], moment: str) -> bool:
    """本地采集与处理共用的授权闸门。"""
    return bool(source.get("enabled", 1)) and not source.get("deleted_at") and any(
        not row.get("revoked_at")
        and (not row.get("expires_at") or str(row["expires_at"]) > moment)
        for row in authorizations
    )


def normalize_text(text: str) -> str:
    """文本规范化：NFKC + 空白折叠 + 去首尾空白（DESIGN §4.2 的 normalize 步骤）。

    只做**无损**变换，不改写内容：全角转半角、兼容字符归并、连续空白折成一个空格。
    ``text_raw`` 保留原文，这里的结果写进 ``text_normalized`` 与检索文本，
    所以这一步必须是确定性的——同一段文本任何时候都要得到同一个结果，
    否则"同一份内容"在去重与检索里会分裂成两份。

    刻意不做的：分词（那是检索层的事，见 DESIGN §5.1）、大小写折叠（中文场景下
    折叠 ASCII 大小写会让 URL 与代码片段变形，等检索层按需处理）。
    """
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.split())


# ---------------------------------------------------------------------------
# 检索分词（DESIGN §5.1）
#
# 为什么中文要自己分词：FTS5 没有中文分词器，`unicode61` 会把一整句中文当成**一个**
# token（中文没有空格），于是 `content_word MATCH '蓝发'` 永远命中不了。而 `trigram`
# 索引在**查询串少于 3 个字符时命中 0 行且不报错**——"雨""蓝发"这种最常见的查询会
# 静默返回空结果。所以：
#
#   * `tokenize_document`（写侧）：CJK 连续串产出**全部单字 + 全部双字**，
#     拉丁/数字/下划线的连续串产出整词。单字让 1 字查询可命中，双字让 2 字查询可命中，
#     而 3 字以上交给 trigram 索引拿子串与容错。
#   * `tokenize_query`（读侧）：原始词长 1~2 时它在词级索引里就是那个词本身；
#     长度 ≥3 的词走 trigram，不用它。
#
# 两侧必须共用本模块：写侧与读侧的切法一旦不同，索引里有的词查询侧就找不到。
# ---------------------------------------------------------------------------

#: 视为"中日韩文字"的码点区间（Han 基本区与扩展 A/兼容、假名、谚文）
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
    (0x3040, 0x30FF),  # 平假名 + 片假名
    (0xAC00, 0xD7A3),  # 谚文音节
    (0x1100, 0x11FF),  # 谚文字母
    (0x20000, 0x2FA1F),  # CJK 扩展 B 及以后
)


def is_cjk(char: str) -> bool:
    """这个字符算不算"中日韩文字"（决定是按字切还是按词切）。"""
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _is_word_char(char: str) -> bool:
    """拉丁/数字/下划线：算"词字符"，连续的一段当成一个整词。

    ``_`` 算词字符是有意的：标签名形如 ``blue_hair``，拆成两个词会让"按标签名搜"
    变成两个 AND 条件，命中范围与直觉不符。
    """
    return not is_cjk(char) and (char.isalnum() or char == "_")


def _runs(text: str) -> list[tuple[bool, str]]:
    """把文本切成"连续的 CJK 段"与"连续的词字符段"，其余字符当分隔符丢弃。"""
    runs: list[tuple[bool, str]] = []
    current: list[str] = []
    current_is_cjk: bool | None = None
    for char in text:
        if is_cjk(char):
            kind = True
        elif _is_word_char(char):
            kind = False
        else:
            if current and current_is_cjk is not None:
                runs.append((current_is_cjk, "".join(current)))
            current, current_is_cjk = [], None
            continue
        if current_is_cjk is None:
            current_is_cjk = kind
        elif current_is_cjk != kind:
            runs.append((current_is_cjk, "".join(current)))
            current, current_is_cjk = [], kind
        current.append(char)
    if current and current_is_cjk is not None:
        runs.append((current_is_cjk, "".join(current)))
    return runs


def tokenize_document(text: str | None) -> tuple[str, ...]:
    """写侧分词：产出写进 ``content_item.text_search`` 的词元（DESIGN §5.1）。

    CJK 段 → 全部单字 + 全部双字；词字符段 → 整词（大小写折叠）。
    标点、空白、emoji 都是分隔符，不进索引（emoji 查询不承诺可搜）。
    """
    if not text:
        return ()
    tokens: list[str] = []
    for is_cjk_run, run in _runs(normalize_text(text)):
        if is_cjk_run:
            tokens.extend(run)
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        else:
            tokens.append(run.casefold())
    return tuple(tokens)


def tokenize_query(term: str) -> tuple[str, ...]:
    """读侧分词：把一个**原始查询词**变成词级索引要匹配的词元。

    长度 ≥3 的词走 trigram 索引（子串语义），不经过这里。这里处理 1~2 字的词：
    它们本身就是词元（单字对应单字 token，双字对应双字 token），所以只需要按
    同样的规则切一次（拉丁词折叠大小写、去掉标点）。

    刻意保持与写侧同一套规则：查询侧切出来的词元必须都在写侧产出的词元集合里，
    否则索引里有、查询却找不到。将来读侧真要分叉（例如拉丁前缀匹配），差异写在这里。
    """
    return tokenize_document(term)


def search_tokens(*parts: str | None) -> str | None:
    """把参与检索的几列拼起来分词，得到写进 ``content_item.text_search`` 的串。

    这是 ``content_word``（词级索引）的**唯一来源**。四列都为空时返回 ``None``——
    "没有可检索文本"与"分词结果为空"是两件事，前者不该被重算命令反复当成"还没分词"。
    """
    text = " ".join(part for part in parts if part)
    tokens = tokenize_document(text)
    return " ".join(tokens) if tokens else None


def search_extra_value(*parts: str | None) -> str:
    """``content_item.search_extra`` 的合成规则（DESIGN §5.1）。

    参与检索的"补充字段"按**固定顺序**拼成一个串，这个顺序就是本函数的契约：

        来源名 → 平台 → 发送者名 → item_type → item_type 中文标签 → 文件名 →
        标签名与同义词 → 来源备注（origin_note）

    顺序只影响可读性与 bm25 的列内位置，但必须写死一处：落库（``pipeline``）与批量重算
    （``refresh_search_derived``）是两处调用，各拼一套就会漂移，而漂移的表现是"刚入库的
    搜得到、重算之后的搜不到"。

    自由文本（文件名、备注）在拼接前按 :data:`SEARCH_EXTRA_TEXT_LIMIT` 截断——它们是用户
    可控的字符串，不设上限的话一个超长备注就能把索引撑大；文件名另有更严的 255 字符上限
    （:data:`FILE_NAME_LIMIT`，在 :func:`filename_from_ref` 里生效）。它是派生的——改标签、
    改来源名或补上文件名之后必须重算（见 ``SearchIndex.rebuild``）。
    """
    capped = [
        part.strip()[:SEARCH_EXTRA_TEXT_LIMIT] if part and part.strip() else None for part in parts
    ]
    return " ".join(part for part in capped if part)


#: 文件名与备注这类自由文本进入 ``search_extra`` 时各自的长度上限（字符）
FILE_NAME_LIMIT = 255

#: ``search_extra`` 里单个自由文本片段的上限（字符）：备注可能很长，索引不该跟着长
SEARCH_EXTRA_TEXT_LIMIT = 500


def filename_from_ref(ref: str) -> str | None:
    """从平台侧的媒体引用里取出**文件名**（本地路径、URL 都适用）。

    只保留 basename：数据库里不存位置（AGENTS「不把媒体文件路径写进数据库」），而且
    DESIGN §6.6 要求「文件名不参与路径拼接」——本函数的返回值只用于展示与检索。

    规则：剥掉 URL 的 query 与 fragment、把反斜杠当分隔符、去掉控制字符、截断到
    :data:`FILE_NAME_LIMIT`；结果为空则返回 ``None``（"没有名字"与"名字是空串"是两件事）。
    **只有站点没有路径的 URL**（``https://example.com/``）也返回 ``None``：那里取出来的
    是主机名，把它当文件名既不准确，又会在「文件名」列里显示成一条假信息。
    """
    trimmed = ref.split("?", 1)[0].split("#", 1)[0].rstrip("/\\")
    if "://" in trimmed:
        _, _, remainder = trimmed.partition("://")
        if "/" not in remainder:
            return None
    base = trimmed.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(char for char in base if unicodedata.category(char) != "Cc")
    cleaned = cleaned.strip()[:FILE_NAME_LIMIT]
    return cleaned or None


__all__ = [
    "FILE_NAME_LIMIT",
    "ITEM_TYPES",
    "ITEM_TYPE_LABELS",
    "MIXED_TYPE",
    "POST_PARTS_LIMIT",
    "POST_SIGNATURE_LIMIT",
    "SEARCH_EXTRA_TEXT_LIMIT",
    "TYPE_FILTER_CHOICES",
    "AiInvolvement",
    "AuthzMode",
    "ClassificationResult",
    "ContentStatus",
    "EgressState",
    "InboxStatus",
    "JobStatus",
    "MediaTooLargeError",
    "ObjectKey",
    "RatingVerdict",
    "ReviewKind",
    "ScanVerdict",
    "SettingsSource",
    "SourceMessage",
    "SourceNotAuthorizedError",
    "StorageClass",
    "SubmissionTier",
    "TagCandidate",
    "TagNamespace",
    "TagSource",
    "filename_from_ref",
    "is_cjk",
    "local_source_authorized",
    "normalize_text",
    "post_composition_label",
    "post_is_mixed",
    "post_matches_type",
    "post_signature",
    "post_type_mask",
    "search_extra_value",
    "search_tokens",
    "tokenize_document",
    "tokenize_query",
]
