"""端口接口总入口。

业务代码**只从本模块取接口**，不直接 import 适配器实现，也不直接调平台 SDK 或
数据库驱动（DESIGN §9.2「应避免的过早绑定」）。

当前状态：全部为接口声明（``typing.Protocol``），骨架阶段没有实现。
实现顺序建议见 DESIGN §9「实施路线图」。

接口分组（采集与提报 / 存储检索与容量 / 模型与安全 / 工作流与配置）
在各自的端口模块文件头说明。
"""

from pc.ports.ingest import Connector, Submitter
from pc.ports.model import CharacterResolver, ModelProvider, Scanner, TagNormalizer
from pc.ports.store import SearchIndex, StorageBackend, StorageManager, VectorStore
from pc.ports.workflow import Notifier, ReviewQueue, SettingsRegistry

__all__ = [
    "CharacterResolver",
    "Connector",
    "ModelProvider",
    "Notifier",
    "ReviewQueue",
    "Scanner",
    "SearchIndex",
    "SettingsRegistry",
    "StorageBackend",
    "StorageManager",
    "Submitter",
    "TagNormalizer",
    "VectorStore",
]
