"""采集与提报端口（DESIGN §9.1：Connector、Submitter）。

两个接口的共同点是「把外部的东西带进系统」，区别在信任与副作用：
    Connector  受信来源的增量拉取，产出统一信封后由摄入层落库
    Submitter  L1 认证提报与 L2 匿名提报的统一入口，产物落隔离区 inbox_item
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pc.domain import SourceMessage


class Connector(Protocol):
    """平台接入适配器（DESIGN §1.3、§9.1）。

    契约要点：
        * ``list_new`` 只负责发现新消息，不做任何重处理；
        * **连接器不得直接写库**——它只产出 ``SourceMessage``，落库与入队由 L2 摄入层负责；
        * 平台政策变化时替换的是本实现，不是业务代码。

    明确不支持的接入方式（DESIGN §1.3）：浏览器自动化会话劫持、私有接口逆向、
    付费墙绕过。它们不是「暂未实现」，而是设计上不提供。
    """

    def list_new(self, cursor: str | None) -> tuple[Sequence[SourceMessage], str | None]:
        """按游标增量拉取。

        Returns:
            (新消息列表, 新游标)。游标语义由各平台决定（``last_seen_id`` / 时间窗 / 分页 token）。
        """
        ...

    def capabilities(self) -> dict[str, Any]:
        """声明本平台能力边界。

        至少应包含：能否读取历史消息、支持的消息类型、附件上限、是否支持编辑/删除回传。
        Bot 通常读不到加入前的历史消息，这类限制必须显式暴露，而不是让上层猜。
        """
        ...

    def authorize(self) -> bool:
        """校验「授权凭证 + 授权范围 + 授权证明记录」三者是否齐备。

        三者缺一不可入库（DESIGN §1.3）。返回 False 时不得继续采集。
        """
        ...

    def rate_limit_hint(self) -> dict[str, Any]:
        """返回本来源的速率与退避建议（令牌桶容量、每秒速率、``Retry-After`` 语义）。"""
        ...


class Submitter(Protocol):
    """提报入口（DESIGN §6.1、§9.1）。

    契约要点：
        * 入口层负责认证与配额，核心层不关心内容从哪来；
        * **L2 匿名提报在审查通过前不产生任何副作用**：不写 message/content_item、
          不下载外链、不打标、不进索引、不进任何外发候选池；
        * 隔离区字节与主存储物理分离，拒绝或超期后按 TTL 清理。
    """

    def receive(self, payload: dict[str, Any]) -> dict[str, Any]:
        """接收一次提报，落 ``inbox_item(status='pending')``。

        Returns:
            已落库的 inbox_item 摘要（含 inbox_id、status、quota_bucket）。
        """
        ...

    def quota_state(self, quota_bucket: str) -> dict[str, Any]:
        """查询某个配额桶的当前用量与剩余额度（按 IP/会话，只存带盐哈希）。"""
        ...


__all__ = ["Connector", "Submitter"]
