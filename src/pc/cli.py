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
import time
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
    "migrations/sqlite/0001_init.sql",
)
#: 依赖检查：import 名 → 用途说明
_REQUIRED_DEPS: dict[str, str] = {
    "fastapi": "Web API（api/app.py）",
    "yaml": "设置项注册表解析（config.py）",
    "pydantic": "数据校验（实现阶段）",
}


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
    _doctor_runtime_dirs(root)
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
    from pc.adapters import sqlite as db

    notes: list[str] = []
    resolved = db.resolve_db_path(getattr(args, "path", None))
    if not resolved.path.is_file():
        notes.append(f"数据库文件还不存在（{resolved.path}），本次只显示文件与环境层的值")
        return {}, 0, notes

    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            rows = db.read_settings(conn)
            revision = db.settings_revision(conn)
    except db.DatabaseError as exc:
        detail = str(exc).splitlines()[0]
        notes.append(f"数据库不可达，本次只显示文件与环境层的值（{detail}）")
        return {}, 0, notes

    if rows:
        notes.append(f"数据库层：已播种 {len(rows)} 项（{resolved.path}）")
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
    from pc.adapters import sqlite as db

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

    resolved = db.resolve_db_path(args.path)
    print(f"[数据库] {resolved.path}")
    print(f"  {resolved.explain()}")

    try:
        with db.open_db(resolved.path) as conn:
            result = db.seed_settings(conn, seeds)
    except db.DatabaseError as exc:
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


def _doctor_runtime_dirs(root: Path) -> None:
    """报告数据根目录及其六个运行时目录，以及 L2 配置段的缺口。

    用 ``db.resolve_paths()`` 作为唯一来源：运行时目录**跟着数据根目录**走，而不是
    跟着数据库文件走——``PC__DB__PATH`` 只挪数据库文件本身（放进 SSD、放到挂载盘），
    不该顺带把媒体本体也挪到别处（对象键不变、落盘位置变了，同样意味着老内容找不到）。
    """
    from pc import config as cfg
    from pc.adapters import sqlite as db

    try:
        resolved = db.resolve_paths()
    except Exception as exc:  # pragma: no cover - 适配器不可导入时的兜底
        print(f"  · 无法解析数据根目录：{exc}")
        return

    print(f"  数据根目录：{resolved.data_root}（来源 {resolved.root_source}）")
    for name, path in resolved.paths.items():
        source = resolved.sources[name]
        # 默认派生路径不值得逐行重复打印；只有被显式指定过的才把落点写出来
        explicit = not source.endswith(f"/{path.name}")
        mark = "✓" if path.is_dir() else "·"
        detail = f"/ → {path}（来源 {source}）" if explicit else "/"
        print(f"    {mark} {name}{detail}")
        # 容器里写 /data/media 是对的；在本机（Windows）却会落到当前盘符下的 \data\media，
        # 这是个静默的落点错误，值得点名——路径本身没错，错的是它出现的环境。
        if os.name == "nt" and explicit and not path.drive:
            print(
                "      ! 这个值看起来是容器/Unix 风格的路径，在 Windows 上会落到当前盘符；"
                "本机运行请写成绝对路径（如 D:\\pc\\media）或留空让它从数据根目录派生"
            )
    if resolved.data_root != (root / "data"):
        print("  提示：仓库内的 data/ 只有把 PC_DATA_ROOT 指过去时才会用到。")

    app_config = Path(root) / cfg.DEFAULT_APP_CONFIG_PATH
    try:
        warnings = cfg.config_warnings(cfg.load_app_config(app_config))
    except cfg.ConfigError as exc:
        print(f"  · 无法校验 {cfg.DEFAULT_APP_CONFIG_PATH}：{str(exc).splitlines()[0]}")
        return
    for message in warnings:
        print(f"  · {message}")


