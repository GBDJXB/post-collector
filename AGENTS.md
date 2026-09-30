---
name: post-collector
description: "自托管内容采集与检索系统的项目入口文档，面向在此仓库工作的 AI coding agent 与新加入的开发者；包含项目概述、开发命令、关键目录、边界约束、设计文档导航，以及「改代码必须同步更新 AGENTS.md 与 README.md」的硬性规范。当需要在本仓库写代码、改配置、加迁移、判断改动是否越界，或准备提交时使用。"
---

# AGENTS.md

## 项目概述

post-collector 是一个自托管工具：把散落在**已授权来源**里的图文内容收集起来，做去重与安全扫描、打标分类、建立检索，并支持人工纠错与容量管理。

核心能力（按实现阶段逐步交付）：多来源增量采集 / 内容规范化与去重 / 本地模型打标与角色特征归档 / 标签与关键词检索 / 人工复核与纠错 / 保留策略与滚动删除。

当前状态：**骨架阶段，但「把本地内容存进库」这条链路已经端到端可用**。已有分层目录、13 个端口接口声明、设置项注册表与健康检查接口；**数据库真实可用**（SQLite 单文件，34 张 STRICT 表 + 2 个 FTS5 虚拟索引 + 6 个同步触发器，权威定义在 `migrations/sqlite/`，当前 4 个版本）；**设置层与配置层真实可用**（注册表加载与校验、`env > db > yaml > default` 四层解析、来源可解释性、首次播种、带审计与回滚的写入路径、`app.yaml` 全段严格校验、数据根与六个运行时目录可解析且带来源）；**采集与处理链路真实可用**（来源登记 + 授权三者齐备校验 → 两条入口：官方导出目录导入（`pc import`）与 root 手工上传（`pc upload`）→ L2 摄入落信封并入队 → 单进程 worker 取字节/扫描/哈希/去重/落盘 → 内容项与媒体多对多 → `pc item list` 查看）；测试套件覆盖以上全部，**510 条 + 2 条环境跳过、10 秒上下、不依赖任何外部服务**；**但检索、打标与界面仍未实现**。

设计基线是 `DESIGN.md` **v3.1**：阶段 1 只启用 root 与邀请制用户（匿名提报后置到阶段 2），分发收敛在 GitHub（原生安装仍需从 PyPI 取依赖，已在文档里如实说明）。**代码与设计在存储引擎上已经一致**——不再有"迁移进行中"的偏差。

设计事实源是 [`DESIGN.md`](DESIGN.md)。它是只读基线：改动实现前先读对应章节；若实现结论与它冲突，修改 `DESIGN.md` 并同步其附录 C 的变更记录，不要只在代码里悄悄偏离。

## 开发命令

