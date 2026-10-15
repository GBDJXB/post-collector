---
name: post-collector
description: "项目开发入口：当前实现、命令与开发约束。操作见 README.md，初版契约见 DESIGN.md。"
---

# AGENTS.md

## 工作方式与当前状态

post-collector 使用 Python、SQLite 和本地目录归档已授权来源的内容。当前已实现本地导入、CLI 与 Web 有序文本/图片/链接上传、Web 可选原子创建组合、扫描隔离、去重落库、root 人工打标、CLI 与本机 Web 检索和内容查看，独立有序组合与组合自身标签，以及配置解析、迁移、备份、索引重建、健康接口、受邀账号管理和独立远程认证。

远程认证应用目前只提供登录、退出和身份 JSON，业务操作标记为 `executable=false`。初版受邀用户的权限限定为检索和下载；当前身份范围为 safe 图片及含图片消息，最终可见范围与下载契约须在实现前讨论并更新 DESIGN。本机 Web 模型 API 标签建议已实现，用户已手动验证单素材真实 API 调用，组合路径尚未手动验收；自然语言检索与指定平台监听尚未实现，自然语言不在本轮范围。

- `DESIGN.md` 定义初版 release 必须交付的核心需求与契约，待讨论细节须明确标注。
- `FUTURE_DESIGN.md` 保存可选、暂缓和撤回方案，不构成交付承诺或核心前置条件。
- `DESIGN_HISTORY.md` 保存历史设计与实测记录，当前契约以 DESIGN 为准。
- `README.md` 只介绍已实现功能及操作；本文件说明开发入口与约束；迁移规则见 `migrations/README.md`。

修改实现前读取对应设计章节。改变设计契约时更新该章节与附录 C。命令、接口、交付状态或项目约定发生变化时，只更新受影响的文档：操作与已实现能力写入 README，开发约束写入 AGENTS，设计契约写入 DESIGN；不要求每次代码修改都同时改 README 与 AGENTS。拼写修正、内部重构和局部缺陷修复未改变上述内容时，无需文档改动。只做文案调整时不修改代码、schema 或已应用迁移；已有未启用表和端口不构成新增功能承诺。匿名投稿当前不做，受邀用户不投稿、不建议标签、不协助审核；模型标签调用固定使用官方 API，未来模型部署变更与容量管理方案须先确定契约。

文档与注释用中文，标识符和提交信息用英文。写作使用具体事实与直接表达，避免夸张、重复总结、套话和无依据的功能承诺；必要安全禁令保留明确表述。提交前核对命令、路径、状态与数量，运行与改动有关的检查。

### 按改动触发验证

- 检索路径修改必须运行十万条真实业务数据基准，命令与预算见下文；单元测试不替代性能验收。
- 认证、会话或授权修改必须运行相关权限边界测试，覆盖受影响的允许与拒绝路径；按改动核对本机/远程隔离、root/tier 限制、Host/Origin/CSRF、会话失效和账号状态。现有入口为 `tests/test_accounts_auth.py` 与 `tests/test_web_app.py`，新增边界补充对应断言。
- 生成资源或其源码、生成配置修改必须检查源码与构建产物一致性。前端按锁文件安装依赖、运行组件测试并重新构建，比较生成文件清单与内容，确认 `src/pc/web` 没有缺失、陈旧或手工修改后无法重现的资源；构建成功本身不证明一致。
- 反复执行的检查步骤优先封装为 `tools/` 下的小脚本，复用现有入口，不创建另一套业务逻辑。输出必须包含检查对象、实际结果、通过/失败/未执行状态及错误原因；失败返回非零退出码。
- 验证结论须有行为断言、结果比较或性能指标支持，不能仅凭进程退出码证明产品功能正确；未运行或受阻的检查明确记录，不计为通过。纯文档修改核对差异、链接、命令与约束一致性，无需触发无关业务测试。

## 开发命令

在仓库根目录使用项目环境：

