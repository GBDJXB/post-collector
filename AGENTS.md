---
name: post-collector
description: "项目开发入口：当前状态、代码导航、命令与实现约束。操作手册见 README.md，设计事实源为 DESIGN.md。"
---

# AGENTS.md

## 当前状态与工作方式

post-collector 使用 Python、SQLite 与本地目录存储，归档已授权来源的内容。当前可用链路为本地导入、CLI 与 Web 上传、扫描、去重、落库、root 人工打标、受邀账号管理与独立远程认证、CLI 与本机 Web 检索和内容查看；配置解析、迁移、备份、索引重建与健康接口已接线。schema 为 35 张 STRICT 表、2 个 FTS5 索引、6 个同步触发器，迁移到 0009；端口共 13 个。

模型打标、敏感度分类、邀请制账户与平台连接器的交付范围见 DESIGN.md §9。本机 Web 使用临时 root 会话，持久 root 账户与远程业务按 §6.1 后续交付；受邀密码认证已实现；匿名提报入口属于阶段 2。

修改实现前读取 DESIGN.md 对应章节。改变设计契约时，同时更新该章节和附录 C。改代码必须同步 AGENTS.md 与 README.md；两份入口文档以当前事实、操作和约束为主，历史方案、实测过程与设计理由放在 DESIGN.md，迁移细节放在 migrations/README.md。用肯定句描述当前路径，必要的安全禁令保留明确表述。

## 开发命令

在仓库根目录使用项目环境：

| 操作 | 命令 |
| --- | --- |
| 安装运行时和开发依赖 | `uv sync`；提交 `uv.lock` |
| 只安装运行时依赖 | `uv sync --no-dev` |
| 体检 | `uv run pc doctor` |
| 静态检查 | `uv run ruff check src`；改过测试时 `uv run ruff check src tests` |
| 前端构建与组件测试 | 在 frontend 中运行 npm ci、npm test、npm run build；提交锁文件和构建产物 |
| 语法检查 | `python -m compileall -q src` |
| 测试 | `uv run pytest` |
| 检索性能验收 | `uv run python tools/bench_search.py --scale 100000 --assert` |
| 本机 Web / API | `uv run pc serve` |
| 应用迁移 / 结构校验 | `uv run pc db apply` / `uv run pc db status` |
| 重算派生数据与索引 | `uv run pc db reindex --recompute-derived` |
| 查看 / 播种设置 | `uv run pc config show --explain` / `uv run pc config seed` |

测试在临时目录运行。最近完整结果为 1009 通过、6 条 Windows 符号链接权限跳过；前端组件测试 7 项通过。新接口测试按库的弃用提示选用客户端。API 提供 `/`、`/healthz`、`/api/v1/meta`、`/docs`。

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
| `src/pc/cli.py` | doctor/config/db/source/import/upload/work/item/search/tag/render/show/open/reveal/user/serve 接线 |
| `src/pc/adapters/sqlite.py` | 路径、连接、迁移、备份、设置与派生数据重算 |
| `src/pc/services/tagging.py` | 标签应用、撤销、建议确认、审计与计数 |
| `src/pc/adapters/tag_normalizer.py` | 规范名、别名解析与有效标签条件 |
| `src/pc/services/viewing.py` | 详情入口、内容类型与复制前置检查 |
| `src/pc/services/desktop_view.py` | CLI 与 Web 共用的文件准备、复查和系统操作 |
| `frontend/` | React 页面、React Router 路由、组件测试与 npm 锁文件 |
| `src/pc/services/uploads.py` | 浏览器提交、幂等身份与本次消息定向处理 |
| `src/pc/services/runtime.py` | CLI 与 HTTP 共用 worker 构造 |
| `src/pc/adapters/upload_files.py` | 有界暂存、文件名与路径检查、终态清理 |
| `src/pc/web/` | React/Vite 构建的单入口 HTML 和本地 JS/CSS 资源 |
| `src/pc/adapters/content_detail.py` | ID 详情查询、读快照与批量组装 |
| `src/pc/adapters/view_files.py` | 有界媒体读取、哈希验证、查看副本与静态 HTML |
| `src/pc/adapters/windows_view.py` | 默认程序、文件定位与 Unicode 文本/文件剪贴板 |
| `src/pc/view_presentation.py` | 终端正文控制字符过滤 |
| `src/pc/presentation.py` | 纯数据表格渲染、中文宽度与终端布局 |
| `src/pc/adapters/fts_search.py` | 索引维护、部件解释及批量组装 |
| `src/pc/adapters/post_search.py` | 帖查询、候选、可见性、折叠与游标 |
| `src/pc/adapters/safe_files.py` | roots 边界、链接/reparse 检查与句柄校验 |
| `src/pc/adapters/local_files.py` | 本地连接器，两套身份策略 |
| `src/pc/adapters/registry.py` | 按 source.kind 构造连接器与凭证边界 |
| `src/pc/adapters/local_storage.py` | 内容寻址、原子写入与幂等存储 |
| `src/pc/adapters/magic_scan.py` | 类型白名单、字节限额、扫描判定 |
| `src/pc/services/` | 来源、摄入、worker、检索业务规则 |
| `src/pc/adapters/passwords.py` | Argon2id 哈希与确定性验证结果 |
| `src/pc/services/accounts.py` | 受邀账号、Argon2id、审计、会话版本与封闭身份目录 |
| `src/pc/api/remote_auth.py` | 独立远程登录、CSRF、有界限流与身份 JSON |
| `src/pc/api/` | 本机会话、参数校验、媒体与业务路由 |
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

