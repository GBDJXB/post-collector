# post-collector 白盒渗透测试 — 交付摘要

## 1. 交付元信息

| 项 | 值 |
| --- | --- |
| 测试目标 | `D:\DSH\projects\post-collector`（Python 3 / SQLite / FastAPI 本地归档应用） |
| Git 分支 | `main` |
| Git 提交 | `441d41b7b2e06f8332a99d7a9ef610633803fdd6`（短号 `441d41b`） |
| 提交标题 | `feat(web): add React uploads and invited-user authentication` |
| 提交时间 | 2026-10-05 21:55:31 -0700 |
| 测试时工作区状态 | 仅未跟踪 `.archify/`（测试前既存，与本次测试无关） |
| 授权声明 | 仓库所有者（工作区用户）明确指示测试本工作区内的 post-collector 仓库；属自有代码授权测试，仅限本地源码与本地回环实例，禁止对外网目标发起攻击 |
| 测试类型 | 白盒源码审计 + 动态验证（本地回环实例 / 进程内 ASGI / 直接调用被审适配器） |
| 交付物 | 本目录（报告、摘要、资产图、复现索引、环境说明、git 版本标注） |

## 2. 测试方法

- **决策层**：记录目标 goal → 拆分为 7 个互不重叠的探索意图 intent，每个意图绑定独立执行子 agent；
  子 agent 以实时检查点方式把已确认事实/资产/漏洞写入父意图，父层不接触原始日志。
- **派生**：在文件边界事实（`fact-1`：`open_scoped` 拒 `..`/UNC/8.3/junction 但不拒硬链接）
  之上派生 `intent-7`，把“路径隔离绕过”从原语升级为端到端影响链验证 → 形成 high 级 finding-19。
- **策略**：所有结论均要求可复现证据；通道不可达时（如无法创建符号链接、无法绑定 80 端口）
  明确降级为静态结论并标注置信度，不把推断写成漏洞。

### 探索链路概览

```
goal-1 (post-collector 仓库渗透测试)
├── intent-1 Web/HTTP 攻击面            → 26 facts, 10 assets, 2 findings
├── intent-2 认证/会话/授权模型          → 19 facts,  3 findings
├── intent-3 文件与路径安全              → 13 facts,  5 findings
├── intent-4 数据层注入与配置解析        → 12 facts,  3 findings
├── intent-5 凭据/配置/供应链泄露        → 19 facts,  2 findings
├── intent-6 内容渲染与前端 XSS          →  9 facts,  2 assets, 1 finding
└── intent-7 路径隔离绕过影响链（派生）  →  7 facts,  3 assets, 1 finding（high）
```

合计：**7 意图 / 105 事实入账 / 20 条漏洞记录 / 15 项资产**。

## 3. 结论统计

| 严重级别 | 数量 | 说明 |
| --- | --- | --- |
| high | 1 | 硬链接越界读取端到端利用（finding-19） |
| medium | 3（去重后） | 迁移器 PRAGMA 绕过、远程登录全局配额耗尽、远程表单池耗尽 |
| low | 12（去重后） | 信息泄露、纵深防护缺口、fail-open 白名单、配置解析等 |
| 合计记录 | 20 | 其中 4 条是并行意图对同一缺陷的独立复现（finding-1≈19、3≈10、4≈11、5≈12） |

**去重后独立漏洞：16 条**（1 high、3 medium、12 low）。

### 总体判断

- **核心安全边界基本有效**：鉴权覆盖、CSRF、Host/Origin 白名单、回环对端校验、会话生命周期、
  账号枚举抗性、SQL/FTS 注入、路径穿越（`..`/UNC/8.3/junction/symlink）、XSS（React 文本节点 +
  严格 CSP + 静态 HTML 转义）、凭据管理等方向经动态验证均**未发现可利用漏洞**。
- **最严重问题集中在"路径隔离"与"资源配额"两类**：前者允许把 roots 之外的同卷文件静默持久化进
  归档并经应用自身接口回读（high）；后者使未认证者可让远程登录整体不可用（medium）。
