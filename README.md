---
name: post-collector-readme
description: "post-collector 使用手册：安装、文件导入、检索、配置、备份与升级。开发约束见 AGENTS.md，设计见 DESIGN.md。"
---

# post-collector

自托管的内容采集与检索工具，用于归档自己拥有或已获授权的图文内容。通过命令行完成本地导入、扫描、去重和存储，通过 CLI 或本机 Web 页面检索、人工打标和查看内容，数据保存为 SQLite 文件与本地媒体文件。

[快速开始](#快速开始) · [本机 Web](#本机-web-操作) · [CLI 使用](#cli-使用) · [配置](#配置) · [备份与升级](#备份与升级) · [开发](#开发)

## 安装要求

- Python 3.12+，SQLite 3.40+；使用 [uv](https://docs.astral.sh/uv/) 安装项目依赖。
- 一个可写的数据目录；备份目标放在另一块盘或 NAS。
- 首次安装依赖需要联网，本地导入和检索离线运行。

## 快速开始

在仓库根目录执行。首次 `pc db apply` 创建库文件。

```powershell
uv sync
uv run pc doctor
uv run pc db apply
uv run pc db status
uv run pc config seed
uv run pc upload "D:\素材\猫.jpg" --note "来源线索"
uv run pc item list
uv run pc search 猫
```

`uv sync` 安装运行时与开发依赖；部署时可用 `uv sync --no-dev`。`uv.lock` 随仓库保存。

启动本机 Web：

```powershell
uv run pc serve
```

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8080/` | 内容操作页面 |
| `http://127.0.0.1:8080/healthz` | 存活检查 |
| `http://127.0.0.1:8080/api/v1/meta` | 运行模式 |
| `http://127.0.0.1:8080/docs` | 本地 OpenAPI 定义 |

Windows PowerShell 使用 `uv run pc ...`。WSL 使用其自身的 Python 与 uv 环境；调用 Windows 环境时使用 `uv.exe run pc ...`。

## 本机 Web 操作

先完成建库，再运行 `uv run pc serve`，打开终端输出的完整授权链接。链接在 10 分钟内有效，使用一次后建立临时 root 会话，页面会清除地址栏中的令牌。会话最长 8 小时，空闲 30 分钟失效；刷新页面保留会话，退出或重启服务使会话失效。需要重新授权时重启 `pc serve`。

服务监听 `127.0.0.1`，浏览器使用输出链接中的地址。可通过 `--port 8135`、`--path <库文件>` 和 `--root <仓库根目录>` 调整启动位置。导航提供加入新内容、检索/管理、整理组合和模型标签链接。临时 root 凭据授予本机内容操作权限，请保管终端中的授权链接。受邀账号的独立登录入口见[账号与独立登录](#账号与独立登录)。

### 在 Web 中加入内容

进入“加入新内容”，用“添加文本条目”“添加链接条目”与图片选择器安排一条消息；各条目可移除、上移、下移，图片按编辑区顺序提交。链接填写完整 HTTP(S) URL 和可选说明；正文中的有效 URL 及独立链接可点击，不访问目标网站。可选勾选“同时创建组合”，填写名称、说明及逐行组合标签；这条路径同步扫描并整体提交，任一归档/组合/标签失败均不留下本次新记录，可能保留隔离证据；已有重复消息和共享媒体不删除。未勾选时继续支持普通上传及可重试任务。填写可选备注后提交，显示成功、重复、隔离或任务失败；可见内容提供详情链接。

支持 PNG、JPEG、GIF、WebP、AVIF、HEIC/HEIF。一次最多 19 张，单图遵循 `ingest.max_media_bytes`（默认 50 MiB），图片合计最多 256 MiB，请求体最多 258 MiB。文本遵循 `ingest.max_text_chars`（默认 100000 字符），备注最多 4096 字符。

上传字节先保存到 `<data_root>/upload-staging/<随机ID>/`。成功和死信清理暂存副本；待重试保留，运行 `uv run pc work` 接续。隔离字节保存在隔离后端，不作为正常图片展示。进程异常留下的暂存目录先保留；手动检查时对照 `message.media_refs` 和对应 `job`，确认无 queued/failed/running 引用后再清理，清理前关闭服务与 worker。

### 检索与管理

- `/manage` 单个检索表单按全局时间顺序列出内容项和组合；“仅组合”过滤只显示组合。支持关键词、来源、组合自身/内容项自身标签、类型及日期；“混合组合”要求符合筛选条件的可见成员含多种类型。关键词可以分别命中组合名称、说明和不同可见成员；一个内容项可与包含它的组合分别出现在结果中。空关键词展示最近结果，下一页沿用同一查询；当前仅支持时间排序，不折叠重复内容项，结果总数不预统计。默认只展示 active、安全内容；摘要视觉限制不截断详情。
- 勾选结果中的内容项可添加或撤销其标签，组合自身标签在整理页操作，互不传播。关联保留撤销历史，有效建议可以勾选确认或拒绝。
- 点击结果中的“查看”，可在详情区阅读内容项原消息或按保存顺序阅读组合；也可输入消息/内容项 ID。隐藏组合成员仅显示位置和不可用状态；标签目录及别名在页面下部管理。
- 本机打开、文件定位和复制操作作用于当前查看目标，或详情中勾选的一个内容项。“同时复制”适用于打开和定位。Windows 文本复制正文，图片复制文件；混合内容打开静态 HTML，复制返回明确错误。

图片读取复用归档文件安全校验，浏览器预览支持 JPEG、PNG、GIF、WebP 和 AVIF，具体解码由浏览器决定。其他格式显示媒体链接与提示。Web 服务与导入/处理分别启动，待处理任务通过 `pc work` 执行。

业务 API 位于 `/api/v1`，需要本机会话；写请求还需同源 Origin 和会话 CSRF 凭据。`GET /api/v1/search?result_version=2` 返回内容项与组合作为同页结果；未指定版本及 CLI 仍用原帖级 v1。上传 `POST /api/v1/uploads` 可传 `parts` JSON 与可选组合字段，不能同时传旧 `text`。标签、关联、别名与详情仍沿用各自版本化 JSON；媒体通过内容项与媒体 ID 获取。接口定义见 `/openapi.json`。查看缓存的保留与清理规则同 CLI。

### 在 Web 中整理组合

进入“整理组合”（`/compositions`）：左侧用关键词查现有消息，分别勾选要加入的文本/图片内容项；右侧按加入顺序显示内容项，可重复加入、移除、拖动调整，或用“上移”“下移”按钮排序。填写组合名称、可选说明和逐行填写的组合标签，确认后展示组合 ID；点击“用此组合测试模型建议”预选模型目标。提交前若有成员失去可见性，服务端会拒绝整个创建操作。

页面上方“已有组合”会自动列出最近 50 个组合，不必填写标签；创建后目录会刷新到未筛选的最近组合并清空目录筛选条件，显示组合 ID、名称和每个成员的位置，可重新进入此页核对已保存的顺序。目录结果可滚动，也可点击“跳到创建新组合”直达编辑区；读取失败时可在目录内重试。输入组合 ID 可精确查找，输入组合名称可按字面子串查找（ASCII 字母不区分大小写，`%`、`_` 不作为通配符）；还能按组合自身标签筛选，或勾选同时匹配可见成员标签。检索条件取交集，在筛选后分页。主“检索内容项与组合”在同一结果中列出两类实体；页面的“整理已有组合”进入目录以便继续编辑。组合自身人工标签不传播给成员；标签目录的 `usage_count` 与 `composition_usage_count` 分别统计内容项和组合。Web 支持组合创建及既有组合的逐项管理；CLI 仍可使用整体成员替换命令。标签关联单独提交；如果创建成功但标签关联失败，页面保留组合 ID，可在原页面重试标签关联，避免再次创建。

从“已有组合”、新建成功提示或 `/manage` 组合结果进入 `/compositions/{id}`：可修改名称/说明，检索并追加可见内容项（可重复），按位置上移/下移或移除**一次**引用，添加/撤销组合自身标签，以及经确认软删除整个组合。移除最后一个成员需要在页面再次明确确认，才会留下空组合；删除组合或解除成员引用都不会删除原内容、媒体或其他组合里的引用。旧成员不可用时仅显示位置，可调整或解除而不会泄露内容 ID；新增成员必须仍然可见。页面过期写入会被拒绝，刷新详情后再操作。空组合保留名称和自身标签，也可重新加入成员；创建组合及 CLI 整体替换成员仍要求至少一个。

### 模型 API 建议标签

进入“模型标签”页（`/model`）：

1. 输入 DeepSeek API 密钥并提交，输入框立即清空。密钥仅保留在服务进程内，不写入配置、数据库或浏览器存储。
2. 勾选外发确认并开启总闸。仅配置密钥或修改持久设置不会自动开启。
3. 选择一个已有内容项 ID，或一个已有独立组合；组合列表及详情展示顺序与重复成员。可从 Web“整理组合”页新建并跳转，组合编辑仍由 CLI 完成。
4. 预览实际正文、图片、完整候选标签目录及风险提示；勾选本次同意后发送一次。
5. 模型只从 DB 已有的 active/deprecated 普通标签选择至少一个建议，不创建新标签。root 确认后才进入有效标签；内容项在管理页审核，组合在模型页审核。组合标签不传播到成员。

固定使用 `https://api.deepseek.com/chat/completions` 的 `deepseek-flash`，不提供自定义 endpoint、代理、自动重试或 fallback。仅外发正文、原始 JPEG/PNG/GIF/WebP 图片与有序位置/类型，不外发文件名、路径、备注、来源、账号或原始消息。原始图片可能包含 EXIF/GPS/作者信息，不会转换或剥离；未知尺寸不代表已验证。来源须启用且授权有效，目标及所有组合成员须当前 active、未删除且 safe，媒体须通过安全读取和扫描状态检查。

候选目录须完整且为 1~500 个标签、最多 128 KiB JSON；超限拒绝，不截断。单次最多 20 个位置、4 次图片出现（重复计数）、每图 5 MiB、图片合计 10 MiB、正文 32 KiB UTF-8、最终请求 16 MiB。进程并发 1，无队列；滚动 24 小时最多 20 次尝试，以持久化预留计次；预留后的放行复查或提供商初始化失败、请求失败和结果未知也计入，**不是金额上限**。预留后、真正派发前失去放行时，页面显示失效原因并记为 stale，不会联网；其他派发前本地检查异常能落账时记 failed/write_failure。预留前 HTTP 4xx 页面提示未外发、不计费；已发请求失效不生成建议。结果未知可能已计费，不自动重发。

在 PowerShell 中按 Ctrl+C 并等待服务进程实际退出时，会主动清除内存中的密钥、放行与预览，无需先在 Web 点击删除。强制终止无法保证退出回调执行，但同环境重启也不能恢复进程内凭据；必须重新输入密钥并明确开启。这里只解除程序持有的引用，不保证物理内存擦除，也不会撤销服务商端的 API 密钥；已经发出的请求不能撤回。

预览有效期 5 分钟，只可消费一次；目标、授权、配置、目录、凭据或会话变化会使其失效。重启、退出、会话到期、替换或删除密钥后须重新设置密钥并开启总闸。发送后和采纳时再次验证，过期结果不写有效标签。任何持久化设置变更（包括重复开启总闸或修改无关设置）都会使已有预览和模型建议证明失效，须重新预览并调用；请先完成审核再更改设置。自然语言检索不在本轮范围，继续使用现有检索。自动回归使用假密钥与模拟 HTTP；用户已手动验证单素材真实 API 建议和置信度，组合及删除相关场景尚未手动验证。

## CLI 使用

本地 CLI 按操作系统文件权限使用；完整参数通过 `uv run pc --help` 或 `uv run pc <命令> --help` 查看。

### 导入文件

#### 手工上传

```powershell
uv run pc upload "D:\下载\猫.jpg" --note "保存来源"
uv run pc upload "D:\下载\图1.png" "D:\下载\图2.gif"
uv run pc item list --limit 20
```

首次上传自动建立 `manual` 来源，授权方式为 `root_self`。上传身份按媒体字节与规范化文本的哈希确定，重复上传幂等，消息时间留空。`--note` 保存来源线索；`--source NAME` 指定已登记的手工来源。

上传默认立即处理；`--no-process` 改为只入队。处理完成前保留原文件，因为 worker 会根据已登记的绝对路径读取字节。空文件与空白文本会跳过并报告。

#### 导出目录

```powershell
uv run pc source add my-files --kind export --roots "D:\素材"
uv run pc import "D:\素材" --source my-files --dry-run
uv run pc import "D:\素材" --source my-files --process
uv run pc source list
```

来源登记同时写入授权记录，`--evidence` 保存授权证明，`--expires-at` 设置有效期。导入身份按根标签与相对路径确定，消息时间取文件 mtime。重复导入幂等；`--process` 在入队后运行 worker。

#### 处理与安全边界

入库复制文件，源目录只读。媒体按内容哈希寻址，多条内容可共享同一份字节。

- 同名 `.txt`、`.md` 或 `.markdown` 可作为媒体配文，图与配文属于同一帖。
- 本地读取限于来源的 roots；符号链接、junction、reparse 路径与隐藏文件会跳过。单文件根额外允许同目录同名文本侧车。
- 扫描失败或类型未放行的文件写入独立隔离目录，状态为 `quarantined`，并建立复核任务。非原子归档中相同字节被新扫描拒绝时，旧的活跃内容项也会被隔离；不会因后续扫描通过而自动恢复。浏览器仅支持图片的渠道限制不据此隔离其他来源已放行的媒体；原子组合扫描失败时不改旧归档，只保留隔离证据。数据库不再引用的旧正常媒体副本可能留在私有目录中，须对照任务和引用人工核对后清理；仅因浏览器渠道拒绝而产生的隔离副本也可能没有媒体行引用，作为复核证据保留。扩展名与魔数不符时建立复核任务。
- `pc upload` 的本地文本文件按**完整规范化文本**生成消息身份；超过 `ingest.max_text_chars` 的尾部仍影响身份，但正文仅保存上限内的字符，不会展示或检索尾部。文件读取失败在跳过记录中说明，不会生成空白文本消息；内容身份模式需在内存中读取并规范化完整文本，极大的输入会增加内存占用，当前没有额外文本字节上限。
- 摄入与处理使用最新授权；来源停用、软删、授权撤销或过期会阻止继续落库。
- 旧哈希墓地记录不阻止同内容导入；同一来源消息身份重复提交仍幂等，相同媒体字节仍共用一份存储。

```powershell
uv run pc work                   # 处理到队列为空
uv run pc work --once            # 处理一条
uv run pc work --loop            # 持续轮询
uv run pc item list --status quarantined
```

### 检索

```powershell
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

`incomplete` 表示关键词候选达到读取预算。纯过滤列表的总数可能为精确值 `exact`、下限 `at_least` 或估计值 `estimate`。`has_more` 表示下一页是否还有结果。

### 人工标签与结果展示

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

`tag list` 返回标签目录；`tag show` 返回目标内容上的关联，包括来源、置信度、证据和撤销时间。root 可用 `pc tag confirm <assign_id> ...` 确认升级前没有模型快照证明的历史建议，或用 `pc tag reject <assign_id> ...` 拒绝建议；本轮模型建议须在本机 Web 的“管理”或“模型标签建议”页确认，确认后进入检索和统计。

常用查询直接显示表格，也可将完整 JSON 交给独立渲染命令：

```powershell
uv run pc search "旅记"
uv run pc search "旅记" --json | uv run pc render table
uv run pc tag list --json | uv run pc render table
uv run pc tag show --message 123 --json | uv run pc render table
```

两种表格入口复用相同渲染函数，按中文显示宽度省略长字段，窄终端优先显示 ID 和主要内容。重定向使用 120 格宽度；JSON 保留完整值。JSON 模式的 stdout 只输出数据，诊断进入 stderr。CLI JSON 将非 ASCII 字符写为 Unicode 转义，解析后保留完整中文与 emoji；Windows PowerShell 5.1 默认管道可直接使用上述命令。

渲染命令读取一个 JSON 对象，支持 search、tags、assignments、aliases 结果；无效输入返回非零状态。

### 查看与复制内容

先用 `pc search` 或 `pc item list` 获取消息 ID；查看单个部件时使用其 `content_id`。`--message` 与 `--content` 互斥，每次选择一个目标。

```powershell
uv run pc show --message 123
uv run pc show --content 456 --json
uv run pc show --message 123 --copy
uv run pc open --message 123
uv run pc open --content 456 --copy
uv run pc reveal --message 123
```

| 目标 | 使用方式 |
| --- | --- |
| 纯文本 | `show` 输出正文；`open` 打开 TXT；`reveal` 定位 TXT；`--copy` 复制正文 |
| 图片 | `show --json` 查询详情；`open` 打开图片；`reveal` 定位文件；`--copy` 复制文件列表 |
| 图文混合 | `show --json` 查询详情；`open` 打开本地 HTML；`reveal` 定位 HTML 与图片目录 |

`open`、`reveal` 与 `--copy` 当前支持 Windows；纯文本 `show` 和详情 JSON 使用 Python 核心。

查看范围为消息与部件未软删、部件 active 且 NSFW 等级为 0 的内容，按部件顺序处理。图片部件带说明正文时按混合内容处理；混合内容 `--copy` 在生成副本前报错。文本和图片以外的类型返回错误。`show --json` 支持完整详情查询，返回 `result_type: content`、`result_version: 1`、`message_id` 与有序 `parts`；它与 `--copy` 互斥，保持只读。`pc render table` 继续用于检索与标签结果。

图片复制采用文件管理器复制文件的格式。多张图片一次复制为文件列表；实际粘贴方式由接收应用决定。复制在打开或定位前完成；后续系统操作失败时，错误信息会注明复制已完成。纯文本多部件以空行连接；终端过滤控制字符，TXT、JSON 与剪贴板保留正文。

查看副本位于 `<data_root>/view-cache/<message_id>/<内容指纹>/`，命令输出其目录。图片按已确认的 MIME 补充扩展名，支持 JPEG、PNG、GIF、WebP、AVIF 与 HEIC；混合 HTML 支持其中的前五种格式。HTML 按部件顺序展示正文与本地图片，正文保留换行。

查看副本独立于归档原件，编辑副本后再次查看会重新校验并修复。每次操作重新检查目标可见性及媒体字节；单文件使用现有媒体限额，本次媒体总量上限为 256 MiB。缓存供命令退出后粘贴使用，当前手动清理：完成粘贴并关闭查看程序后，可在文件管理器删除 `view-cache` 下对应消息目录，后续查看会重新生成。删除归档或处理隐私清理时，也应清除对应查看副本；缓存属于可重建数据。

## 账号与独立登录

本机 root 使用现有一次性令牌和管理页面。登录响应包含身份说明，登录后访问 `/identity` 可直接查看 JSON。受邀账号通过独立认证入口登录。

```powershell
uv run pc user create reader
uv run pc user list --json
uv run pc user disable reader
uv run pc user enable reader
uv run pc user reset-password reader
uv run pc serve --mode remote --port 8081 --public-origin https://archive.example.com
```

账号命令支持 `--path` 和 `--root`。创建账号先校验用户名和重名，通过后隐藏输入两次密码；重置密码同样隐藏输入两次，长度 15~128 字符；用户名为 3~64 位 ASCII 字母、数字、点、下划线或连字符，统一小写，root 为保留名称。密码以 Argon2id 哈希保存。停用、重新启用和重置密码使旧会话失效，账号操作遵循本机文件系统权限边界。

远程服务监听 `127.0.0.1:8081`，由 HTTPS 代理或隧道转发。`--public-origin` 为精确的 HTTPS origin，例如 `https://archive.example.com`，不带末尾斜杠、路径、凭据、查询或 fragment。代理保留该 Host，只转发远程端口；本机管理端口独立。应用不信任转发头，也不根据代理的本机地址授予 root 权限。先确保其他应用没有占用端口，再启动两个入口。

远程首页提供用户名和密码表单；成功后直接返回 `identity_version=1` JSON，包含 `identity`、`scope`、`operations` 和会话 `csrf`。`GET /api/v1/session` 可刷新身份，`POST /api/v1/logout` 携带同源 Origin 和 `X-CSRF-Token` 退出。登录表单的短期 CSRF 凭据随页面生成，5 分钟有效、使用一次。远程会话 8 小时绝对到期、30 分钟空闲到期，退出或服务重启后失效。

远程入口提供登录、会话身份查询、退出和健康检查。身份 JSON 的 `scope` 为策略说明，`operations` 包含 CLI 命令模板和中文注释，`executable=false` 表示模板仅供阅读；此入口的实际操作是账号认证与身份查询。本机 root 的操作标记 `execution_context=local_cli`，表示在本机终端执行。

登录请求体最多 8 KiB，按实际接收字节计算；每个规范化用户名每分钟 5 次尝试，整个入口每分钟 30 次，超限返回 429。限流和会话状态驻内存且有容量上限，进程重启清空。远程仅接受有效受邀账号；未知账号、错误密码、停用账号和 root 账号使用统一认证失败消息。权限说明不授予远程用户服务器终端或本机文件系统访问能力。

## 配置

库文件为 `<data_root>/pc.sqlite3`。默认数据根目录：

| 平台 | 目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\post-collector` |
| Linux | `$XDG_DATA_HOME/post-collector`，缺省 `~/.local/share/post-collector` |
| macOS | `~/Library/Application Support/post-collector` |

归档字节位于 `media/`，隔离字节位于 `quarantine/`；Web 上传和查看副本分别使用 `upload-staging/` 与 `view-cache/`。

| 配置 | 作用 |
| --- | --- |
| `PC_DATA_ROOT` | 数据根及默认子目录 |
| `PC__DB__PATH` | 单独指定库文件，媒体目录仍由数据根配置决定 |
| `PC__PATHS__MEDIA_ROOT` 等 | 单独指定运行时目录 |
| `config/app.yaml` 的 `db` / `paths` | 部署路径基线 |
| `.env` | 部署变量，样例为 `.env.example`，本地文件由 Git 忽略 |

库位置优先级：`--path` > 进程环境中的 `PC__DB__PATH` / `PC_DATA_ROOT` > `.env` 同名项 > `app.yaml` 的 `db.path` / `db.data_root` > 平台默认。目录配置同样按进程环境、`.env`、YAML、派生默认值解析。`pc doctor` 输出实际路径与来源。

设置项的生效优先级为环境变量 > 数据库 > YAML > 注册表默认值：

```powershell
uv run pc config show --explain
uv run pc config show --key search --json
uv run pc config seed
```

`config seed` 仅补齐缺失键。已播种键由数据库提供，环境变量可覆盖，例如 PowerShell 中设置 `$env:PC__SEARCH__DEFAULT_PAGE_SIZE = "50"`。部署段 `db/paths/jobs/ingest/security/models` 保存在配置文件。

## 备份与升级

备份目标使用不同物理介质上的新文件名：

```powershell
uv run pc db backup --to "E:\备份\pc-2026-10-08.sqlite3"
```

命令用 `VACUUM INTO` 生成一致快照并校验完整性，目标已存在时报错。媒体和隔离区字节另用文件备份工具同步。备份恢复需要同时匹配数据库与对应媒体文件。

升级现有实例时，停止 Web 服务与 worker，备份库文件和媒体，再更新代码与依赖并执行：

```powershell
uv sync
uv run pc db apply
uv run pc db reindex --recompute-derived
uv run pc db status
```

迁移按连续编号和文件事务执行，结构指纹漂移会报告。迁移操作见 [migrations/README.md](migrations/README.md)。

CLI 与默认 Web v1 检索 JSON 为 `message_id + parts`，脚本据此读取结果；显式 Web `result_version=2` 返回内容项和组合平级结果，不适用于旧脚本。v1 时间排序游标支持空消息时间，旧游标需重新查询。旧相对 roots 或媒体引用需更新为绝对路径，或用新来源重新导入。

恢复快照：停止所有项目进程，留存当前库与对应 WAL/SHM，再将快照复制到库文件位置，清除该位置的旧 WAL/SHM，执行 `pc db status` 校验后启动。

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

开发约束与代码导航见 [AGENTS.md](AGENTS.md)，初版设计契约见 [DESIGN.md](DESIGN.md)，实测和历史记录见 [DESIGN_HISTORY.md](DESIGN_HISTORY.md)。

## 开发

后端在仓库根目录执行：

```powershell
uv sync
uv run ruff check src tests
uv run python -m compileall -q src
uv run pytest
```

修改实现前读取 DESIGN 对应章节；命令、接口、交付状态或项目约定变化时更新受影响的文档。未改变这些内容的拼写修正、内部重构和局部缺陷修复无需同步修改 README 与 AGENTS。

专项验证按改动触发：帖级检索运行 `uv run python tools/bench_search.py --scale 100000 --assert`，Web v2 运行 `uv run python tools/bench_web_search.py --scale 100000 --compositions 1000 --assert`；认证、会话或授权修改运行相关权限边界测试（现有入口为 `tests/test_accounts_auth.py`、`tests/test_web_app.py`）；生成资源及其源码或生成配置修改检查源码与构建产物一致性。Windows 窄编码控制台跑帖级基准前先设置 `$env:PYTHONIOENCODING='utf-8'`。具体检查范围见 [AGENTS.md](AGENTS.md#按改动触发验证)。重复步骤优先封装为 `tools/` 小脚本，输出检查对象、结果、状态与错误；产品行为须由断言、结果比较或指标验证，不能仅依据退出码。

### 前端

前端是 React 单入口 SPA，路径为 `/`、`/add`、`/manage`、`/compositions`、`/compositions/{id}` 和 `/model`。JS/CSS 为同源静态资源。使用 Node.js 22.12 以上版本，在 `frontend` 目录执行：

```powershell
npm ci
npm test
npm run build
```

构建输出到 Python 包的 `src/pc/web`，修改源码或构建配置后重新构建并提交受影响的产物；依赖变化时同步提交 npm 锁文件。修改生成资源时同样检查其与源码一致，核对重新构建后的文件清单和内容。运行服务使用已构建资源，Python 部署无需 Node。组件测试使用模拟 API，实际页面验收建议先设置独立的 `PC_DATA_ROOT` 和 `PC__DB__PATH`，然后执行 `pc db apply`。

## 独立有序组合（CLI）

组合引用已有内容项，可以跨来源和消息，保留顺序与重复成员。原消息、文件与成员标签保持独立，组合由本机 root 手动创建。

```powershell
uv run pc composition create --name "教程" --description "按顺序阅读" --content 3 --content 7 --content 3
uv run pc composition list --json
uv run pc composition show 1 --json
uv run pc composition update 1 --name "教程修订"
uv run pc composition set-members 1 --content 7 --content 3
uv run pc composition remove-member 1 --position 0
# 若只剩最后一位，显式 -f 可解除并保留空组合
uv run pc composition remove-member 1 --position 0 -f
uv run pc tag add --composition 1 --tag "教程"
uv run pc tag show --composition 1
uv run pc tag remove --composition 1 --tag "教程"
uv run pc composition list --tag "教程" --tag "截图" --match-member-tags --json
uv run pc composition delete 1
```

名称最多 255 字符、单行且非空；说明遵守 ingest.max_text_chars（默认 100000 字符）。创建及 `set-members` 至少一个成员，新增须当前 active、未删除且 safe。`remove-member --position N` 的 N 是 `show` 中从零起算的位置；只解除此位置一次引用，默认不能移除最后成员，显式 `-f` 才能保留空组合。失去可见性的旧成员只显示位置与不可用状态，可解除或移动而不影响其他位置。删除组合保留原内容和文件。以上命令支持 --path/--root；list 使用 --limit（默认 50，最大 100）及 --offset 分页。组合 JSON 保留完整值，人工展示过滤终端控制字符。

组合标签默认只用于自身；--match-member-tags 允许组合与可见成员共同满足标签 AND 条件，JSON 返回命中目标及成员位置。pc tag --message 仍给各部件分别打标。pc tag list --json 的 usage_count 仍计内容项，composition_usage_count 独立统计组合。组合不自动生成；本机 Web 可通过帖级检索选取内容项创建组合，并在 `/compositions/{id}` 管理成员、属性和自身标签或软删组合；组合目录可按自身标签筛选。Web v2 可按组合名称、说明与可见成员关键词搜索；组合下载尚未提供。

### 升级至 0011

先停止本机服务与 worker，确认目标库路径并备份数据库与媒体；现有备份流程见“备份与升级”。随后对同一库执行：

```powershell
uv run pc db apply --path "D:\archive\pc.sqlite3"
uv run pc db reindex --recompute-derived --path "D:\archive\pc.sqlite3"
uv run pc db status --path "D:\archive\pc.sqlite3"
```

0010 新增三张组合表，清空旧派生帖签名和折叠关系。重算前各消息独立显示；重算后，A/B、B/A、A/B/A 保持不同结果，相同顺序与构成仍可折叠。0011 重建分类记录和组合标签约束，分类目标只能为内容项或组合之一，新增仅含元数据的外发日志；0012 增加有序消息条目及规范链接索引，共39张 STRICT 表。文件继续按 SHA-256 复用，历史标签和原始消息保持原样。升级不会配置密钥、开启模型总闸或自动调用 API。