## 标签与输出契约

- 人工生产者生成 `ClassificationResult(source=human)`；统一服务当前接受 human/suggested，未来模型适配器只负责解析结果。普通名称由 root 自定义，namespace 默认 general，rating/source/system 留给对应组件。
- 标签写操作验证目标与 root 身份，关联、分类记录、审计、计数和派生索引同事务完成。本地 CLI 沿用 upload 的操作系统权限边界，审计标记 local_root；显式 actor_id 必须为有效 root。
- `tag_assignment` 的 content/tag/source 唯一；撤销与恢复复用行、保留审计。建议确认生成 human 关联、撤销 suggested，证据保留建议 ID 与模型来源；拒绝只撤销建议。
- 有效标签为未撤销的 human/system/auto/merged 关联，标签状态 active/deprecated。suggested/pending 排除于过滤、输出、关键词索引及计数；usage_count 按不同内容项计数。
- `tag_synonym` 为别名事实源，服务同步 tag.aliases。规范名与别名交叉冲突在写事务内拒绝；名称采用 NFKC、casefold、空白/下划线统一，规范化后 1~255 字符，拒绝控制字符。
- `SearchIndex.index_many` 参与调用方事务，空集合保持只读；标签刷新保持帖签名原值，维护重算继续分批提交。
- 查询服务返回业务数据，CLI 序列化或渲染；默认表格与 `pc render table` 共用 presentation，渲染器只读输入。JSON 保留完整值，stdout 为单一 JSON，诊断进入 stderr。CLI JSON 统一使用 ensure_ascii=True，兼容 Windows PowerShell 5.1 默认 ASCII 管道；人工展示保留 Unicode。
- 结果类型 search/tags/assignments/aliases，版本 result_version=1。保持检索现有分页字段；渲染层控制列宽与总宽，前端全局排序经查询端执行。

## 内容查看契约

- `show/open/reveal` 每次接受一个消息或内容项 ID，按 ordinal/content_id 和媒体 ordinal/media_id/role 排序；详情服务返回 content v1，JSON 完整且只读。
- 当前默认可见性与检索一致；展示文本与图片。图片说明正文算混合内容；混合 `--copy` 在缓存与系统调用前报错，`--json` 与 `--copy` 互斥。
- `show` 当前显示纯文本，过滤终端控制字符；终端图片按 DESIGN §5.1 的 DST 验证结果暂缓。系统打开、定位与复制支持 Windows。
- 媒体按 backend_id 读取本地后端绝对 root，验证 canonical 对象键、大小、哈希与魔数，拒绝删除/隔离/未通过扫描媒体。单次媒体总量上限 256 MiB；缓存不替代实时可见性检查。
- 查看副本放在 data_root/view-cache，独立复制、原子替换并验证链接/reparse 与硬链接。静态 HTML 转义正文，仅引用生成的相对图片名；HEIC 混合内容报错。
- 剪贴板文本为 CF_UNICODETEXT，图片为 CF_HDROP，立即渲染且文件在命令退出后保留。当前手动清理查看缓存，删除归档时须同时清理对应副本；后续容量管理接入该目录。

## 本机 Web 契约

