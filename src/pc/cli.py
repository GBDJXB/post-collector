"""命令行入口（``pc``）。

设计目标（DESIGN §6.2.3、§6.2.4）：让不懂技术的人**不需要碰 docker 命令、不需要读
堆栈**就能知道「现在是什么状态、下一步该做什么」；同时让开发者能回答
「这个值为什么是这样」。

本模块**只依赖标准库**：在还没执行 ``uv sync`` 的环境里，``pc doctor``
也应当能跑起来并告诉你缺了什么。数据库相关子命令才按需 import adapters 层。

退出码约定：
    0  成功
    2  环境问题（缺依赖、缺配置文件、数据库不可达、库结构与迁移不一致）
       —— 提示里会给出可操作的动作
    3  保留给「功能尚未实现」：当前所有子命令都已接线，因此暂时没有路径会返回它
       —— 留着是为了让将来新增的半成品子命令有一个区别于「环境坏了」的退出码
    1  argparse 用法错误（由 argparse 自行返回）
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pc import __version__

#: 关键文件清单：doctor 逐项检查存在性
_EXPECTED_FILES: tuple[str, ...] = (
    "DESIGN.md",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "docker-compose.yml",
    "Dockerfile",
    ".env.example",
    ".gitignore",
    "config/app.yaml",
    "config/settings-registry.yaml",
    "migrations/0001_init.sql",
)

#: 依赖检查：import 名 → 用途说明
_REQUIRED_DEPS: dict[str, str] = {
    "fastapi": "Web API（api/app.py）",
    "yaml": "设置项注册表解析（config.py）",
    "psycopg": "PostgreSQL 访问（adapters/postgres.py、pc db）",
    "pydantic": "数据校验（实现阶段）",
}

#: 运行时数据目录：不存在时应提示，但不视为错误
_RUNTIME_DIRS: tuple[str, ...] = ("data", "data/media", "data/cold", "data/quarantine", "data/backup", "models")


def _force_utf8_stdio() -> None:
    """把标准输出/错误切到 UTF-8。

    Windows 上控制台或管道的默认代码页可能是 cp1252 / cp936 等，直接 print 中文会抛
    ``UnicodeEncodeError``——体检工具自己崩掉是最糟的体验（DESIGN §6.2.3）。
    这里统一改成 UTF-8 并把无法编码的字符替换掉：宁可少一个字，也不要崩。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被重定向到非文本流时
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")
        with contextlib.suppress(ValueError, OSError):
            # 个别环境不接受重设编码；至少把不可编码字符替换掉，别让体检工具自己崩
            reconfigure(errors="replace")


def find_repo_root(start: Path | None = None) -> Path:
    """向上查找仓库根（以 ``pyproject.toml`` + ``DESIGN.md`` 同时存在为准）。

    先看当前工作目录，再看本文件位置——这样无论从哪个子目录运行都能定位，
    也避免把 ``site-packages`` 当成仓库根。
    """
    candidates: list[Path] = []
    if start is not None:
        candidates.append(Path(start).resolve())
    candidates.append(Path.cwd().resolve())
    candidates.append(Path(__file__).resolve().parents[2])  # src/pc/cli.py → 仓库根

    for base in candidates:
        for path in (base, *base.parents):
            if (path / "pyproject.toml").exists() and (path / "DESIGN.md").exists():
                return path
    return Path.cwd().resolve()


