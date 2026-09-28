---
name: post-collector
description: "自托管内容采集与检索系统的项目入口文档，面向在此仓库工作的 AI coding agent 与新加入的开发者；包含项目概述、开发命令、关键目录、边界约束、设计文档导航，以及「改代码必须同步更新 AGENTS.md 与 README.md」的硬性规范。当需要在本仓库写代码、改配置、加迁移、判断改动是否越界，或准备提交时使用。"
---

# AGENTS.md

## 项目概述

post-collector 是一个自托管工具：把散落在**已授权来源**里的图文内容收集起来，做去重与安全扫描、打标分类、建立检索，并支持人工纠错与容量管理。

核心能力（按实现阶段逐步交付）：多来源增量采集 / 内容规范化与去重 / 本地模型打标与角色特征归档 / 标签与关键词检索 / 人工复核与纠错 / 保留策略与滚动删除。

当前状态：**骨架阶段**。已有分层目录、13 个端口接口声明、设置项注册表、数据库建表脚本与健康检查接口，**没有任何业务实现，也没有测试**。

设计事实源是 [`DESIGN.md`](DESIGN.md)。它是只读基线：改动实现前先读对应章节；若实现结论与它冲突，修改 `DESIGN.md` 并同步其附录 C 的变更记录，不要只在代码里悄悄偏离。

## 开发命令

| 操作 | 命令 | 说明 |
| --- | --- | --- |
| 安装依赖 | `uv sync` | 会一并装上 dev 组（pytest / ruff / httpx）；只要运行时用 `uv sync --no-dev`。生成 `uv.lock`，必须提交 |
| 环境体检 | `uv run python -m pc doctor` | 等价 `uv run pc doctor`；装依赖前的替代写法是 `PYTHONPATH=src python -m pc doctor`（不带 `uv run` 会报 `No module named pc`） |
| 本地启动 | `uv run uvicorn pc.api.app:create_app --factory --port 8080` | 端点见 `/`、`/healthz`、`/api/v1/meta`、`/docs` |
| 静态检查 | `uv run ruff check src` | ruff 随 dev 组安装；配置在 `pyproject.toml`，提交前必须为 All checks passed |
| 语法自检 | `python -m compileall -q src` | 无需安装依赖 |
| 运行测试 | `uv run pytest` | 测试目录尚未创建；`[tool.pytest.ini_options]` 已就位。注意当前 starlette 会对 `httpx` 版的 TestClient 报弃用告警，写测试时按提示换 `httpx2` |
| 建表 | `psql "$PC__DB__DSN" -v ON_ERROR_STOP=1 -f migrations/0001_init.sql` | 尚未在真实 PostgreSQL 上执行过 |
| 起数据库 | `docker compose up -d db` | 需要 Docker；开发机当前未安装，路径未验证 |

Windows PowerShell 下如需在未装依赖时运行 CLI：`$env:PYTHONPATH="src"; python -m pc doctor`；装好依赖后一律用 `uv run`，不必设 `PYTHONPATH`。

## 关键目录

```
post-collector/
├── AGENTS.md                     本文件：面向 agent 的项目入口
├── DESIGN.md                     设计基线（架构/数据模型/算法/安全/路线图）
├── README.md                     面向使用者的手册
├── pyproject.toml                包元数据、依赖、ruff 与 pytest 配置
├── Dockerfile / docker-compose.yml   单机编排：db + app
├── .env.example                  部署层变量样例（复制为 .env，后者不入库）
├── .gitattributes                行尾统一：仓库内一律 LF（覆盖 Windows 的 autocrlf）
├── config/
│   ├── app.yaml                  L2 开发配置 + 首次启动播种用的 defaults 段
│   └── settings-registry.yaml    设置项声明式注册表
├── migrations/
│   └── 0001_init.sql             核心建表脚本（32 张表 + 索引/约束）
├── data/                         运行时产物：PG 数据、媒体、冷层、隔离区、备份
├── models/                       本地模型权重（不入库）
└── src/pc/
    ├── domain.py                 状态枚举与跨层数据结构（零第三方依赖）
    ├── ports/                    13 个端口接口（Protocol），业务代码只依赖这一层
    ├── config.py                 注册表加载与分层解析（签名就位，实现留空）
    ├── cli.py                    doctor 可运行；config show 为占位
    └── api/                      FastAPI 工厂与路由
```

| 路径 | 职责 | 状态 |
| --- | --- | --- |
| `src/pc/ports/` | 采集与提报 / 存储检索与容量 / 模型与安全 / 工作流与配置 四组接口 | 接口已定，无实现 |
| `src/pc/domain.py` | 状态取值与传输结构；`ObjectKey` 定义内容寻址键 `ab/cd/<sha256>` | 可运行 |
| `src/pc/config.py` | `env > db > yaml > default` 四层解析与快照 | 待实现 |
| `src/pc/cli.py` | `doctor`（真实可用）、`config show`（占位，退出码 2/3） | 部分可用 |
| `tests/` | 单元与集成测试（`integration` marker 需真实数据库） | 尚未创建 |

## 边界约束

**两条红线（需以断言级测试守住）**

1. **不外发**：当前阶段不得引入任何第三方 AI 调用的代码路径。阶段 2 引入外发网关时，所有调用必须收敛到单一内部函数并逐条落 `egress_log`；`egress_state='cleared'` 不是免检通行证。
2. **匿名提报零副作用**：`inbox_item` 在审查通过前不得写入 `message` / `content_item`，不得打标、不得进索引、不得出现在任何检索结果里；隔离区字节与主存储物理分离。

**分层与解耦**

