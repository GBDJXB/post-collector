# 复现环境与已知限制

## 1. 本测试使用的运行环境

| 项 | 值 |
| --- | --- |
| 仓库路径 | `D:\DSH\projects\post-collector` |
| git 提交 | `441d41b7b2e06f8332a99d7a9ef610633803fdd6`（`main`） |
| 虚拟环境 | 仓库内 `.venv`（Python 3.13.7） |
| 测试时工作区 | 干净（仅测试前既存的未跟踪 `.archify/`） |
| 测试数据位置 | 全部实验文件与临时数据库位于 `$env:TEMP` 下的唯一子目录，未写入仓库 `data/`、`migrations/` 或源码 |

## 2. 必需的复现调整

```powershell
# (1) uv 默认缓存不可写 —— 必须重定向
$env:UV_CACHE_DIR = "$env:TEMP\pc-uvcache"

# (2) pc serve / pc upload 的数据目录 —— 用 PC_DATA_ROOT，不是 PC__PATHS__DATA_ROOT
$env:PC_DATA_ROOT = "$env:TEMP\pc-lab\data"
$env:PC__DB__PATH = "$env:TEMP\pc-lab\data\pc.sqlite3"
uv run pc db apply
```

> 若不设置 `PC_DATA_ROOT`，upload-staging 会落到 `%LOCALAPPDATA%\post-collector` 并被沙箱拒绝，
> 表现为上传 503（易被误判为产品缺陷）。

## 3. 沙箱造成的硬性限制（已影响验证手段，不影响结论有效性）

| 限制 | 表现 | 采取的对策 |
| --- | --- | --- |
| `uv run` 缓存不可写 | `Access is denied` | 设 `UV_CACHE_DIR` 到 TEMP，或改用 `.venv\Scripts\python.exe` |
| pytest `tmp_path` | `PermissionError: WinError 5 ...pytest-of-lexin`；`--basetemp`/`TEMP` 重定向后仍在 `pytest_sessionfinish` 被拒 | 改用直接调用被审适配器的独立脚本；仅 `tests/test_accounts_auth.py` 全量通过 |
| esbuild 子进程 | `spawn EPERM` | 无法 `npx vite build` 做字节级重构建比对，改用源码↔产物字面量对照 + 危险构造扫描 + CSS 归一化比对 |
| 文件/目录符号链接 | `mklink` 需管理员权限（`You do not have sufficient privilege`） | 用 junction（`mklink /J`）验证同一 `_check_redirects` 代码路径，symlink 结论标注为静态、置信度中高 |
| 绑定 80 端口 | 无法绑定 | `--port 80` 场景仅静态分析（Host 期望含 `:80` 而浏览器省略端口 → 整体 403，失败关闭） |
| 写工作区外 | 沙箱拒绝 | `pc serve` 的数据目录全部指向 `$env:TEMP` |

## 4. 有意未执行的测试

- `actions/open|reveal` 使用真实内容 ID：会触发本机默认程序打开文件，为避免副作用，仅用非法 ID
  验证 400 校验路径。
- 破坏性操作：未对仓库 `data/`、`migrations/`、`config/` 做任何写入；迁移器绕过验证在 `$env:TEMP`
  下新建的迁移目录中进行。
- 未做压力型暴力破解：限流验证限 6~30 次小规模请求，并核查内部状态有界性。

## 5. 遗留物

- 仓库根目录存在本次测试中一次失败尝试产生的 `tmp-pt/`、`tmpuf3wp6tq/`（受 ACL 保护，非管理员
  无法删除）。二者未被 git 跟踪且未被 `.gitignore` 覆盖；git 因权限无法读取，不会将其纳入提交。
  清理方式（管理员）：`Remove-Item -Recurse -Force D:\DSH\projects\post-collector\tmp-pt, D:\DSH\projects\post-collector\tmpuf3wp6tq`。
- 测试期间启动的 `pc serve` 实例（8080 / 8081 / 18123 / 18124）均已停止，无残留监听进程。
