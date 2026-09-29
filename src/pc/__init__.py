"""post-collector —— 面向个人授权来源的内容采集、分类与检索系统。

骨架阶段：本包只提供分层结构、端口接口与基础设施声明，不含业务实现。

分层（DESIGN §2.2）：
    domain   纯数据类型与状态取值，无外部依赖
    ports    端口接口（Protocol），业务代码只依赖这一层
    adapters 适配器实现（SQLite / 本地目录 / 各平台连接器），阶段 1 逐步加入
    api      FastAPI 应用与路由
    jobs     任务队列与 worker（阶段 1 加入）

红线：MVP 阶段不存在任何 AI 外发代码路径（DESIGN §8.1）。
"""

__version__ = "0.0.1"
__all__ = ["__version__"]
