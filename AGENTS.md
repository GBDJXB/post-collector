---
name: post-collector
description: "自托管内容采集与检索系统的项目入口文档，面向在此仓库工作的 AI coding agent 与新加入的开发者；包含项目概述、开发命令、关键目录、边界约束、设计文档导航，以及「改代码必须同步更新 AGENTS.md 与 README.md」的硬性规范。当需要在本仓库写代码、改配置、加迁移、判断改动是否越界，或准备提交时使用。"
---

# AGENTS.md

## 项目概述

post-collector 是一个自托管工具：把散落在**已授权来源**里的图文内容收集起来，做去重与安全扫描、打标分类、建立检索，并支持人工纠错与容量管理。

核心能力（按实现阶段逐步交付）：多来源增量采集 / 内容规范化与去重 / 本地模型打标与角色特征归档 / 标签与关键词检索 / 人工复核与纠错 / 保留策略与滚动删除。

当前状态：**骨架阶段，存储引擎已换成 SQLite**。已有分层目录、13 个端口接口声明、设置项注册表与健康检查接口；**数据库已经真实可用**（SQLite 单文件，34 张 STRICT 表 + 2 个 FTS5 虚拟索引 + 6 个同步触发器，权威定义在 `migrations/sqlite/0001_init.sql`）；**设置层已经真实可用**（注册表加载与校验、`env > db > yaml > default` 四层解析、来源可解释性、首次播种 `setting` 表、带审计与回滚的写入路径）；测试套件覆盖数据库适配层、设置层、SQLite 行为契约与 schema 红线，**128 条、0.5 秒、不依赖任何外部服务**；**但业务功能仍未实现**。

设计基线是 `DESIGN.md` **v3.1**：阶段 1 只启用 root 与邀请制用户（匿名提报后置到阶段 2），分发收敛在 GitHub（原生安装仍需从 PyPI 取依赖，已在文档里如实说明）。**代码与设计在存储引擎上已经一致**——不再有"迁移进行中"的偏差。

设计事实源是 [`DESIGN.md`](DESIGN.md)。它是只读基线：改动实现前先读对应章节；若实现结论与它冲突，修改 `DESIGN.md` 并同步其附录 C 的变更记录，不要只在代码里悄悄偏离。

## 开发命令

| 操作 | 命令 | 说明 |
| --- | --- | --- |
| 安装依赖 | `uv sync` | 会一并装上 dev 组（pytest / ruff / httpx）；只要运行时用 `uv sync --no-dev`。生成 `uv.lock`，必须提交 |
| 环境体检 | `uv run python -m pc doctor` | 等价 `uv run pc doctor`；装依赖前的替代写法是 `PYTHONPATH=src python -m pc doctor`（不带 `uv run` 会报 `No module named pc`）。体检含只读「数据库」段，**数据库不可达不影响退出码** |
| 本地启动 | `uv run uvicorn pc.api.app:create_app --factory --port 8080` | 端点见 `/`、`/healthz`、`/api/v1/meta`、`/docs` |
| 静态检查 | `uv run ruff check src` | ruff 随 dev 组安装；配置在 `pyproject.toml`，提交前必须为 All checks passed。改过 `tests/` 时一并跑 `uv run ruff check src tests` |
| 语法自检 | `python -m compileall -q src` | 无需安装依赖 |
| 运行测试 | `uv run pytest` | **128 条，全部在临时 SQLite 文件上跑：不依赖任何外部服务，也不会跳过**（0.5 秒内跑完）。`integration` marker 仍然保留，留给将来真正需要外部服务（平台接口、模型下载、对象存储）的测试。注意 starlette 会对 `httpx` 版 TestClient 报弃用告警，写新测试时按提示换 `httpx2` |
| 建库 / 应用迁移 | `uv run pc db apply [--path PATH]` | **整篇在单个显式事务内执行**（`BEGIN IMMEDIATE` … `COMMIT`），失败整体回滚；已应用则短路并补记结构指纹。默认库文件是 `<data_root>/pc.sqlite3`。语句用 `sqlite3.complete_statement()` 切分，**不要用 `text.split(';')`** |
| 校验库结构 | `uv run pc db status [--path PATH]` | 只读：表数（预期 34）、两个 FTS5 索引、`journal_mode`、`foreign_keys`、完整性检查、记账版本与结构指纹漂移；结构不符时退出码 2 |
| 备份数据库 | `uv run pc db backup [--to FILE]` | `VACUUM INTO` 生成事务一致的快照，随后打开快照跑完整性检查并核对表数；**拒绝覆盖已存在的文件**。默认落到 `<data_root>/backup/pc-<UTC 时间戳>.sqlite3`。备份必须与库放在**不同物理介质**上（DESIGN §7.1） |
| 重建检索索引 | `uv run pc db reindex` | 重建两个 FTS5 索引（索引是派生数据，随时可重建）。正常路径由 6 个触发器维持同步，这条是兜底与排错手段 |
| 检索性能基准 | `uv run python tools/bench_search.py --scale 100000 --assert` | 在合成库上跑九类检索场景并报 P50/P95/P99（口径见 DESIGN §5.6）；`--assert` 时超预算即退出码 1。**这是性能红线的执行入口**：任何改动检索路径的提交都要跑它。`--keep` 保留合成库以便排查 |
| 看设置项生效值 | `uv run pc config show [--explain] [--key K] [--json]` | 逐项打印生效值 + **来源**（env / db / yaml / default），回答「这个值为什么是 8」。数据库不可达不影响退出码，只降级成文件与环境层并打印一行说明 |
| 首次播种设置项 | `uv run pc config seed` | 只插入缺失的键，**绝不覆盖已有值**；重复运行安全。播种不写 `setting_change`（那不是谁做的改动） |