- 外部依赖一律经 `src/pc/ports/` 的接口进出：不在业务代码里直接调平台 SDK、数据库驱动或某个检索引擎的专有语法。
- 不把模型名与 prompt 硬编码在业务逻辑里；不把媒体文件路径写进数据库以外的任何地方（只存对象键，路径由存储后端解析）。
- 路由只做参数校验与鉴权，业务规则写在服务层。
- `StorageManager` 的 `plan` 与 `apply` 必须分离：计划默认 dry-run，确认后才执行。
- `ModelProvider` 必须支持「仅本地」模式：缺模型是正常状态，不是故障，管线跳过而非产生失败任务。
- 外部内容永远是数据、永不进入指令位置；模型输出不能直接触发写操作。

**迁移与数据**

- `migrations/` 下按序号追加 SQL，不修改已应用的版本。
- `user` 表因 PostgreSQL 保留字落地为 `app_user`；`before`/`after` 落地为 `before_state`/`after_state`。全部偏差写在 `0001_init.sql` 文件头。
- 执行迁移请整条语句执行（`psql -f`）；不要用 `text.split(';')` 之类的方式切分语句，文件内既有分号又有相邻字面量拼接。
- 派生数据（索引、向量、标签、caption）一律可重建；`PostgreSQL` 与媒体目录是唯一需要认真备份的东西。

**文档与提交**

- 文档与注释用中文；标识符、表名、枚举值、提交信息用英文。
- 文档不使用 Emoji；需要视觉标记时用文字或 ASCII 符号。
- 一个提交做一件事；涉及设计取舍的改动同时更新 `DESIGN.md` 的变更记录。

**文档同步（硬性规范：改代码必须同一次改动里更新文档）**

`AGENTS.md`（给 agent：命令、结构、约束）与 `README.md`（给使用者：怎么装、怎么用、出问题怎么办）描述的是同一套事实的两个视角。两者不得互相矛盾，也不得落后于代码。**下表任一列被改动时，必须在下一次提交前同步更新对应文档；不接受「先合并代码、文档回头补」。**

| 你改了什么 | 必须同步到哪里 |
| --- | --- |
| 新增 / 重命名 / 删除文件或目录 | 本文件「关键目录」的树与状态表；使用者可见的写进 README「数据存在哪」 |
| 安装、启动、检查、测试命令 | 本文件「开发命令」表；README「五分钟跑起来」与「日常操作」 |
| 依赖机制或分组（如 dev 组、`--no-dev`） | 本文件「开发命令」表；README 第 1 步的说明 |
| 新增 / 修改 HTTP 端点 | 本文件「本地启动」那一行；README 第 5 步的端点表 |
| 新增 / 删除端口接口 | 本文件三处写死的接口数量；README「现在能用到什么程度」 |
| 新增 / 修改设置项 | `config/settings-registry.yaml`；README「参数怎么调」的设置项清单 |
| 新增迁移或改动表结构 | 本文件「关键目录」里的表数量；`migrations/` 文件头的偏差声明 |
| 推进阶段（骨架 → 阶段 1 → …） | 本文件「项目概述」的状态句；README「现在能用到什么程度」与「当前版本还不能做什么」 |
| 新增已知限制、踩坑、弃用告警 | 本文件「边界约束」；使用者会踩到的同时写进 README「出问题了怎么办」 |
| 改动设计取舍 | `DESIGN.md` 对应章节 + 附录 C 变更记录，并回改上述受影响的表述 |

提交前自检，逐条过一遍：

- 用 `grep -rn "个接口\|张表\|骨架阶段\|尚未实现" AGENTS.md README.md` 复核写死的数量与状态词是否仍然成立。这类数字最容易过期，宁可少写数字，也不要把错的留在文档里。
- 两份文档对同一件事的表述是否一致：端点、命令、目录、默认值、默认安全行为。
- 本次新增或改名的文件，是否已经出现在本文件的目录树里。
- 使用者能感知到的变化，是否写进了 README，而不是只留在本文件里。

## AI 上下文

按任务查 `DESIGN.md` 对应章节，不要凭印象开工：

| 任务 | 章节 |
| --- | --- |
| 判断某功能是否属于本项目 | §1 项目范围与可行性（含非目标清单） |
| 改数据模型、加表、加索引 | §3 数据模型与存储设计（§3.2 字段级设计、§3.3 状态机与可见性矩阵） |
| 写连接器、接入新平台 | §1.3 接入模式分级、§9.1 `Connector` 契约 |
| 写处理管线、打标、角色识别 | §4 内容处理与算法方案（§4.7 参与度档位、§4.9 角色兜底归档） |
| 写检索 | §5 搜索与推荐设计（阶段 1 用 PG FTS，接口需可替换） |
| 涉及权限、配置、外发 | §6.1 三层用户、§6.2 配置分层、§6.3 外发网关 |
| 涉及删除、保留期、磁盘 | §7.5 容量管理与滚动删除（dry-run 是默认模式） |
| 选型与替换 | §8 技术选型建议（含替代方案与迁移成本） |
| 确认阶段范围与验收 | §9 实施路线图 |
| 落地前必须确认的外部事实 | 附录 A 需核实清单 |

其它上下文入口：

- 端口契约细节：`src/pc/ports/` 各模块文件头，13 个接口逐条写明契约要点与所属章节。
- 设置项字段规范：`config/settings-registry.yaml` 文件头（`label`/`help`/`impact`/`dangerous`/`cost`/`restart_required`/`recompute`/`requires` 各自的用途）。
- 当前部署形态：`docker-compose.yml` 末尾注释列出待加入的 `worker` / `sync` / `retention` 服务与对应卷。

**当前不包含**（不要按「补个小功能」处理，它们各自需要先落一个模块）：链接自动抓取、AI 外发、向量检索、多租户、Meilisearch / MinIO、Web 界面、worker / sync / retention 进程、测试套件、备份脚本。