def _dep_state(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def cmd_doctor(args: argparse.Namespace) -> int:
    """体检：报告环境、文件、依赖与运行时目录；只读，不修改任何东西。"""
    root = Path(args.root).resolve() if args.root else find_repo_root()

    print(f"post-collector {__version__} —— 骨架阶段（业务功能尚未实现）")
    print()
    print("[运行时]")
    print(f"  Python      : {platform.python_version()}  ({sys.executable})")
    print(f"  平台        : {platform.system()} {platform.release()}")
    if sys.version_info < (3, 12):  # noqa: UP036 —— 故意保留：让旧解释器得到人话提示而不是语法错误
        print("  ✗ 需要 Python 3.12 及以上（pyproject.toml 的 requires-python）")
    print()
    print(f"[仓库] {root}")
    missing_files: list[str] = []
    for rel in _EXPECTED_FILES:
        ok = (root / rel).exists()
        print(f"  {'✓' if ok else '✗'} {rel}")
        if not ok:
            missing_files.append(rel)
    print()
    print("[依赖]（骨架阶段可以不装；Web API 与设置页需要先执行 uv sync）")
    missing_deps: list[str] = []
    for module, purpose in _REQUIRED_DEPS.items():
        ok = _dep_state(module)
        print(f"  {'✓' if ok else '·'} {module:<10} {purpose}")
        if not ok:
            missing_deps.append(module)
    print()
    print("[数据库]（只读探测；不可达不影响本次体检结论）")
    _doctor_database_section()
    print()
    print("[运行时目录]（运行时产物，已被 .gitignore 忽略）")
    for rel in _RUNTIME_DIRS:
        exists = (root / rel).is_dir()
        print(f"  {'✓' if exists else '·'} {rel}")
    print()

    if missing_files:
        print("结论：仓库文件不完整，请检查是否在正确的目录运行。")
        return 2
    if missing_deps:
        print(f"结论：结构就绪，尚未安装依赖（{', '.join(missing_deps)}）。")
        print("      安装后即可启动：uv sync && uv run uvicorn pc.api.app:create_app --factory")
    else:
        print("结论：结构与依赖均已就绪。")
        print("      启动：uv run uvicorn pc.api.app:create_app --factory --port 8080")
    print("      说明：本阶段没有任何 AI 外发代码路径，内容不会离开本机。")
    return 0


def cmd_config_show(args: argparse.Namespace) -> int:
    """显示设置项的生效值与来源（``--explain`` 时附带含义与影响的解释）。

    回答的是 DESIGN §6.2.4 那个问题：「这个值为什么是 8」。来源按
    env > db > yaml > default 解析，并明确写出每一层分别是什么。

    数据库不可达**不影响退出码**（与 ``pc doctor`` 一致）：文件层与环境层的值
    照样能看，只是会打印一行说明，避免让人误以为看到的就是在跑实例的值。
    """
    from pc import config as config_mod

    root = Path(args.root).resolve() if args.root else find_repo_root()
    try:
        specs = config_mod.load_registry(root / config_mod.DEFAULT_REGISTRY_PATH)
        index = config_mod.registry_index(specs)
        yaml_defaults = config_mod.load_app_defaults(
            root / config_mod.DEFAULT_APP_CONFIG_PATH, registry=index
        )
    except config_mod.SettingsRegistryError as exc:
        print(f"无法读取设置项注册表：{exc}", file=sys.stderr)
        return 2
    except config_mod.ConfigError as exc:
        print(f"无法读取开发配置：{exc}", file=sys.stderr)
        return 2

    db_rows, revision, notes = _read_setting_layer(args)
    try:
        snap = config_mod.snapshot(
            registry=index,
            yaml_defaults=yaml_defaults,
            db_values=db_rows,
            env=os.environ,
            revision=revision,
        )
    except config_mod.ConfigError as exc:
        print(f"解析设置项失败：{exc}", file=sys.stderr)
        return 2

    keys = _select_keys(snap, args.key)
    if keys is None:
        print(f"没有匹配的设置项：{args.key}", file=sys.stderr)
        print("用 `pc config show` 看全部键名。", file=sys.stderr)
        return 2

    if args.json:
        payload = snap.as_dict()
        payload["settings"] = {key: payload["settings"][key] for key in keys}
        print(config_mod.dump_json(payload))
        return 0

    print(f"设置项生效值（优先级 env > db > yaml > default），共 {len(keys)} 项")
    if revision:
        print(f"配置版本号：{revision}（setting_change 的最大编号）")
    for note in notes:
        print(f"  · {note}")
    print()
    for key in keys:
        resolved = config_mod.resolve(
            key,
            registry=index,
            yaml_defaults=yaml_defaults,
            db_values=db_rows,
        )
        print(f"  {key} = {resolved.humanize()}")
        print(f"      来源：{resolved.explain()}")
        if args.explain and resolved.spec is not None:
            spec = resolved.spec
            bounds = ""
            if spec.range:
                low, high = spec.range
                bounds = f"，范围 {low} ~ {high}" if high is not None else f"，下限 {low}"
            unit = f"（单位：{spec.unit}）" if spec.unit else ""
            print(f"      含义：{spec.label} —— {spec.help}")
            print(f"      影响：{spec.impact}")
            print(f"      类型：{spec.type}{bounds}{unit}")
            check = config_mod.validate_value(spec, resolved.value, registry=index, values=snap.values)
            if check["warning"]:
                print(f"      提示：{check['warning']}")
            if spec.note:
                print(f"      备注：{spec.note}")

    warnings = config_mod.app_default_warnings(yaml_defaults, index)
    for message in warnings:
        print(f"\n  ! {message}")
    for message in snap.env_warnings:
        print(f"\n  ! {message}")
    for key in snap.orphans:
        print(f"\n  ! 数据库里有 {key}，但注册表里已经没有这一项（界面不会再显示它）")
    return 0


def _read_setting_layer(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], int, list[str]]:
    """读取数据库层（``setting`` 表）；不可达时降级为空并附一句说明。"""
    from pc.adapters import postgres as pg

    notes: list[str] = []
    if not pg.psycopg_available():
        notes.append("缺少依赖 psycopg，未读取数据库层（运行 uv sync 后可见）")
        return {}, 0, notes

    resolved = pg.resolve_dsn(getattr(args, "dsn", None))
    try:
        with pg.connect(resolved.dsn, connect_timeout=3) as conn:
            rows = pg.read_settings(conn)
            revision = pg.settings_revision(conn)
    except pg.DatabaseError as exc:
        detail = str(exc).splitlines()[0]
        notes.append(f"数据库不可达，本次只显示文件与环境层的值（{detail}）")
        return {}, 0, notes

    if rows:
        notes.append(f"数据库层：已播种 {len(rows)} 项（{pg.mask_dsn(resolved.dsn)}）")
    else:
        notes.append("数据库层：setting 表还没有内容，可运行 `uv run pc config seed` 播种")
    return dict(rows), revision, notes