| 操作 | 命令 |
| --- | --- |
| 安装全部 / 运行时依赖 | `uv sync` / `uv sync --no-dev`；提交 `uv.lock` |
| 体检 | `uv run pc doctor` |
| 静态检查 | `uv run ruff check src`；改过测试时检查 `src tests` |
| 语法检查 | `uv run python -m compileall -q src` |
| 测试 | `uv run pytest`；测试在临时目录运行 |
| 检索性能验收 | 帖级 v1：`uv run python tools/bench_search.py --scale 100000 --assert`；Web v2：`uv run python tools/bench_web_search.py --scale 100000 --compositions 1000 --assert` |
| 前端组件测试与构建 | 在 `frontend` 运行 `npm ci`、`npm test`、`npm run build` |
| 本机 Web / API | `uv run pc serve` |
| 应用迁移 / 结构校验 | `uv run pc db apply` / `uv run pc db status` |
| 重算派生数据与索引 | `uv run pc db reindex --recompute-derived` |
| 查看 / 播种设置 | `uv run pc config show --explain` / `uv run pc config seed` |

前端开发构建要求 Node 22.12 以上，提交 npm 锁文件和 `src/pc/web` 构建产物；运行服务无需 Node。本机 API 提供 `/`、`/healthz`、`/api/v1/meta`、`/docs`。doctor 各段不可达时仍完成其余体检；完整参数和维护操作见 README。新接口测试按依赖的弃用提示选用客户端。

## 代码导航

| 路径 | 职责 |
| --- | --- |
| `migrations/sqlite/`、`migrations/README.md` | schema 权威定义与迁移执行规则 |
| `config/settings-registry.yaml`、`config/app.yaml` | 设置声明、部署基线与待播种默认值 |
| `src/pc/domain.py`、`src/pc/ports/` | 跨层结构、规范化、分词、帖签名与 Protocol |
| `src/pc/config.py`、`src/pc/cli.py` | 配置解析、设置闸门与 CLI 接线 |
| `src/pc/adapters/sqlite.py` | 连接、迁移、备份、设置与派生数据重算 |
| `src/pc/services/` | 来源、摄入、worker、标签、查看、上传、账号与检索业务 |
| `src/pc/adapters/local_files.py`、`registry.py` | 本地连接器、来源构造与凭证边界 |
| `src/pc/adapters/safe_files.py`、`local_storage.py`、`magic_scan.py` | 安全读取、内容寻址存储与扫描 |
| `src/pc/adapters/fts_search.py`、`post_search.py` | 索引、候选、帖可见性、折叠与游标 |
| `src/pc/adapters/tag_normalizer.py` | 标签规范名、别名与有效性 |
| `src/pc/adapters/content_detail.py`、`view_files.py`、`windows_view.py` | 详情、媒体验证、查看副本与 Windows 操作 |
| `src/pc/adapters/upload_files.py`、`passwords.py` | 有界暂存与 Argon2id |
| `src/pc/presentation.py`、`view_presentation.py` | 表格布局、中文宽度与终端控制字符过滤 |
| `src/pc/api/`、`src/pc/api/remote_auth.py` | 本机业务路由与独立远程认证 |
| `frontend/`、`src/pc/web/` | React 源码、组件测试与随 wheel 分发的构建资源 |
| `tests/`、`tools/bench_search.py` | 回归与真实检索基准 |

## 安全与分层

- 本机 Web 模型标签建议已实现，唯一第三方 AI 例外见 DESIGN §4.5/§6.3；其他路径仍禁止。模型只能选择 DB 已有标签且至少一个，不创建 tag。外发须经审计网关、最新权限/授权/配置复查及 root 逐次确认；外部内容放在数据位置，模型输出由确定性代码校验，写操作由业务层执行。
- 外部依赖经过端口或对应适配器。数据库连接统一使用 `pc.adapters.sqlite` 与 `db.open_db()` 管理生命周期。当前服务层仍有 SQLite SQL；更换数据库需要核对实际耦合。
- 路由校验参数和身份，业务规则进入 services。同步数据库及文件操作在同步路由执行边界运行，连接按请求关闭。单进程仍可能存在多线程、多连接，写一致性由事务和锁保证。
- 媒体引用保存对象键，由后端解析存储路径。备份包含库文件与媒体字节，放在不同物理介质。
- 容量管理提供建议，由管理员决定；不引入自动删除内容的路径。保留 `StorageManager.plan/apply` 的计划与执行边界，默认 dry-run，确认后执行。

## 来源、摄入与任务

