---
name: post-collector-readme
description: "post-collector 使用手册：安装、文件导入、检索、配置、备份与升级。开发约束见 AGENTS.md，设计见 DESIGN.md。"
---

# post-collector

自托管的内容采集与检索工具，用于归档自己拥有或已获授权的图文内容。当前通过命令行完成本地文件导入、扫描、去重、存储和检索，数据保存为 SQLite 文件与本地媒体文件。

## 当前能力

| 能力 | 状态 |
| --- | --- |
| 官方导出目录导入、手工上传、来源授权校验 | 可用 |
| 魔数扫描、字节限额、隔离存储、SHA-256 去重 | 可用 |
| 关键词与来源、标签、类型、时间过滤，整帖展示与翻页 | 可用 |
| 数据库迁移、结构校验、备份、索引重建 | 可用 |
| 配置校验、设置项播种、生效值与来源查询 | 可用 |
| 健康检查与运行模式接口 | 可用 |

当前操作入口为 CLI。打标、Web 界面、平台连接器、缩略图、内容删除与自动容量管理按 [DESIGN.md §9](DESIGN.md#9-实施路线图) 交付。

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

## 数据与配置

库文件为 `<data_root>/pc.sqlite3`。默认数据根目录：

| 平台 | 目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\post-collector` |
| Linux | `$XDG_DATA_HOME/post-collector`，缺省 `~/.local/share/post-collector` |
| macOS | `~/Library/Application Support/post-collector` |

数据根包含 `media/`、`thumbs/`、`cold/`、`quarantine/`、`backup/` 和 `models/`；当前实际内容主要在库文件、媒体与隔离目录中。

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