def _select_keys(snap: Any, selector: str | None) -> list[str] | None:
    """按 ``--key`` 过滤；支持给命名空间前缀（如 ``dedup``）。"""
    keys = list(snap.values)
    if not selector:
        return keys
    exact = [key for key in keys if key == selector]
    if exact:
        return exact
    prefix = [key for key in keys if key.startswith(f"{selector}.")]
    return prefix or None


def cmd_config_seed(args: argparse.Namespace) -> int:
    """首次播种 ``setting`` 表：只插入缺失的键，绝不覆盖已有值。"""
    from pc import config as config_mod
    from pc.adapters import postgres as pg

    root = Path(args.root).resolve() if args.root else find_repo_root()
    try:
        index = config_mod.registry_index(
            config_mod.load_registry(root / config_mod.DEFAULT_REGISTRY_PATH)
        )
        yaml_defaults = config_mod.load_app_defaults(
            root / config_mod.DEFAULT_APP_CONFIG_PATH, registry=index
        )
        seeds = config_mod.setting_seeds(index, yaml_defaults)
    except config_mod.ConfigError as exc:
        print(f"无法准备播种数据：{exc}", file=sys.stderr)
        return 2

    resolved = pg.resolve_dsn(args.dsn)
    print(f"[数据库] {pg.mask_dsn(resolved.dsn)}")
    print(f"  {resolved.explain()}")

    if not pg.psycopg_available():
        print("  缺少依赖 psycopg，无法连接。请先在仓库根运行 `uv sync`。", file=sys.stderr)
        return 2

    try:
        with pg.connect(resolved.dsn) as conn:
            result = pg.seed_settings(conn, seeds)
    except pg.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    inserted, existing = result["inserted"], result["existing"]
    print(f"  ✓ 已播种 {len(inserted)} 项，保留既有 {len(existing)} 项（播种不会覆盖任何已有值）")
    if inserted:
        print(f"    新写入：{', '.join(inserted)}")
    if existing:
        print(f"    已存在：{', '.join(existing)}")
    print("    播种来源：config/app.yaml 的 defaults 段优先，其余取注册表默认值；")
    print("    此后一切以数据库为准，改 YAML 不再影响这个实例。")
    return 0