- **medium/low 中相当一部分是"控制被静默解除"**：迁移器 PRAGMA 禁令、危险设置二次确认、
  未扫描媒体的展示门控、白名单默认值 —— 共同特征是**声明的安全契约与实际代码路径不一致**。

## 4. 漏洞清单（按严重级别，含编号映射）

| # | 级别 | 标题 | 位置 | 报告章节 |
| --- | --- | --- | --- | --- |
| 19 | **high** | `safe_files.open_scoped` 未校验硬链接：roots 外文件被归档落库并经媒体接口回读（端到端） | `src/pc/adapters/safe_files.py` | finding-19 |
| 1 | medium（19 的前置原语，保留） | 未校验硬链接，root 内硬链接可读取并归档 root 外同卷文件 | `safe_files.py` | finding-1 |
| 16 | medium | 迁移器 PRAGMA 拒绝可被注释/空白前导绕过，借 `writable_schema` 篡改 `sqlite_master` | `src/pc/adapters/sqlite.py` `_reject_pragma` | finding-16 |
| 10 | medium | 远程登录全局配额可被任意单一用户名耗尽，60 秒窗口内全账号无法登录 | `src/pc/api/remote_auth.py` `AuthState.throttle` | finding-10 |
| 11 | medium | 远程登录表单池（512）/会话池（1024）无隔离，未认证 `GET /` 即可阻断登录 | `src/pc/api/remote_auth.py` `AuthState.form` | finding-11 |
| 2 | low | `open_scoped` 不拒绝 NTFS 备用数据流（`file.txt:stream`） | `safe_files.py` | finding-2 |
| 9 | low | 本机 `/openapi.json` 未鉴权暴露完整 API 清单（19 路由 + schema） | `src/pc/api/app.py` | finding-9 |
| 20 | low | `/api/v1/assignments` 泄露 pydantic 原始校验详情（内部模型名、依赖版本链接） | `src/pc/api/app.py` | finding-20 |
| 7 | low | HTTP 400 错误响应回显服务端绝对路径 | `app.py:191-194` + `safe_files.py:79/91/94/97` | finding-7 |
| 12 | low | 本机 logout 的 Cookie 删除丢失 `HttpOnly`/`SameSite=strict`（属性降级） | `src/pc/api/app.py` logout | finding-12 |
| 5 | low | 同上（并行意图重复记录） | 同上 | finding-5 |
| 13 | low | `view_files.read_images` 把 `scan_status='skipped'` 当作可查看媒体 | `src/pc/adapters/view_files.py` | finding-13 |
| 14 | low | `LocalStorageBackend.put` 命中已有对象时早退，不校验内容即报成功 | `src/pc/adapters/local_storage.py` | finding-14 |
| 15 | low | `magic_scan` 的 `ftyp` 分支对未知/缺失 brand 默认放行为 `video/mp4`（fail-open） | `src/pc/adapters/magic_scan.py` | finding-15 |
| 18 | low | 检索 `page` 无上界，`page=10^18` 触发 `OverflowError` 返回 500 | `post_search.py:281` + `app.py` SearchInput | finding-18 |
| 17 | low | YAML 重复键被静默接受（后者胜出），可静默解除危险项声明 | `src/pc/config.py` 加载路径 | finding-17 |
| 6 | low | 终端正文过滤遗漏 Unicode 格式化字符（Cf），可双向显示伪造 `pc show` 输出 | `src/pc/view_presentation.py` | finding-6 |
| 8 | low | 容器默认以 root 运行，基础镜像/构建工具链用可变标签、`uv sync` 未加 `--frozen` | `Dockerfile` / `docker-compose.yml` | finding-8 |
| 3 | medium | 全局配额耗尽（并行意图重复记录） | 同 finding-10 | finding-3 |
| 4 | medium | 表单池耗尽（并行意图重复记录） | 同 finding-11 | finding-4 |

> 每条漏洞的完整描述与**可复现步骤（命令级）**见 `pentest-report.md` 中对应 finding 章节。

## 5. 修复优先级建议