def _doctor_database_section() -> None:
    """doctor 的数据库段：只报告状态，**绝不因数据库问题改变退出码**。

    README 承诺过「体检命令不依赖数据库」，而 ``/healthz`` 也刻意不校验数据库
    （避免把依赖故障误报成进程死亡）。这里保持一致：数据库只是体检报告的一行，
    不是体检的判据。数据库改成 SQLite 本机文件后，「不可达」基本只剩三种：
    SQLite 版本过旧、文件还不存在、目录不可写。
    """
    try:
        from pc.adapters import sqlite as db
    except Exception as exc:  # pragma: no cover - 适配器层不可导入时的兜底
        print(f"  · 适配器不可用：{exc}")
        return

    try:
        db.ensure_version_gate()
    except db.DatabaseError as exc:
        print(f"  · {str(exc).splitlines()[0]}")
        return

    resolved = db.resolve_db_path(None)
    if not resolved.path.is_file():
        print(f"  · 文件 {resolved.path}（{resolved.source}）：尚未创建")
        print("    建库：uv run pc db apply")
        return
    size_mb = resolved.path.stat().st_size / (1024 * 1024)
    print(f"  · 文件 {resolved.path}（{resolved.source}，{size_mb:.1f} MB）")

    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            status = db.database_status(conn)
            try:
                setting_rows: dict[str, Any] | None = db.read_settings(conn)
            except db.DatabaseError:
                setting_rows = None
    except db.DatabaseError as exc:
        print(f"  · 打不开：{str(exc).splitlines()[0]}")
        return

    applied = "、".join(str(v) for v in status["migrations_applied"]) or "无"
    print(
        f"  ✓ SQLite {status['sqlite_version']}，表 {len(status['tables'])} 张，"
        f"迁移记账 version {applied}"
    )
    if status["migrations_missing"]:
        print(
            "  · 有未应用的迁移版本："
            + "、".join(str(v) for v in status["migrations_missing"])
            + "（跑 `pc db apply`）"
        )
    print(
        f"  · journal_mode={status['journal_mode']}"
        f"  foreign_keys={'ON' if status['foreign_keys'] else 'OFF'}"
        f"  FTS5 {len(status['fts_tables'])}/2"
        f"  完整性 {status['integrity']}"
    )
    if setting_rows is None:
        print("  · 设置项：读不到（setting 表缺失，先应用迁移）")
    elif setting_rows:
        print(f"  · 设置项：已播种 {len(setting_rows)} 项")
    else:
        print("  · 设置项：尚未播种（uv run pc config seed）")