def _doctor_database_section() -> None:
    """doctor 的数据库段：只报告状态，**绝不因数据库问题改变退出码**。

    README 承诺过「体检命令不依赖数据库」，而 ``/healthz`` 也刻意不校验数据库
    （避免把依赖故障误报成进程死亡）。这里保持一致：数据库只是体检报告的一行，
    不是体检的判据。
    """
    try:
        from pc.adapters import postgres as pg
    except Exception as exc:  # pragma: no cover - 适配器层不可导入时的兜底
        print(f"  · 适配器不可用：{exc}")
        return

    if not pg.psycopg_available():
        print("  · 缺少依赖 psycopg，跳过（运行 uv sync 后可见）")
        return

    resolved = pg.resolve_dsn(None)
    print(f"  · 连接串 {pg.mask_dsn(resolved.dsn)}（{resolved.source}）")
    try:
        with pg.connect(resolved.dsn, connect_timeout=3) as conn:
            status = pg.database_status(conn)
            try:
                setting_rows: dict[str, Any] | None = pg.read_settings(conn)
            except pg.DatabaseError:
                setting_rows = None
    except pg.DatabaseError as exc:
        detail = str(exc).splitlines()[0]
        print(f"  · 不可达：{detail}")
        print("    提示：docker compose up -d db；宿主机侧连接串见 .env 的 PC_TEST_DSN")
        return

    applied = "已应用" if status["migration_version"] else "未应用"
    print(
        f"  ✓ 可达：server {status['server_version']}，表 {len(status['tables'])} 张，"
        f"迁移 0001 {applied}"
    )
    if setting_rows is None:
        print("  · 设置项：读不到（setting 表缺失，先应用迁移）")
    elif setting_rows:
        print(f"  · 设置项：已播种 {len(setting_rows)} 项")
    else:
        print("  · 设置项：尚未播种（uv run pc config seed）")


def cmd_db_status(args: argparse.Namespace) -> int:
    """只读报告「库里的结构是否与迁移一致」（不改任何东西）。"""
    from pc.adapters import postgres as pg

    resolved = pg.resolve_dsn(args.dsn)
    print(f"[数据库] {pg.mask_dsn(resolved.dsn)}")
    print(f"  {resolved.explain()}")

    if not pg.psycopg_available():
        print("  缺少依赖 psycopg，无法连接。请先在仓库根运行 `uv sync`。", file=sys.stderr)
        return 2

    path = args.file or pg.DEFAULT_MIGRATION_FILE
    try:
        with pg.connect(resolved.dsn) as conn:
            status = pg.database_status(conn, path)
    except pg.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    print(f"  服务器 {status['server_version']} / 库 {status['database']} / 用户 {status['user']}")
    print(f"  pg_trgm：{'已安装' if status['pg_trgm'] else '缺失'}")
    print(f"  表：{len(status['tables'])} 张（预期 {len(pg.EXPECTED_TABLES)} 张）")
    if status["migration_version"]:
        print(f"  迁移记账：version {status['migration_version']} 已应用")
    else:
        print(f"  迁移记账：version {pg.MIGRATION_VERSION} 未应用（库状态：{status['state']}）")

    if not status["migration_file_present"]:
        print(f"  结构指纹：无法比对（找不到迁移文件 {path}）")
    elif not status["checksum_recorded"]:
        print("  结构指纹：未记录（可用 `pc db apply` 补记，之后即可发现文件被改动）")
    elif status["checksum_drift"]:
        print("  结构指纹：与文件不一致（漂移）")
    else:
        print("  结构指纹：与文件一致")

    if not status["migration_file_present"]:
        print(f"  ✗ 找不到迁移文件：{path}")
        print("     用 --file 指定路径，或确认在仓库根目录运行（容器内路径是 /app/migrations/）")
    if status["missing_tables"]:
        print(f"  ✗ 缺少表：{', '.join(status['missing_tables'])}")
    if status["deferred_present"]:
        print(f"  ✗ 出现本阶段必须缺席的表：{', '.join(status['deferred_present'])}")
        print("     这三张表属阶段 2（向量与外发），出现即触碰「无外发」红线，见 DESIGN §8.1")
    if status["unexpected_tables"]:
        print(f"  · 计划外的表：{', '.join(status['unexpected_tables'])}")
    if status["checksum_drift"]:
        print("  · 迁移文件在应用之后被改过（结构指纹变化）。本工具不会自动改数据库；")
        print("    若改动确有必要，请以新迁移追加，而不是改已应用的版本。")

    if status["ok"]:
        print("结论：结构就绪，与 migrations/0001_init.sql 一致。")
        return 0
    print("结论：结构未就绪，见上面的 ✗ 项。", file=sys.stderr)
    return 2