Windows PowerShell 下如需在未装依赖时运行 CLI：`$env:PYTHONPATH="src"; python -m pc doctor`；装好依赖后一律用 `uv run`，不必设 `PYTHONPATH`。

## 关键目录

```
post-collector/
├── AGENTS.md                     本文件：面向 agent 的项目入口
├── DESIGN.md                     设计基线（架构/数据模型/算法/安全/路线图）
├── README.md                     面向使用者的手册
├── pyproject.toml                包元数据、依赖、ruff 与 pytest 配置
├── Dockerfile / docker-compose.yml   可选隔离路径：单个 app 容器（没有数据库容器）
├── .env.example                  部署层变量样例（复制为 .env，后者不入库）
├── .gitattributes                行尾统一：仓库内一律 LF（覆盖 Windows 的 autocrlf）
├── config/
│   ├── app.yaml                  L2 开发配置 + 首次启动播种用的 defaults 段
│   └── settings-registry.yaml    设置项声明式注册表
├── migrations/
│   ├── sqlite/0001_init.sql      **权威**：34 张 STRICT 表 + 2 个 FTS5 虚拟表 + 6 个触发器
│   ├── postgres/0001_init.sql    历史：PG 版，已冻结（随 PG 路径退役删除）
│   └── README.md                 两套 schema 的关系、执行语义与写迁移的规矩
├── data/                         运行时产物：pc.sqlite3（含 -wal/-shm）、媒体、缩略图、冷层、隔离区、备份
├── models/                       本地模型权重（不入库）
├── tests/                        conftest（临时库夹具）+ 适配器/行为单元测试 + schema 与设置层测试
└── src/pc/
    ├── domain.py                 状态枚举与跨层数据结构（零第三方依赖）
    ├── ports/                    13 个端口接口（Protocol），业务代码只依赖这一层
    ├── config.py                 设置层：注册表加载与校验、四层解析、变更闸门、不可变快照
    ├── cli.py                    doctor / db apply,status,backup,reindex / config show,seed 已接线
    ├── adapters/                 适配器实现（sqlite.py：路径解析、迁移应用、结构校验、备份、FTS5 重建、setting 表读写）
    └── api/                      FastAPI 工厂与路由
```

| 路径 | 职责 | 状态 |
| --- | --- | --- |
| `src/pc/ports/` | 采集与提报 / 存储检索与容量 / 模型与安全 / 工作流与配置 四组接口 | 接口已定，无实现 |
| `src/pc/domain.py` | 状态取值与传输结构；`ObjectKey` 定义内容寻址键 `ab/cd/<sha256>` | 可运行 |
| `src/pc/config.py` | `env > db > yaml > default` 四层解析、来源可解释性、`plan_setting_change` 闸门与不可变快照 | 可用（单元测试覆盖） |
| `src/pc/adapters/` | SQLite：路径解析、PRAGMA 基线、迁移应用、结构校验、备份与 FTS5 重建、`setting`/`setting_change` 读写（带审计与回滚） | 首个适配器，可用 |
| `src/pc/cli.py` | `doctor`（含只读数据库段）、`db apply/status/backup/reindex`、`config show/seed` | 可用 |
| `tests/` | 适配器单测 + SQLite 行为契约 + schema 红线 + 设置层（全部在临时库上跑，无外部依赖） | 128 条，0.5 秒全绿 |

## 边界约束

**两条红线（需以断言级测试守住）**

1. **不外发**：当前阶段不得引入任何第三方 AI 调用的代码路径。阶段 2 引入外发网关时，所有调用必须收敛到单一内部函数并逐条落 `egress_log`；`egress_state='cleared'` 不是免检通行证。
2. **匿名提报零副作用**：`inbox_item` 在审查通过前不得写入 `message` / `content_item`，不得打标、不得进索引、不得出现在任何检索结果里；隔离区字节与主存储物理分离。**该入口后置到阶段 2**（阶段 1 不开放任何公开入口），但红线本身不弱化：一旦启用就必须以断言级测试守住。

