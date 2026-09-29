"""适配器层：端口接口的具体实现与基础设施访问。

DESIGN §2.2 的分层里，adapters 是**唯一允许接触外部系统**的一层：
数据库、本地目录、各平台连接器、检索引擎。业务代码只依赖 ``pc.ports`` 的接口，
不直接 import 本层（见 ``src/pc/ports/__init__.py`` 的约定）。

当前已落地：

    sqlite          SQLite 数据库：库文件与运行时目录解析、迁移应用、结构校验、
                    备份与 FTS5 重建（``pc db apply`` / ``status`` / ``backup`` / ``reindex``）
    local_storage   本地目录存储后端（内容寻址 ``ab/cd/<sha256>``，原子写入）
    magic_scan      魔数嗅探扫描器（白名单 + 字节上限 + fail closed）
    local_files     本地文件连接器（手工上传与官方导包共用）
    registry        连接器工厂：``source.kind`` → 实现，也是唯一解密凭证的位置

**数据库访问收敛在 ``pc.adapters.sqlite``**（DESIGN §9.2）：业务代码不得自己
``sqlite3.connect()``——``PRAGMA foreign_keys`` 默认关闭，任何一处漏设都会让外键
约束静默失效，而且 PRAGMA 基线与事务边界必须只有一处定义。

其余适配器（FTS5 检索索引、平台型连接器、模型与向量）随实现阶段加入，
顺序见 DESIGN §9「实施路线图」。
"""