| 操作 | 命令 | 说明 |
| --- | --- | --- |
| 安装依赖 | `uv sync` | 会一并装上 dev 组（pytest / ruff / httpx）；只要运行时用 `uv sync --no-dev`。生成 `uv.lock`，必须提交 |
| 环境体检 | `uv run python -m pc doctor` | 等价 `uv run pc doctor`；装依赖前的替代写法是 `PYTHONPATH=src python -m pc doctor`（不带 `uv run` 会报 `No module named pc`）。体检含只读「数据库」段（**不可达不影响退出码**）与「运行时目录」段（逐项打印落点与**来源**，并报告 `config/app.yaml` 的缺口） |
| 本地启动 | `uv run uvicorn pc.api.app:create_app --factory --port 8080` | 端点见 `/`、`/healthz`、`/api/v1/meta`、`/docs` |
| 静态检查 | `uv run ruff check src` | ruff 随 dev 组安装；配置在 `pyproject.toml`，提交前必须为 All checks passed。改过 `tests/` 时一并跑 `uv run ruff check src tests` |
| 语法自检 | `python -m compileall -q src` | 无需安装依赖 |
| 运行测试 | `uv run pytest` | **510 条 + 2 条跳过，全部在临时目录上跑：不依赖任何外部服务**（本机 6~13 秒，取决于磁盘与杀软——每个 CLI 测试都要建一个真库）。两条 skip 都是符号链接相关（"链接文件被跳过"与"文件根本身是符号链接"）——Windows 默认不给创建符号链接的权限，属于环境限制而不是功能缺失。`integration` marker 仍然保留，留给将来真正需要外部服务（平台接口、模型下载、对象存储）的测试。注意 starlette 会对 `httpx` 版 TestClient 报弃用告警，写新测试时按提示换 `httpx2` |
| 建库 / 应用迁移 | `uv run pc db apply [--path PATH] [--dir DIR] [--root DIR]` | **按文件名序号应用迁移目录下全部未应用的 `.sql`，每个文件一个显式事务**（`BEGIN IMMEDIATE` … `COMMIT`）：某个文件失败只回滚它自己，已成功的版本保持已应用。迁移目录默认是 `<项目根>/migrations/sqlite`；库文件默认是 `<data_root>/pc.sqlite3`。语句用 `sqlite3.complete_statement()` 切分，**不要用 `text.split(';')`**。命令会打印它实际用了哪个迁移目录 |
| 校验库结构 | `uv run pc db status [--path PATH] [--dir DIR] [--root DIR]` | 只读：表数（预期 34）、两个 FTS5 索引、`journal_mode`、`foreign_keys`、完整性检查、**每个版本的记账与结构指纹漂移**、有无未应用/未知版本；结构不符时退出码 2 |
| 备份数据库 | `uv run pc db backup [--to FILE]` | `VACUUM INTO` 生成事务一致的快照，随后打开快照跑完整性检查并核对表数；**拒绝覆盖已存在的文件**。默认落到 `<data_root>/backup/pc-<UTC 时间戳>.sqlite3`。备份必须与库放在**不同物理介质**上（DESIGN §7.1） |
| 重建检索索引 | `uv run pc db reindex` | 重建两个 FTS5 索引（索引是派生数据，随时可重建）。正常路径由 6 个触发器维持同步，这条是兜底与排错手段 |
| 检索性能基准 | `uv run python tools/bench_search.py --scale 100000 --assert` | 在合成库上跑九类检索场景并报 P50/P95/P99（口径见 DESIGN §5.6）；`--assert` 时超预算即退出码 1。**这是性能红线的执行入口**：任何改动检索路径的提交都要跑它。`--keep` 保留合成库以便排查 |
| 看设置项生效值 | `uv run pc config show [--explain] [--key K] [--json]` | 逐项打印生效值 + **来源**（env / db / yaml / default），回答「这个值为什么是 8」。数据库不可达不影响退出码，只降级成文件与环境层并打印一行说明 |
| 首次播种设置项 | `uv run pc config seed` | 只插入缺失的键，**绝不覆盖已有值**；重复运行安全。播种不写 `setting_change`（那不是谁做的改动） |
| 登记来源 | `uv run pc source add NAME [--kind export\|manual] [--roots DIR] [--authorize MODE] [--evidence TEXT]` | 一个事务里同时写 `source` 与 `source_authorization`（**三者齐备才可采集**，DESIGN §1.3）；`--roots` 会写进 `source.config`。**`--authorize` 可留空**：按 kind 取默认（`manual`→`root_self`，其余→`manual_export`）。`--kind manual` 的 `--roots` 也可以不写——`pc upload` 自己会补 |
| 列出来源 | `uv run pc source list [--json]` | 带上授权状态（是否有效、什么方式、何时过期） |
| 导入本地文件 | `uv run pc import PATH... --source NAME [--process] [--dry-run] [--limit N]` | 只做 L2（落 `message` + 入队）；`--process` 接着跑 worker。**路径（含单个文件）会持久化进 `source.config`**——worker 在另一个时刻取字节，只读那里 |
| 手工上传 | `uv run pc upload PATH... [--note TEXT] [--source NAME] [--no-process] [--dry-run]` | root 自己丢文件进来的正式入口：**身份按内容哈希**（重传同一份内容不会变成两条）、`sent_at` 留空、`--note` 记来源线索。默认用（必要时新建）`kind=manual` 的「manual」来源，**不需要任何凭证**；**默认接着处理**（媒体引用是文件路径，原文件被删就只能死信） |
| 跑处理管线 | `uv run pc work [--once] [--max N] [--loop] [--interval S]` | 抢占任务 → 取字节 → 扫描 → 哈希去重 → 落 media/content_item/content_media；确定性失败进死信，偶发失败退避重试 |
| 看内容 | `uv run pc item list [--limit N] [--source NAME] [--status S] [--json]` | 这是「内容真的进库了」最直接的证据；带「备注」列（来源备注，没有则 `-`）；未建库时只提示不报错 |

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
│   ├── app.yaml                  L2 开发配置：defaults 段（待播种）与部署/安全基线各段
│   └── settings-registry.yaml    设置项声明式注册表
├── migrations/
│   ├── sqlite/0001_init.sql      **权威**：34 张 STRICT 表 + 2 个 FTS5 虚拟表 + 6 个触发器
│   ├── sqlite/0002_*.sql         文本精确重复改为归组（去掉 content_hash 的唯一索引）
│   ├── sqlite/0003_*.sql         message.media_refs：媒体引用落进信封，消息才可重放
│   ├── sqlite/0004_*.sql         重建两张表放宽 kind/mode 枚举 + message.origin_note
│   └── README.md                 执行语义、方言历史、写迁移与重建表的规矩
├── data/                         运行时产物：pc.sqlite3（含 -wal/-shm）、媒体、缩略图、冷层、隔离区、备份
├── models/                       本地模型权重（不入库）
├── tests/                        conftest（临时库/干净库夹具）+ 适配器、服务层、CLI 端到端、红线守卫
└── src/pc/
    ├── domain.py                 状态枚举、跨层数据结构、跨层错误与文本规范化（零第三方依赖）
    ├── ports/                    13 个端口接口（Protocol），业务代码只依赖这一层
    ├── config.py                 设置层与 L2 配置段：注册表校验、四层解析、变更闸门、快照、限额与安全策略
    ├── cli.py                    doctor / config / db / source / import / upload / work / item 全部已接线
    ├── adapters/
    │   ├── sqlite.py             库文件与运行时目录解析、迁移应用、结构校验、备份、FTS5 重建、setting 表读写
    │   ├── local_storage.py      本地目录存储后端（内容寻址 ab/cd/<sha256>，原子写入、幂等）
    │   ├── magic_scan.py         魔数嗅探扫描器（白名单 + 字节上限 + fail closed）
    │   ├── local_files.py        本地文件连接器（手工上传与官方导包共用）
    │   └── registry.py           连接器工厂：source.kind → 实现，唯一解密凭证处
    ├── services/                 业务规则：来源登记、L2 摄入、L3 单进程 worker
    └── api/                      FastAPI 工厂与路由
