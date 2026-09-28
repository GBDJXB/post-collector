"""工作流与配置端口（DESIGN §9.1：ReviewQueue、Notifier、SettingsRegistry）。

本模块的三个接口服务于「人在环路」：审查、告警、以及面向两类 root 用户的配置。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol


class ReviewQueue(Protocol):
    """复核队列（DESIGN §3.3、§9.1）。

    三类队列共用同一套机制，但在 UI 上必须分开呈现，因为它们的决策依据与风险
    完全不同（混在一起会让审阅者麻木）：
        1. 提报审查  对象是 inbox_item       —— 这条能不能进系统
        2. 外发放行  对象是 content_item     —— 这条能不能送第三方（阶段 2）
        3. 质量纠错  对象是 content_item/tag —— 这条标注对不对

    契约要点：审查动作必须**原子**且写审计；批量操作是第一等公民
    （多选 + 批量动作，一次操作一条审计记录但保留逐条明细）。
    """

    def enqueue(
        self,
        kind: str,
        target_type: str,
        target_id: int,
        priority: int = 100,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """入队一条复核任务，返回 ``task_id``。同目标重复入队应幂等。"""
        ...

    def resolve(
        self,
        task_id: int,
        action: str,
        actor: int,
        *,
        resolution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """处理一条任务。

        审查决策本身是评估集样本：拒绝/否决的**原因码必须结构化保存**，
        用于后续调阈值（DESIGN §3.3）。
        """
        ...

    def pending(self, kind: str | None = None, limit: int = 50) -> Sequence[dict[str, Any]]:
        """列出待处理任务；阶段 2 按风险分组的排序策略也在这里生效。"""
        ...


class Notifier(Protocol):
    """告警与通知（DESIGN §7.1、§9.1）。

    本地 / 邮件 / webhook / IM 各不相同，统一 ``notify(event)``。
    告警应避免依赖单一云服务；需要告警的事件包括：来源同步长时间无新数据
    （可能是凭证失效）、死信增长、磁盘 > 85%、大量扫描拒绝。
    """

    def notify(self, event: dict[str, Any]) -> None:
        """发送一条通知。通知失败不得影响主流程（静默丢弃 + 计入内部指标）。"""
        ...


class SettingsRegistry(Protocol):
    """设置项注册表（DESIGN §6.2.1、§6.2.2、§9.1）。

    声明式注册表是「设置页 / 校验 / 审计 / 文档」四者的唯一数据源：
    新增一个设置项只加一段 YAML 声明，**不改前端代码**。

    每个注册项必须包含：``key`` / ``type`` / ``default`` / ``range`` /
    ``label`` / ``help`` / ``impact`` / ``dangerous`` / ``cost`` /
    ``restart_required`` / ``recompute`` / ``requires``。

    热更新契约：界面保存 → 注册表校验（类型、range、requires 依赖链）→ 写
    ``setting`` 表（含前后值与操作者，危险项二次确认）→ 发布单调递增的配置版本号
    → 各进程原子替换内存中的配置快照 → 界面显示「已生效」或「已生效，有 N 项需重启」，
    **绝不静默**。新配置导致进程启动失败时必须保留旧快照并回滚，不能把系统卡在不可用状态。
    """

    def all_specs(self) -> Sequence[dict[str, Any]]:
        """返回全部注册项声明（供设置页、校验与 ``config show`` 派生）。"""
        ...

    def validate(self, key: str, value: Any) -> dict[str, Any]:
        """校验单个值：类型、range、requires 依赖链。

        Returns:
            ``{"ok": bool, "reason": str, "warning": str | None}``。
            错误信息必须能被人读懂（面向非技术 root），不能只是异常堆栈。
        """
        ...

    def explain(self, key: str) -> dict[str, Any]:
        """返回某个生效值及其**来源**（default / yaml / db / env）与含义。

        「这个值为什么是 8」必须能被回答，否则调试配置会变成猜谜。
        """
        ...

    def snapshot(self) -> dict[str, Any]:
        """返回当前配置的不可变快照。

        worker 处理单个任务时使用同一份快照，避免一次任务内配置中途变化
        导致状态不一致。
        """
        ...


__all__ = ["Notifier", "ReviewQueue", "SettingsRegistry"]