def cmd_db_status(args: argparse.Namespace) -> int:
    """只读报告「库里的结构是否与迁移一致」（不改任何东西）。"""
    from pc.adapters import sqlite as db

    resolved = db.resolve_db_path(args.path)
    print(f"[数据库] {resolved.path}")
    print(f"  {resolved.explain()}")

    if not resolved.path.is_file():
        print(
            f"  ✗ 数据库文件还不存在。建库：uv run pc db apply --path \"{resolved.path}\"",
            file=sys.stderr,
        )
        return 2

    directory = args.dir or db.DEFAULT_MIGRATIONS_DIR
    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            status = db.database_status(conn, directory)
    except db.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    print(f"  SQLite {status['sqlite_version']} / 库 {status['db_path']}")
    print(f"  journal_mode：{status['journal_mode']}")
    print(f"  foreign_keys：{'ON' if status['foreign_keys'] else 'OFF（外键会静默失效）'}")
    print(f"  完整性检查：{status['integrity']}")
    print(
        f"  FTS5 索引：{len(status['fts_tables'])}/2"
        f"（{'、'.join(status['fts_tables']) or '无'}）"
    )
    print(f"  表：{len(status['tables'])} 张（预期 {len(db.EXPECTED_TABLES)} 张）")

    applied = status["migrations_applied"]
    expected = status["migrations_expected"]
    if applied:
        print(f"  迁移记账：已应用 version {'、'.join(str(v) for v in applied)}")
    else:
        print(f"  迁移记账：一条都没有（库状态：{status['state']}）")
    if expected:
        print(
            f"  迁移目录：{directory.as_posix()} 共 {len(expected)} 个版本"
            f"（最新 {expected[-1]}）"
        )
    if status["migrations_missing"]:
        print(
            "  ✗ 未应用的版本："
            + "、".join(str(v) for v in status["migrations_missing"])
            + "（跑 `pc db apply` 应用）"
        )
    if status["migrations_unknown"]:
        print(
            "  ✗ 库里的版本比迁移目录更新："
            + "、".join(str(v) for v in status["migrations_unknown"])
            + "（这个程序可能比库旧，请升级后再操作）"
        )

    if not status["migrations_dir_present"]:
        print(f"  结构指纹：无法比对（找不到迁移目录 {directory}）")
    elif status["checksum_unrecorded_versions"]:
        print(
            "  结构指纹：部分版本未记录"
            f"（{'、'.join(str(v) for v in status['checksum_unrecorded_versions'])}）；"
            "可用 `pc db apply` 补记，之后即可发现文件被改动"
        )
    elif status["checksum_drift"]:
        print("  结构指纹：与文件不一致（漂移）")
    else:
        print("  结构指纹：与文件一致")

    if not status["migrations_dir_present"]:
        print(f"  ✗ 找不到迁移目录：{directory}")
        print("     用 --dir 指定路径，或确认在仓库根目录运行")
    if status["missing_tables"]:
        print(f"  ✗ 缺少表：{', '.join(status['missing_tables'])}")
    if status["deferred_present"]:
        print(f"  ✗ 出现本阶段必须缺席的表：{', '.join(status['deferred_present'])}")
        print("     这三张表属阶段 2（向量与外发），出现即触碰「无外发」红线，见 DESIGN §8.1")
    if status["unexpected_tables"]:
        print(f"  · 计划外的表：{', '.join(status['unexpected_tables'])}")
    if not status["fts_ok"]:
        print(
            f"  ✗ FTS5 索引不完整（预期 {'、'.join(db.FTS_VIRTUAL_TABLES)}）；"
            "可运行 `pc db reindex` 重建"
        )
    if str(status["journal_mode"]).lower() != "wal":
        print("  ✗ journal_mode 不是 WAL：并发读会退化成互斥，见 DESIGN §3.2")
    if not status["foreign_keys"]:
        print("  ✗ foreign_keys 未开启：外键约束会静默失效，见 DESIGN §3.2")
    if status["checksum_drift"]:
        print(
            "  · 已应用的迁移文件在应用之后被改过（结构指纹变化，涉及 version "
            + "、".join(str(v) for v in status["checksum_drift_versions"])
            + "）。本工具不会自动改数据库；"
        )
        print("    若改动确有必要，请以新迁移追加，而不是改已应用的版本。")

    if status["ok"]:
        print(f"结论：结构就绪，与 {directory.as_posix()} 的迁移一致。")
        return 0
    print("结论：结构未就绪，见上面的 ✗ 项。", file=sys.stderr)
    return 2


def cmd_db_apply(args: argparse.Namespace) -> int:
    """应用迁移：逐个文件在各自的事务内执行，失败只回滚该文件。"""
    from pc.adapters import sqlite as db

    resolved = db.resolve_db_path(args.path)
    print(f"[数据库] {resolved.path}")
    print(f"  {resolved.explain()}")

    directory = args.dir or db.DEFAULT_MIGRATIONS_DIR
    try:
        with db.open_db(resolved.path) as conn:
            result = db.apply_migration(conn, directory)
    except db.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    applied = result["applied"]
    if applied:
        print(
            f"  ✓ 已应用 version {'、'.join(str(v) for v in applied)}"
            f"（共 {result['statements']} 条语句，每个版本一个事务）"
        )
        print(
            f"    共 {len(db.EXPECTED_TABLES)} 张表 + "
            f"{len(db.FTS_VIRTUAL_TABLES)} 个 FTS5 索引；可用 `pc db status` 复核。"
        )
    else:
        print("  · 迁移已是最新，本次未改动数据库。")
    if result["checksum_backfilled"]:
        print(
            "    顺带补记了结构指纹（version "
            + "、".join(str(v) for v in result["checksum_backfilled"])
            + "），用于日后发现文件被改动。"
        )
    if result["drifted"]:
        print(
            "  ! 已应用的版本与文件内容不一致（结构指纹漂移）：version "
            + "、".join(str(v) for v in result["drifted"]),
            file=sys.stderr,
        )
        print("    没有改动数据库。请确认改动是否应以新迁移追加。", file=sys.stderr)
        return 2
    return 0