```

| 路径 | 职责 | 状态 |
| --- | --- | --- |
| `src/pc/ports/` | 采集与提报 / 存储检索与容量 / 模型与安全 / 工作流与配置 四组接口 | 接口已定；`Connector` 的取字节与推送两条路已补齐，其余无实现 |
| `src/pc/domain.py` | 状态取值、传输结构与跨层错误（`MediaTooLargeError` / `SourceNotAuthorizedError`）；`ObjectKey` 定义内容寻址键 `ab/cd/<sha256>` | 可运行 |
| `src/pc/config.py` | `env > db > yaml > default` 四层解析、来源可解释性、`plan_setting_change` 闸门与不可变快照；`load_app_config` 严格校验 app.yaml 各段，`ingest_limits` / `security_policy` / `job_settings` 给类型化取值 | 可用（单元测试覆盖） |
| `src/pc/adapters/` | 数据库（库文件与六个运行时目录的解析、PRAGMA 基线、迁移、结构校验、备份、FTS5、setting 读写）、本地存储后端、魔数扫描器、本地文件连接器（两种身份策略）、连接器工厂 | 可用（单元测试覆盖） |
| `src/pc/services/` | 来源登记（source + authorization 同生共死、手工来源的 `ensure_manual_source`）、L2 摄入（幂等落信封 + 入队 + 来源备注）、L3 单进程 worker（抢占/重试/死信/去重/隔离/落账） | 可用（单元与端到端测试覆盖） |
| `src/pc/cli.py` | `doctor`、`config show/seed`、`db apply/status/backup/reindex`、`source add/list`、`import`、`upload`、`work`、`item list` | 可用 |
| `tests/` | 适配器 + 服务层 + CLI 端到端（导入与上传各一条）+ 项目根解析 + 迁移器（含表重建）+ 配置段与路径 + 连接器/存储/扫描器 + schema 红线 + 出网守卫（全部在临时目录上跑，无外部依赖） | 510 条 + 2 条跳过（Windows 无符号链接权限），10 秒上下全绿 |

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
- **连接器只有两条路，且必须自己声明走哪条**：拉取型实现 `list_new`，推送型实现 `parse_event`，另一条抛 `NotImplementedError`；`capabilities()` 里的 `supports_pull` / `supports_push` / `supports_media_fetch` 是调用方唯一的判据（微信/QQ 这类回调型来源与 Telegram 这类拉取型来源因此产出同一个信封）。
- **凭证按需，不造占位凭证**：`capabilities()` 里的 `requires_credential` 声明本来源要不要平台凭证。本地两类（`export` / `manual`）声明 `False`——它们的授权来自手工导出或操作者本人在场；平台型必须声明 `True`，并且在 `credential_secret` 表与系统密钥库落地前**无法实现**（`pc db status` 之外没有任何地方需要它们）。给不需要凭证的来源塞一张空凭证，会让审计里出现"看着验证过、其实什么都没有"的记录，将来无法区分真验证与占位。真正拦截调用的是 `authorize()`，统一语义是"**本来源所需的授权条件是否满足**"。
- **同一端口可以有多套身份策略**：`kind='export'` 按相对路径定身份（`platform_msg_id = 根标签/相对路径`，时间取 mtime），`kind='manual'` 按内容定身份（`upload:<媒体 sha256>[:<文本 sha256>]`，`sent_at` 留空）。两者共用同一个连接器实现，靠构造参数区分——**不要**为"又一种上传入口"写第二个类，那会把取字节/扫描/限流的逻辑复制一遍。
- **适配器不自己开数据库连接**：连接器由 `pc.adapters.registry.connector_for()` 构造，来源行与它的授权行由服务层读好后注入，`authorize()` 只对注入的记录做判断。凭证解密将来也只加在 `registry.py` 这一处（DESIGN §6.1）。
- **扫描是 fail closed 的**：`MagicNumberScanner` 对未知类型、压缩包、可执行文件、PDF、脚本一律 `passed=False`，把 SVG 与"扩展名与魔数不符"标成 `gray=True`；`passed=False` 的含义是"不许直接落盘"，调用方应当隔离并开复核任务，**不是丢弃**。

**迁移与数据**

- `migrations/sqlite/` 下的文件**按序号从 1 连续编号**（`0001_init.sql`、`0002_content_hash_not_unique.sql`…），适配器按序应用**整个目录**。**已应用的版本不可修改**：任何结构改动都以新文件追加，并同步更新 `pc.adapters.sqlite.EXPECTED_TABLES`（新表时）与 `tests/test_schema_integration.py` 的 `EXPECTED_MIGRATION_NAMES`（新版本时）——两处都是刻意的强制提醒。改已应用文件的 DDL 会让结构指纹漂移，`pc db status` 会报出来（**只报告、不覆盖**）。**PostgreSQL 版的建表脚本已经随 PG 路径一起删除**（记在 git 历史与 `migrations/README.md` 的方言对照表里），本项目现在只有一套 schema。
- 保留字落地改名：`user` → `app_user`；`before`/`after` → `before_state`/`after_state`；`authorization` → `source_authorization`。这三处改名沿用 PG 时期的决定，理由与全部偏差记录留在 git 历史与 `migrations/README.md` 里。
- **迁移文件里不得出现 `PRAGMA`**：PRAGMA 基线（`journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=NORMAL`、`temp_store=MEMORY`）由 `pc.adapters.sqlite.connect()` 在建立连接时执行。`journal_mode` 在事务内无法切换，写进 `.sql` 会直接报错——适配器会拒绝这种迁移文件（`split_statements` 里拦）。
- 执行迁移是**逐文件事务**：语句用 `sqlite3.complete_statement()` 切分（**不要**用 `text.split(';')`，字符串字面量与注释里都可能有分号），每个文件在它自己的 `BEGIN IMMEDIATE` 内整篇执行，成功才写该版本的记账行并提交；某个文件失败只回滚它自己，**已成功的版本保持已应用**。适配器刻意用 `isolation_level=None` 自己管事务——Python 3.12 起 DDL 不再隐式开启事务，依赖隐式机制会让 `CREATE TABLE` 落到自动提交里，"失败整体回滚"当场失效。记账表 `schema_migration` 由适配器兜底创建（它不计入业务表，所以"只有它"仍是干净库）。
- **放宽 `CHECK` 枚举必须重建表**（SQLite 没有 `ALTER TABLE DROP CONSTRAINT`）：迁移文件头写一行 `-- pc:foreign_keys=off`，迁移器会在 `BEGIN` **之前**关掉外键、提交前跑 `PRAGMA foreign_key_check` 兜底、结束后恢复（恢复失败不掩盖真正的错误）。**未知指令一律报错**（打错一个字母却静默失效，会让重建在开着外键时跑，报的错与真正原因无关）。临时表名一律以 `_rebuild` 结尾且必须在同一文件里改名成正式表；表清单校验忽略这个后缀，另一条测试保证库里不留临时表。重建的 DDL 必须逐字复制原表、只改那一处枚举——`test_schema_integration` 有一条逐字比对断言守着。**不需要 `legacy_alter_table`**（3.50.4 上实测，理由见 `migrations/README.md`）。
- **`foreign_keys` 默认关闭，任何一处漏设都会让外键静默失效**，因此数据库访问必须收敛在 `pc.adapters.sqlite`：业务代码不得自己 `sqlite3.connect()`。另外 `sqlite3.Connection` 的 `with` 协议管的是**事务**而不是关闭连接，一律用 `db.open_db()`。
- **仓库文件一律相对「项目根目录」解析，不相对当前工作目录**：迁移目录、`config/app.yaml`、`.env` 都走 `cli._project_root()`（`--root` 优先，否则按 `pyproject.toml` + `DESIGN.md` 向上查找，含 `__file__` 方向）。这条曾经只在 `doctor` / `config` 上成立，`db` 子命令用的是「当前目录 + 相对路径」，于是从仓库外运行会报「找不到迁移目录」，更糟的是**配置分层被静默跳过**（命令成功、库却建在平台默认目录）。现在有测试从仓库外运行并断言 `app.yaml:db.data_root` 仍然生效。**已知缺口**：`uv tool install` 装的包不带迁移文件，届时迁移目录必须打包进 wheel（`importlib.resources`），当前报错信息里已写明这一点。
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

**配置层（L2，`config/app.yaml` 的其余段）**

- `defaults` 段是要播种进数据库的设置项（走注册表校验）；`db` / `paths` / `jobs` / `ingest` / `security` / `models` 六段是**留在文件里**的部署与安全基线，刻意不进设置页。它们的已知键清单写死在 `pc.config.APP_CONFIG_SECTIONS`，**未知段名与未知键一律报错**（拼错的键被静默忽略正是「改了文件没生效」最难查的一类故障），并给出最接近的键名建议；测试断言这张清单与仓库里那份真实文件逐段一致，防止清单过期。
- 取值一律**类型严格**：整数字段里写 `true` 会报错（`bool` 是 `int` 的子类，静默变成 1 最难查）；布尔字段里写 `"false"` 同样报错。安全相关项的默认值一律在保守那一侧（`fail_closed_on_scan_error=True`、`allow_svg=False`），把它们做成配置项不等于可以随手关掉。
- 运行时目录的解析在 `pc.adapters.sqlite.resolve_paths()`，与 `resolve_db_path()` 放在**同一个模块**：两者必须对"数据根目录在哪"给出一致答案。优先级：数据根目录取 `PC_DATA_ROOT` > `.env` > `app.yaml:db.data_root` > 平台默认；每个子目录再按 `PC__PATHS__<NAME>_ROOT` > `.env` > `app.yaml:paths.<name>_root` > `<data_root>/<默认名>` 逐项覆盖。**媒体目录跟着数据根目录走，不跟着数据库文件走**——`PC__DB__PATH` 只挪库文件本身（放进 SSD、放到挂载盘），顺带挪走媒体会让老内容静默失联，这条有回归测试钉着。

**采集与处理（DESIGN §2.1、§2.2、§2.3）**

- **任务结局必须落账，成功也要**：`Worker._finalize` 是唯一改 `job.status` 的地方，成功路径也必须走它。只在异常分支写状态的话，任务会永远停在 `running`——而 `running` 不会被再次抢占，于是队列看起来"处理过了"，其实什么都没记下（这个 bug 真发生过，现在有测试钉着）。
- **`attempts` 在抢占时就 +1**：抢占即一次尝试，进程被杀也要算数，否则一个能稳定搞崩 worker 的任务会被无限重试。`status IN ('queued','failed')` 且 `attempts < max_attempts` 才可抢占；启动时把 `locked_at` 超时（默认 15 分钟）的 `running` 重置为 `queued`。
- **确定性失败进死信，偶发失败才退避**：文件不存在、超过字节上限、命中哈希墓地、类型不放行、授权失效、来源类型没有实现——这些重试只会得到同样结论，一律 `dead` 并写 `dead_reason`；其余异常按 `jobs.backoff_seconds` 退避重试，到 `max_attempts` 转死信。
- **幂等护栏**：处理前先查这条消息有没有内容项，有就直接成功返回。崩溃恢复会把任务重置为排队并重跑，护栏保证不会产生第二份内容。
- **一份字节只有一行 `media`**：`media.sha256` 唯一；多个内容项各自通过 `content_media` 引用同一行（内容项与媒体是**多对多**）。`media.scan_status` 是**字节**的属性，复用时不因某次"扩展名骗人"就改写它；本次判定落在 `content_item.status` 上。
- **隔离区是独立的存储后端行**：扫描不通过的字节写进 `quarantine_root` 并记 `storage_backend_id = local-quarantine`、`quarantine_key` 非空。`media.object_key` 因此**是相对所属后端根目录的键**——判断"这份字节在主存储还是隔离区"要看 `quarantine_key`，不能只看 `object_key`。
- **`text_content` 存原文、`text_search` 刻意留空**：`text_search` 的契约是"应用层分词结果，空格连接"（DESIGN §5.1）；本切片没有分词器，塞进未分词原文只会让 `content_word` 里出现一个巨大的单 token。词级检索随检索切片一起接，届时**要覆盖这一列**（不是只在插入时写）。trigram 索引已经在 `text_content` 上工作，所以 ≥3 字符的子串检索今天是可用的；<3 字符命中 0 行（§5.1 已实测）。
- **`pc import` 必须把目录持久化进 `source.config["roots"]`**：worker 在另一个时刻取字节，它只读那里；目录只活在命令行参数里的话，导入能成功而处理阶段拿不到引用（无法续跑）。`--dry-run` 除外——它什么都不写。
- **连接器的根可以是一个文件**：`pc upload a.jpg` / `pc import a.jpg` 时根就是那个文件本身，`discover()` 必须只产出它（`relative` 取文件名）。这条曾经是缺陷：`Path("a.jpg").rglob("*")` 是空的，于是"导入单个文件"静默地什么都不做。修法是让文件根产出它自己，而**不是**把根换成父目录——那会把整个目录都导进来，是另一种误伤。单文件路径照样进 `source.config["roots"]`。
- **内容身份用的就是内容层那两个哈希**：媒体取字节的 `sha256`（等于 `media.sha256`），文本取 `sha256(normalize_text(原文))`（等于 `content_item.content_hash`）。两处同一套判据，只差空白的文本文件因此算同一条，而不是先分裂成两条再靠 `dup_group_id` 归组。**0 字节的文件一律不产生消息**——所有空文件的哈希是同一个值，拿它当身份会让它们互相顶掉；这不会丢内容，媒体的同名侧车文本本身就是文本文件、会被单独发现（有测试钉着这个坑）。
- **`pc upload` 的媒体引用是绝对路径，所以默认立刻处理**：`media_refs` 存的是文件路径，用户上传完把原文件删掉/移走，任务就只能死信。`pc upload` 因此默认接着跑 worker（`--no-process` 才是分开跑），与 `pc import` 的默认值刻意相反。
- **`message.origin_note` 是调用方注解，不是平台信封的一部分**：它回答"这批内容我从哪儿弄来的"，整批套用同一个值；平台的"转自哪里"走 `forward_chain`。备注**不进 `raw_payload`**（那是原始载荷的自证哈希），也不用去动 `SourceMessage`。
- 规范化（`pc.domain.normalize_text`）只做**无损**变换（NFKC + 空白折叠），且必须幂等：它的结果进 `content_hash`，是文本精确去重的分组键，不确定就会让同一份内容分裂成两份。

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

**当前不包含**（不要按「补个小功能」处理，它们各自需要先落一个模块）：**检索（FTS5 查询层与分词）**、tag 与打标、链接自动抓取、AI 外发、向量检索、多租户、Meilisearch / MinIO、Web 界面、**缩略图与感知哈希**、worker 的常驻服务化（`pc work --loop` 有了，缺的是 systemd / 计划任务那种托管）、**备份的定时调度**（手动入口 `pc db backup` 已经有了，缺的是每日任务）、**匿名提报入口与第二个连接器（DESIGN v3.1 已明确后置到阶段 2）**。测试套件已覆盖数据库适配层、SQLite 行为契约、迁移器、配置段与路径解析、连接器/存储/扫描器、服务层与 CLI 端到端、设置层、schema 红线与出网守卫；**没有检索与打标的测试**（因为那两块还没有实现）。