**分层与解耦**

- 外部依赖一律经 `src/pc/ports/` 的接口进出：不在业务代码里直接调平台 SDK、数据库驱动或某个检索引擎的专有语法。
- 不把模型名与 prompt 硬编码在业务逻辑里；不把媒体文件路径写进数据库以外的任何地方（只存对象键，路径由存储后端解析）。
- 路由只做参数校验与鉴权，业务规则写在服务层。
- `StorageManager` 的 `plan` 与 `apply` 必须分离：计划默认 dry-run，确认后才执行。
- `ModelProvider` 必须支持「仅本地」模式：缺模型是正常状态，不是故障，管线跳过而非产生失败任务。
- 外部内容永远是数据、永不进入指令位置；模型输出不能直接触发写操作。

**迁移与数据**

- `migrations/sqlite/` 下按序号追加 SQL，不修改已应用的版本。权威脚本是 `0001_init.sql`（34 张 STRICT 表 + 2 个 FTS5 虚拟表 + 6 个触发器），后续结构改动一律以 `0002_*.sql` 追加，并同步更新 `pc.adapters.sqlite.EXPECTED_TABLES`（否则新表会被 `pc db status` 报成"计划外的表"）。`migrations/postgres/` 是**已冻结的历史版本**，不要再改，它随 PG 路径退役删除。
- 保留字落地改名：`user` → `app_user`；`before`/`after` → `before_state`/`after_state`；`authorization` → `source_authorization`。全部偏差写在 PG 版文件头，SQLite 版沿用同一套名字。
- **迁移文件里不得出现 `PRAGMA`**：PRAGMA 基线（`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=NORMAL`、`temp_store=MEMORY`）由 `pc.adapters.sqlite.connect()` 在建立连接时执行。`journal_mode` 在事务内无法切换，写进 `.sql` 会直接报错——适配器会拒绝这种迁移文件（`split_statements` 里拦）。
- 执行迁移是**整篇单事务**：语句用 `sqlite3.complete_statement()` 切分（**不要**用 `text.split(';')`，字符串字面量与注释里都可能有分号），在 `BEGIN IMMEDIATE` 内逐条执行，成功才写记账行并提交，失败整体回滚。适配器刻意用 `isolation_level=None` 自己管事务——Python 3.12 起 DDL 不再隐式开启事务，依赖隐式机制会让 `CREATE TABLE` 落到自动提交里，"失败整体回滚"当场失效。
- **`foreign_keys` 默认关闭，任何一处漏设都会让外键静默失效**，因此数据库访问必须收敛在 `pc.adapters.sqlite`：业务代码不得自己 `sqlite3.connect()`。另外 `sqlite3.Connection` 的 `with` 协议管的是**事务**而不是关闭连接，一律用 `db.open_db()`。
- 数据库位置优先级：`--path` > 环境变量 `PC__DB__PATH` > 环境变量 `PC_DATA_ROOT`（→ `<root>/pc.sqlite3`）> `.env` 里的同名两项 > `config/app.yaml` 的 `db.path`/`db.data_root` > 平台默认用户数据目录。**进程环境整层高于 `.env` 整层**——这条曾经写错（两层按同一个键混着查，导致 `.env` 压过环境变量），现在有回归测试钉着。
- 结构指纹：`schema_migration.checksum` 存的是**剔除整行注释后**的迁移 SHA-256，所以往文件头追加修订记录不会造成漂移；行尾注释算内容（保守取向，宁可多报一次也别漏掉真实 DDL 改动）。`pc db status` 会比对它并报告漂移，**只报告不覆盖**。
- 时间戳默认值与 PG 版**逐列对齐**：PG 里带 `DEFAULT now()` 的列，这里是 `DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))`；PG 里没有默认值的列（`deleted_at`/`finished_at`/`snapshot_at`/`expires_at`/`setting_change.applied_at`）也一律不给。`updated_at` 由应用层在 UPDATE 时重写，不建触发器。UUID 由应用层生成（SQLite 没有 `gen_random_uuid()`）。
- 时间戳一律 ISO-8601 UTC 毫秒（字典序即时间序）；布尔是 `INTEGER` + `CHECK (col IN (0,1))`，断言时写 `== 1` 而不是 `is True`；JSON 存 `TEXT` + `json_valid` 校验，直接 SQL 读会拿到字符串（适配器的 `_decode()` 只作用于 `read_settings`）。
- 派生数据（FTS5 索引、向量、标签、caption、缩略图）一律可重建；**`pc.sqlite3` 与媒体目录是唯一需要认真备份的东西**，且备份必须与库放在**不同物理介质**上（`pc db backup` 产出的快照也一样）。