- Connector 用 capabilities 声明 pull/push/media_fetch/requires_credential；实现支持的入口，另一入口抛 `NotImplementedError`。
- export/manual 共用 LocalFilesConnector。export 身份为根标签/相对路径，时间取 mtime；manual 身份为媒体与规范化文本哈希，sent_at 留空。采集须有有效授权。
- 来源和授权由服务层注入，凭证解密边界在 registry。摄入事务、worker 收集前和落账事务使用最新授权。
- roots 和媒体引用保存绝对路径，使用 abspath 保留链接证据；旧相对路径明确报错。读取经 `safe_files.open_scoped` 限定 roots、拒绝链接/junction/reparse、验证实际句柄。文件根仅发现该文件及同目录同名文本侧车；媒体先检查大小，再读取上限 + 1 字节。
- 扫描 fail closed：未通过字节进入独立隔离后端并建立复核。media 后端由 storage_backend_id/quarantine_key 判断；scan_status 是字节属性，本次扫描判定落在内容项上。
- source.config 持久化来源路径，dry-run 保持只读。空批次且游标前进时继续；非空且不前进报错，空且不前进结束。
- normalize_text 为幂等 NFKC 与空白折叠；旧 hash_tombstone 表保留但不参与导入拒绝。相同内容可以重新导入，来源消息身份保持幂等；media.sha256 唯一，content_media 为多对多。
- worker 抢占时 attempts +1，完成经 `_finalize` 落账。超时 running 尚有次数则 queued，耗尽则 dead；写原因、finished_at、审计并清锁。确定性失败死信，偶发异常退避。
- message.origin_note 是调用方注解，raw_payload 保存原载荷；search_extra 与文本索引在落库和重算时保持同值。

## 独立组合

- CLI composition create/list/show/update/set-members/delete 管理手动组合，可跨消息引用，保留顺序与重复成员。迁移到 0012，39 张 STRICT 表；仅明确勾选的 Web 上传同时新建组合，其他消息不自动建组合。
- 组合标签 --composition 独立关联，保留 --message 展开部件；usage_count 仍计内容项，composition_usage_count 单独返回。标签默认仅匹配自身，--match-member-tags 扩到当前可见成员；隐藏成员只返回位置及不可用状态。
- 组合编辑与审计同事务、详情读快照；软删组合不删除成员。CLI 为本机 root；本机 Web 的组合编辑器沿用帖级 v1 检索选内容项，支持创建组合、独立人工标签和模型建议；上传可明确请求组合与归档原子创建。`/manage` v2 统一搜索内容项、组合名称/说明及可见成员；组合更新、成员替换及删除仍由 CLI 管理，不开放远程或下载。

## 标签、检索与输出

