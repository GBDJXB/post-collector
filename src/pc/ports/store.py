"""存储、检索与容量管理端口（DESIGN §9.1）。

四个接口的共同要求：**派生数据一律可重建**。检索索引、向量库都可以从
SQLite 单文件 + 媒体目录全量重建，因此它们的实现可以被随时替换。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pc.domain import ObjectKey


class StorageBackend(Protocol):
    """媒体本体存储（DESIGN §3.5、§9.1）。

    契约要点：**键必须内容寻址**（``ab/cd/<sha256>``，见 ``domain.ObjectKey``），
    这样从本地目录换到 NAS、MinIO、对象存储时对象键不变，不需要数据搬迁。
    """

    def put(self, data: bytes, *, content_type: str | None = None) -> ObjectKey:
        """写入字节并返回内容寻址键；同内容重复写入应幂等。"""
        ...

    def get(self, key: ObjectKey) -> bytes:
        """按键读取；键不存在时抛 ``KeyError``。"""
        ...

    def exists(self, key: ObjectKey) -> bool:
        """按键判断本体是否存在（用于一致性对账，见 DESIGN §7.1）。"""
        ...

    def delete(self, key: ObjectKey) -> None:
        """删除本体。**删除元数据不删本体**，反之亦然，由一致性任务对账。"""
        ...

    def presign(self, key: ObjectKey, *, expires_in: int = 300) -> str:
        """生成临时访问地址；本地目录实现可直接返回路径，S3 实现返回签名 URL。"""
        ...


class SearchIndex(Protocol):
    """检索索引（DESIGN §5.1、§5.3、§9.1）。

    阶段 1 用 SQLite FTS5（trigram + 预分词 unicode61 双索引）；阶段 2 可换
    Meilisearch；数据库始终是事实源，索引只是**可重建的派生层**。
    查询构造在接口内完成，不让上层依赖某个引擎的专有语法。
    """

    def index(self, doc: dict[str, Any]) -> None:
        """写入或更新一个检索文档。"""
        ...

    def delete(self, doc_id: str) -> None:
        """删除一个检索文档（软删同时要调用，避免软删内容仍被搜到）。"""
        ...

    def query(
        self,
        q: str,
        filters: dict[str, Any] | None = None,
        sort: str | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """检索。

        Returns:
            含 ``hits`` 与 ``total`` 的结果；**每条结果必须能解释为什么出现**
            （命中字段、命中的 tag 与置信度、命中的过滤器），见 DESIGN §5.5。
        """
        ...

    def rebuild(self) -> None:
        """全量重建索引；任何索引异常的第一步都是重建，而不是修索引。"""
        ...


class VectorStore(Protocol):
    """向量库（DESIGN §5.2、§5.3）。

    本项目 10 万条量级下 pgvector 是**终局选择而非过渡**，
    仅在向量数 > 50 万或需要多向量/量化时才考虑 Qdrant。
    """

    def upsert(self, vec_id: str, vector: Sequence[float], payload: dict[str, Any]) -> None:
        """写入或覆盖一个向量及其过滤用载荷。"""
        ...

    def search(
        self,
        vector: Sequence[float],
        filter: dict[str, Any] | None = None,
        k: int = 20,
    ) -> list[dict[str, Any]]:
        """近邻检索；元数据过滤必须下推（pre-filter），不做事后过滤。"""
        ...

    def delete(self, vec_id: str) -> None:
        """删除向量（内容硬删时同步删除，避免派生数据残留）。"""
        ...


class StorageManager(Protocol):
    """容量管理与保留策略执行器（DESIGN §7.5、§9.1）。

    契约要点：**plan 与 apply 分离是硬要求**。
    dry-run 是默认模式，任何策略先出报告，root 确认后才执行；
    ``retention_run`` 表同时记录 dry_run 与实际执行，两者结果必须一致。
    """

    def plan(self, policy_id: int) -> dict[str, Any]:
        """只做计算，不产生任何写入（dry-run）。

        Returns:
            影响预估报告：将降级/归档/软删/硬删多少条、释放多少字节、
            以及被安全阀挡下的条数与原因。
        """
        ...

    def apply(self, report: dict[str, Any]) -> dict[str, Any]:
        """执行 ``plan`` 产出的报告。不允许跳过 plan 直接 apply。"""
        ...

    def usage(self) -> dict[str, Any]:
        """返回按后端与存储类别的占用，并给出水位与增长预测。

        「按当前增速还有多少天到水位线」比「删数据」更有用，
        它让扩容与清理成为可提前规划的决定（DESIGN §7.5）。
        """
        ...


__all__ = ["SearchIndex", "StorageBackend", "StorageManager", "VectorStore"]