def cmd_db_apply(args: argparse.Namespace) -> int:
    """应用迁移：整篇在单事务内执行，失败整体回滚；已应用则安全短路。"""
    from pc.adapters import postgres as pg

    resolved = pg.resolve_dsn(args.dsn)
    print(f"[数据库] {pg.mask_dsn(resolved.dsn)}")
    print(f"  {resolved.explain()}")

    if not pg.psycopg_available():
        print("  缺少依赖 psycopg，无法连接。请先在仓库根运行 `uv sync`。", file=sys.stderr)
        return 2

    path = args.file or pg.DEFAULT_MIGRATION_FILE
    try:
        with pg.connect(resolved.dsn) as conn:
            result = pg.apply_migration(conn, path)
    except pg.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    fingerprint = str(result.get("checksum", ""))[:12]
    reason = result["reason"]
    if reason == "applied":
        print(f"  ✓ 已应用 {Path(path).name}（结构指纹 {fingerprint}…）")
        print(f"    共 {len(pg.EXPECTED_TABLES)} 张表；可用 `pc db status` 复核。")
        return 0
    if reason == "already_applied":
        print("  · 迁移已应用，本次未改动数据库。")
        if result.get("checksum_recorded"):
            print(f"    顺带补记了结构指纹（{fingerprint}…），用于日后发现文件被改动。")
        return 0

    print("  ! 迁移已应用，但文件与记账的结构指纹不一致：", file=sys.stderr)
    recorded = str(result.get("checksum_recorded_value") or "")[:12]
    print(f"    记账 {recorded}… / 现在 {fingerprint}…", file=sys.stderr)
    print("    没有改动数据库。请确认改动是否应以新迁移追加。", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc",
        description="post-collector 命令行（骨架阶段；doctor、config、db 已接线）",
    )
    parser.add_argument("--version", action="version", version=f"post-collector {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="体检：报告环境、文件、依赖与运行时目录")
    p_doctor.add_argument("--root", help="指定仓库根目录（默认自动查找）")
    p_doctor.set_defaults(func=cmd_doctor)

    p_config = sub.add_parser("config", help="配置相关操作")
    p_config_sub = p_config.add_subparsers(dest="config_command", required=True)

    p_show = p_config_sub.add_parser("show", help="显示设置项的生效值与来源")
    p_show.add_argument("--explain", action="store_true", help="附带含义、影响与改动后果")
    p_show.add_argument("--key", help="只看某一项；也可给命名空间前缀（如 dedup）")
    p_show.add_argument("--json", action="store_true", help="输出 JSON，便于脚本与贴 issue")
    p_show.add_argument("--root", help="指定仓库根目录（默认自动查找）")
    p_show.add_argument("--dsn", help="显式连接串；默认按 env > .env > app.yaml > 默认解析")
    p_show.set_defaults(func=cmd_config_show)

    p_seed = p_config_sub.add_parser(
        "seed", help="首次播种 setting 表（只插入缺失的键，不覆盖已有值）"
    )
    p_seed.add_argument("--root", help="指定仓库根目录（默认自动查找）")
    p_seed.add_argument("--dsn", help="显式连接串；默认按 env > .env > app.yaml > 默认解析")
    p_seed.set_defaults(func=cmd_config_seed)

    p_db = sub.add_parser("db", help="数据库：应用迁移与查看结构状态")
    p_db_sub = p_db.add_subparsers(dest="db_command", required=True)

    p_db_apply = p_db_sub.add_parser(
        "apply", help="应用迁移（整篇单事务执行；已应用则安全短路）"
    )
    p_db_apply.add_argument("--dsn", help="显式连接串；默认按 env > .env > app.yaml > 默认解析")
    p_db_apply.add_argument("--file", help="迁移文件路径（默认 migrations/0001_init.sql）")
    p_db_apply.set_defaults(func=cmd_db_apply)

    p_db_status = p_db_sub.add_parser("status", help="只读报告库结构与迁移是否一致")
    p_db_status.add_argument("--dsn", help="显式连接串；默认按 env > .env > app.yaml > 默认解析")
    p_db_status.add_argument("--file", help="迁移文件路径（默认 migrations/0001_init.sql）")
    p_db_status.set_defaults(func=cmd_db_status)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