- 人工标签使用 `ClassificationResult(source=human)`。当前服务接受 human/suggested；受邀用户无标签写权限。普通标签由 root 自定义，默认 namespace=general；rating/source/system 留给对应组件。
- 标签写操作验证目标和 root 身份，关联、分类记录、审计、计数与索引同事务完成。本机 CLI 使用操作系统权限边界并审计 local_root；显式 actor_id 必须为有效 root。
- tag_assignment 的 content/tag/source 唯一，撤销和恢复复用行。确认建议生成 human 关联并撤销 suggested，保留来源证据；拒绝只撤销建议。
- 有效标签为未撤销 human/system/auto/merged 且标签 active/deprecated；suggested/pending 不进入过滤、输出、索引或计数。usage_count 按不同内容项计数。
- tag_synonym 为别名事实源，同步 tag.aliases；规范名与别名交叉冲突在写事务拒绝。名称 NFKC、casefold、空白/下划线统一，规范化后 1~255 字符，拒绝控制字符。
- `SearchIndex.index_many` 参与调用方事务，空集合只读；标签刷新保留帖签名，维护重算分批提交。
- FTS 文档为内容项。CLI 和默认 Web v1 以帖返回/分页，hit 带 message_id 与全部可见 parts，关键词和标签 AND 可跨部件；`/manage` 与显式 Web result_version=2 以内容项和组合为平级结果，关键词可跨组合名称/说明/不同可见成员，统一时间游标分页，组合标签只查自身；来源/类型/状态过滤由同一个可见成员满足，组合发送时间取可见成员非空最新值。默认可见为未软删、active、nsfw_level <=0；消息软删排除整帖。
- 类型按可见部件计算，多种类型 OR，mixed 至少两种类型。存储按内容哈希去重；CLI/Web v1 检索按 post 签名折叠，代表帖须满足完整查询，缺签名保留显示；Web v2 不折叠内容项。
- post 构成、时间和签名在 pipeline._upsert_post 与 sqlite.sync_post_rows 保持同值；type_mask 为 ITEM_TYPES 的 distinct 位值之和。可见性实时计算，签名 v2 由 Python 生成有序 JSON，保留类型与重复次数，缺摘要不折叠；导入和重算共用摘要读取，全量归组按参数上限分批。0010 清空旧派生签名，升级后重算恢复折叠。
- CJK 1~2 字和拉丁词走 content_word，CJK >=3 字走 content_fts；混查询逐词 AND。用户词转为转义的引号短语，纯标点/emoji 跳过，MATCH 语法仅由适配器构造。
- 相关度取命中部件最小 bm25，按帖去重后预算 500/2000，带标签预算 2000；截断标 incomplete。候选不足时扩读并校验未读分数边界。
- CLI/Web v1 关键词 total=None，纯过滤计数上限 5000；Web v2 无论关键词或纯过滤均不预统计 total，当前仅全局时间排序，不提供相关度及内容项折叠。usage_count 仅作估计。has_more 描述下一页。v1 时间排序用键集游标、相关度用 page；v2 使用独立查询指纹的全局时间游标；sent 空值排最后并被时间范围排除。
- message.sent_at 为事实源，content_item/post 的 sent_at 为派生副本，重算和 doctor 对齐。时间索引和最小标签集按估算切换，0008/0009 覆盖索引与连接顺序属于性能契约。
- 查询共享读快照；v1 组装固定三条 SQL：主查询、标签批量、媒体批量；v2 限定每类候选后合并，再批量取标签。search_extra 顺序见 domain.search_extra_value，落库与重算同步修改；文件名只保存 basename，用于展示和索引。
- 服务返回数据，CLI 负责序列化/渲染，渲染器只读。JSON 保留完整值，stdout 单一 JSON、诊断 stderr；CLI 使用 ensure_ascii=True 兼容 PowerShell 5.1 管道。CLI 和默认 Web 返回 result_version=1，显式 Web 版本 2 返回平级实体结果；新增表格列须更新对应 MAX_WIDTHS，前端全局排序由查询端执行。
- 检索路径改动必须跑十万条真实基准：50 帖/页、热缓存、最多 4 并发，P50 <150 ms、P99 <500 ms。超预算在同一改动修复或记录解释；历史实测见 DESIGN_HISTORY。

## 内容查看与本机 Web

- `show/open/reveal` 每次接受一个消息或内容项 ID；按 ordinal/content_id、媒体 ordinal/media_id/role 排序，返回 content v1。默认可见性与检索一致；当前展示文本和图片，show 过滤终端控制字符。
- 图片说明正文算混合内容；混合 `--copy` 在缓存及系统调用前报错，`--json` 与 `--copy` 互斥。Windows 支持系统打开、定位、CF_UNICODETEXT 文本复制和 CF_HDROP 文件复制。
- 媒体验证后端绝对 root、canonical 对象键、大小、哈希和魔数，拒绝删除/隔离/未通过扫描媒体；单次总量 256 MiB，缓存不替代实时可见性检查。
- 查看副本在 data_root/view-cache，独立复制、原子替换，检查链接/reparse 和硬链接。静态 HTML 转义正文，只引用生成的相对图片名；HEIC 混合内容报错。缓存目前手动清理，删除归档须同步清理对应副本。
- `pc serve` 校验库结构，单进程监听 127.0.0.1，关闭代理头信任。Web 上传立即处理本次消息，其他任务由 `pc work` 执行，共用配置注入的运行时。
- 本机令牌在 fragment 中传入，10 分钟有效；交换后清除 fragment，保留路径与查询。内存会话 Cookie 为 HttpOnly/SameSite=Strict，8 小时绝对期限、30 分钟空闲期限；退出或重启失效。
- 业务校验会话、本机对端与当前 Host；写操作校验 Origin/CSRF。身份由服务器授予 local_root，客户端不能指定 actor_id。媒体接口检查可见关联并复用安全读取，存储和缓存保持私有。
- React 文本节点展示归档文本，同源资源受 CSP 约束，图片地址使用内容项/媒体 ID。系统动作复用服务，不执行 CLI 子进程；部分失败保留 copied 状态。
- `/`、`/add`、`/manage`、`/compositions`、`/model` 共用 HTML，刷新可直接访问。上传 multipart 为旧 text/note/images 或有序 parts JSON 加可选组合名称/说明/标签，身份为会话 root，来源 manual；组合请求在一次归档写事务中完成，失败不留本次新记录且保留隔离证据；读请求体前鉴权。按实际字节限制单图、256 MiB 媒体、258 MiB 请求和最多 19 张；文本按配置，备注 4096 字符。
- 暂存在 data_root/upload-staging/UUID，按选择顺序存独立子目录，roots 持久化供 worker 接续。成功或死信清理，可重试保留；清理仅限该根 UUID 直接子目录，异常残留须对照任务检查。
- worker.run_message 只恢复和抢占指定消息。单图/纯文本身份与 CLI 兼容，多图身份含有序媒体哈希和规范化文本哈希，文件名与备注不参与身份。

