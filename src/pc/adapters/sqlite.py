"""SQLite 适配器：数据库路径解析、迁移应用、schema 校验、备份与设置层读写。

本模块取代了曾经的 ``pc.adapters.postgres``，职责仍然刻意收窄到五件事：

* 解析数据库**文件路径**，并回答「这个路径是从哪来的」（对应 DESIGN §6.2.4 的来源可解释性）；
* 把 ``migrations/sqlite/*.sql`` 应用到数据库（**整篇单事务**）；
* 报告库里的结构与迁移是否一致（表、FTS5 索引、PRAGMA、记账版本、checksum 漂移）；
* 备份（``VACUUM INTO`` + 完整性校验）与 FTS5 重建；
* ``setting`` / ``setting_change`` 两张表的读写（首次播种与带审计的变更）。

查询构造、业务规则与领域逻辑都不在这里：设置项的**语义**（类型、范围、依赖、
危险项闸门）属于 ``pc.config``，本模块只负责把它们落表，并保证「要么都写、要么
都不写」。

三条硬约束
----------

1. **事务由本模块显式管理**。连接以 ``isolation_level=None`` 建立（自动提交），
   因此 Python 的隐式事务机制不介入，``BEGIN IMMEDIATE`` / ``COMMIT`` /
   ``ROLLBACK`` 全部由 :func:`_begin` / :func:`_commit` / :func:`_rollback` 发出。
   这一点很重要：Python 3.12 起 DDL 不再隐式开启事务，若依赖隐式机制，
   ``CREATE TABLE`` 会落在自动提交里，「整篇迁移失败整体回滚」的承诺就会失效。
2. **PRAGMA 基线只在连接时执行**，绝不写进迁移文件（``journal_mode`` 在事务内
   无法切换，写进 ``.sql`` 会直接报错）。
3. **数据库访问收敛在本模块**（DESIGN §9.2）：业务代码不得自己
   ``sqlite3.connect()``——``foreign_keys`` 默认关闭，任何一处漏设都会让外键
   约束静默失效。

``EXPECTED_TABLES`` 是「迁移事实清单」的唯一来源，``pc db status`` 与集成测试共用它。
将来新增迁移时**必须同步更新它**——否则新表会被报成 unexpected，这是刻意的强制提醒。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

#: 默认路径（相对仓库根）
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_APP_CONFIG = Path("config/app.yaml")
#: 迁移目录（按序号应用其中的全部 ``*.sql``，见 :func:`discover_migrations`）
DEFAULT_MIGRATIONS_DIR = Path("migrations/sqlite")

#: 基线迁移文件（0001）；保留它是因为测试与工具常需要"那份真正建表的文件"
DEFAULT_MIGRATION_FILE = DEFAULT_MIGRATIONS_DIR / "0001_init.sql"

#: 数据库文件名：数据根目录下的这一个文件就是全部持久状态（DESIGN §10.2）
DB_FILENAME = "pc.sqlite3"

#: SQLite 版本门槛（DESIGN §3.2）：STRICT 表需 3.37、FTS5 trigram 需 3.34、
#: RETURNING 需 3.35，取 3.40 作为统一底线。
MIN_SQLITE_VERSION: tuple[int, int] = (3, 40)

#: 写锁等待毫秒数，与 DESIGN §3.2 的 PRAGMA 基线一致
BUSY_TIMEOUT_MS = 5000

#: 连接建立后立即执行的 PRAGMA 基线（DESIGN §3.2）。顺序有意义：
#: ``journal_mode`` 必须最先执行，且不能在事务内执行。
PRAGMA_BASELINE: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA temp_store = MEMORY",
)

#: 两个 FTS5 虚拟表（DESIGN §5.1）。它们由迁移脚本创建，索引可全量重建。
FTS_VIRTUAL_TABLES: tuple[str, ...] = ("content_fts", "content_word")

#: 0001 建出的 34 张基础表（含迁移记账表）。
#: 与 PostgreSQL 版的 32 张相比：新增 ``content_media``（媒体与内容项的多对多）
#: 与 ``hash_tombstone``（硬删后的哈希墓地），见 DESIGN §3.2、§3.8。
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "account_identity",
        "api_credential",
        "app_user",
        "audit_log",
        "classification_run",
        "content_item",
        "content_media",
        "fetch_log",
        "hash_tombstone",
        "inbox_item",
        "job",
        "link",
        "media",
        "message",
        "model_version",
        "permission",
        "retention_policy",
        "retention_run",
        "review_task",
        "role",
        "role_binding",
        "schema_migration",
        "setting",
        "setting_change",
        "source",
        "source_authorization",
        "storage_backend",
        "storage_usage",
        "sync_cursor",
        "tag",
        "tag_assignment",
        "tag_relation",
        "tag_synonym",
        "user_source_scope",
    }
)

#: 阶段 2 才引入、当前**必须缺席**的三张表。
#: 缺席本身就是「无外发」红线的一部分（DESIGN §8.1 的 MVP 明确不做外发）。
DEFERRED_TABLES: frozenset[str] = frozenset(
    {"content_embedding", "ai_provider_policy", "egress_log"}
)

#: ``setting.source`` 的允许取值，与 0001 的 CHECK 约束保持一致。
SETTING_SOURCES: tuple[str, ...] = ("default", "yaml", "db", "env")

#: ``setting_change.changed_via`` 的允许取值，与 0001 的 CHECK 约束保持一致。
SETTING_CHANGE_VIA: tuple[str, ...] = ("ui", "yaml", "env", "migration")


class DatabaseError(RuntimeError):
    """数据库相关问题的基类。

    子类消息一律是**可直接打印给人看的人话**（DESIGN §6.2.3「出错时知道怎么办」），
    CLI 不应该在它之后追加堆栈。
    """


class DatabaseUnavailableError(DatabaseError):
    """数据库文件打不开：路径不可写、磁盘满、SQLite 版本过旧、文件被占用超时。"""


class MigrationError(DatabaseError):
    """迁移应用失败。"""


class SchemaMismatchError(MigrationError):
    """库里的结构与迁移记账对不上（脏库）。不会自动修补，交给人处理。"""


class MigrationState(StrEnum):
    """当前库相对迁移目录的状态。"""

    EMPTY = "empty"  # 干净库：一张表都没有，可以全量应用
    APPLIED = "applied"  # 预期版本全部已应用
    PENDING = "pending"  # 已应用一部分，还有未应用的版本（正常的升级路径）
    PARTIAL = "partial"  # 脏库：有业务表但一条记账都没有


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """生效的数据库文件路径及其来源；``source`` 回答「这个值从哪来」。"""

    path: Path
    source: str

    def explain(self) -> str:
        """输出一句人话解释，供 CLI 与排错使用。"""
        return f"数据库路径来源：{self.source}"


# ---------------------------------------------------------------------------
# 版本门槛
# ---------------------------------------------------------------------------


def sqlite_version_ok(version_info: tuple[int, ...] | None = None) -> bool:
    """当前（或指定）的 SQLite 版本是否达到 :data:`MIN_SQLITE_VERSION`。"""
    current = sqlite3.sqlite_version_info if version_info is None else version_info
    return tuple(current[:2]) >= MIN_SQLITE_VERSION


def ensure_version_gate(version_info: tuple[int, ...] | None = None) -> None:
    """版本不足时 fail closed，并给出两条可执行的出路（DESIGN §3.2、§10.1）。

    Raises:
        DatabaseUnavailableError: SQLite 版本低于门槛。
    """
    if sqlite_version_ok(version_info):
        return
    current = ".".join(str(part) for part in (sqlite_version_info_of(version_info)))
    required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
    raise DatabaseUnavailableError(
        f"你的 Python 自带 SQLite {current}，低于本项目要求的 {required}。\n"
        "  两条出路：\n"
        "    1) 升级 Python（3.12+ 一般自带 3.40 以上）；\n"
        "    2) 改用随 Release 分发的单文件包（自带运行时，不依赖本机 Python）。"
    )


def sqlite_version_info_of(version_info: tuple[int, ...] | None) -> tuple[int, ...]:
    """把可能被测试替换的版本元组规范化成三元组，便于打印。"""
    if version_info is None:
        return tuple(sqlite3.sqlite_version_info)
    return tuple(version_info)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def read_env_file(path: Path | str | None = DEFAULT_ENV_FILE) -> dict[str, str]:
    """读取极简 ``.env``：``KEY=VALUE``，忽略注释与空行，剥离成对引号。

    刻意不做变量插值、不处理转义、不引入 python-dotenv：部署层变量是扁平的字面量，
    多一层语法就多一类「改了文件没生效」的故障（DESIGN §6.2 的单一来源原则）。
    """
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}

    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def default_data_root(
    platform: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """平台默认的用户数据目录（DESIGN §10.2）。

    Windows ``%LOCALAPPDATA%\\post-collector``、Linux
    ``$XDG_DATA_HOME/post-collector``（缺省 ``~/.local/share/post-collector``）、
    macOS ``~/Library/Application Support/post-collector``。
    """
    environ = os.environ if env is None else env
    name = sys.platform if platform is None else platform
    base_home = Path.home() if home is None else home

    if name.startswith("win"):
        raw = environ.get("LOCALAPPDATA")
        base = Path(raw) if raw else base_home / "AppData" / "Local"
        return base / "post-collector"
    if name == "darwin":
        return base_home / "Library" / "Application Support" / "post-collector"
    raw = environ.get("XDG_DATA_HOME")
    base = Path(raw) if raw else base_home / ".local" / "share"
    return base / "post-collector"


def now_iso() -> str:
    """当前时间的 ISO-8601 UTC 毫秒表示，与 SQL 默认值同形，可直接比较排序。"""
    now = datetime.now(UTC)
    return f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"


def resolve_db_path(
    explicit: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    env_file: Path | str | None = DEFAULT_ENV_FILE,
    app_config: Path | str | None = DEFAULT_APP_CONFIG,
) -> ResolvedPath:
    """按优先级解析出要用的数据库文件路径。

    优先级：``explicit`` > 环境变量 ``PC__DB__PATH`` > 环境变量 ``PC_DATA_ROOT``
    > ``.env`` 里的 ``PC__DB__PATH`` / ``PC_DATA_ROOT``
    > ``config/app.yaml`` 的 ``db.path`` / ``db.data_root`` > 平台默认数据目录。

    ``PC_DATA_ROOT`` 指的是**数据根目录**（媒体、缩略图、备份都挂在它下面），
    数据库文件固定叫 ``pc.sqlite3``；``PC__DB__PATH`` 则直接指定数据库文件本身。
    """
    environ = os.environ if env is None else env
    from_file = read_env_file(env_file)

    # 刻意把「进程环境」与「.env 文件」分成两层依次查，而不是按同一个键各查一次：
    # 后者会让 .env 里的 PC__DB__PATH 压过进程环境里的 PC_DATA_ROOT，与下面写明的
    # 优先级不符（.env 只是部署默认值，进程环境永远是更明确的意图）。
    def from_environ(key: str) -> tuple[str, str] | None:
        value = environ.get(key)
        return (value, f"env:{key}") if value else None

    def from_env_file(key: str) -> tuple[str, str] | None:
        value = from_file.get(key)
        return (value, f"env-file:{key}") if value else None

    if explicit:
        return ResolvedPath(Path(explicit).expanduser(), "explicit")

    for lookup in (from_environ, from_env_file):
        direct = lookup("PC__DB__PATH")
        if direct:
            return ResolvedPath(Path(direct[0]).expanduser(), direct[1])
        root = lookup("PC_DATA_ROOT")
        if root:
            return ResolvedPath(Path(root[0]).expanduser() / DB_FILENAME, root[1])

    configured = _db_paths_from_app_config(app_config)
    label = (
        Path(app_config).as_posix() if app_config is not None else DEFAULT_APP_CONFIG.as_posix()
    )
    if configured.get("path"):
        return ResolvedPath(
            Path(str(configured["path"])).expanduser(),
            f"{label}:db.path",
        )
    if configured.get("data_root"):
        return ResolvedPath(
            Path(str(configured["data_root"])).expanduser() / DB_FILENAME,
            f"{label}:db.data_root",
        )

    return ResolvedPath(default_data_root(env=environ) / DB_FILENAME, "default")


def _db_paths_from_app_config(path: Path | str | None) -> dict[str, str]:
    """从 ``config/app.yaml`` 读 ``db.path`` / ``db.data_root``；缺文件或缺 PyYAML 时跳过。"""
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        import yaml
    except ModuleNotFoundError:
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get("db")
    if not isinstance(section, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("path", "data_root"):
        value = section.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return result


#: 运行时目录：``(名字, app.yaml 键, 默认子目录名, 环境变量名)``（DESIGN §10.2）
RUNTIME_DIR_SPECS: tuple[tuple[str, str, str], ...] = (
    ("media", "media_root", "media"),
    ("thumb", "thumb_root", "thumbs"),
    ("cold", "cold_root", "cold"),
    ("quarantine", "quarantine_root", "quarantine"),
    ("backup", "backup_root", "backup"),
    ("models", "models_root", "models"),
)


@dataclass(frozen=True, slots=True)
class ResolvedPaths:
    """一次解析出来的运行时目录（数据根 + 六个派生目录）。

    每个目录都带**来源**，这样「为什么媒体跑到了别处」不需要靠猜（DESIGN §6.2 的
    可解释性要求，与 :class:`ResolvedPath` 同一套做法）。
    """

    data_root: Path
    root_source: str
    paths: Mapping[str, Path]
    sources: Mapping[str, str]

    def __getattr__(self, name: str) -> Path:
        if name.endswith("_root"):
            key = name[: -len("_root")]
            if key in self.paths:
                return self.paths[key]
        raise AttributeError(name)

    def explain(self) -> str:
        lines = [f"数据根目录：{self.data_root}（来源 {self.root_source}）"]
        for name in self.paths:
            lines.append(f"  {name}_root：{self.paths[name]}（来源 {self.sources[name]}）")
        return "\n".join(lines)


def _paths_from_app_config(path: Path | str | None) -> dict[str, str]:
    """从 ``config/app.yaml`` 读 ``db.data_root`` 与 ``paths.*``；缺文件时返回空。"""
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        import yaml
    except ModuleNotFoundError:
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}

    result: dict[str, str] = {}
    db_section = data.get("db")
    if isinstance(db_section, dict):
        value = db_section.get("data_root")
        if isinstance(value, str) and value.strip():
            result["data_root"] = value.strip()
    paths_section = data.get("paths")
    if isinstance(paths_section, dict):
        for _, key, _ in RUNTIME_DIR_SPECS:
            value = paths_section.get(key)
            if isinstance(value, str) and value.strip():
                result[key] = value.strip()
    return result


def resolve_paths(
    *,
    env: Mapping[str, str] | None = None,
    env_file: Path | str | None = DEFAULT_ENV_FILE,
    app_config: Path | str | None = DEFAULT_APP_CONFIG,
) -> ResolvedPaths:
    """解析数据根目录与六个运行时目录。

    优先级（逐项独立解析）：

    * 数据根目录：环境变量 ``PC_DATA_ROOT`` > ``.env`` 里的同名项 >
      ``config/app.yaml`` 的 ``db.data_root`` > 平台默认用户数据目录。
    * 各目录：环境变量 ``PC__PATHS__<NAME>_ROOT`` > ``.env`` 里的同名项 >
      ``config/app.yaml`` 的 ``paths.<name>_root`` > ``<data_root>/<默认子目录>``。

    **媒体目录刻意跟着数据根目录走，而不是跟着数据库文件走**：``PC__DB__PATH`` /
    ``db.path`` 只改数据库文件本身的位置（放进 SSD、放到挂载盘），如果它顺带把媒体
    也挪走，一次「把库放到别处」的操作就会静默改变对象存储的位置——对象键虽然不变，
    落盘位置变了同样意味着老内容找不到。

    容器与多盘部署用 ``paths.*`` / ``PC__PATHS__*`` 显式指定每一项。
    """
    environ = os.environ if env is None else env
    from_file = read_env_file(env_file)
    configured = _paths_from_app_config(app_config)
    label = (
        Path(app_config).as_posix() if app_config is not None else DEFAULT_APP_CONFIG.as_posix()
    )

    def lookup(env_key: str, yaml_key: str) -> tuple[str, str] | None:
        """依次查进程环境 → .env → app.yaml，返回 (值, 来源说明)。"""
        value = environ.get(env_key)
        if value:
            return value, f"env:{env_key}"
        value = from_file.get(env_key)
        if value:
            return value, f"env-file:{env_key}"
        value = configured.get(yaml_key)
        if value:
            return value, f"{label}:{yaml_key}"
        return None

    root_hit = lookup("PC_DATA_ROOT", "data_root")
    if root_hit is None:
        data_root = default_data_root(env=environ)
        root_source = "default"
    else:
        data_root = Path(root_hit[0]).expanduser()
        root_source = root_hit[1]

    paths: dict[str, Path] = {}
    sources: dict[str, str] = {}
    for name, yaml_key, default_dir in RUNTIME_DIR_SPECS:
        hit = lookup(f"PC__PATHS__{name.upper()}_ROOT", yaml_key)
        if hit is None:
            paths[name] = data_root / default_dir
            sources[name] = f"{root_source} + /{default_dir}"
        else:
            paths[name] = Path(hit[0]).expanduser()
            sources[name] = hit[1]

    return ResolvedPaths(
        data_root=data_root,
        root_source=root_source,
        paths=MappingProxyType(paths),
        sources=MappingProxyType(sources),
    )


# ---------------------------------------------------------------------------
# 连接与事务
# ---------------------------------------------------------------------------


def connect(path: Path | str, *, expect_exists: bool = False) -> sqlite3.Connection:
    """打开数据库文件并执行 PRAGMA 基线。

    Args:
        path: 数据库文件路径；父目录不存在时自动创建。
        expect_exists: 为 ``True`` 时要求文件已存在（``pc db status`` 这类只读命令用），
            否则给人话提示而不是创建一个空库。

    Returns:
        以 ``isolation_level=None``（自动提交）打开的连接，事务由本模块显式管理；
        ``row_factory`` 为 :class:`sqlite3.Row`，可按列名取值。

    Raises:
        DatabaseUnavailableError: 版本不足、目录不可写、文件打不开或 PRAGMA 失败。
    """
    ensure_version_gate()
    p = Path(path)
    if expect_exists and not p.is_file():
        raise DatabaseUnavailableError(
            f"数据库文件还不存在：{p}\n  先建库：uv run pc db apply --path \"{p}\""
        )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseUnavailableError(
            f"数据目录不可写：{p.parent}\n  请换一个路径（PC_DATA_ROOT / PC__DB__PATH），或检查权限。\n"
            f"  原始错误：{exc}"
        ) from exc

    try:
        conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError(
            f"打不开数据库文件：{p}\n  原始错误：{exc}"
        ) from exc

    conn.row_factory = sqlite3.Row
    try:
        for pragma in PRAGMA_BASELINE:
            conn.execute(pragma)
    except sqlite3.Error as exc:
        conn.close()
        raise DatabaseUnavailableError(
            f"数据库设置（PRAGMA 基线）执行失败：{p}\n  原始错误：{exc}"
        ) from exc
    return conn


@contextmanager
def open_db(path: Path | str, *, expect_exists: bool = False) -> Iterator[sqlite3.Connection]:
    """打开数据库并在退出时关闭连接。

    刻意提供这个包装：``sqlite3.Connection`` 的 ``with`` 协议管的是**事务**（退出时
    提交或回滚），**不是关闭连接**——直接 ``with sqlite3.connect(...)`` 会泄漏连接。
    CLI 与测试一律用本函数。
    """
    conn = connect(path, expect_exists=expect_exists)
    try:
        yield conn
    finally:
        conn.close()


def pragma_value(conn: sqlite3.Connection, name: str) -> str | None:
    """读取一个 PRAGMA 的值（用于体检与测试断言）。"""
    row = conn.execute(f"PRAGMA {name}").fetchone()
    if row is None:
        return None
    value = row[0]
    return None if value is None else str(value)


def database_file_of(conn: sqlite3.Connection) -> Path | None:
    """从连接反查数据库文件路径（内存库或临时库返回 ``None``）。"""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == "main":
            raw = str(row[2])
            return Path(raw) if raw else None
    return None


def _begin(conn: sqlite3.Connection) -> None:
    """显式开启写事务：``BEGIN IMMEDIATE`` 立刻取写锁，避免升级锁时的死锁窗口。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        # SQLite 的原话是 "cannot start a transaction within a transaction"，
        # 它几乎总是意味着调用方嵌了两层 db.transaction —— 直接说清楚，省一轮排查。
        if "within a transaction" in str(exc):
            raise DatabaseError(
                "已经在事务里了，不能嵌套 db.transaction()。\n"
                "  常见原因：在 with db.transaction(...) 里又调了一个自己开事务的辅助函数。\n"
                "  请把那次写入挪到外层事务之外（DESIGN §2.3 要求写事务尽量短）。"
            ) from exc
        raise


