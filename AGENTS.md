---
name: post-collector
description: "项目开发入口：当前状态、代码导航、命令与实现约束。操作手册见 README.md，设计事实源为 DESIGN.md。"
---

# AGENTS.md

## 当前状态与工作方式

post-collector 使用 Python、SQLite 与本地目录存储，归档已授权来源的内容。当前可用链路为本地导入/上传、扫描、去重、落库、CLI 检索；配置解析、迁移、备份、索引重建与健康接口已接线。schema 为 35 张 STRICT 表、2 个 FTS5 索引、6 个同步触发器，迁移到 0009；端口共 13 个。

打标、界面与平台连接器的交付范围见 DESIGN.md §9。阶段 1 的权限范围为 root 与邀请制用户，匿名提报入口属于阶段 2。

修改实现前读取 DESIGN.md 对应章节。改变设计契约时，同时更新该章节和附录 C。改代码必须同步 AGENTS.md 与 README.md；两份入口文档以当前事实、操作和约束为主，历史方案、实测过程与设计理由放在 DESIGN.md，迁移细节放在 migrations/README.md。用肯定句描述当前路径，必要的安全禁令保留明确表述。

## 开发命令

在仓库根目录使用项目环境：

| 操作 | 命令 |
| --- | --- |
| 安装运行时和开发依赖 | `uv sync`；提交 `uv.lock` |
| 只安装运行时依赖 | `uv sync --no-dev` |
| 体检 | `uv run pc doctor` |
| 静态检查 | `uv run ruff check src`；改过测试时 `uv run ruff check src tests` |
| 语法检查 | `python -m compileall -q src` |
| 测试 | `uv run pytest` |
| 检索性能验收 | `uv run python tools/bench_search.py --scale 100000 --assert` |
| 本地 API | `uv run uvicorn pc.api.app:create_app --factory --host 127.0.0.1 --port 8080` |
| 应用迁移 / 结构校验 | `uv run pc db apply` / `uv run pc db status` |
| 重算派生数据与索引 | `uv run pc db reindex --recompute-derived` |
| 查看 / 播种设置 | `uv run pc config show --explain` / `uv run pc config seed` |

测试在临时目录运行。最近完整结果为 818 通过、3 条 Windows 符号链接权限跳过。新接口测试按库的弃用提示选用客户端。API 提供 `/`、`/healthz`、`/api/v1/meta`、`/docs`。

doctor 报告库、索引与运行时目录的状态和配置来源；各段不可达时仍完成体检。完整 CLI 参数、备份和升级步骤见 README.md。

## 代码导航

| 路径 | 职责 |
| --- | --- |
| `DESIGN.md` | 架构、数据模型、安全、算法、路线图及变更记录 |
| `README.md` | 安装、导入、检索、配置、备份与升级 |
| `migrations/sqlite/` | schema 权威定义，连续编号的迁移 |
| `migrations/README.md` | 迁移执行与表重建规则 |
| `config/settings-registry.yaml` | 设置项声明 |
| `config/app.yaml` | 部署与安全基线、待播种默认值 |
| `src/pc/domain.py` | 状态、跨层结构、规范化、分词与帖签名 |
| `src/pc/ports/` | 13 个 Protocol 端口 |
| `src/pc/config.py` | 严格校验、四层解析、设置变更闸门 |
| `src/pc/cli.py` | doctor/config/db/source/import/upload/work/item/search 接线 |
| `src/pc/adapters/sqlite.py` | 路径、连接、迁移、备份、设置与派生数据重算 |
| `src/pc/adapters/fts_search.py` | 索引维护、部件解释及批量组装 |
| `src/pc/adapters/post_search.py` | 帖查询、候选、可见性、折叠与游标 |
| `src/pc/adapters/safe_files.py` | roots 边界、链接/reparse 检查与句柄校验 |
| `src/pc/adapters/local_files.py` | 本地连接器，两套身份策略 |
| `src/pc/adapters/registry.py` | 按 source.kind 构造连接器与凭证边界 |
| `src/pc/adapters/local_storage.py` | 内容寻址、原子写入与幂等存储 |
| `src/pc/adapters/magic_scan.py` | 类型白名单、字节限额、扫描判定 |
| `src/pc/services/` | 来源、摄入、worker、检索业务规则 |
| `src/pc/api/` | 参数校验、鉴权与路由 |
| `tests/` | 单元、端到端、schema、安全与性能工具回归 |