def cmd_db_backup(args: argparse.Namespace) -> int:
    """用 ``VACUUM INTO`` 生成事务一致的快照，并校验它能打开、结构自洽（DESIGN §7.1）。"""
    from pc.adapters import sqlite as db

    resolved = db.resolve_db_path(args.path)
    if not resolved.path.is_file():
        print(f"  ✗ 数据库文件还不存在：{resolved.path}", file=sys.stderr)
        return 2

    target = (
        Path(args.to).expanduser()
        if args.to
        else resolved.path.parent / "backup" / db.backup_filename()
    )

    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            result = db.backup_to(conn, target)
    except db.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    size_mb = result["size_bytes"] / (1024 * 1024)
    print(f"  ✓ 已备份到 {result['target']}（{size_mb:.1f} MB，{result['tables']} 张表）")
    print(f"    完整性检查：{result['integrity']}")
    if result["integrity"] != "ok":
        print("    ! 备份完整性检查未通过，请勿依赖这份备份。", file=sys.stderr)
        return 2
    print("    恢复方式：停掉 pc 进程，把这份文件复制成 pc.sqlite3（确认没有 -wal 残留）。")
    return 0


def cmd_db_reindex(args: argparse.Namespace) -> int:
    """全量重建两个 FTS5 索引（索引是派生数据，随时可重建，DESIGN §9.1）。"""
    from pc.adapters import sqlite as db

    resolved = db.resolve_db_path(args.path)
    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            result = db.reindex_fts(conn)
    except db.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    for name in sorted(result["after"]):
        before = result["before"].get(name, 0)
        print(f"  ✓ {name}：{before} 行 → {result['after'][name]} 行")
    return 0


def _db_and_paths(args: argparse.Namespace) -> tuple[Any, Any]:
    """一次性解析库文件与运行时目录——写命令都需要这两者，且必须来自同一份配置。"""
    from pc.adapters import sqlite as db

    return db.resolve_db_path(getattr(args, "path", None)), db.resolve_paths()


def _require_database(resolved: Any) -> bool:
    if resolved.path.is_file():
        return True
    print(f"  ✗ 数据库文件还不存在：{resolved.path}", file=sys.stderr)
    print("    先建库：uv run pc db apply", file=sys.stderr)
    return False


def _load_app_config(root: Path) -> Any:
    from pc import config as cfg

    return cfg.load_app_config(Path(root) / cfg.DEFAULT_APP_CONFIG_PATH)