## 受邀账号与独立远程认证

- `pc user create/list/disable/enable/reset-password` 仅由本机 CLI 管理 app_user，创建固定 tier=1。先只读校验，再隐藏输入两次密码，写事务再次查重；用户名为小写 ASCII、3~64 位，root 保留；密码 15~128 字符，保持原样。Argon2id 为 19 MiB、2 次迭代、并行度 1。
- `pc serve --mode remote --port 8081 --public-origin https://archive.example.com` 创建独立认证应用，只监听回环。HTTPS 代理保留精确 Host，后端检查 socket/Host/Origin，关闭转发头信任；不挂载本机资源、归档业务或 OpenAPI。
- 入口只有 `/`、`/api/v1/session`、`/api/v1/logout`、`/healthz`，登录返回 identity_version=1。只接受有效 tier=1，拒绝 root 和其他 tier；操作目录仅作说明。
- 本机与远程 Cookie、凭据、会话独立，远程回环 socket 不是 root 身份证明。远程 Cookie 为 Secure/HttpOnly/SameSite=Strict/Path=/；表单 CSRF 5 分钟、单次有效，登录轮换会话。
- 内存会话 8 小时绝对、30 分钟空闲期限，每请求检查 tier、状态、密码与账号版本。管理操作推进 updated_at；停用后启用不恢复旧会话。
- 登录实际字节上限 8 KiB，用户名每分钟 5 次、全局每分钟 30 次，限流状态有界。认证错误统一，凭据禁止进入日志和账号结果。

## 数据、迁移与配置

- 迁移连续追加，已应用文件保持原样。新增版本更新 test_schema_integration.EXPECTED_MIGRATION_NAMES，新增表更新 sqlite.EXPECTED_TABLES。
- 每文件显式事务，用 sqlite3.complete_statement 切语句，成功后记账；迁移器拒绝 SQL PRAGMA。连接设置 WAL、foreign_keys=ON、busy_timeout=5000、synchronous=NORMAL、temp_store=MEMORY。
- CHECK 枚举调整用表重建，文件头 `-- pc:foreign_keys=off`，提交前 foreign_key_check、结束恢复外键；临时表以 _rebuild 结尾，原表 DDL 保持对应，细节见 migrations/README。
- 仓库配置、迁移和 .env 相对项目根解析；PC__DB__PATH 单独移动库文件。指纹剔除整行注释后 SHA-256，漂移只报告。时间为 ISO-8601 UTC 毫秒，updated_at/UUID 由应用写；布尔 INTEGER，JSON 为 json_valid 校验的 TEXT。
- 配置顺序 env > db > yaml > default，播种只补缺失键，环境覆盖只影响生效值。config.py 定义语义，适配器保证 setting/setting_change 事务和审计。
- 注册表 enum 加引号；未知键、重复键、越界和类型错误报错。app.yaml 部署段为 db/paths/jobs/ingest/security/models；设置变更经 write_setting_change 保留审计、回滚和危险项确认闸门。当前 CLI 提供查看与播种，默认值以注册表和校验测试为准。

## 按任务读取设计

修改前读取 DESIGN 的对应章节：

| 任务 | DESIGN 章节 |
| --- | --- |
| 来源与连接器 | §1.3、§9.1 |
| schema、迁移、去重 | §3，尤其 §3.8；性能实测入口 §3.7 |
| worker、标签、模型 API | §4；外发边界 §6.3 |
| 检索与内容查看 | §5；查看 §5.1、预算 §5.6、文件安全 §6.5 |
| 权限、配置与外发 | §6；账号与权限 §6.1、配置 §6.2.1 |
| 备份与容量 | §7 |
| 交付范围与进程 | §9、§10；运行目录 §10.2 |

未来方案读 FUTURE_DESIGN，历史实测读 DESIGN_HISTORY；二者不能替代当前核心契约。
