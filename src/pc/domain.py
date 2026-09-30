"""领域类型：状态取值与跨层传输的数据结构。

本模块**只依赖标准库**：它被 CLI（要能在没装任何依赖时运行）与所有适配器共同引用。
业务规则、校验与数据库访问都不在这里，见 DESIGN §3、§4、§9.1。

命名约定：标识符英文，注释中文；每个枚举的取值顺序与 DESIGN 表格顺序一致。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
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


__all__ = [
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
    "normalize_text",
]