def _commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def _rollback(conn: sqlite3.Connection) -> None:
    # 事务可能因为更早的错误已经结束；回滚失败不该掩盖原始错误。
    with suppress(sqlite3.Error):
        conn.execute("ROLLBACK")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """把一组写操作包进一个显式事务：要么都写，要么都不写。"""
    _begin(conn)
    try:
        yield conn
    except BaseException:
        _rollback(conn)
        raise
    else:
        _commit(conn)


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def migration_checksum(sql: str) -> str:
    """计算迁移的**结构指纹**：剔除整行注释与空行后，取 SHA-256。

    刻意不直接对文件字节取哈希：迁移文件头就是用来追加修订记录的，
    整行注释的增删不该被报成「已应用的文件被改过」。剔除后，只有真正的 DDL 变化
    才会让指纹改变。

    保守取向：**行尾注释算内容**。要正确忽略它需要按字符串字面量做 SQL 词法分析，
    代价与风险都不划算；宁可多报一次「漂移」让人看一眼，也不要漏掉真正的 DDL 改动。
    """
    meaningful = [
        line.strip()
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    return hashlib.sha256("\n".join(meaningful).encode("utf-8")).hexdigest()


def is_comment_only(text: str) -> bool:
    """整段是否只有注释与空白（用于容忍文件末尾的尾注）。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return False
    return True


def split_statements(sql: str) -> list[str]:
    """把整篇 SQL 切成完整语句，供逐条执行。

    用标准库的 :func:`sqlite3.complete_statement` 判断「这一条是否写完了」，
    因此字符串字面量里的分号、``--`` 注释里的分号都不会切坏语句——
    这正是迁移文件头禁止朴素 ``split(';')`` 的原因。

    Raises:
        MigrationError: 文件以半截语句结尾（缺少分号），或含 ``PRAGMA`` 语句
            （PRAGMA 由 :func:`connect` 负责，写进迁移文件会在事务里报错）。
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            _append_statement(statements, buffer)
            buffer = ""
    if not is_comment_only(buffer):
        stripped = buffer.strip()
        # PRAGMA 缺分号时优先报它自己的错：那比「半截语句」更能说明该怎么改
        _reject_pragma(stripped)
        raise MigrationError(
            "迁移文件以半截语句结尾（最后一条缺少分号）：\n"
            f"  {stripped[:200]}\n"
            "  请在文件末尾补上分号后重试。"
        )
    return statements


def _reject_pragma(statement: str) -> None:
    """拒绝迁移文件里的 PRAGMA 语句（PRAGMA 基线属于适配器，不属于 schema）。"""
    if statement.lstrip().upper().startswith("PRAGMA"):
        raise MigrationError(
            "迁移文件里不允许出现 PRAGMA 语句：\n"
            f"  {statement[:200]}\n"
            "  PRAGMA 基线由适配器在建立连接时执行（journal_mode 在事务内无法切换）。"
        )


def _append_statement(statements: list[str], raw: str) -> None:
    statement = raw.strip()
    if not statement or is_comment_only(statement):
        return
    _reject_pragma(statement)
    statements.append(statement)


# ---------------------------------------------------------------------------
# 迁移文件的指令注释
#
# 有些 schema 改动在 SQLite 里**做不到**就地修改，最典型的是放宽 CHECK 约束：
# 没有 ALTER TABLE DROP CONSTRAINT，只能重建表（建新表 → 拷数据 → 删旧表 → 改名）。
# 而重建必须在**外键关闭**时进行，否则 DROP 旧表会做隐式 DELETE 并被子表的外键挡下。
#
# `PRAGMA foreign_keys` 在事务内是空操作，而迁移器保证"一个文件一个事务"——所以这条
# 指令必须是**声明式的**：迁移器在 BEGIN 之前关掉外键，提交前用 foreign_key_check
# 兜底，结束后恢复（恢复失败不掩盖真正的迁移错误）。迁移文件里仍然不允许任何
# PRAGMA 语句。
#
# 顺带记一条实测结论，省得后来者再照抄文档：**本项目不需要 `legacy_alter_table`**。
# SQLite 3.50.4 上开关两种设置下重建都成功，子表外键照样指向重建后的表——外键子句
# 只在运行时解析，指向一张暂时不存在的表不会让 `ALTER TABLE RENAME` 的 schema
# 重解析失败。真正需要它的是**视图与触发器**里引用旧表名，而这两张表上没有。
# ---------------------------------------------------------------------------

#: 指令注释：``-- pc:<名字> = <取值>``（整行）
_DIRECTIVE_PATTERN = re.compile(r"^--\s*pc:([a-z_]+)\s*=\s*(\S+)\s*$", re.MULTILINE)

#: 已知指令及其允许取值。**未知指令一律报错**——打错一个字母就静默失效，
#: 会让"重建表"在开着外键的情况下执行，报出来的错与真正的原因差着十万八千里。
KNOWN_DIRECTIVES: dict[str, frozenset[str]] = {"foreign_keys": frozenset({"off"})}


def migration_directives(sql: str) -> dict[str, str]:
    """解析迁移文件里的指令注释。

    Raises:
        MigrationError: 出现未知指令或未知取值。
    """
    directives: dict[str, str] = {}
    for match in _DIRECTIVE_PATTERN.finditer(sql):
        name, value = match.group(1), match.group(2)
        allowed = KNOWN_DIRECTIVES.get(name)
        if allowed is None:
            raise MigrationError(
                f"迁移文件里有无法识别的指令注释：-- pc:{name}={value}\n"
                f"  已知指令：{', '.join(sorted(KNOWN_DIRECTIVES))}。"
            )
        if value not in allowed:
            raise MigrationError(
                f"指令 -- pc:{name}={value} 的取值不认识；"
                f"允许：{', '.join(sorted(allowed))}。"
            )
        directives[name] = value
    return directives


def set_foreign_keys(conn: sqlite3.Connection, enabled: bool) -> None:
    """切换 ``foreign_keys`` 并**确认它真的生效**。

    `PRAGMA foreign_keys` 在事务里是空操作；如果这里不校验，重建迁移会在开着外键的
    情况下继续跑，然后在 `DROP TABLE` 处报一个与真实原因无关的错。
    """
    conn.execute(f"PRAGMA foreign_keys = {'ON' if enabled else 'OFF'}")
    is_on = _pragma_is_on(conn, "foreign_keys")
    if is_on != enabled:
        raise MigrationError(
            f"无法把 foreign_keys 切到 {'ON' if enabled else 'OFF'}"
            f"（当前 {'ON' if is_on else 'OFF'}）。\n"
            "  `pc:foreign_keys=off` 指令必须在事务之外生效——迁移器会在 BEGIN 之前调用它。"
        )


def _pragma_is_on(conn: sqlite3.Connection, name: str) -> bool:
    """读一个布尔型 PRAGMA。它返回的是 0/1，不是 ``ON``/``OFF``。"""
    raw = (pragma_value(conn, name) or "0").strip().upper()
    return raw in {"1", "ON", "TRUE", "YES"}


def check_foreign_keys(conn: sqlite3.Connection, name: str) -> None:
    """提交前的安全网：重建表之后必须没有外键违例（SQLite 官方 12 步流程的最后一步）。"""
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    if rows:
        detail = "；".join(f"{row[0]} 第 {row[1]} 行 → {row[2]}" for row in rows[:5])
        raise MigrationError(
            f"{name} 执行后存在外键违例，已整体回滚：{detail}"
            + ("（还有更多）" if len(rows) > 5 else "")
        )


def public_tables(conn: sqlite3.Connection) -> set[str]:
    """当前库里的**基础表**名。

    刻意排除三类：SQLite 内部表（``sqlite_*``）、FTS5 虚拟表本身
    （``content_fts`` / ``content_word``）以及它们的影子表
    （``content_fts_data`` / ``_idx`` / ``_docsize`` / ``_config`` 等）。
    影子表是 FTS5 的实现细节，把它们算进表清单会让「结构是否与迁移一致」失去意义。
    """
    result: set[str] = set()
    for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        name = str(row[0])
        if _is_virtual_table(row[1]) or is_fts_shadow_table(name):
            continue
        result.add(name)
    return result


def fts_tables(conn: sqlite3.Connection) -> set[str]:
    """库里存在的 FTS5 虚拟表名（应当恰好是 :data:`FTS_VIRTUAL_TABLES`）。"""
    result: set[str] = set()
    for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        if _is_virtual_table(row[1]):
            result.add(str(row[0]))
    return result


def _is_virtual_table(sql: Any) -> bool:
    return isinstance(sql, str) and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")


def is_fts_shadow_table(name: str) -> bool:
    """名字是否是某个 FTS5 虚拟表的影子表。"""
    return any(name.startswith(f"{virtual}_") for virtual in FTS_VIRTUAL_TABLES)


@dataclass(frozen=True, slots=True)
class MigrationFile:
    """一个迁移文件：版本号来自文件名前缀（``0002_xxx.sql`` → 2）。"""

    version: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


#: 迁移文件名：``<序号>_<描述>.sql``
_MIGRATION_FILENAME = re.compile(r"^(\d+)_(.+)\.sql$")

#: 记账表由**适配器兜底创建**（0001 里也有，带 IF NOT EXISTS，等价）。
#: 这样"迁移目录里恰好没写这张表"也不会让整个迁移器崩掉——记账表属于运行器的基础
#: 设施，而不是某一版 schema 的业务内容；而且它不计入"业务表"（见 migration_state）。
_SCHEMA_MIGRATION_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    checksum   TEXT,
    applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    note       TEXT
) STRICT
"""


def discover_migrations(directory: Path | str = DEFAULT_MIGRATIONS_DIR) -> list[MigrationFile]:
    """扫描迁移目录，按版本升序返回全部迁移文件。

    规则（同时写在 ``migrations/README.md``）：

    * 文件名形如 ``<序号>_<描述>.sql``，序号就是版本号；
    * **序号必须从 1 连续**（1、2、3…）——断号会让"哪些已应用"变得不可回答；
    * 同一序号只能有一个文件。

    Raises:
        MigrationError: 目录不存在、没有迁移文件、文件名不合规、序号重复或断号。
    """
    base = Path(directory)
    if not base.is_dir():
        raise MigrationError(
            f"找不到迁移目录：{base}\n"
            "  三种办法：\n"
            "    1) 在仓库根目录运行（那里有 migrations/sqlite/）；\n"
            "    2) 用 --dir 指定迁移目录的绝对路径；\n"
            "    3) 用 --root 指定仓库根目录，迁移目录会从它下面找。\n"
            "  如果是用 uv tool install 装的，那是它没有随包带上迁移文件（已知的分发缺口），"
            "请从仓库源码运行。"
        )

    found: dict[int, Path] = {}
    for path in sorted(base.iterdir()):
        if not path.is_file():
            continue
        match = _MIGRATION_FILENAME.match(path.name)
        if match is None:
            if path.suffix == ".sql":
                raise MigrationError(
                    f"迁移文件名不合规：{path.name}；应形如 0002_描述.sql（前缀是版本号）。"
                )
            continue
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"迁移版本 {version} 出现了两个文件：{found[version].name} 与 {path.name}。"
            )
        found[version] = path

    if not found:
        raise MigrationError(f"迁移目录里没有任何 .sql 文件：{base}")
    expected = list(range(1, len(found) + 1))
    if sorted(found) != expected:
        raise MigrationError(
            f"迁移序号必须从 1 连续：实际 {sorted(found)}，期望 {expected}。"
        )
    return [MigrationFile(version=version, path=found[version]) for version in expected]


def latest_migration_version(directory: Path | str = DEFAULT_MIGRATIONS_DIR) -> int:
    """迁移目录里的最大版本号（也就是"这个程序认识的 schema 版本"）。"""
    return discover_migrations(directory)[-1].version


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """``schema_migration`` 里已记录的版本号；表不存在时返回空集。"""
    if "schema_migration" not in public_tables(conn):
        return set()
    rows = conn.execute("SELECT version FROM schema_migration").fetchall()
    return {int(row[0]) for row in rows}


def migration_state(
    conn: sqlite3.Connection, files: list[MigrationFile] | None = None
) -> MigrationState:
    """判断当前库相对迁移目录处于什么状态。

    ``PENDING`` 是**正常**的升级路径（已应用一部分）；``PARTIAL`` 才是脏库
    （有业务表却一条记账都没有），只有它会让 :func:`apply_migration` 拒绝动手。
    """
    expected = {f.version for f in (files if files is not None else discover_migrations())}
    recorded = applied_versions(conn)
    actual = public_tables(conn)
    has_business_tables = bool(actual & (EXPECTED_TABLES - {"schema_migration"}))

    if not recorded:
        return MigrationState.PARTIAL if has_business_tables else MigrationState.EMPTY
    if expected <= recorded:
        return MigrationState.APPLIED
    return MigrationState.PENDING


def recorded_checksum(conn: sqlite3.Connection, version: int) -> str | None:
    """某个版本在 ``schema_migration`` 里记录的 checksum；未记录时为 ``None``。"""
    if "schema_migration" not in public_tables(conn):
        return None
    row = conn.execute(
        "SELECT checksum FROM schema_migration WHERE version = ?", (version,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _store_checksum(conn: sqlite3.Connection, version: int, checksum: str) -> None:
    conn.execute(
        "UPDATE schema_migration SET checksum = ? WHERE version = ?",
        (checksum, version),
    )


def _record_migration(conn: sqlite3.Connection, version: int, name: str, checksum: str) -> None:
    """写入（或补齐）某个版本的记账行；由 :func:`apply_migration` 在同一事务内调用。"""
    conn.execute(
        "INSERT INTO schema_migration (version, name, checksum, applied_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (version) DO UPDATE SET name = excluded.name, checksum = excluded.checksum",
        (version, name, checksum, now_iso()),
    )


def apply_migration(
    conn: sqlite3.Connection, directory: Path | str = DEFAULT_MIGRATIONS_DIR
) -> dict[str, Any]:
    """把迁移目录里**尚未应用**的版本按序应用。

    行为契约：

    * 每个文件在**自己的显式事务**内整体执行，成功后写入该版本的记账行；某个文件失败
      只回滚它自己，已成功的版本保持已应用（这是"一个文件一个事务"的取舍：比"全目录
      一个大事务"更容易恢复，也不会因为第 3 个文件失败而回退掉前两个）。
    * 已应用的版本不重复执行；若 checksum 未记录则补记（``checksum_backfilled``）。
    * 已应用的版本与文件内容不符 -> 计入 ``drifted`` 而**不覆盖**（漂移要被看见，
      不该被抹掉）。
    * 脏库（有业务表却一条记账都没有）-> 抛 :class:`SchemaMismatchError`，不猜测、不修补。

    Returns:
        含 ``applied`` / ``skipped`` / ``drifted`` / ``checksum_backfilled`` / ``statements``
        的结果字典（``applied`` 等是版本号列表）。
    """
    files = discover_migrations(directory)
    state = migration_state(conn, files)
    if state is MigrationState.PARTIAL:
        actual = sorted(public_tables(conn))
        raise SchemaMismatchError(
            "数据库处于半成品状态：存在业务表但迁移记账完全缺失。\n"
            f"  实际表（{len(actual)} 张）：{', '.join(actual) or '(无)'}\n"
            "  本工具**不会**自动修补这种情况。建议先 `pc db backup` 留档，再删除该文件重建：\n"
            "    uv run pc db apply"
        )

    # 记账表是运行器的基础设施：先确保它存在，再谈"应用了哪些版本"
    if "schema_migration" not in public_tables(conn):
        with transaction(conn):
            conn.execute(_SCHEMA_MIGRATION_DDL)

    recorded = applied_versions(conn)
    applied: list[int] = []
    skipped: list[int] = []
    drifted: list[int] = []
    backfilled: list[int] = []
    statements_total = 0

    for migration in files:
        checksum = migration_checksum(migration.path.read_text(encoding="utf-8"))
        if migration.version in recorded:
            skipped.append(migration.version)
            known = recorded_checksum(conn, migration.version)
            if known is None:
                with transaction(conn):
                    _store_checksum(conn, migration.version, checksum)
                backfilled.append(migration.version)
            elif known != checksum:
                drifted.append(migration.version)
            continue

        statements = split_statements(migration.path.read_text(encoding="utf-8"))
        directives = migration_directives(migration.path.read_text(encoding="utf-8"))
        rebuild = directives.get("foreign_keys") == "off"
        try:
            if rebuild:
                # 必须在 BEGIN 之前：PRAGMA foreign_keys 在事务内是空操作
                set_foreign_keys(conn, False)
            with transaction(conn):
                for statement in statements:
                    conn.execute(statement)
                if rebuild:
                    check_foreign_keys(conn, migration.name)
                _record_migration(conn, migration.version, migration.name, checksum)
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(
                f"应用 {migration.name} 失败，已在单事务内整体回滚，该版本未被记账。\n"
                f"  之前成功的版本仍然有效（每个文件一个事务）。\n原始错误：{exc}"
            ) from exc
        finally:
            if rebuild:
                # 恢复失败**不能掩盖**真正的迁移错误（否则排查会被带偏到错误的方向）；
                # 只有本来没有异常在途时，才把恢复失败抛出去。
                in_flight = sys.exc_info()[0] is not None
                try:
                    set_foreign_keys(conn, True)
                except MigrationError:
                    if not in_flight:  # pragma: no cover - 罕见路径
                        raise
        applied.append(migration.version)
        statements_total += len(statements)

    return {
        "applied": applied,
        "skipped": skipped,
        "drifted": drifted,
        "checksum_backfilled": backfilled,
        "statements": statements_total,
        "latest": files[-1].version,
    }


# ---------------------------------------------------------------------------
# 状态清单
# ---------------------------------------------------------------------------


def integrity_check(conn: sqlite3.Connection) -> str:
    """``PRAGMA integrity_check`` 的结论；``ok`` 表示结构与索引自洽。"""
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "unknown"


def database_status(
    conn: sqlite3.Connection, directory: Path | str = DEFAULT_MIGRATIONS_DIR
) -> dict[str, Any]:
    """只读地报告「库里的结构是否与迁移目录一致」。

    Returns:
        含下列键的字典（全部为只读探测结果）：

        ``sqlite_version`` / ``db_path`` / ``journal_mode`` / ``foreign_keys`` /
        ``fts_tables`` / ``fts_ok`` / ``integrity`` / ``tables`` / ``missing_tables`` /
        ``unexpected_tables`` / ``deferred_present`` / ``state`` /
        ``migrations_expected`` / ``migrations_applied`` / ``migrations_missing`` /
        ``migrations_unknown`` / ``migration_version`` / ``migrations_dir_present`` /
        ``checksum_drift_versions`` / ``checksum_unrecorded_versions`` /
        ``checksum_recorded`` / ``checksum_drift`` / ``ok``
    """
    actual = public_tables(conn)
    expected = set(EXPECTED_TABLES)
    missing = tuple(sorted(expected - actual))
    unexpected = tuple(sorted(actual - expected))
    deferred_present = tuple(sorted(DEFERRED_TABLES & actual))

    base = Path(directory)
    dir_present = base.is_dir()
    files: list[MigrationFile] = []
    if dir_present:
        try:
            files = discover_migrations(base)
        except MigrationError:
            files = []
    expected_versions = tuple(f.version for f in files)
    actual_versions = applied_versions(conn)
    missing_versions = tuple(v for v in expected_versions if v not in actual_versions)
    unknown_versions = tuple(sorted(actual_versions - set(expected_versions)))

    drift_versions: list[int] = []
    backfillable: list[int] = []
    for migration in files:
        if migration.version not in actual_versions:
            continue
        known = recorded_checksum(conn, migration.version)
        current = migration_checksum(migration.path.read_text(encoding="utf-8"))
        if known is None:
            backfillable.append(migration.version)
        elif known != current:
            drift_versions.append(migration.version)

    state = migration_state(conn, files) if files else MigrationState.PARTIAL
    present_fts = fts_tables(conn)
    fts_ok = set(FTS_VIRTUAL_TABLES).issubset(present_fts)
    journal_mode = pragma_value(conn, "journal_mode") or "unknown"
    foreign_keys = (pragma_value(conn, "foreign_keys") or "0") == "1"
    integrity = integrity_check(conn)
    applied_max = max(actual_versions) if actual_versions else None

    ok = (
        state is MigrationState.APPLIED
        and not missing
        and not deferred_present
        and not unknown_versions
        and fts_ok
        and journal_mode.lower() == "wal"
        and foreign_keys
        and integrity == "ok"
        # 拿不到迁移目录就无法比对结构指纹，此时不能声称「一致」
        and dir_present
        and not drift_versions
    )

    db_path = database_file_of(conn)
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "db_path": str(db_path) if db_path else "(内存库)",
        "journal_mode": journal_mode,
        "foreign_keys": foreign_keys,
        "fts_tables": tuple(sorted(present_fts)),
        "fts_ok": fts_ok,
        "integrity": integrity,
        "tables": tuple(sorted(actual)),
        "missing_tables": missing,
        "unexpected_tables": unexpected,
        "deferred_present": deferred_present,
        "state": str(state),
        "migrations_expected": expected_versions,
        "migrations_applied": tuple(sorted(actual_versions)),
        "migrations_missing": missing_versions,
        "migrations_unknown": unknown_versions,
        "migration_version": applied_max,
        "migrations_dir_present": dir_present,
        "checksum_drift_versions": tuple(drift_versions),
        "checksum_unrecorded_versions": tuple(backfillable),
        "checksum_recorded": bool(actual_versions) and not backfillable,
        "checksum_drift": bool(drift_versions),
        "ok": ok,
    }


# ---------------------------------------------------------------------------
# 备份与索引重建（DESIGN §7.1、§9.1）
# ---------------------------------------------------------------------------


def backup_filename(now: datetime | None = None) -> str:
    """默认备份文件名：``pc-YYYYMMDDTHHMMSSZ.sqlite3``（UTC，可直接排序）。"""
    moment = datetime.now(UTC) if now is None else now
    return f"pc-{moment:%Y%m%dT%H%M%SZ}.sqlite3"


def backup_to(conn: sqlite3.Connection, target: Path | str) -> dict[str, Any]:
    """用 ``VACUUM INTO`` 生成一个事务一致的快照，并校验它可读且结构自洽。

    刻意**拒绝覆盖已存在的文件**：备份目标被静默覆盖是丢数据的经典方式。

    Returns:
        含 ``target`` / ``size_bytes`` / ``tables`` / ``integrity`` 的结果字典。
    """
    destination = Path(target)
    if destination.exists():
        raise DatabaseError(f"备份目标已存在，拒绝覆盖：{destination}\n  换个文件名，或先移走它。")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseError(f"备份目录不可写：{destination.parent}\n  原始错误：{exc}") from exc

    escaped = str(destination).replace("'", "''")
    try:
        conn.execute(f"VACUUM INTO '{escaped}'")
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"备份失败（VACUUM INTO）：{destination}\n  原始错误：{exc}"
        ) from exc

    check = sqlite3.connect(str(destination))
    check.row_factory = sqlite3.Row
    try:
        integrity = integrity_check(check)
        tables = len(public_tables(check))
    finally:
        check.close()

    return {
        "target": str(destination),
        "size_bytes": destination.stat().st_size,
        "tables": tables,
        "integrity": integrity,
    }


def reindex_fts(conn: sqlite3.Connection) -> dict[str, Any]:
    """全量重建两个 FTS5 索引，返回重建前后的行数。

    索引是派生数据，随时可以重建（DESIGN §9.1 的 ``SearchIndex.rebuild``）。
    正常路径由触发器维持同步，本函数是兜底与排错手段。
    """
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    try:
        with transaction(conn):
            for virtual in FTS_VIRTUAL_TABLES:
                before[virtual] = _fts_row_count(conn, virtual)
                conn.execute(f"INSERT INTO {virtual}({virtual}) VALUES('rebuild')")
                after[virtual] = _fts_row_count(conn, virtual)
    except sqlite3.Error as exc:
        raise DatabaseError(f"重建 FTS5 索引失败：{exc}") from exc
    return {"before": before, "after": after}


def _fts_row_count(conn: sqlite3.Connection, virtual: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {virtual}").fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# 设置层读写（DESIGN §6.2）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingRow:
    """``setting`` 表的一行：值 + 它是怎么来的。"""

    key: str
    value: Any
    value_type: str
    source: str


def _json_text(value: Any) -> str:
    """把任意值渲染成 JSON 文本，供 ``TEXT`` 列存储。"""
    return json.dumps(value, ensure_ascii=False)


def _decode(value: Any) -> Any:
    """把 ``TEXT`` 列里的 JSON 文本解回 Python 对象。

    与 PostgreSQL 版行为对齐：那边 psycopg 会把 ``jsonb`` 直接解成 Python 对象，
    这里显式做同样的事，调用方无需关心存储形态。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _ensure_table(conn: sqlite3.Connection, table: str, hint: str) -> None:
    """确认某张表存在；不存在时给人话提示，而不是把驱动报错原样抛给用户。"""
    if table not in public_tables(conn):
        raise DatabaseError(f"库里没有 {table} 表。{hint}")