**设置层（DESIGN §6.2）**

- 设置项的**语义**只写在 `src/pc/config.py`：类型、范围、enum 取值、`requires` 依赖链、危险项与 dry-run 闸门都在那里；适配器只负责落表，并保证 `setting` 与 `setting_change` 同生共死。
- `config/settings-registry.yaml` 里的 enum 取值**必须加引号**：YAML 1.1 会把裸写的 `off` / `on` / `yes` / `no` 解析成布尔值，于是 `off` 变成 `False`。加载器直接报错并提示加引号（fail closed），不会静默把取值改名。
- 注册表与 `app.yaml` 的加载规则：未知字段、未知键、键名重复、默认值越界都**直接报错**；`defaults` 段的值与注册表默认值不同**只提示**（它就是 L2 层的值），因为已播种的实例一律以数据库为准。
- 播种（`pc config seed`）只插不改、不写 `setting_change`：它不是谁做的一次改动。环境变量覆盖**永不播种**，否则会留下「环境变量撤掉后值还在」的幽灵配置。
- 命令行**没有**设置写入路径，这是刻意的：`setting_change.changed_via` 的 CHECK 只允许 `ui|yaml|env|migration`，从 CLI 写入就得先把审计字段编出来。写入路径 `pc.adapters.sqlite.write_setting_change` 已就绪（带审计、回滚、危险项二次确认断言），等设置页来接。
- `pc config show` 只读数据库层，且**数据库不可达不影响退出码**：它会把生效值降级成文件与环境层并打印说明，避免让人误以为看到的就是在跑实例的值。

**性能预算（回归红线）**

- 检索路径有明确的性能承诺：**10 万条规模下，任意关键词与过滤器组合的检索 P50 < 150 ms、P99 < 500 ms**（50 条/页、热缓存、单用户或 ≤4 并发读者；缩略图字节与 AI 解析不计入；口径与机制见 DESIGN §5.6）。
- **任何改动检索路径的提交都必须跑基准脚本**（`uv run python tools/bench_search.py --scale 100000 --assert`，见「开发命令」表），确认 P50 / P99 没有越过预算；越过就必须在同一个提交里修回或给出书面解释。首次 10 万条实测见 DESIGN §3.7（最慢场景 P99 16.8 ms，余量约 30 倍）。
- 三个最容易击穿预算的实现错误，评审时优先看：**组装结果出现 N+1**、**缩略图在查询时同步生成或内联 base64**、**数据库调用写在 `async def` 端点里**。
- 分页上限 50 与预算绑定：`search.default_page_size` 的 `range` 不得放宽到 50 以上，除非同步重写 §5.6 的预算与口径。

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
| 改数据模型、加表、加索引 | §3 数据模型与存储设计（§3.2 字段级设计、§3.3 状态机与可见性矩阵、**§3.8 删除与引用计数的可测试语义**） |
| 写连接器、接入新平台 | §1.3 接入模式分级、§9.1 `Connector` 契约 |
| 写处理管线、打标、角色识别 | §4 内容处理与算法方案（§4.7 参与度档位、§4.9 角色兜底归档） |
| 写检索 | §5 搜索与推荐设计（阶段 1 用 FTS5 双索引；**性能预算见 §5.6**；接口需可替换） |
| 涉及权限、配置、外发 | §6.1 三层用户、§6.2 配置分层、§6.3 外发网关 |
| 涉及删除、保留期、磁盘 | §7.5 容量管理与滚动删除（dry-run 是默认模式） |
| 选型与替换 | §8 技术选型建议（含替代方案与迁移成本） |
| 确认阶段范围与验收 | §9 实施路线图 |
| 落地前必须确认的外部事实 | 附录 A 需核实清单 |

其它上下文入口：

- 端口契约细节：`src/pc/ports/` 各模块文件头，13 个接口逐条写明契约要点与所属章节。
- 设置项字段规范：`config/settings-registry.yaml` 文件头（`label`/`help`/`impact`/`dangerous`/`cost`/`restart_required`/`recompute`/`requires` 各自的用途）。
- 当前部署形态：`docker-compose.yml` 末尾注释列出待加入的 `worker` / `sync` / `retention` 服务与对应卷。

**当前不包含**（不要按「补个小功能」处理，它们各自需要先落一个模块）：链接自动抓取、AI 外发、向量检索、多租户、Meilisearch / MinIO、Web 界面、worker / sync / retention 进程、**备份的定时调度**（手动入口 `pc db backup` 已经有了，缺的是每日任务）、**匿名提报入口与第二个连接器（DESIGN v3.1 已明确后置到阶段 2）**。测试套件已覆盖数据库适配层、SQLite 行为契约、设置层与 schema 红线，但**没有业务逻辑测试**（因为还没有业务逻辑）。
