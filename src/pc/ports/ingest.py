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

    **拉取与推送是同一个端口的两条路**，由 ``capabilities()`` 声明走哪条：

        * 拉取型（Telegram ``getUpdates``、RSS、本地目录导出）：实现 ``list_new`` 的游标
          语义，``parse_event`` 抛 :class:`NotImplementedError`；
        * 推送型（Discord 网关、微信/QQ 的回调）：实现 ``parse_event`` 把一次回调载荷
          解析成 ``SourceMessage``，``list_new`` 抛 :class:`NotImplementedError`。

    两条路产出**同一个信封**，因此摄入层不需要知道消息是怎么来的。

    适配器还必须自己承担三件事，不要让上层替它做：**平台鉴权**（``fetch_media``
    需要凭证时由它自己带）、**限流与退避**（``rate_limit_hint`` 给建议，实际等待由
    调用方按它执行）、**字节上限**（超过 ``ingest.max_media_bytes`` 抛
    :class:`~pc.domain.MediaTooLargeError`，不要先读进来再判断——大文件要边读边数）。

    明确不支持的接入方式（DESIGN §1.3）：浏览器自动化会话劫持、私有接口逆向、
    付费墙绕过。它们不是「暂未实现」，而是设计上不提供。
    """

    def list_new(self, cursor: str | None) -> tuple[Sequence[SourceMessage], str | None]:
        """按游标增量拉取。

        Returns:
            (新消息列表, 新游标)。游标语义由各平台决定（``last_seen_id`` / 时间窗 / 分页 token）。
            返回空列表并把游标原样带回表示「暂时没有新的」。
        """
        ...

    def capabilities(self) -> dict[str, Any]:
        """声明本平台能力边界。

        至少应包含：能否读取历史消息、支持的消息类型、附件上限、是否支持编辑/删除回传。
        Bot 通常读不到加入前的历史消息，这类限制必须显式暴露，而不是让上层猜。

        另需显式声明三个**走哪条路**的开关，调用方先看它们再决定调哪个方法：

            ``supports_pull``         能否 ``list_new``
            ``supports_push``         能否 ``parse_event``
            ``supports_media_fetch``  能否 ``fetch_media``
        """
        ...

    def authorize(self) -> bool:
        """校验「授权凭证 + 授权范围 + 授权证明记录」三者是否齐备。

        三者缺一不可入库（DESIGN §1.3）。返回 False 时不得继续采集。

        实现方式由适配器决定，但**判据必须来自真实记录**：本地文件来源查的是
        ``source_authorization`` 里有没有未撤销、未过期的那一条，而不是"本地目录
        应该没问题吧"。适配器通过构造函数拿到来源与它的授权行，**不自己开数据库连接**。
        """
        ...

    def rate_limit_hint(self) -> dict[str, Any]:
        """返回本来源的速率与退避建议（令牌桶容量、每秒速率、``Retry-After`` 语义）。

        没有速率限制的来源（本地文件）也要返回这个结构，把"没有限制"写成显式事实，
        而不是返回空字典让调用方去猜。
        """
        ...

    def fetch_media(self, ref: str) -> bytes:
        """按平台侧引用取回媒体本体。

        Args:
            ref: ``SourceMessage.media_refs`` 里的那一项，语义由平台决定
                （file_id / CDN URL / 本地路径）。

        Returns:
            媒体字节。**调用方负责内容寻址与落盘**，适配器只负责取回来。

        Raises:
            MediaTooLargeError: 超过 ``ingest.max_media_bytes``。
            KeyError: 引用的对象已不存在（消息还在、附件被删了）。
        """
        ...

    def parse_event(self, payload: dict[str, Any]) -> Sequence[SourceMessage]:
        """把一次推送事件解析成统一信封（推送型适配器的入口）。

        Returns:
            该事件对应的消息；一条事件可能展开成多条（例如一次转发的多图相册）。

        Raises:
            NotImplementedError: 本适配器是拉取型。
        """
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
