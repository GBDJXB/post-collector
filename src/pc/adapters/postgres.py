"""PostgreSQL 适配器：连接串解析、迁移应用与 schema 校验。

本模块是 adapters 层的第一个实现，职责刻意收窄到三件事：

* 解析连接串，并回答「这个连接串是从哪来的」（对应 DESIGN §6.2.4 的来源可解释性）；
* 把 ``migrations/*.sql`` 应用到数据库；
* 报告库里的结构与迁移是否一致（表、扩展、记账版本、checksum 漂移）。

查询构造、业务规则与领域逻辑都不在这里。

两条硬约束：

1. **整篇 SQL 一次执行**。``migrations/0001_init.sql`` 的文件头明确禁止按分号切分语句；
   psycopg 在「无参数 + 文本格式」时走简单查询协议（``PQsendQuery``），
   一次调用即可执行多条语句——这正是本模块依赖的行为，因此绝不使用
   ``sql.split(";")`` 之类的土办法。
2. **密码不落日志**。任何要打印或记录的连接串都必须先过 :func:`mask_dsn`（DESIGN §6.1：
   密钥禁止写入日志）。

``EXPECTED_TABLES`` 是「迁移事实清单」的唯一来源，``pc db status`` 与集成测试共用它。
将来新增迁移时**必须同步更新它**——否则新表会被报成 unexpected，这是刻意的强制提醒。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

#: 默认路径（相对仓库根）
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_APP_CONFIG = Path("config/app.yaml")
DEFAULT_MIGRATION_FILE = Path("migrations/0001_init.sql")

#: 当前由代码认识的迁移版本（``schema_migration.version``）
MIGRATION_VERSION = 1

#: 内置兜底连接串，与 ``config/app.yaml`` 的 ``db.dsn`` 保持一致
DEFAULT_DSN = "postgresql://pc:change-me@localhost:5432/pc"

#: 0001 建出的 32 张表（含迁移记账表），已在真实 PostgreSQL 16.15 上核对
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "account_identity",
        "api_credential",
        "app_user",
        "audit_log",
        "classification_run",
        "content_item",
        "fetch_log",
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
#: 缺席本身就是「无外发」红线的一部分（DESIGN §8.1 的 MVP 明确不做外发；
#: 偏差[6] 记录在 migrations/0001_init.sql 文件头）。
DEFERRED_TABLES: frozenset[str] = frozenset(
    {"content_embedding", "ai_provider_policy", "egress_log"}
)


class DatabaseError(RuntimeError):
    """数据库相关问题的基类。

    子类消息一律是**可直接打印给人看的人话**（DESIGN §6.2.3「出错时知道怎么办」），
    CLI 不应该在它之后追加堆栈。
    """


class DatabaseUnavailableError(DatabaseError):
    """连不上数据库：进程没起、端口不对、密码不对、驱动缺失。"""


class MigrationError(DatabaseError):
    """迁移应用失败。"""


class SchemaMismatchError(MigrationError):
    """库里的结构与迁移记账对不上（脏库）。不会自动修补，交给人处理。"""


class MigrationState(StrEnum):
    """当前库相对于 0001 的状态。"""

    EMPTY = "empty"  # 干净库：可以应用
    APPLIED = "applied"  # 记账里有 version 1
    PARTIAL = "partial"  # 脏库：有表但记账缺失，或有记账但表不全


@dataclass(frozen=True, slots=True)
class ResolvedDsn:
    """一个生效的连接串及其来源；``source`` 回答「这个值从哪来」。"""

    dsn: str
    source: str

    def explain(self) -> str:
        """输出一句人话解释，供 CLI 与排错使用。"""
        return f"连接串来源：{self.source}"


# ---------------------------------------------------------------------------
# 连接串解析
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


def resolve_dsn(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    env_file: Path | str | None = DEFAULT_ENV_FILE,
    app_config: Path | str | None = DEFAULT_APP_CONFIG,
) -> ResolvedDsn:
    """按优先级解析出要用的连接串。

    优先级：``explicit`` > 环境变量 ``PC_TEST_DSN`` > 环境变量 ``PC__DB__DSN``
    > ``.env`` 里的 ``PC_TEST_DSN`` > ``config/app.yaml`` 的 ``db.dsn`` > 内置默认。

    为什么 ``PC_TEST_DSN`` 排在 ``PC__DB__DSN`` **前面**：前者是宿主侧工具专用
    （主机名 ``localhost``），后者是容器内应用用的（主机名是服务名 ``db``）。
    容器里只会存在 ``PC__DB__DSN``；而宿主机上两者**可能同时存在**——当 ``.env``
    被进程环境加载时（本仓库的开发环境就是这样，harness 会把 .env 注入子进程）
    就会两者都有。此时若先取 ``PC__DB__DSN``，宿主机就会去解析 ``db`` 这个主机名，
    报出令人费解的 ``failed to resolve host 'db'``。

    同理，``.env`` 里的 ``PC__DB__DSN`` **刻意不读**：它指向容器网络。
    """
    environ = os.environ if env is None else env

    if explicit:
        return ResolvedDsn(explicit, "explicit")

    value = environ.get("PC_TEST_DSN")
    if value:
        return ResolvedDsn(value, "env:PC_TEST_DSN")

    value = environ.get("PC__DB__DSN")
    if value:
        return ResolvedDsn(value, "env:PC__DB__DSN")

    value = read_env_file(env_file).get("PC_TEST_DSN")
    if value:
        return ResolvedDsn(value, "env-file:PC_TEST_DSN")

    value = _dsn_from_app_config(app_config)
    if value:
        return ResolvedDsn(value, f"{DEFAULT_APP_CONFIG.as_posix()}:db.dsn")

    return ResolvedDsn(DEFAULT_DSN, "default")


def _dsn_from_app_config(path: Path | str | None) -> str | None:
    """从 ``config/app.yaml`` 读 ``db.dsn``；缺文件或缺 PyYAML 时静默跳过。"""
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        import yaml
    except ModuleNotFoundError:
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    db_section = data.get("db")
    if not isinstance(db_section, dict):
        return None
    dsn = db_section.get("dsn")
    return dsn if isinstance(dsn, str) and dsn else None


def mask_dsn(dsn: str) -> str:
    """把连接串里的密码替换成 ``***``，用于日志与终端输出。

    同时支持 URL 形式（``postgresql://user:pw@host/db``）与 libpq 关键字形式
    （``host=... password=...``）。解析不出来时也不原样回显，避免把密码漏出去。
    """
    if "://" not in dsn:
        return re.sub(r"(password\s*=\s*)\S+", r"\1***", dsn, flags=re.IGNORECASE)

    try:
        parts = urlsplit(dsn)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "<无法解析的连接串>"

    user = parts.username or ""
    auth = f"{user}:***@" if user else ""
    netloc = f"{auth}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


def psycopg_available() -> bool:
    """驱动是否可用；不可用时 CLI 应提示 ``uv sync`` 而不是抛堆栈。"""
    return importlib.util.find_spec("psycopg") is not None


def _import_psycopg() -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as exc:  # pragma: no cover - 依赖缺失时的兜底
        raise DatabaseUnavailableError(
            "缺少依赖 psycopg，无法访问数据库。请先在仓库根运行 `uv sync` 安装依赖。"
        ) from exc
    return psycopg


def connect(dsn: str, *, autocommit: bool = False, connect_timeout: int = 5) -> Any:
    """建立一个连接；失败时抛 :class:`DatabaseUnavailableError`，消息里带排查顺序。"""
    psycopg = _import_psycopg()
    try:
        return psycopg.connect(dsn, connect_timeout=connect_timeout, autocommit=autocommit)
    except psycopg.OperationalError as exc:
        raise DatabaseUnavailableError(_connect_hint(exc, dsn)) from exc


def _hostname_of(dsn: str) -> str:
    try:
        return (urlsplit(dsn).hostname or "").lower()
    except ValueError:
        return ""


def _connect_hint(exc: Exception, dsn: str = "") -> str:
    text = str(exc).strip()
    detail = text.splitlines()[0] if text else exc.__class__.__name__
    lines = [f"连不上数据库：{detail}", "  按顺序排查："]
    if _hostname_of(dsn) == "db":
        # 这是最常见也最费解的一种：宿主机上拿了容器内的连接串
        lines.extend(
            [
                "    0) 当前连接串的主机名是 db —— 那是容器网络里的服务名，宿主机解析不了。",
                "       宿主机侧要用 .env 里的 PC_TEST_DSN（主机名 localhost），不是 PC__DB__DSN。",
            ]
        )
    lines.extend(
        [
            "    1) 数据库是否在跑：docker compose ps（应显示 db healthy）",
            "    2) 宿主机侧的连接串主机名必须是 localhost；容器内才是服务名 db",
            "    3) .env 的 PC_TEST_DSN 是否已改掉 change-me，且与 POSTGRES_PASSWORD 一致",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 迁移应用
# ---------------------------------------------------------------------------


def migration_checksum(sql: str) -> str:
    """计算迁移的**结构指纹**：剔除整行注释与空行后，取 SHA-256。

    刻意不直接对文件字节取哈希：``0001`` 的文件头就是用来追加修订记录的，
    整行注释的增删不该被报成「已应用的文件被改过」。剔除后，只有真正的 DDL 变化
    才会让指纹改变。

    保守取向：**行尾注释算内容**。要正确忽略它需要按字符串字面量做 SQL 词法分析，
    代价与风险都不划算；宁可多报一次「漂移」让人看一眼，也不要漏掉真正的 DDL 改动。
    （限制：不处理块注释 ``/* */``，本仓库的迁移不使用它。）
    """
    meaningful = [
        line.strip()
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    return hashlib.sha256("\n".join(meaningful).encode("utf-8")).hexdigest()


def public_tables(conn: Any) -> set[str]:
    """当前库 ``public`` schema 下的基础表名。"""
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def applied_versions(conn: Any) -> set[int]:
    """``schema_migration`` 里已记录的版本号；表不存在时返回空集。"""
    if "schema_migration" not in public_tables(conn):
        return set()
    rows = conn.execute("SELECT version FROM schema_migration").fetchall()
    return {int(row[0]) for row in rows}


def migration_state(conn: Any) -> MigrationState:
    """判断当前库相对 0001 处于什么状态。"""
    actual = public_tables(conn)
    has_business_tables = bool(actual & (EXPECTED_TABLES - {"schema_migration"}))
    if MIGRATION_VERSION in applied_versions(conn):
        return MigrationState.APPLIED
    return MigrationState.PARTIAL if has_business_tables else MigrationState.EMPTY


def recorded_checksum(conn: Any) -> str | None:
    """``schema_migration`` 里记录的 checksum；未记录时为 ``None``。"""
    if "schema_migration" not in public_tables(conn):
        return None
    row = conn.execute(
        "SELECT checksum FROM schema_migration WHERE version = %s", (MIGRATION_VERSION,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _store_checksum(conn: Any, checksum: str) -> None:
    conn.execute(
        "UPDATE schema_migration SET checksum = %s WHERE version = %s",
        (checksum, MIGRATION_VERSION),
    )


def apply_migration(conn: Any, path: Path | str = DEFAULT_MIGRATION_FILE) -> dict[str, Any]:
    """把整篇迁移应用到连接所在的库。

    行为契约：

    * 干净库 -> 在**单个事务**内整篇执行，成功后写入 checksum；失败整体回滚，
      绝不留下半成品（这是纯 Python 路径相对 ``psql -f`` 的额外保证）。
    * 已应用 -> 不重复执行，返回 ``already_applied``；若 checksum 未记录则补记，
      若 checksum 与文件不符则返回 ``checksum_drift`` 而**不覆盖**（漂移要被看见，不该被抹掉）。
    * 脏库（有表没记账，或有记账表不全）-> 抛 :class:`SchemaMismatchError`，不猜测、不修补。

    Returns:
        含 ``applied`` / ``reason`` / ``checksum`` 等键的结果字典。
    """
    p = Path(path)
    if not p.is_file():
        raise MigrationError(f"找不到迁移文件：{p}。请在仓库根目录运行，或用 --file 指定路径。")

    sql = p.read_text(encoding="utf-8")
    checksum = migration_checksum(sql)
    state = migration_state(conn)

    if state is MigrationState.APPLIED:
        recorded = recorded_checksum(conn)
        if recorded is None:
            _store_checksum(conn, checksum)
            conn.commit()
            return {
                "applied": False,
                "reason": "already_applied",
                "checksum": checksum,
                "checksum_recorded": True,
            }
        return {
            "applied": False,
            "reason": "checksum_drift" if recorded != checksum else "already_applied",
            "checksum": checksum,
            "checksum_recorded": False,
            "checksum_recorded_value": recorded,
        }

    if state is MigrationState.PARTIAL:
        actual = sorted(public_tables(conn))
        raise SchemaMismatchError(
            "数据库处于半成品状态：存在业务表但迁移记账不完整，或记账与表不一致。\n"
            f"  实际表（{len(actual)} 张）：{', '.join(actual) or '(无)'}\n"
            "  本工具**不会**自动修补这种情况。请人工确认后用 dropdb/createdb 重建该库，"
            "或在确认数据可弃后手工清理，再重新应用迁移。"
        )

    try:
        # 无参数 -> psycopg 走简单查询协议，可一次执行整篇多语句 SQL
        conn.execute(sql)
        _store_checksum(conn, checksum)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise MigrationError(
            f"应用 {p.name} 失败，已在单事务内整体回滚，数据库未被改动。\n原始错误：{exc}"
        ) from exc

    return {
        "applied": True,
        "reason": "applied",
        "checksum": checksum,
        "checksum_recorded": True,
    }


# ---------------------------------------------------------------------------
# 状态清单
# ---------------------------------------------------------------------------


def database_status(conn: Any, path: Path | str = DEFAULT_MIGRATION_FILE) -> dict[str, Any]:
    """只读地报告「库里的结构是否与迁移一致」。

    Returns:
        含下列键的字典（全部为只读探测结果）：

        ``server_version`` / ``database`` / ``user`` / ``pg_trgm`` /
        ``tables`` / ``missing_tables`` / ``unexpected_tables`` /
        ``deferred_present`` / ``state`` / ``migration_version`` /
        ``migration_file_present`` / ``checksum_recorded`` / ``checksum_current`` /
        ``checksum_drift`` / ``ok``
    """
    actual = public_tables(conn)
    expected = set(EXPECTED_TABLES)
    missing = tuple(sorted(expected - actual))
    unexpected = tuple(sorted(actual - expected))
    deferred_present = tuple(sorted(DEFERRED_TABLES & actual))

    p = Path(path)
    file_present = p.is_file()
    checksum_current = migration_checksum(p.read_text(encoding="utf-8")) if file_present else ""
    checksum_recorded = recorded_checksum(conn)
    drift = bool(checksum_recorded and checksum_current and checksum_recorded != checksum_current)

    state = migration_state(conn)
    extension_ok = _extension_present(conn, "pg_trgm")
    ok = (
        state is MigrationState.APPLIED
        and not missing
        and not deferred_present
        and extension_ok
        # 拿不到迁移文件就无法比对结构指纹，此时不能声称「一致」
        and file_present
        and not drift
    )

    return {
        "server_version": _scalar(conn, "SELECT current_setting('server_version')") or "unknown",
        "database": _scalar(conn, "SELECT current_database()") or "unknown",
        "user": _scalar(conn, "SELECT current_user") or "unknown",
        "pg_trgm": extension_ok,
        "tables": tuple(sorted(actual)),
        "missing_tables": missing,
        "unexpected_tables": unexpected,
        "deferred_present": deferred_present,
        "state": str(state),
        "migration_version": MIGRATION_VERSION if state is MigrationState.APPLIED else None,
        "migration_file_present": file_present,
        "checksum_recorded": checksum_recorded,
        "checksum_current": checksum_current,
        "checksum_drift": drift,
        "ok": ok,
    }


def _extension_present(conn: Any, name: str) -> bool:
    row = conn.execute("SELECT count(*) FROM pg_extension WHERE extname = %s", (name,)).fetchone()
    return bool(row and row[0])


def _scalar(conn: Any, sql: str) -> str | None:
    row = conn.execute(sql).fetchone()
    return str(row[0]) if row and row[0] is not None else None


__all__ = [
    "DEFAULT_APP_CONFIG",
    "DEFAULT_DSN",
    "DEFAULT_ENV_FILE",
    "DEFAULT_MIGRATION_FILE",
    "DEFERRED_TABLES",
    "EXPECTED_TABLES",
    "MIGRATION_VERSION",
    "DatabaseError",
    "DatabaseUnavailableError",
    "MigrationError",
    "MigrationState",
    "ResolvedDsn",
    "SchemaMismatchError",
    "applied_versions",
    "apply_migration",
    "connect",
    "database_status",
    "mask_dsn",
    "migration_checksum",
    "migration_state",
    "psycopg_available",
    "public_tables",
    "read_env_file",
    "recorded_checksum",
    "resolve_dsn",
]
