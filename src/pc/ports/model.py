"""模型与安全端口（DESIGN §9.1：ModelProvider、Scanner、TagNormalizer、CharacterResolver）。

**红线**：MVP 阶段不存在任何 AI 外发代码路径。``ModelProvider`` 在本阶段只有
「本地可用」与「不可用」两种状态；阶段 2 引入外发网关后，外发调用必须收敛到
单一内部函数，并由 ``egress_log`` 记录（DESIGN §6.3 硬性规则）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pc.domain import (
    ClassificationResult,
    ScanVerdict,
    TagCandidate,
)


class ModelProvider(Protocol):
    """模型统一入口：本地模型与（未来的）云端模型共用同一签名（DESIGN §4.7.5、§9.1）。

    契约要点：
        * 输入输出与 ``model_version`` 绑定，所有输出都要能追溯「哪个模型、哪个 prompt 版本」；
        * **必须支持「仅本地」模式**：``ai.involvement = off`` 时本接口返回「不可用」，
          而**不是报错**——缺模型是正常状态，不是故障（DESIGN §4.7.4）；
        * 输出不能直接落库，必须经 ``tag_assignment`` 的 ``pending``/``suggested`` 流程；
        * 模型没有任何工具、插件、网络访问或代码执行能力；
        * 阶段 2 起，实际外发只能经由单一内部函数，便于审计与断言
          （「任何绕过 egress_log 的外发调用视为缺陷」）。
    """

    def available(self) -> bool:
        """当前档位下本模型是否可用。返回 False 时管线应跳过该步骤，不产生失败任务。"""
        ...

    def classify(self, content: dict[str, Any]) -> ClassificationResult | None:
        """内容分类与敏感度判定。

        返回 ``None`` 表示本档位下不参与（例如 ``ai.involvement = off``）。
        敏感度判定必须**向上取整**：外部模型只能提高、不能降低本地给出的级别。
        """
        ...

    def caption(self, content: dict[str, Any]) -> str | None:
        """生成图片描述。描述**不是事实**，只作检索辅助，且必须可编辑。"""
        ...

    def embed(self, content: dict[str, Any]) -> Sequence[float] | None:
        """生成文本或图像向量；同一内容只应计算一次（缓存键：内容哈希 + 模型版本）。"""
        ...

    def model_version(self) -> dict[str, Any]:
        """返回本次调用对应的模型版本元信息，用于写入 ``classification_run``。"""
        ...


class Scanner(Protocol):
    """媒体与文件安全扫描（DESIGN §6.5、§9.1）。

    统一 ``scan(bytes) -> ScanVerdict``，**fail closed**：
    任何扫描失败都按「未通过」处理，进人工复核而不是放行。
    扫描器会被替换与叠加（魔数嗅探、压缩炸弹检测、可选 ClamAV、SVG/PDF 处理）。
    """

    def scan(self, data: bytes, *, declared_mime: str | None = None) -> ScanVerdict:
        """扫描一段字节。

        以**魔数嗅探**为准，扩展名与声明的 Content-Type 仅作参考；
        不匹配即告警，必要时隔离到 ``quarantine_key``。
        """
        ...


class TagNormalizer(Protocol):
    """标签归一化与同义词扩展（DESIGN §4.8、§9.1）。

    独立可测试单元：归一化（小写、全半角统一、下划线统一）、查重、
    同义词检测、层级与冲突关系查询。手动建 tag 也必须走这里，
    否则「纯手动模式」会成为标签空间失控的入口。
    """

    def normalize(self, name: str) -> str:
        """归一化为 ``name_norm``：小写、全半角、下划线统一。"""
        ...

    def expand(self, name: str) -> set[str]:
        """检索时把查询词扩展为同义词集合（中/日/英/罗马音/拼写变体）。"""
        ...

    def resolve_conflicts(self, candidates: Sequence[TagCandidate]) -> list[TagCandidate]:
        """按 namespace、互斥关系与来源优先级消解冲突；人工 tag 优先级最高。"""
        ...


class CharacterResolver(Protocol):
    """角色判定（DESIGN §4.9、§9.1）。

    不把「判定角色」当作单次分类，而是分解为可独立验证的特征再组合投票：
        ``character_scores[c] = Σ_f weight(f, c) × confidence(f) × idf(f)``

    阈值与权重表是**配置，不是代码**；识别失败不产生角色 tag，而是落到可浏览的
    ``char_bucket`` 归档桶（例如 ``unclassified/by_hair_color/blue``）。
    长尾角色失败是预期行为，不是故障。
    """

    def resolve(
        self,
        features: Sequence[TagCandidate],
        embedding: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        """输入特征（可选 embedding），输出角色候选与归档桶。

        Returns:
            含 ``character``（可能为 None）、``score``、``bucket``、``candidates`` 的字典。
            分数不过阈值时 ``character`` 必须为 None——宁可进桶，也不错误地落角色 tag。
        """
        ...


__all__ = ["CharacterResolver", "ModelProvider", "Scanner", "TagNormalizer"]
