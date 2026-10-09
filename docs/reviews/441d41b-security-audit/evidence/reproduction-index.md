# 复现索引（按漏洞编号）

所有漏洞的完整描述与**命令级可复现步骤**在 `../pentest-report.md` 的对应 `### finding-N`
章节内。下表给出快速定位与复现所需的基准环境。

## 复现前置（所有条目通用）

```powershell
cd D:\DSH\projects\post-collector
$env:UV_CACHE_DIR = "$env:TEMP\pc-uvcache"      # 本沙箱 uv 默认缓存不可写
$env:PC_DATA_ROOT  = "$env:TEMP\pc-lab\data"     # 必须用 PC_DATA_ROOT（不是 PC__PATHS__DATA_ROOT）
$env:PC__DB__PATH  = "$env:TEMP\pc-lab\data\pc.sqlite3"
uv run pc db apply
```

> 直接用 `.venv\Scripts\python.exe` + `sys.path.insert(0,'src')` 调用被审适配器，可绕过
> `uv`/`pytest` 的沙箱限制（见 `env-limitations.md`），报告中多数步骤已采用该形式。

## 编号 → 章节 → 复现要点

| # | 级别 | 报告章节 | 复现要点（最短路径） |
| --- | --- | --- | --- |
| 19 | high | `### finding-19` | 临时目录建 roots + 根外机密文件 → `mklink /H` → `pc source add/import/work` → `pc search PCPT7` 与 `GET /api/v1/media/4/1` 回读原字节 |
| 1 | medium | `### finding-1` | 同 19 的读取段：`LocalFilesConnector.discover()` + `fetch_media` 返回 root 外内容（`st_nlink=2`、同 inode） |
| 16 | medium | `### finding-16` | 临时迁移目录写 `/* note */ PRAGMA writable_schema=ON;` + `UPDATE sqlite_master` → `db.apply_migration` 输出 `applied:[1]`，随后查 `sqlite_master` 见被改写 DDL |
| 10 | medium | `### finding-10` | `httpx.ASGITransport` 内嵌 `create_remote_app` → 同一任意用户名发满 30 次 → 真实账号正确口令得 `429 + Retry-After: 60` |
| 11 | medium | `### finding-11` | 对远程应用发 520 次带空 Cookie 的 `GET /` → 第 513 次 429，`app.state.auth.forms` 恒为 512 |
| 2 | low | `### finding-2` | `open_scoped(ROOT/'inside.txt:evil',[ROOT])` 成功读出 ADS 内容 |
| 9 | low | `### finding-9` | `curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8080/openapi.json` → 200 |
| 20 | low | `### finding-20` | 带会话请求 `/api/v1/assignments?message_ids=0` → 400 响应含 `Targets` 与 `https://errors.pydantic.dev/2.13/...` |
| 7 | low | `### finding-7` | 静态定位：`Select-String -Path src\pc\api\app.py -Pattern 'str\(exc\)'` + `safe_files.py` 内嵌绝对路径的异常消息 |
| 12 / 5 | low | `### finding-12` / `### finding-5` | 换会话后 `POST /api/v1/logout`，对比两条 `Set-Cookie`（登录 `HttpOnly; SameSite=strict` → 删除 `SameSite=lax` 且无 `HttpOnly`） |
| 13 | low | `### finding-13` | 内存库 + `view_files.read_images`，`scan_status='skipped'` 输出 1；改 `pending`/`suspicious` 抛 `ViewError` |
| 14 | low | `### finding-14` | 预置硬链接对象后 `LocalStorageBackend.put(b'planted')` 返回正确键，磁盘字节仍为 `VICTIM-CONTENT` |
| 15 | low | `### finding-15` | `MagicNumberScanner().scan(b'\x00\x00\x00\x20ftyp')` → `passed=True, mime=video/mp4` |
| 18 | low | `### finding-18` | `run_search(conn, SearchQuery(q='safe', page=10**18))` → `OverflowError`；HTTP `?page=1000000000000000000` → 500 |
| 17 | low | `### finding-17` | TEMP 复制注册表并追加重复 `dangerous:` 键 → `registry_index(...).dangerous` 为 `False` 且无报错 |
| 6 | low | `### finding-6` | `terminal_text('gpj.exe\u202e')` 断言 RLO 未被过滤；对照 ESC/OSC 8 已被移除 |
| 8 | low | `### finding-8` | `Select-String -Path Dockerfile -Pattern '^USER\s'` 无输出；`FROM`/`uv:latest`/`uv sync` 可变标签 |
| 3 / 4 | medium | `### finding-3` / `### finding-4` | 与 10 / 11 等价的并行复现记录 |

## 已动态验证为"不可利用"的方向（可用于关闭假设）

路径穿越（`..`/UNC/`\\?\`/`\\.\`/8.3 短名/junction/symlink）· 上传文件名与暂存清理边界 ·
SQL 注入 · FTS5 语法注入 · 游标伪造越权 · 配置四层优先级与危险项闸门 · WAL 读快照与丢失更新 ·
存储型 XSS（React 文本节点 + 严格 CSP + 静态 HTML 转义）· 终端 ANSI/OSC 注入 · 会话固定与
账号枚举 · Host/Origin/转发头绕过 · CSRF 覆盖 · 媒体对象键与哈希校验。

对应的动态证据见 `../pentest-report.md` 的 facts 段落（fact-1 … fact-105）。