def read_settings(conn: sqlite3.Connection) -> dict[str, SettingRow]:
    """读出全部设置项行（只读）。

    Raises:
        DatabaseError: 库里没有 ``setting`` 表——那说明迁移还没应用。
    """
    _ensure_table(conn, "setting", "请先应用迁移：uv run pc db apply")
    rows = conn.execute(
        "SELECT key, value, value_type, source FROM setting ORDER BY key"
    ).fetchall()
    return {
        str(row["key"]): SettingRow(
            key=str(row["key"]),
            value=_decode(row["value"]),
            value_type=str(row["value_type"]),
            source=str(row["source"]),
        )
        for row in rows
    }


def settings_revision(conn: sqlite3.Connection) -> int:
    """当前配置版本号：``setting_change`` 的最大编号（0 表示从未改过）。

    对应 DESIGN §6.2.2 里那条「发布单调递增的配置版本号」：进程之间靠它判断
    「我手里这份快照是不是旧的」。播种不算变更，所以干净实例是 0。
    """
    if "setting_change" not in public_tables(conn):
        return 0
    row = conn.execute("SELECT COALESCE(MAX(change_id), 0) FROM setting_change").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def seed_settings(
    conn: sqlite3.Connection, seeds: Iterable[tuple[str, Any, str, str]]
) -> dict[str, list[str]]:
    """首次启动播种 ``setting`` 表：**只插入缺失的键，绝不覆盖已有值**。

    Args:
        seeds: ``(key, value, value_type, source)`` 四元组（``pc.config.setting_seeds``
            生成）。``source`` 必须是 :data:`SETTING_SOURCES` 里的取值。

    Returns:
        ``{"inserted": [...], "existing": [...]}``：本次真正插入的键，与因为
        已经存在而原样保留的键。幂等——重复运行不会改动任何已初始化的实例。

    刻意**不写** ``setting_change``：播种不是谁做的一次改动，而是一次初始化。
    审计表留给真正的变更，否则「谁把值改了」会被初始化噪音淹没。

    「是否真的插入了」靠 ``RETURNING key`` 判断，而不是 ``cursor.rowcount``：
    冲突跳过的 rowcount 是实现细节，RETURNING 是明确契约。
    """
    inserted: list[str] = []
    existing: list[str] = []
    _ensure_table(conn, "setting", "请先应用迁移：uv run pc db apply")
    try:
        with transaction(conn):
            for key, value, value_type, source in seeds:
                if source not in SETTING_SOURCES:
                    raise DatabaseError(
                        f"播种 {key} 的 source={source!r} 不合法；"
                        f"可用取值：{', '.join(SETTING_SOURCES)}。"
                    )
                row = conn.execute(
                    "INSERT INTO setting (key, value, value_type, source) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (key) DO NOTHING RETURNING key",
                    (key, _json_text(value), value_type, source),
                ).fetchone()
                (inserted if row else existing).append(key)
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(
            f"播种设置项失败，已在单事务内整体回滚，数据库未被改动。\n原始错误：{exc}"
        ) from exc
    return {"inserted": inserted, "existing": existing}