## 安全与分层

- 当前阶段禁止引入第三方 AI 调用路径。未来外发统一经过内部网关并逐条写 `egress_log`，每次执行完整资格检查；契约见 DESIGN §6.3。
- `inbox_item` 审查通过前仅存在于隔离区，禁止写入 message/content_item、打标或索引；启用入口时用断言级测试守住。
- 外部依赖经过 ports 或相应适配器。数据库连接统一使用 `pc.adapters.sqlite`，连接生命周期用 `db.open_db()`。
- 路由做参数校验与鉴权，业务规则进入 services。模型名与 prompt 由配置提供。
- 外部内容只放在数据位置。模型输出经确定性代码校验，写操作由业务层执行；缺模型时跳过模型阶段。
- `StorageManager.plan` 与 `apply` 分离，计划默认 dry-run，确认后执行。
- 媒体引用使用对象键，后端解析存储路径。备份包括库文件与媒体字节，放在不同物理介质。

## 来源与处理契约

- Connector 用 capabilities 声明 pull/push/media_fetch/requires_credential。拉取实现 list_new，推送实现 parse_event，另一入口抛 NotImplementedError。
- export/manual 共用 LocalFilesConnector。export 身份为根标签/相对路径，时间取 mtime；manual 身份为媒体与规范化文本哈希，sent_at 留空。两者以有效授权记录采集；平台凭证实现以 credential_secret 与系统密钥库为前置。
- 来源及授权由服务层读好注入连接器，解密边界在 registry.py。摄入事务、worker 收集前和落账事务使用最新授权。
- 新 roots 与媒体引用保存绝对路径，使用 abspath 保留链接证据。旧相对配置或引用明确报错，提示更新或重导入。
- 所有本地读取经 safe_files.open_scoped：限定 roots、拒绝链接/junction/reparse、校验实际句柄。文件根只发现该文件，额外允许同目录同名文本侧车。媒体先检查句柄大小，再读取上限 + 1 字节。
- 扫描 fail closed。passed=False 的字节进入独立隔离后端并建立复核；SVG、扩展名与魔数不符的复核规则见 scanner。media 的后端由 storage_backend_id/quarantine_key 判断。
- 来源路径写入 source.config，dry-run 保持只读。空批次且游标前进时继续；非空且不前进报错，空且不前进结束。
- 文本 normalize_text 为 NFKC 与空白折叠，结果幂等。文本墓地为 alg=content_hash，媒体为 alg=sha256；任一有效墓地命中阻止整帖落库。
- worker 抢占时 attempts +1；正常完成经 _finalize 落账。超时 running 尚有次数则 queued，耗尽则 dead，写原因、finished_at、审计并清锁。确定性失败死信，偶发异常退避。
- 处理幂等；media.sha256 唯一，content_media 为多对多。media.scan_status 是字节属性，本次扫描判定落在内容项上。
- message.origin_note 为调用方注解，raw_payload 保存原始载荷。search_extra 与文本索引在落库和批量重算中保持同值。

## 检索契约