def _build_worker(conn: Any, paths: Any, root: Path, *, worker_id: str | None = None) -> Any:
    """组装 worker：存储、隔离区、扫描器与队列参数全部来自配置（没有硬编码字面量）。"""
    from pc import config as cfg
    from pc.adapters.local_storage import LocalStorageBackend
    from pc.adapters.magic_scan import MagicNumberScanner
    from pc.services.pipeline import Worker

    app_config = _load_app_config(root)
    limits = cfg.ingest_limits(app_config)
    return Worker(
        conn,
        storage=LocalStorageBackend(paths.media_root),
        quarantine=LocalStorageBackend(paths.quarantine_root),
        scanner=MagicNumberScanner(policy=cfg.security_policy(app_config), limits=limits),
        limits=limits,
        job_settings=cfg.job_settings(app_config),
        worker_id=worker_id,
    )


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    """等宽列打印；不用第三方表格库，免得为了排版多一个依赖。"""
    text_rows = [["" if cell is None else str(cell) for cell in row] for row in rows]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in text_rows)) if text_rows else len(headers[index])
        for index in range(len(headers))
    ]
    print("  " + "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  " + "  ".join("-" * width for width in widths))
    for row in text_rows:
        print("  " + "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def cmd_source_add(args: argparse.Namespace) -> int:
    """登记一个来源，并同时写下它的授权记录（三者齐备才可采集，DESIGN §1.3）。"""
    from pc.adapters import sqlite as db
    from pc.adapters.registry import PLANNED_KINDS, SUPPORTED_KINDS
    from pc.services import sources as sources_service

    resolved, _ = _db_and_paths(args)
    if not _require_database(resolved):
        return 2

    config: dict[str, Any] = {}
    roots = [str(Path(item).expanduser()) for item in (args.roots or [])]
    if roots:
        config["roots"] = roots

    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            info = sources_service.create_source(
                conn,
                name=args.name,
                platform=args.platform,
                kind=args.kind,
                trust_level=args.trust,
                external_ref=args.ref,
                config=config,
                authorization_mode=args.authorize,
                evidence_ref=args.evidence,
                expires_at=args.expires_at,
            )
    except (db.DatabaseError, sources_service.SourceError) as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    print(
        f"  ✓ 已登记来源「{info['name']}」（source_id={info['source_id']}，"
        f"kind={info['kind']}，信任级别={info['trust_level']}）"
    )
    print(
        f"    授权：mode={info['authorization_mode']}（authz_id={info['authz_id']}）"
        + ("，无到期时间" if not args.expires_at else f"，到期 {args.expires_at}")
    )
    if roots:
        print(f"    目录：{'、'.join(roots)}")
    if info["kind"] in PLANNED_KINDS:
        print(f"  · 注意：kind={info['kind']} 的实现还没落地（{PLANNED_KINDS[info['kind']]}）")
        print(f"    现在能用的只有：{'、'.join(sorted(SUPPORTED_KINDS))}")
    return 0


def cmd_source_list(args: argparse.Namespace) -> int:
    """列出全部来源与授权状态。"""
    from pc.adapters import sqlite as db
    from pc.services import sources as sources_service

    resolved, _ = _db_and_paths(args)
    if not resolved.path.is_file():
        print(f"  · 还没有数据库文件（{resolved.path}）；建库后这里会列出来源。")
        return 0

    with db.open_db(resolved.path, expect_exists=True) as conn:
        rows = sources_service.list_sources(conn)

    if args.json:
        print(cfg_dump_json([{**row, "authorization": dict(row["authorization"])} for row in rows]))
        return 0
    if not rows:
        print("  · 还没有任何来源。用 `pc source add` 登记一个。")
        return 0

    table: list[list[Any]] = []
    for row in rows:
        state = row["authorization"]
        table.append(
            [
                row["source_id"],
                row["name"],
                row["platform"],
                row["kind"],
                row["trust_level"],
                "启用" if row["enabled"] else "停用",
                "授权有效" if state["ok"] else state["reason"],
                "、".join(state["modes"]) or "-",
                row["deleted_at"] or "-",
            ]
        )
    _print_table(
        ["id", "名称", "平台", "kind", "信任", "状态", "授权", "方式", "软删"], table
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """把本地文件导入库：落信封并入队（L2），``--process`` 时接着跑 worker（L3）。"""
    from pc import config as cfg
    from pc.adapters import sqlite as db
    from pc.adapters.registry import (
        SourceConfigError,
        UnsupportedSourceKindError,
        connector_for,
        source_config,
    )
    from pc.services import ingest as ingest_service
    from pc.services import sources as sources_service

    resolved, paths = _db_and_paths(args)
    if not _require_database(resolved):
        return 2

    requested = [Path(item).expanduser() for item in args.paths]
    missing = [str(item) for item in requested if not item.exists()]
    if missing:
        print(f"  ✗ 找不到这些路径：{'、'.join(missing)}", file=sys.stderr)
        return 2

    root = Path(args.root).resolve() if getattr(args, "root", None) else find_repo_root()
    app_config = _load_app_config(root)
    limits = cfg.ingest_limits(app_config)

    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            source = sources_service.require_source(conn, args.source)
            authorizations = sources_service.load_authorizations(conn, int(source["source_id"]))
            # 目录必须持久化：worker 在另一个时刻取字节，它只读 source.config
            roots = [str(item) for item in requested]
            if not args.dry_run:
                roots = sources_service.merge_roots(conn, source, roots)
            else:
                existing = source_config(source).get("roots") or []
                roots = list(dict.fromkeys([*[str(item) for item in existing], *roots]))

            connector = connector_for(
                source,
                authorizations,
                roots=roots,
                max_media_bytes=limits.max_media_bytes,
                max_text_chars=limits.max_text_chars,
            )
            if not connector.authorize():
                print(
                    f"  ✗ 来源「{source['name']}」未通过授权校验："
                    "凭证 / 授权范围 / 授权证明三者不齐备（DESIGN §1.3）",
                    file=sys.stderr,
                )
                return 2

            capabilities = connector.capabilities()
            print(f"[导入] 来源「{source['name']}」（{capabilities['kind']}）")
            print(f"  目录：{'、'.join(capabilities['roots'])}")
            if not capabilities.get("supports_pull", False):
                print("  ✗ 该来源是推送型，不能用 pc import 拉取", file=sys.stderr)
                return 2

            report = ingest_service.IngestReport()
            cursor: str | None = None
            scanned = 0
            while True:
                messages, cursor = connector.list_new(cursor)
                if not messages:
                    break
                scanned += len(messages)
                if args.dry_run:
                    report = report.merge(
                        ingest_service.IngestReport(seen=len(messages))
                    )
                else:
                    report = report.merge(
                        ingest_service.ingest_messages(conn, source, messages)
                    )
                if args.limit and scanned >= args.limit:
                    break
    except (
        db.DatabaseError,
        sources_service.SourceError,
        UnsupportedSourceKindError,
        SourceConfigError,
    ) as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"  · dry-run：发现 {report.seen} 个文件，未写任何行。去掉 --dry-run 才会真正导入。")
        return 0

    print(
        f"  ✓ 新消息 {report.inserted} 条，重复（已存在）{report.duplicates} 条，"
        f"入队 {report.enqueued} 个任务"
    )
    if not args.process:
        print("    接着处理：uv run pc work（或下次直接加 --process）")
        return 0

    print()
    with db.open_db(resolved.path, expect_exists=True) as conn:
        worker = _build_worker(conn, paths, root, worker_id=args.worker_id)
        work = worker.run_once()
    _print_work_report(work, prefix="  ")
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    """跑 worker：抢占任务、取字节、扫描、去重、落库。"""
    from pc.adapters import sqlite as db

    resolved, paths = _db_and_paths(args)
    if not _require_database(resolved):
        return 2

    root = Path(args.root).resolve() if getattr(args, "root", None) else find_repo_root()
    limit = args.max if args.max else (1 if args.once else None)

    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            worker = _build_worker(conn, paths, root, worker_id=args.worker_id)
            print(f"[worker] id={worker.worker_id}")
            if args.loop:
                total = 0
                while True:
                    report = worker.run_once(limit=limit)
                    total += report.claimed
                    _print_work_report(report, prefix="  ")
                    if not report.claimed:
                        print(f"  · 队列空了（本次共处理 {total} 条），{args.interval} 秒后再看；Ctrl+C 退出")
                    time.sleep(max(1, args.interval))
            else:
                report = worker.run_once(limit=limit)
    except db.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        print("\n  · 已中断")
        return 0

    if not args.loop:
        _print_work_report(report, prefix="  ")
        if not report.claimed:
            print("  · 队列里没有可处理的任务（`pc import` 之后才有）")
    return 0


def _print_work_report(report: Any, *, prefix: str = "") -> None:
    if report.recovered:
        print(f"{prefix}· 崩溃恢复：重置了 {report.recovered} 条超时任务")
    print(
        f"{prefix}✓ 处理 {report.claimed} 条：成功 {report.succeeded}，"
        f"失败（待重试）{report.failed}，死信 {report.dead}"
    )
    for job_id, status, note in report.notes:
        if status in {"succeeded", "failed", "dead"} and job_id:
            print(f"{prefix}  job {job_id} {status}：{note}")


def cmd_item_list(args: argparse.Namespace) -> int:
    """列出内容项——这是「内容真的进库了」最直接的证据。"""
    from pc.adapters import sqlite as db

    resolved, _ = _db_and_paths(args)
    if not resolved.path.is_file():
        print(f"  · 还没有数据库文件（{resolved.path}）；建库并导入之后这里会有内容。")
        return 0

    sql = [
        "SELECT c.content_id, c.item_type, c.status, s.name AS source, c.ordinal,",
        "       c.byte_size, c.char_count, c.text_content,",
        "       (SELECT count(*) FROM content_media cm WHERE cm.content_id = c.content_id) AS media_count,",
        "       (SELECT m.sha256 FROM content_media cm JOIN media m ON m.media_id = cm.media_id",
        "         WHERE cm.content_id = c.content_id LIMIT 1) AS sha256,",
        "       (SELECT m.scan_status FROM content_media cm JOIN media m ON m.media_id = cm.media_id",
        "         WHERE cm.content_id = c.content_id LIMIT 1) AS scan_status,",
        "       c.created_at",
        "  FROM content_item c",
        "  JOIN message m ON m.message_id = c.message_id",
        "  JOIN source s ON s.source_id = m.source_id",
        " WHERE c.deleted_at IS NULL",
    ]
    params: list[Any] = []
    if args.source:
        sql.append("   AND s.name = ?")
        params.append(args.source)
    if args.status:
        sql.append("   AND c.status = ?")
        params.append(args.status)
    sql.append(" ORDER BY c.content_id DESC LIMIT ?")
    params.append(max(1, args.limit))

    with db.open_db(resolved.path, expect_exists=True) as conn:
        rows = [dict(row) for row in conn.execute("\n".join(sql), params)]
        total = conn.execute(
            "SELECT count(*) FROM content_item WHERE deleted_at IS NULL"
        ).fetchone()[0]

    if args.json:
        print(cfg_dump_json({"total": int(total), "items": rows}))
        return 0
    if not rows:
        print("  · 还没有内容。试试：pc source add … → pc import <目录> --source … --process")
        return 0

    table: list[list[Any]] = []
    for row in rows:
        if row["text_content"]:
            summary = str(row["text_content"])[:32].replace("\n", " ")
            if len(str(row["text_content"])) > 32:
                summary += "…"
        elif row["sha256"]:
            summary = f"媒体 {row['sha256'][:12]}…（{_human_size(row['byte_size'])}）"
        else:
            summary = "（空）"
        table.append(
            [
                row["content_id"],
                row["item_type"],
                row["status"],
                row["source"],
                summary,
                row["scan_status"] or "-",
                row["media_count"],
                row["created_at"],
            ]
        )
    _print_table(["id", "类型", "状态", "来源", "内容", "扫描", "媒体", "建库时间"], table)
    print(f"  共 {total} 个内容项（上面显示最近 {len(rows)} 条）")
    return 0


def _human_size(size: Any) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):  # pragma: no cover - 列是整数
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover - 上一步已覆盖