def write_setting_change(
    conn: sqlite3.Connection,
    *,
    key: str,
    value: Any,
    value_type: str,
    source: str = "db",
    changed_via: str = "ui",
    actor: int | None = None,
    precheck: Mapping[str, Any] | None = None,
    dangerous: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """写入一个设置项并留下审计记录：**两张表同生共死**。

    设计约束（DESIGN §6.2.2）：

    * ``setting`` 的新值与 ``setting_change`` 的前后值在一个事务里落库；
      任何一步失败都整体回滚，界面据此显示错误原因，而不是留下「值变了但没记录」
      或「记录写了但值没变」。
    * ``dangerous`` 的项必须 ``confirmed=True`` —— 这个闸门在适配器里再拦一次，
      因为它是「误操作会毁数据」的那一类，不该只靠调用方自觉。
    * 校验（类型、范围、依赖、dry-run 预览）由 ``pc.config.plan_setting_change``
      负责，本函数收到的应当已经是校验过的值；``precheck`` 原样存档，供日后
      回答「当时按什么影响面确认的」。

    Returns:
        含 ``change_id`` / ``before`` / ``after`` / ``applied`` 的结果字典。

    Raises:
        DatabaseError: 参数不合法、危险项未确认，或写库失败（已回滚）。
    """
    if changed_via not in SETTING_CHANGE_VIA:
        raise DatabaseError(
            f"changed_via={changed_via!r} 不合法；可用取值：{', '.join(SETTING_CHANGE_VIA)}。"
        )
    if source not in SETTING_SOURCES:
        raise DatabaseError(
            f"source={source!r} 不合法；可用取值：{', '.join(SETTING_SOURCES)}。"
        )
    if dangerous and not confirmed:
        raise DatabaseError(
            f"{key} 是危险项，必须显式确认（confirmed=True）后才能写入。未经确认的改动一律不落库。"
        )

    _ensure_table(conn, "setting", "请先应用迁移：uv run pc db apply")
    _ensure_table(conn, "setting_change", "请先应用迁移：uv run pc db apply")

    try:
        with transaction(conn):
            # 取写锁后再读旧值，避免「读了旧值、写时已被别人改过」的竞态。
            # SQLite 没有 SELECT ... FOR UPDATE，这里靠 BEGIN IMMEDIATE 达到同样效果。
            row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
            before = _decode(row["value"]) if row else None

            conn.execute(
                "INSERT INTO setting (key, value, value_type, source, last_validated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "value_type = excluded.value_type, source = excluded.source, "
                "updated_at = ?, last_validated_at = ?",
                (key, _json_text(value), value_type, source, now_iso(), now_iso(), now_iso()),
            )

            change = conn.execute(
                "INSERT INTO setting_change "
                "(key, before_state, after_state, changed_by, changed_via, precheck_result, "
                "applied, applied_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?) RETURNING change_id",
                (
                    key,
                    _json_text(before),
                    _json_text(value),
                    actor,
                    changed_via,
                    _json_text(dict(precheck or {})),
                    now_iso(),
                ),
            ).fetchone()
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(
            f"写入设置项 {key} 失败，已在单事务内整体回滚（值与审计记录都没有改动）。\n"
            f"原始错误：{exc}"
        ) from exc

    return {
        "key": key,
        "change_id": int(change[0]) if change else None,
        "before": before,
        "after": value,
        "changed_via": changed_via,
        "applied": True,
    }


__all__ = [
    "BUSY_TIMEOUT_MS",
    "DB_FILENAME",
    "DEFAULT_APP_CONFIG",
    "DEFAULT_ENV_FILE",
    "DEFAULT_MIGRATIONS_DIR",
    "DEFAULT_MIGRATION_FILE",
    "DEFERRED_TABLES",
    "EXPECTED_TABLES",
    "FTS_VIRTUAL_TABLES",
    "KNOWN_DIRECTIVES",
    "MIN_SQLITE_VERSION",
    "PRAGMA_BASELINE",
    "RUNTIME_DIR_SPECS",
    "SETTING_CHANGE_VIA",
    "SETTING_SOURCES",
    "DatabaseError",
    "DatabaseUnavailableError",
    "MigrationError",
    "MigrationFile",
    "MigrationState",
    "ResolvedPath",
    "ResolvedPaths",
    "SchemaMismatchError",
    "SettingRow",
    "applied_versions",
    "apply_migration",
    "backup_filename",
    "backup_to",
    "check_foreign_keys",
    "connect",
    "database_file_of",
    "database_status",
    "default_data_root",
    "discover_migrations",
    "ensure_version_gate",
    "fts_tables",
    "integrity_check",
    "is_comment_only",
    "is_fts_shadow_table",
    "latest_migration_version",
    "migration_checksum",
    "migration_directives",
    "migration_state",
    "now_iso",
    "open_db",
    "pragma_value",
    "public_tables",
    "read_env_file",
    "read_settings",
    "recorded_checksum",
    "reindex_fts",
    "resolve_db_path",
    "resolve_paths",
    "seed_settings",
    "set_foreign_keys",
    "settings_revision",
    "split_statements",
    "sqlite_version_ok",
    "transaction",
    "write_setting_change",
]
