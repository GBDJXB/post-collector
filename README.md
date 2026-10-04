---
name: post-collector-readme
description: "post-collector 使用手册：安装、文件导入、检索、配置、备份与升级。开发约束见 AGENTS.md，设计见 DESIGN.md。"
---

# post-collector

自托管的内容采集与检索工具，用于归档自己拥有或已获授权的图文内容。当前通过命令行完成本地文件导入、扫描、去重、存储、人工打标、检索和内容查看，数据保存为 SQLite 文件与本地媒体文件。

## 当前能力

| 能力 | 状态 |
| --- | --- |
| 官方导出目录导入、手工上传、来源授权校验 | 可用 |
| 魔数扫描、字节限额、隔离存储、SHA-256 去重 | 可用 |
| 关键词与来源、标签、类型、时间过滤，整帖展示与翻页 | 可用 |
| root 人工标签、别名、建议确认与 JSON 表格管道 | 可用 |
| 正文与详情 JSON、Windows 默认程序打开、文件定位与复制 | 可用 |
| 数据库迁移、结构校验、备份、索引重建 | 可用 |
| 配置校验、设置项播种、生效值与来源查询 | 可用 |
| 健康检查与运行模式接口 | 可用 |

当前操作入口为 CLI。模型打标、敏感度分类、Web 界面、平台连接器、缩略图、内容删除与自动容量管理按 [DESIGN.md §9](DESIGN.md#9-实施路线图) 交付。

## 准备环境

- Python 3.12+，自带 SQLite ≥ 3.40；使用 `uv` 安装项目依赖。
- 建议 4 核 CPU、8 GB 内存，数据库与近期媒体放在 SSD。
- 一个可写的数据目录；备份目标放在另一块盘或 NAS。
- 首次安装依赖需要联网，本地导入和检索离线运行。

## 五分钟跑起来

在仓库根目录执行。首次 `pc db apply` 创建库文件。

```bash
uv sync
uv run pc doctor
uv run pc db apply
uv run pc db status
uv run pc config seed
uv run pc upload <文件或目录> --note "来源线索"
uv run pc item list
uv run pc search <关键词>
```

`uv sync` 安装运行时与开发依赖；部署时可用 `uv sync --no-dev`。`uv.lock` 随仓库保存。

查看接口服务：

```bash
uv run uvicorn pc.api.app:create_app --factory --host 127.0.0.1 --port 8080
```

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8080/` | 版本、阶段与端点索引 |
| `http://127.0.0.1:8080/healthz` | 存活检查 |
| `http://127.0.0.1:8080/api/v1/meta` | 运行模式 |
| `http://127.0.0.1:8080/docs` | 接口文档 |

Windows PowerShell 使用 `uv run pc ...`。WSL 使用其自身的 Python 与 uv 环境；调用 Windows 环境时使用 `uv.exe run pc ...`。

## 导入文件

### 手工上传

```bash
uv run pc upload D:\下载\猫.jpg --note "保存来源"
uv run pc upload D:\下载\图1.png D:\下载\图2.gif
uv run pc item list --limit 20
```

首次上传自动建立 `manual` 来源，授权方式为 `root_self`。上传身份按媒体字节与规范化文本的哈希确定，重复上传幂等，消息时间留空。`--note` 保存来源线索；`--source NAME` 指定已登记的手工来源。

上传默认立即处理；`--no-process` 改为只入队。处理完成前保留原文件，因为 worker 会根据已登记的绝对路径读取字节。空文件与空白文本会跳过并报告。

### 导出目录

```bash
uv run pc source add my-files --kind export --roots D:\素材
uv run pc import D:\素材 --source my-files --dry-run
uv run pc import D:\素材 --source my-files --process
uv run pc source list
```

来源登记同时写入授权记录，`--evidence` 保存授权证明，`--expires-at` 设置有效期。导入身份按根标签与相对路径确定，消息时间取文件 mtime。重复导入幂等；`--process` 在入队后运行 worker。

### 处理与安全边界

入库复制文件，源目录只读。媒体按内容哈希寻址，多条内容可共享同一份字节。

- 同名 `.txt`、`.md` 或 `.markdown` 可作为媒体配文，图与配文属于同一帖。
- 本地读取限于来源的 roots；符号链接、junction、reparse 路径与隐藏文件会跳过。单文件根额外允许同目录同名文本侧车。
- 扫描失败或类型未放行的文件写入独立隔离目录，状态为 `quarantined`，并建立复核任务。扩展名与魔数不符时建立复核任务。
- 摄入与处理使用最新授权；来源停用、软删、授权撤销或过期会阻止继续落库。
- 文本或媒体命中有效哈希墓地时，整帖进入死信。

```bash
uv run pc work                   # 处理到队列为空
uv run pc work --once            # 处理一条
uv run pc work --loop            # 持续轮询
uv run pc item list --status quarantined
```

## 检索

```bash
uv run pc search 蓝发 精灵
uv run pc search sunset
uv run pc search 壁纸 --source manual
uv run pc search 雨 --tag blue_hair
uv run pc search --type image --since 2026-09-01 --limit 50
uv run pc search --type mixed
uv run pc search 雨 --sort time
uv run pc search --time-field sent --since 2026-09-01
uv run pc search 雨 --json
uv run pc search 雨 --timings
```

中文 1~2 字使用词级索引，3 字及以上使用子串索引；拉丁词支持前缀匹配。媒体文件名、类型名和来源备注可作为关键词，纯文本文件目前按内容检索。

检索单位是一帖。多个关键词或标签按 AND 匹配，可分别命中同帖的不同可见部件；结果包含全部可见部件及命中理由。默认显示有效、未软删、NSFW 等级为 0 的部件，按整帖签名折叠重复帖。

| 参数 | 含义 |
| --- | --- |
| `--source NAME` / 多个 `--tag NAME` | 来源过滤 / 标签 AND |
| `--type image`、`--type text` 等 | 可见部件全为指定类型的纯帖 |
| `--type mixed` | 至少含两种可见类型；多个 `--type` 按 OR |
| `--status quarantined` | 查看隔离内容 |
| `--nsfw-level 0..4` | 可见等级上限 |
| `--include-duplicates` | 显示重复帖 |
| `--time-field ingest` | 入库时间，默认值 |
| `--time-field sent` | 消息时间；手工上传留空，时间范围排除空值，倒序将空值排在最后 |
| `--sort relevance` / `--sort time` | 相关度 / 所选时间字段倒序 |
| `--page N` | 相关度翻页 |
| `--cursor C` | 时间序翻页，取上次 JSON 的 `next_cursor`，保持同一时间字段与过滤条件 |
| `--limit N` | 每页 1~50 帖 |

JSON 的每条 `hits` 使用 `message_id` 标识帖，`parts` 保存部件的 `content_id`、类型、正文、文件名、标签、媒体与命中理由。顶层字段为展示摘要，附件读取 `parts[].media.items`。表格会截断长字段，`--json` 保留完整值。

关键词查询返回候选数，候选预算为 500/2000 帖；带标签时预算为 2000，相关度页凑齐后可提前结束读取。`incomplete` 表示候选被截断。纯过滤列表的总数口径为 `exact`、`at_least`（上限 5000）或标签用量 `estimate`。`has_more` 表示下一页是否还有结果。

## 人工标签与结果展示

本地 CLI 由操作系统权限保护，按 root 操作入口使用。先用 `pc search` 查看帖 ID，或用 `pc item list --json` 查看 `message_id` 和部件 `content_id`。

```powershell
# 给整帖的未删除部件打标；名称由自己定义
uv run pc tag add --message 123 --tag "旅行记录" --tag "待整理"
# 精确标注一个部件；namespace 默认 general
uv run pc tag add --content 456 --tag "自选角色名称" --namespace character
uv run pc tag show --message 123
uv run pc tag list
uv run pc tag remove --message 123 --tag "待整理"

uv run pc tag alias add --tag "旅行记录" --alias "旅记"
uv run pc tag alias list --tag "旅行记录"
uv run pc search --tag "旅记"
uv run pc tag alias remove --alias "旅记"
```

`--message` 与 `--content` 互斥，均可重复；`--tag` 可重复。人工输入立即生效，撤销操作只撤销人工关联，重新添加复用关联 ID。标签目录的 `usage_count` 按具有有效关联的不同内容项计数，作为检索估计值；它与可见帖数可能不同。标签名称经过 NFKC、大小写与空白/下划线统一，规范名与别名共享唯一解析规则。

`tag list` 返回标签目录；`tag show` 返回目标内容上的关联，包括来源、置信度、证据和撤销时间。建议关联先保存，root 用 `pc tag confirm <assign_id> ...` 确认或用 `pc tag reject <assign_id> ...` 拒绝；确认后进入检索和统计。当前人工组件支持普通标签，敏感度分类和模型 API 接入见 DESIGN §4.7。

常用查询直接显示表格，也可将完整 JSON 交给独立渲染命令：

```powershell
uv run pc search "旅记"
uv run pc search "旅记" --json | uv run pc render table
uv run pc tag list --json | uv run pc render table
uv run pc tag show --message 123 --json | uv run pc render table
```

两种表格入口复用相同渲染函数，按中文显示宽度省略长字段，窄终端优先显示 ID 和主要内容。重定向使用 120 格宽度；JSON 保留完整值。JSON 模式的 stdout 只输出数据，诊断进入 stderr。CLI JSON 将非 ASCII 字符写为 Unicode 转义，解析后保留完整中文与 emoji；Windows PowerShell 5.1 默认管道可直接使用上述命令。

查询结果带 `result_version: 1` 与 `result_type`：检索为 `search`/`hits`，标签目录为 `tags`/`tags`，关联为 `assignments`/`assignments`，别名为 `aliases`/`aliases`。渲染命令读取一个 JSON 对象，支持上述类型；无效输入返回非零状态。前端按这些业务字段渲染，跨页排序通过查询参数执行。

## 查看与复制内容

先用 `pc search` 或 `pc item list` 获取消息 ID；查看单个部件时使用其 `content_id`。`--message` 与 `--content` 互斥，每次选择一个目标。

```powershell
uv run pc show --message 123
uv run pc show --content 456 --json
uv run pc show --message 123 --copy
uv run pc open --message 123
uv run pc open --content 456 --copy
uv run pc reveal --message 123
```

| 内容 | `show` | `open` | `reveal` | `--copy` |
| --- | --- | --- | --- | --- |
| 纯文本 | 正文 | 默认程序打开 TXT | 文件管理器选中 TXT | 复制正文 |
| 图片 | 当前返回能力不可用错误 | 默认程序打开各图片 | 选中全部图片文件 | 复制文件列表 |
| 图文混合 | 当前返回能力不可用错误 | 默认浏览器打开本地 HTML | 选中 HTML，目录内包含正文与图片 | 返回错误，保持无副作用 |

`open`、`reveal` 与 `--copy` 当前支持 Windows；纯文本 `show` 和详情 JSON 使用 Python 核心。终端图片的验证记录见 [DESIGN.md §5.1](DESIGN.md#51-阶段-1bm25--倒排--过滤sqlite-fts5)。

查看范围为消息与部件未软删、部件 active 且 NSFW 等级为 0 的内容，按部件顺序处理。图片部件带说明正文时按混合内容处理。文本和图片以外的类型返回错误。`show --json` 支持完整详情查询，返回 `result_type: content`、`result_version: 1`、`message_id` 与有序 `parts`；它与 `--copy` 互斥，保持只读。`pc render table` 继续用于检索与标签结果。

图片复制采用文件管理器复制文件的格式。多张图片一次复制为文件列表；实际粘贴方式由接收应用决定。复制在打开或定位前完成；后续系统操作失败时，错误信息会注明复制已完成。纯文本多部件以空行连接；终端过滤控制字符，TXT、JSON 与剪贴板保留正文。

查看副本位于 `<data_root>/view-cache/<message_id>/<内容指纹>/`，命令输出其目录。图片按已确认的 MIME 补充扩展名，支持 JPEG、PNG、GIF、WebP、AVIF 与 HEIC；混合 HTML 支持其中的前五种格式。HTML 按部件顺序展示正文与本地图片，正文保留换行。

查看副本独立于归档原件，编辑副本后再次查看会重新校验并修复。每次操作重新检查目标可见性及媒体字节；单文件使用现有媒体限额，本次媒体总量上限为 256 MiB。缓存供命令退出后粘贴使用，当前手动清理：完成粘贴并关闭查看程序后，可在文件管理器删除 `view-cache` 下对应消息目录，后续查看会重新生成。删除归档或处理隐私清理时，也应清除对应查看副本；缓存属于可重建数据。

## 数据与配置

库文件为 `<data_root>/pc.sqlite3`。默认数据根目录：

| 平台 | 目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\post-collector` |
| Linux | `$XDG_DATA_HOME/post-collector`，缺省 `~/.local/share/post-collector` |
| macOS | `~/Library/Application Support/post-collector` |

数据根包含 `media/`、`thumbs/`、`cold/`、`quarantine/`、`backup/`、`models/` 和 `view-cache/`；当前实际内容主要在库文件、媒体与隔离目录中。

| 配置 | 作用 |
| --- | --- |
| `PC_DATA_ROOT` | 数据根及默认子目录 |
| `PC__DB__PATH` | 单独指定库文件，媒体目录仍由数据根配置决定 |
| `PC__PATHS__MEDIA_ROOT` 等 | 单独指定运行时目录 |
| `config/app.yaml` 的 `db` / `paths` | 部署路径基线 |
| `.env` | 部署变量，样例为 `.env.example`，本地文件由 Git 忽略 |

库位置优先级：`--path` > 进程环境中的 `PC__DB__PATH` / `PC_DATA_ROOT` > `.env` 同名项 > `app.yaml` 的 `db.path` / `db.data_root` > 平台默认。目录配置同样按进程环境、`.env`、YAML、派生默认值解析。`pc doctor` 输出实际路径与来源。

设置项的生效优先级为环境变量 > 数据库 > YAML > 注册表默认值：

```bash
uv run pc config show --explain
uv run pc config show --key search --json
uv run pc config seed
```

`config seed` 仅补齐缺失键。已播种键由数据库提供，环境变量可覆盖，例如 `PC__SEARCH__DEFAULT_PAGE_SIZE=50`。部署段 `db/paths/jobs/ingest/security/models` 保存在配置文件；模型和界面功能的实施计划见 DESIGN.md。

## 备份与升级

备份目标使用不同物理介质上的新文件名：

```bash
uv run pc db backup --to <另一块盘或NAS上的快照文件>
```

命令用 `VACUUM INTO` 生成一致快照并校验完整性，目标已存在时报错。媒体和隔离区字节另用文件备份工具同步。备份恢复需要同时匹配数据库与对应媒体文件。

升级现有实例时，停止 worker，先备份，再执行：

```bash
uv run pc db apply
uv run pc db reindex --recompute-derived
uv run pc db status
```

当前有 9 个迁移版本。0007 建立帖表，重算补齐整帖签名；0008/0009 添加检索覆盖索引。迁移按文件事务执行，结构指纹漂移会报告。

检索 JSON 为 `message_id + parts`，脚本据此读取结果。v2 游标支持空消息时间，旧游标需重新查询。旧相对 roots 或媒体引用需更新为绝对路径，或用新来源重新导入。

恢复快照：停止所有项目进程，留存当前库与对应 WAL/SHM，再将快照复制到库文件位置，清除该位置的旧 WAL/SHM，执行 `pc db status` 校验后启动。保留期与恢复演练见 [DESIGN.md §7.1](DESIGN.md#71-可靠性设计)。

## 排错与维护

| 现象 | 操作 |
| --- | --- |
| 模块或依赖缺失 | 执行 `uv sync`，通过 `uv run pc ...` 调用 |
| 找不到配置或迁移目录 | 检查项目根；数据库命令可用 `--root` / `--dir` 指定 |
| 数据路径异常 | 用 `pc doctor` 核对路径来源，检查进程变量、`.env` 与 YAML |
| 数据目录不可写或磁盘空间不足 | 检查权限和可用空间；搬迁时保留库与媒体的对应关系 |
| `database is locked` | 检查其他写入进程；写锁等待上限为 5 秒 |
| 数据库结构未就绪 | 查看 `pc db status`；未应用迁移用 `pc db apply` |
| 结构指纹漂移 | 核对迁移文件，恢复原文件或追加新迁移；保留原库与备份 |
| 来源授权失效 | 用 `pc source list` 核对有效期和撤销状态 |
| 内容搜不到、派生数据过期 | `pc doctor` 后执行 `pc db reindex --recompute-derived` |
| 配置修改未生效 | `pc config show --explain` 查看当前值及来源 |
| 翻页游标不兼容 | 保持查询条件，重新查询并复制 `next_cursor` |
| 中文输出异常 | 使用 UTF-8 终端；Windows 可执行 `chcp 65001` |

开发与验证命令见 [AGENTS.md](AGENTS.md)。设计、路线图与接入条件见 [DESIGN.md](DESIGN.md)。