- FTS 文档为内容项，返回、分页和预算单位为帖。每条 hit 带 message_id 和全部可见 parts；关键词和标签 AND 可跨可见部件。
- 默认可见部件为未软删、active、nsfw_level <=0；消息软删排除整帖。隐藏部件不参与命中、类型或输出。
- image/text 等按可见构成选纯类型，mixed 为至少两种类型，多个类型 OR。
- content_item 的哈希归组为存储去重；检索折叠使用 post 签名。代表帖须签名相同且满足完整查询；签名缺失保留显示。
- post 保存稳定构成、时间和签名，可见性实时计算。pipeline._upsert_post 与 sqlite.sync_post_rows 保持同值；type_mask 是按 ITEM_TYPES 位序的 distinct 位值之和。签名在 Python 计算，全量归组按 SQLite 参数上限分批。
- CJK 1~2 字及拉丁词走 content_word，CJK ≥3 字走 content_fts。混查询合并候选并逐词求 AND。用户词转换为带引号短语并转义，纯标点/emoji 跳过；MATCH 语法仅在适配器构造。
- 相关度为命中部件最小 bm25；按帖去重后用 500/2000 预算，截断标 incomplete。带标签时预算为 2000，相关度页凑齐可提前结束。候选不足时扩展读取并校验未读分数边界。
- 关键词 total=None；纯过滤计数到 5000，标签 usage_count 仅作估计，打标模块负责维护。has_more 描述下一页。
- 时间排序用 v2 键集游标，相关度用 page。游标编码时间字段、可空时间与 message_id；旧游标要求重查。时间字段同时控制范围、排序与游标，sent 空值排最后并由范围排除。
- message.sent_at 为事实源，content_item.sent_at/post.sent_at 为派生副本；重算和 doctor 对齐。使用帖的单表时间索引排序。
- 标签查询按估算扫描量在时间索引与最小标签集驱动间切换。保持 SQL 文本与参数顺序一致；0008/0009 的覆盖索引与连接顺序参与性能契约。
- 查询共享读快照；组装固定三条 SQL：主查询、标签批量、媒体批量。命中解释使用已取列；语句计数测试守住批量组装。
- search_extra 顺序见 domain.search_extra_value，落库与重算两处同步修改。自由文本按 FILE_NAME_LIMIT/SEARCH_EXTRA_TEXT_LIMIT 截断。文件名由 filename_from_ref 保存 basename，只用于展示和索引；老行在单媒体引用时回填。
- CLI 表格按显示宽度对齐，各列定义宽度上限；加列同时更新 ITEM_LIST_MAX_WIDTHS/SOURCE_LIST_MAX_WIDTHS/SEARCH_MAX_WIDTHS，JSON 保留完整值。
- 检索路径改动必须跑十万条真实基准：50 帖/页、热缓存、≤4 并发，P50 <150 ms、P99 <500 ms。超预算在同一改动中修复或记录解释。实测见 DESIGN §3.7；缩略图预生成，数据库调用走同步执行边界。

## 数据、迁移与配置

- 迁移按序号连续追加，已应用文件保持原样。新增版本更新 test_schema_integration.EXPECTED_MIGRATION_NAMES；新增表更新 sqlite.EXPECTED_TABLES。
- 每文件显式事务，sqlite3.complete_statement 切语句，成功后记账。迁移文件的 PRAGMA 由迁移器拒绝；连接统一设置 WAL、foreign_keys=ON、busy_timeout=5000、synchronous=NORMAL、temp_store=MEMORY。
- CHECK 枚举调整用表重建，文件头 `-- pc:foreign_keys=off`，提交前 foreign_key_check、结束恢复外键。临时表以 _rebuild 结尾，原表 DDL 保持对应；细节见 migrations/README.md。
- 仓库配置、迁移、.env 相对项目根解析。数据库与运行时目录位置优先级见 README；PC__DB__PATH 单独移动库文件。uv tool 安装包的迁移资源打包属于待交付项。
- 指纹剔除整行注释后计算 SHA-256，漂移仅报告。时间为 ISO-8601 UTC 毫秒，updated_at 由应用写；UUID 由应用生成。布尔为 INTEGER，JSON 为经 json_valid 校验的 TEXT。
- 设置语义在 config.py，适配器保证 setting/setting_change 的事务与审计。解析顺序 env > db > yaml > default，播种仅补缺失键，环境覆盖只影响生效值。
- 注册表 enum 全部加引号，未知键、重复键、越界或类型错误直接报错。app.yaml 的部署段为 db/paths/jobs/ingest/security/models。
- 设置变更通过 write_setting_change，保留审计、回滚与危险项确认闸门；当前 CLI 提供查看与播种。源码与配置默认值以注册表和校验测试为准。

## 按任务读取设计

| 任务 | DESIGN 章节 |
| --- | --- |
| 来源与连接器 | §1.3、§9.1 |
| schema、迁移、删除、去重 | §3，尤其 §3.8 |
| worker、打标与模型供给 | §4 |
| 检索 | §5，预算 §5.6 |
| 权限、配置与外发 | §6 |
| 备份、容量与保留 | §7 |
| 交付范围与进程模型 | §9、§10 |

文档与注释用中文，标识符和提交信息用英文。提交前检查命令、路径、状态与数量；运行适合改动的检查。说明文档保留当前操作和约束，实测表及历史复盘通过章节链接按需读取。