1. **P0 — 硬链接越界（finding-19/1）**：在 `safe_files.open_scoped` 打开后增加 `st_nlink == 1` 门限
   （与 `upload_files`、`view_files` 现有语义对齐）；同时拒绝备用数据流名（`name:stream` / `::$DATA`，
   注意区分盘符冒号）；在 `discover` 阶段预筛 `nlink` 以便早报错；补硬链接 + ADS 回归测试。
2. **P1 — 远程登录可用性（finding-10/11）**：全局配额与表单池按对端/来源隔离或提高上限并引入
   退避与优雅降级（如配额耗尽时仍允许已建立表单提交）；表单池改为 LRU 回收而非硬 429。
3. **P1 — 迁移器完整性（finding-16）**：`_reject_pragma` 先做 SQL 注释剥离再判定首个非注释 token，
   并显式禁止 `writable_schema` 等感知型 PRAGMA；加入回归测试。
4. **P2 — 契约一致性**：`scan_status='skipped'` 是否可查看需二选一（拒绝或文档化为可信）；
   `magic_scan` 未知 brand 应 fail closed；`write_setting_change` 的 `dangerous` 闸门应自行查注册表
   而非依赖调用方传参（当前无生产调用方，属未来风险）。
5. **P3 — 信息与纵深**：本机应用设 `openapi_url=None`；统一错误响应形状（不泄露 pydantic 细节与
   绝对路径）；`logout` 删除 Cookie 时保留原属性；`page` 增加上界校验；终端过滤由 Cc 扩展为 Cc+Cf；
   YAML 加载器检测重复键；Dockerfile 增加非 root 用户与镜像 digest 固定。

## 6. 未覆盖项与环境限制

- **无法绑定 80 端口**：`--port 80` 场景（浏览器省略默认端口导致 Host 校验整体 403）未动态复现，
  仅为静态结论（失败关闭，属可用性问题）。
- **符号链接（symlink）**：Windows 创建文件/目录符号链接需管理员权限，本机 `mklink` 失败；
  结论基于同一 `_check_redirects` 代码路径（junction 已动态验证），置信度中高。
- **前端字节级重构建比对**：沙箱禁止 esbuild 启动子进程（`spawn EPERM`），无法执行 `npx vite build`；
  改用源码字面量逐条对照 + 危险构造全量扫描 + CSS 归一化比对，未发现不一致。
- **pytest 套件**：沙箱内 `tmp_path` 创建被拒（`WinError 5`）且 `--basetemp` 重定向后在 sessionfinish
  仍被拒，故多数结论改用直接调用被审适配器的独立脚本验证；`tests/test_accounts_auth.py` 全量通过。
- **`uv run`**：本环境 uv 默认缓存不可写，需设 `$env:UV_CACHE_DIR` 指向可写目录。
- **依赖 CVE**：`python-multipart` 2026 年系列通告（CVE-2026-53537/53538/53539/53540、CVE-2026-42561）
  未取到可核实的定版区间，锁定版本 `0.0.32` 未确认命中，**未计为漏洞**；`starlette==1.7.0` 不命中
  CVE-2026-48710（BadHost，影响 `<1.0.1`）。
- **仓库内遗留目录**：`tmp-pt/`、`tmpuf3wp6tq/` 为本次测试过程中一次失败尝试产生的临时目录，
  受 ACL 保护无法删除（未跟踪、未被 `.gitignore` 覆盖，git 无法读取故不会被提交）；
  如需清理请以管理员身份 `Remove-Item -Recurse -Force`。测试前后仓库跟踪状态一致。

## 7. 目录内容

| 文件 | 内容 |
| --- | --- |
| `SUMMARY.md` | 本文件：交付摘要、结论统计、修复优先级、限制 |
| `pentest-report.md` | 完整渗透测试报告（目标/授权、探索链路全量事实、逐条漏洞与可复现步骤、资产图） |
| `git-version.txt` | 测试基准的 git 版本、分支与工作区状态 |
| `evidence/reproduction-index.md` | 按漏洞编号索引的可复现步骤入口（指向报告章节） |
| `evidence/env-limitations.md` | 复现所需环境变量与已知沙箱限制 |