- `pc serve` 校验已有库结构后，单进程监听 `127.0.0.1`，关闭代理头信任；Web 上传立即处理本次消息，其他后台任务由 `pc work` 执行。业务运行时从配置解析后注入应用，直接调用工厂提供页面、探针和接口定义。
- 一次性授权令牌在 fragment 中传入，10 分钟有效；交换后清除 fragment。会话驻内存，Cookie 为 HttpOnly/SameSite=Strict，8 小时绝对到期、30 分钟空闲到期，退出及进程重启立即失效。
- 业务请求验证会话、本机对端与当前 Host；写操作验证 Origin 与 CSRF。身份由服务器授予本机 root，客户端不能指定 actor_id。标签写入复用 local_root 审计与事务。
- 查询与详情沿用既有服务和版本化结果，默认 active/safe。数据库及文件操作在同步路由执行边界运行，连接按请求关闭。媒体接口检查可见关联并复用安全读取，存储与缓存目录保持私有。
- 页面使用 React 文本节点展示归档文本，以内容项/媒体 ID 构造图片地址；同源资源受 CSP 约束。系统动作调用共享服务，不执行 CLI 子进程；混合复制在生成文件前拒绝，部分失败响应保留 copied 状态。

- 前端源码在 frontend，使用 npm ci/npm test/npm run build；构建产物 src/pc/web 随 wheel 分发并提交。运行服务无需 Node，开发构建要求 Node 22.12 以上。路由 /、/add、/manage 共用 HTML，刷新可直接访问；会话交换清除 fragment 并保留路径与查询。
- POST /api/v1/uploads 接受 text/note/images multipart，身份为会话 root，来源固定 manual。鉴权在请求体读取前完成，按实际字节校验单图、256 MiB 媒体总量与 258 MiB 请求上限，最多 19 张；文本用配置上限，备注 4096 字符。
- 暂存位于 data_root/upload-staging/UUID，按选择顺序存独立子目录；来源 roots 持久化以支持 pc work 接续。成功或死信清理、可重试保留，清理仅允许该根的 UUID 直接子目录；异常残留先保留并人工对照任务检查。
- worker.run_message 只恢复与抢占指定消息任务，执行复用原有扫描、授权、重试和审计。单图/纯文本身份与 CLI 兼容，多图身份包含有序媒体哈希与规范化文本哈希；文件名和备注不参与身份。

## 受邀账号与远程认证契约

- pc user create/list/disable/enable/reset-password 仅经本机 CLI 管理 app_user；创建固定 tier=1；先只读校验用户名与重名，再隐藏输入两次密码，写入事务再次查重。用户名规范化为小写 ASCII、3~64 位，root 保留；密码 15~128 字符且保持原样。Argon2id 参数为 19 MiB、2 次迭代、并行度 1。
- pc serve --mode remote --port 8081 --public-origin https://archive.example.com 创建独立认证应用，只监听回环。HTTPS 代理保留精确 Host，仅转发此端口；后端检查 socket/Host/Origin，关闭转发头信任。远程应用不挂载本机资源、归档业务或 OpenAPI。
- 远程入口仅有 /、/api/v1/session、/api/v1/logout、/healthz。登录接收有界 URL 编码表单，成功返回 identity_version=1 JSON。受邀策略固定为全库 safe 图片及含图片混合消息，业务 executable=false；root 及其他 tier 均拒绝远程认证。
- 本机临时 root 保留一次性令牌、现有业务和 csrf 字段，新增身份 JSON 与受保护 /identity。两个应用的 Cookie、凭据与会话独立；远程本机 socket 不构成 root 证明。操作目录仅作说明，不能执行 CLI。
- 远程 Cookie 使用 Secure/HttpOnly/SameSite=Strict/Path=/，登录表单 CSRF 5 分钟单次有效，登录轮换会话。会话 8 小时绝对期限、30 分钟空闲期限，驻内存；每请求检查 tier、状态、密码与账号版本。管理操作推进 updated_at，停用后启用也不能恢复旧会话。
- 登录实际字节上限 8 KiB；用户名每分钟 5 次、全局每分钟 30 次，有界驻内存状态。认证错误统一，凭据禁止进日志与账号结果。权限与账号认证复用现有表，本次无迁移、无通用 RBAC 编辑、无实际远程内容接口。

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
| 检索与内容查看 | §5，预算 §5.6；文件安全 §6.5；目录 §10.2 |
| 权限、配置与外发 | §6 |
| 备份、容量与保留 | §7 |
| 交付范围与进程模型 | §9、§10 |

文档与注释用中文，标识符和提交信息用英文。提交前检查命令、路径、状态与数量；运行适合改动的检查。说明文档保留当前操作和约束，实测表及历史复盘通过章节链接按需读取。