def cfg_dump_json(payload: Any) -> str:
    from pc import config as cfg

    return cfg.dump_json(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc",
        description="post-collector 命令行（doctor / config / db / source / import / work / item）",
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
    p_show.add_argument("--path", help="显式指定数据库文件；默认按 env > .env > app.yaml > 平台默认解析")
    p_show.set_defaults(func=cmd_config_show)

    p_seed = p_config_sub.add_parser(
        "seed", help="首次播种 setting 表（只插入缺失的键，不覆盖已有值）"
    )
    p_seed.add_argument("--root", help="指定仓库根目录（默认自动查找）")
    p_seed.add_argument(
        "--path", help="显式指定数据库文件；默认按 env > .env > app.yaml > 平台默认解析"
    )
    p_seed.set_defaults(func=cmd_config_seed)

    p_db = sub.add_parser("db", help="数据库：应用迁移、查看结构、备份与重建索引")
    p_db_sub = p_db.add_subparsers(dest="db_command", required=True)

    db_path_help = "显式指定数据库文件；默认按 env > .env > app.yaml > 平台默认解析"
    migrations_dir_help = "迁移目录（默认 migrations/sqlite；按文件名序号顺序应用全部 .sql）"

    p_db_apply = p_db_sub.add_parser(
        "apply", help="应用迁移（逐个文件各自一个事务；已应用则安全短路）"
    )
    p_db_apply.add_argument("--path", help=db_path_help)
    p_db_apply.add_argument("--dir", help=migrations_dir_help)
    p_db_apply.set_defaults(func=cmd_db_apply)

    p_db_status = p_db_sub.add_parser("status", help="只读报告库结构与迁移是否一致")
    p_db_status.add_argument("--path", help=db_path_help)
    p_db_status.add_argument("--dir", help=migrations_dir_help)
    p_db_status.set_defaults(func=cmd_db_status)

    p_db_backup = p_db_sub.add_parser(
        "backup", help="生成事务一致的快照并校验（VACUUM INTO）"
    )
    p_db_backup.add_argument("--path", help=db_path_help)
    p_db_backup.add_argument(
        "--to",
        help="快照输出路径（默认 <data_root>/backup/pc-<UTC 时间戳>.sqlite3）；已存在则拒绝覆盖",
    )
    p_db_backup.set_defaults(func=cmd_db_backup)

    p_db_reindex = p_db_sub.add_parser("reindex", help="全量重建两个 FTS5 索引")
    p_db_reindex.add_argument("--path", help=db_path_help)
    p_db_reindex.set_defaults(func=cmd_db_reindex)

    # -- 来源与导入 ---------------------------------------------------------

    p_source = sub.add_parser("source", help="来源登记：授权三者齐备才可采集")
    p_source_sub = p_source.add_subparsers(dest="source_command", required=True)

    p_source_add = p_source_sub.add_parser("add", help="登记一个来源（同时写下授权记录）")
    p_source_add.add_argument("name", help="来源名称，唯一")
    p_source_add.add_argument("--platform", default="local", help="平台标识（默认 local）")
    p_source_add.add_argument(
        "--kind",
        default="export",
        choices=["export", "bot_api", "webhook", "rss", "user_api"],
        help="来源类型（默认 export：本地文件/官方导出目录）",
    )
    p_source_add.add_argument(
        "--trust", type=int, default=1, choices=[1, 2, 3], help="信任级别：1=自有 2=已授权群 3=外部"
    )
    p_source_add.add_argument("--ref", help="平台侧标识（频道 ID / 群 ID / 导出包标识）")
    p_source_add.add_argument(
        "--roots", action="append", help="该来源允许读取的目录（可重复）；会写进 source.config"
    )
    p_source_add.add_argument(
        "--authorize",
        default="manual_export",
        choices=["manual_export", "owner_token", "admin_consent", "public_feed"],
        help="授权取得方式（默认 manual_export：自己的导出与文件）",
    )
    p_source_add.add_argument("--evidence", help="授权证明留档：文件路径 / 链接 / 截图哈希")
    p_source_add.add_argument("--expires-at", help="授权到期时间（ISO-8601，留空表示不过期）")
    p_source_add.add_argument("--path", help=db_path_help)
    p_source_add.set_defaults(func=cmd_source_add)

    p_source_list = p_source_sub.add_parser("list", help="列出来源与授权状态")
    p_source_list.add_argument("--json", action="store_true", help="输出 JSON")
    p_source_list.add_argument("--path", help=db_path_help)
    p_source_list.set_defaults(func=cmd_source_list)

    p_import = sub.add_parser("import", help="把本地文件导入库（落信封并入队）")
    p_import.add_argument("paths", nargs="+", help="文件或目录（目录会递归扫描）")
    p_import.add_argument("--source", required=True, help="来源名称或 source_id")
    p_import.add_argument("--process", action="store_true", help="导入后立刻跑 worker 处理")
    p_import.add_argument("--limit", type=int, help="最多扫描这么多条（调试用）")
    p_import.add_argument("--dry-run", action="store_true", help="只报告会发现什么，不写任何行")
    p_import.add_argument("--worker-id", help="处理阶段的 worker 标识")
    p_import.add_argument("--root", help="仓库根目录（默认自动查找，用于读 config/app.yaml）")
    p_import.add_argument("--path", help=db_path_help)
    p_import.set_defaults(func=cmd_import)

    p_work = sub.add_parser("work", help="跑 worker：取字节、扫描、去重、落库")
    p_work.add_argument("--once", action="store_true", help="只处理一条")
    p_work.add_argument("--max", type=int, help="最多处理这么多条")
    p_work.add_argument("--loop", action="store_true", help="常驻轮询（Ctrl+C 退出）")
    p_work.add_argument("--interval", type=int, default=5, help="--loop 时的轮询间隔秒数（默认 5）")
    p_work.add_argument("--worker-id", help="worker 标识（默认 pid@主机名）")
    p_work.add_argument("--root", help="仓库根目录（默认自动查找，用于读 config/app.yaml）")
    p_work.add_argument("--path", help=db_path_help)
    p_work.set_defaults(func=cmd_work)

    p_item = sub.add_parser("item", help="内容项：查看库里到底有什么")
    p_item_sub = p_item.add_subparsers(dest="item_command", required=True)
    p_item_list = p_item_sub.add_parser("list", help="列出最近的内容项")
    p_item_list.add_argument("--limit", type=int, default=20, help="最多显示多少条（默认 20）")
    p_item_list.add_argument("--source", help="只看某个来源")
    p_item_list.add_argument(
        "--status", choices=["active", "quarantined", "archived", "deleted"], help="只看某个状态"
    )
    p_item_list.add_argument("--json", action="store_true", help="输出 JSON")
    p_item_list.add_argument("--path", help=db_path_help)
    p_item_list.set_defaults(func=cmd_item_list)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
