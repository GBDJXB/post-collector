"""适配器层：端口接口的具体实现与基础设施访问。

DESIGN §2.2 的分层里，adapters 是**唯一允许接触外部系统**的一层：
PostgreSQL、本地目录、各平台连接器、检索引擎。业务代码只依赖 ``pc.ports`` 的接口，
不直接 import 本层（见 ``src/pc/ports/__init__.py`` 的约定）。

当前已落地：

    postgres    PostgreSQL 连接串解析、迁移应用、schema 校验（``pc db apply`` / ``pc db status``）

其余适配器（本地内容寻址存储、PG FTS 检索索引、平台连接器）随实现阶段加入，
顺序见 DESIGN §9「实施路线图」。
"""
