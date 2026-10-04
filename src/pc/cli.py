"""命令行入口（``pc``）。

设计目标（DESIGN §6.2.3、§6.2.4）：报告当前状态与可执行的下一步，
提供配置来源解释；查询可输出完整 JSON 或终端表格。

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

from pc import __version__, domain
from pc.adapters.fts_search import TIME_FIELD_INGEST, TIME_FIELDS
from pc.presentation import (
    ELLIPSIS as ELLIPSIS,
)
from pc.presentation import (
    ITEM_LIST_HEADERS as ITEM_LIST_HEADERS,
)
from pc.presentation import (
    ITEM_LIST_MAX_WIDTHS as ITEM_LIST_MAX_WIDTHS,
)
from pc.presentation import (
    SEARCH_HEADERS as SEARCH_HEADERS,
)
from pc.presentation import (
    SEARCH_MAX_WIDTHS as SEARCH_MAX_WIDTHS,
)
from pc.presentation import (
    SOURCE_LIST_HEADERS as SOURCE_LIST_HEADERS,
)
from pc.presentation import (
    SOURCE_LIST_MAX_WIDTHS as SOURCE_LIST_MAX_WIDTHS,
)
from pc.presentation import (
    _display_width as _display_width,
)
from pc.presentation import (
    _pad_display as _pad_display,
)
from pc.presentation import (
    _prepare_row as _prepare_row,
)
from pc.presentation import (
    _print_table as _print_table,
)
from pc.presentation import (
    _truncate_display as _truncate_display,
)
from pc.presentation import (
    render_result,
)

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
    _doctor_database_section(root)
    print()
    print("[检索]（只读探测：两个 FTS5 索引的规模与派生列是否跟上了）")
    _doctor_search_section(root)
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
        print(cfg_dump_json(payload))
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
    resolved, _ = _db_and_paths(args)
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

    resolved, _ = _db_and_paths(args)
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
        resolved = db.resolve_paths(
            env_file=root / ".env", app_config=root / "config/app.yaml"
        )
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


def _doctor_database_section(root: Path) -> None:
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

    resolved = db.resolve_db_path(
        None, env_file=root / ".env", app_config=root / "config/app.yaml"
    )
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


def _doctor_search_section(root: Path) -> None:
    """doctor 的检索段：索引规模 + 有多少行的派生数据过期。

    "搜不到"最常见的两个原因就在这里：索引是空的（还没重建），或者派生数据没算过
    （升级前落库的老行、没对齐的帖表）。两者都只报告，不改动任何东西，也不影响退出码。
    """
    from pc.adapters import sqlite as db

    try:
        db.ensure_version_gate()
    except db.DatabaseError as exc:
        print(f"  · {str(exc).splitlines()[0]}")
        return

    resolved = db.resolve_db_path(
        None, env_file=root / ".env", app_config=root / "config/app.yaml"
    )
    if not resolved.path.is_file():
        print(f"  · 还没有数据库文件（{resolved.path}）")
        return

    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            counts = {
                name: int(conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
                for name in db.FTS_VIRTUAL_TABLES
            }
            stale = db.stale_search_row_count(conn)
            items = int(
                conn.execute(
                    "SELECT count(*) FROM content_item WHERE deleted_at IS NULL"
                ).fetchone()[0]
            )
    except db.DatabaseError as exc:
        print(f"  · 打不开：{str(exc).splitlines()[0]}")
        return

    counts_text = "，".join(f"{name} {count} 行" for name, count in sorted(counts.items()))
    print(f"  ✓ 索引 {counts_text}（有效内容 {items} 条）")
    if stale:
        print(
            f"  · {stale} 条内容的检索派生数据过期（没分词、文件名没补上，或帖表没对齐）："
            "跑 `pc db reindex --recompute-derived`"
            "（1~2 字的中文查询与按文件名的查询会搜不到它们）"
        )
    elif items and not any(counts.values()):
        print("  · 索引是空的：跑 `pc db reindex --recompute-derived` 重建")
    else:
        print("  · 派生数据已跟上，无需重算")


def cmd_db_status(args: argparse.Namespace) -> int:
    """只读报告「库里的结构是否与迁移一致」（不改任何东西）。"""
    from pc.adapters import sqlite as db

    resolved, _ = _db_and_paths(args)
    print(f"[数据库] {resolved.path}")
    print(f"  {resolved.explain()}")

    if not resolved.path.is_file():
        print(
            f"  ✗ 数据库文件还不存在。建库：uv run pc db apply --path \"{resolved.path}\"",
            file=sys.stderr,
        )
        return 2

    directory = _migrations_dir(args)
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

    resolved, _ = _db_and_paths(args)
    print(f"[数据库] {resolved.path}")
    print(f"  {resolved.explain()}")

    directory = _migrations_dir(args)
    print(f"[迁移] {directory}")
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

    resolved, _ = _db_and_paths(args)
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
    """重建两个 FTS5 索引；``--recompute-derived`` 时先重算两列检索派生数据。

    索引是派生数据（DESIGN §9.1），随时可重建。但**派生列**不是索引：``text_search``
    与 ``search_extra`` 是应用层算出来写在表里的，索引只是把它们搬进倒排表。所以
    "搜不到"分两种情况：索引坏了（重建索引即可）、派生列过期或从来没算过（要先重算）。
    这个开关一次把两件事都做完，顺序不能反。
    """
    from pc.adapters import sqlite as db

    resolved, _ = _db_and_paths(args)
    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            if getattr(args, "recompute_derived", False):
                stale = db.stale_search_row_count(conn)
                print(f"  · 待重算：{stale} 行没有分词结果（其余行只是核对，值相同不写）")
                result = db.refresh_search_derived(
                    conn,
                    batch_size=getattr(args, "batch_size", None) or 500,
                    progress=_reindex_progress,
                )
                print(
                    f"  ✓ 派生列：扫描 {result['scanned']} 行，更新 {result['updated']} 行"
                    f"（其中补上文件名 {result['named']} 行；共 {result['total']} 行）"
                )
                print(
                    f"  ✓ 帖表：对齐 {result['posts']} 帖"
                    + (f"，清掉 {result['posts_removed']} 张空帖" if result["posts_removed"] else "")
                )
            reindexed = db.reindex_fts(conn)
    except db.DatabaseError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    for name in sorted(reindexed["after"]):
        before = reindexed["before"].get(name, 0)
        print(f"  ✓ {name}：{before} 行 → {reindexed['after'][name]} 行")
    return 0


def _reindex_progress(scanned: int, total: int) -> None:
    """每两千行报一次进度：十万行的重算要跑一会儿，静默会让人以为卡住了。"""
    if scanned % 2000 == 0 or scanned >= total:
        print(f"    … 已核对 {scanned}/{total} 行")


def _project_root(args: argparse.Namespace) -> Path:
    """定位项目根目录：``--root`` 优先，否则按 ``pyproject.toml`` + ``DESIGN.md`` 向上查找。

    **凡是依赖仓库文件的东西都必须经过这里**（迁移目录、``config/app.yaml``、``.env``）。
    之前只有 doctor 与 config 走了这套查找，db 子命令用的是「当前工作目录 + 相对路径」，
    于是从别的目录运行时会报「找不到迁移目录」——那是实现缺陷，不是使用姿势问题。
    """
    explicit = getattr(args, "root", None)
    return Path(explicit).resolve() if explicit else find_repo_root()


def _db_and_paths(args: argparse.Namespace) -> tuple[Any, Any]:
    """一次性解析库文件与运行时目录——写命令都需要这两者，且必须来自同一份配置。

    ``env_file`` 与 ``app_config`` 传**绝对路径**：相对路径会跟着当前工作目录变，
    于是「从别的目录运行」会安静地少掉两层配置（DESIGN §6.2 的分层就白设了）。
    """
    from pc.adapters import sqlite as db

    root = _project_root(args)
    return (
        db.resolve_db_path(
            getattr(args, "path", None),
            env_file=root / ".env",
            app_config=root / "config/app.yaml",
        ),
        db.resolve_paths(env_file=root / ".env", app_config=root / "config/app.yaml"),
    )


def _migrations_dir(args: argparse.Namespace) -> Path:
    """迁移目录：``--dir`` 优先，否则 ``<项目根>/migrations/sqlite``。"""
    from pc.adapters import sqlite as db

    explicit = getattr(args, "dir", None)
    if explicit:
        return Path(explicit).expanduser()
    return _project_root(args) / db.DEFAULT_MIGRATIONS_DIR


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


# ---------------------------------------------------------------------------
# 等宽表格
#
# 终端里"字符数"不等于"占几格"：一个汉字占 2 格，组合记号与零宽字符占 0 格。用
# len() 排版会让中文表格整体错位——这是开发者调试时最先受不了的一类问题。所以这里
# 统一按**显示宽度**测量与截断，并且给每一列一个上限（否则一列会由最长的那个单元格
# 决定，中文内容下等于没有上限）。
# ---------------------------------------------------------------------------

#: 截断标记。刻意只用一个字符；如果哪个终端把 `…` 渲染成 2 格（歧义宽度按 CJK 习惯
#: 渲染），把它换成 "..." 即可——其余代码不用动。


def cmd_source_add(args: argparse.Namespace) -> int:
    """登记一个来源，并同时写下它的授权记录（三者齐备才可采集，DESIGN §1.3）。"""
    from pc.adapters import sqlite as db
    from pc.adapters.registry import PLANNED_KINDS, SUPPORTED_KINDS
    from pc.services import sources as sources_service

    resolved, _ = _db_and_paths(args)
    if not _require_database(resolved):
        return 2

    config: dict[str, Any] = {}
    roots = [os.path.abspath(Path(item).expanduser()) for item in (args.roots or [])]
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
    elif info["authorization_mode"] == "root_self":
        print("  · 这个来源不需要凭证：授权来自操作者本人在场（`pc upload` 会直接用它）")
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
    _print_table(SOURCE_LIST_HEADERS, table, max_widths=SOURCE_LIST_MAX_WIDTHS)
    return 0


def _prepare_local_connector(connector: Any, source: Any, *, command: str) -> bool:
    """授权校验 + 拉取型校验；不过关就打印人话并返回 False。

    ``pc import`` 与 ``pc upload`` 共用：这两条命令的差别只在"来源怎么来"，授权与
    "能不能拉取"的判据必须完全一致。
    """
    if not connector.authorize():
        print(
            f"  ✗ 来源「{source['name']}」未通过授权校验："
            "凭证 / 授权范围 / 授权证明三者不齐备（DESIGN §1.3）",
            file=sys.stderr,
        )
        return False
    if not connector.capabilities().get("supports_pull", False):
        print(f"  ✗ 该来源是推送型，不能用 {command} 拉取", file=sys.stderr)
        return False
    return True


def _ingest_loop(
    conn: Any,
    source: Any,
    connector: Any,
    *,
    dry_run: bool,
    limit: int | None,
    origin_note: str | None = None,
) -> tuple[Any, int]:
    """按批 ``list_new`` → 落库（或 dry-run 计数），返回 ``(报告, 扫描到的消息数)``。

    ``pc import`` 与 ``pc upload`` 共用这一段：取字节之后的流程必须一模一样，否则
    两条路会各自漂移。差别只在调用前怎么构造连接器、传不传 ``origin_note``。
    """
    from pc.services import ingest as ingest_service

    report = ingest_service.IngestReport()
    cursor: str | None = None
    scanned = 0
    while True:
        previous = cursor
        messages, cursor = connector.list_new(cursor)
        if cursor == previous:
            if messages:
                raise RuntimeError("连接器游标未推进，拒绝重复摄入")
            break
        if not messages:
            continue
        scanned += len(messages)
        if dry_run:
            report = report.merge(ingest_service.IngestReport(seen=len(messages)))
        else:
            report = report.merge(
                ingest_service.ingest_messages(conn, source, messages, origin_note=origin_note)
            )
        if limit and scanned >= limit:
            break
    return report, scanned


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
    from pc.services import sources as sources_service

    resolved, paths = _db_and_paths(args)
    if not _require_database(resolved):
        return 2

    requested = [Path(os.path.abspath(Path(item).expanduser())) for item in args.paths]
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
            if not _prepare_local_connector(connector, source, command="pc import"):
                return 2

            capabilities = connector.capabilities()
            print(f"[导入] 来源「{source['name']}」（{capabilities['kind']}）")
            print(f"  目录：{'、'.join(capabilities['roots'])}")
            report, _scanned = _ingest_loop(
                conn, source, connector, dry_run=args.dry_run, limit=args.limit
            )
    except (
        db.DatabaseError,
        sources_service.SourceError,
        UnsupportedSourceKindError,
        SourceConfigError,
        domain.SourceNotAuthorizedError,
        RuntimeError,
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


def _print_skipped(skipped: Any) -> None:
    """报告"跳过了哪些文件、为什么"——跳过不能是静默的。"""
    if not skipped:
        return
    print(f"  · 跳过 {len(skipped)} 个文件（内容身份不成立）：")
    for name, reason in list(skipped)[:5]:
        print(f"      {name} —— {reason}")
    if len(skipped) > 5:
        print(f"      …还有 {len(skipped) - 5} 个")


def cmd_upload(args: argparse.Namespace) -> int:
    """root 手工上传：一条命令把文件丢进库。

    与 ``pc import`` 的差别是刻意的（都写在 DESIGN §1.3 的那张表里）：

    * **来源可以省略**：默认用（必要时新建）「manual」这个"来源待补"的归档桶；
    * **身份按内容哈希**：重传同一份内容不会变成两条，改名换目录也算同一条；
    * ``--note`` 记下"这是从哪儿弄来的"（可选，写在 ``message.origin_note``）；
    * **默认接着处理**：媒体引用存的是文件路径，你手一快把原文件删了，任务就只能死信。
    """
    from pc import config as cfg
    from pc.adapters import sqlite as db
    from pc.adapters.registry import (
        SourceConfigError,
        UnsupportedSourceKindError,
        connector_for,
    )
    from pc.services import sources as sources_service

    resolved, paths = _db_and_paths(args)
    if not _require_database(resolved):
        return 2

    requested = [Path(os.path.abspath(Path(item).expanduser())) for item in args.paths]
    missing = [str(item) for item in requested if not item.exists()]
    if missing:
        print(f"  ✗ 找不到这些路径：{'、'.join(missing)}", file=sys.stderr)
        return 2

    root = Path(args.root).resolve() if getattr(args, "root", None) else find_repo_root()
    app_config = _load_app_config(root)
    limits = cfg.ingest_limits(app_config)

    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            if args.source:
                source = sources_service.require_source(conn, args.source)
                if source.get("kind") != "manual":
                    print(
                        f"  ✗ 来源「{source['name']}」的 kind 是 {source.get('kind')}，不是 manual。\n"
                        "    手工上传按**内容**定身份（重传同一份内容不会变成两条）；别的 kind 按路径\n"
                        "    定身份，同一张图换个名字重传就会多出一条。\n"
                        "    要么别加 --source（用默认的「manual」来源），"
                        "要么 `pc source add <名字> --kind manual` 建一个。",
                        file=sys.stderr,
                    )
                    return 2
                created = False
            else:
                ensured = sources_service.ensure_manual_source(conn)
                source = ensured["source"]
                created = bool(ensured["created"])

            authorizations = sources_service.load_authorizations(conn, int(source["source_id"]))
            # **扫描范围就是这次给的路径**（`pc upload a.jpg` 只处理 a.jpg）。合并进
            # source.config 的 roots 是给 worker 用的（它在另一个时刻按来源构造连接器），
            # 但**不能**拿来当本次的扫描范围：那样第二次上传一个文件会把以前传过的路径
            # 全部重扫一遍，报告里冒出"重复 N 条"，N 还不是你这次给的数量。
            requested_roots = [str(item) for item in requested]
            if not args.dry_run:
                sources_service.merge_roots(conn, source, requested_roots)

            connector = connector_for(
                source,
                authorizations,
                roots=requested_roots,
                max_media_bytes=limits.max_media_bytes,
                max_text_chars=limits.max_text_chars,
            )
            if not _prepare_local_connector(connector, source, command="pc upload"):
                return 2

            capabilities = connector.capabilities()
            print(
                f"[上传] 来源「{source['name']}」"
                f"（{capabilities['kind']}，身份={capabilities['identity']}）"
                + ("，本次新建" if created else "")
            )
            print(f"  需要凭证：{'是' if capabilities.get('requires_credential') else '否'}")
            if args.note:
                print(f"  备注：{args.note}")
            report, _scanned = _ingest_loop(
                conn,
                source,
                connector,
                dry_run=args.dry_run,
                limit=args.limit,
                origin_note=args.note,
            )
            # 跳过清单是连接器的报告属性（不是端口契约的一部分），取不到就当没有
            skipped = getattr(connector, "skipped", ())
    except (
        db.DatabaseError,
        sources_service.SourceError,
        UnsupportedSourceKindError,
        SourceConfigError,
        domain.SourceNotAuthorizedError,
        RuntimeError,
    ) as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    _print_skipped(skipped)
    if args.dry_run:
        print(f"  · dry-run：发现 {report.seen} 个文件，未写任何行。去掉 --dry-run 才会真正上传。")
        return 0

    print(
        f"  ✓ 新消息 {report.inserted} 条，重复（已存在）{report.duplicates} 条，"
        f"入队 {report.enqueued} 个任务"
    )
    if not args.process:
        print("    接着处理：uv run pc work（或下次直接去掉 --no-process）")
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
        "SELECT c.content_id, c.message_id, c.item_type, c.status, s.name AS source, c.ordinal,",
        "       c.byte_size, c.char_count, c.text_content, m.origin_note,",
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
            summary = str(row["text_content"])
        elif row["sha256"]:
            summary = f"媒体 {row['sha256'][:12]}…（{_human_size(row['byte_size'])}）"
        else:
            summary = "（空）"
        table.append(
            [
                row["content_id"],
                row["message_id"],
                row["item_type"],
                row["status"],
                row["source"],
                summary,
                row["scan_status"] or "-",
                row["media_count"],
                row["origin_note"] or "-",
                row["created_at"],
            ]
        )
    # 截断与对齐都交给 _print_table（按显示宽度，中文算 2 格）；这里只做领域格式化
    _print_table(ITEM_LIST_HEADERS, table, max_widths=ITEM_LIST_MAX_WIDTHS)
    print(f"  共 {total} 个内容项（上面显示最近 {len(rows)} 条）")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """按关键词与过滤器检索内容（DESIGN §5）。

    默认**只看安全、有效、未软删的内容**（`status='active'` 且 `nsfw_level <= 0`），
    同一段文本的重复项折叠到代表项——放宽必须显式给参数。关键词检索不返回精确总数
    （§5.6 M1b），只报候选数；纯过滤列表给精确条数。
    """
    from pc import config as config_mod
    from pc.adapters import sqlite as db
    from pc.services import search as search_svc

    resolved, _ = _db_and_paths(args)
    if not resolved.path.is_file():
        if args.json:
            print("数据库尚未建立，请先运行 pc db apply", file=sys.stderr)
            return 2
        print(f"  · 还没有数据库文件（{resolved.path}）；建库并导入之后这里就能搜到东西。")
        return 0

    root = _project_root(args)
    registry: Any = None
    yaml_defaults: dict[str, Any] = {}
    registry_note: str | None = None
    try:
        registry = config_mod.registry_index(
            config_mod.load_registry(root / config_mod.DEFAULT_REGISTRY_PATH)
        )
        yaml_defaults = config_mod.load_app_defaults(
            root / config_mod.DEFAULT_APP_CONFIG_PATH, registry=registry
        )
    except config_mod.ConfigError as exc:
        # 页大小读不到就按默认值兜底，但要说清楚——静默降级比报错更难查
        registry_note = str(exc)

    query = search_svc.SearchQuery(
        q=" ".join(args.terms),
        source=args.source,
        item_types=tuple(args.type or ()),
        statuses=(args.status,) if args.status else (),
        tags=tuple(args.tag or ()),
        since=args.since,
        until=args.until,
        time_field=args.time_field,
        nsfw_max_level=args.nsfw_level,
        include_duplicates=args.include_duplicates,
        sort=args.sort,
        limit=args.limit,
        cursor=args.cursor,
        page=args.page,
    )

    with db.open_db(resolved.path, expect_exists=True) as conn:
        try:
            result = search_svc.run_search(
                conn,
                query,
                registry=registry,
                yaml_defaults=yaml_defaults,
                env=os.environ,
            )
        except search_svc.SearchError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            return 2
        stale = search_svc.stale_derived_count(conn) if not result["hits"] else 0

    if registry_note:
        print(f"  · 设置项注册表不可用（{registry_note}），每页条数按默认值兜底。", file=sys.stderr)
    if args.json:
        print(cfg_dump_json(result))
        return 0
    if stale:
        print(f"  {stale} 条内容还没有分词结果，请重算：pc db reindex --recompute-derived", file=sys.stderr)
    return render_result(result, timings=args.timings)


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


def cfg_dump_json(payload: Any, *, ensure_ascii: bool = True) -> str:
    """CLI JSON 用 Unicode 转义保留完整值，兼容 ASCII 原生命令管道。"""
    import json

    return json.dumps(payload, ensure_ascii=ensure_ascii, indent=2, default=str)


def cmd_render(args: argparse.Namespace) -> int:
    """从 stdin 读取查询结果，复用直接输出的表格渲染。"""
    import json

    try:
        raw = (sys.stdin.buffer.read().decode("utf-8-sig")
               if hasattr(sys.stdin, "buffer") else sys.stdin.read().lstrip("\ufeff"))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("输入须为结果对象")
        return render_result(payload)
    except (ValueError, TypeError, KeyError) as exc:
        print(f"渲染失败：{exc}", file=sys.stderr)
        return 2


def cmd_tag(args: argparse.Namespace) -> int:
    """CLI 负责人工候选输入和输出；标签写入由共用服务执行。"""
    import sqlite3

    from pc.adapters import sqlite as db
    from pc.services import tagging

    resolved, _ = _db_and_paths(args)
    if not _require_database(resolved):
        return 2
    try:
        with db.open_db(resolved.path, expect_exists=True) as conn:
            operation = args.tag_command
            targets = {"message_ids": getattr(args, "message", None) or (),
                       "content_ids": getattr(args, "content", None) or ()}
            if operation == "add":
                output = tagging.apply_classification(
                    conn, tagging.manual_result(args.tag, args.namespace), **targets)
            elif operation == "remove":
                output = tagging.remove_tags(conn, args.tag, **targets)
            elif operation == "show":
                output = tagging.show_tags(conn, **targets)
            elif operation == "list":
                output = tagging.list_tags(conn)
            elif operation in {"confirm", "reject"}:
                output = tagging.review_suggestions(conn, args.ids, confirm=operation == "confirm")
            elif args.alias_command == "list":
                output = tagging.list_aliases(conn, args.tag)
            else:
                output = tagging.change_alias(conn, args.alias, name=getattr(args, "tag", None))
        if args.json:
            print(cfg_dump_json(output))
        elif "result_type" in output:
            render_result(output)
        else:
            print(f"  标签操作完成：{cfg_dump_json(output, ensure_ascii=False)}")
        return 0
    except (ValueError, sqlite3.Error, db.DatabaseError) as exc:
        print(f"标签操作失败：{exc}", file=sys.stderr)
        return 2


def cmd_view(args: argparse.Namespace) -> int:
    """内容详情、默认程序与文件管理器入口。"""
    import sqlite3

    from pc.adapters import sqlite as db
    from pc.adapters import view_files, windows_view
    from pc.services import viewing

    copied = False
    try:
        if getattr(args, "json", False) and args.copy:
            raise viewing.ViewError("--json 与 --copy 不能同时使用")
        resolved, paths = _db_and_paths(args)
        if not _require_database(resolved):
            return 2
        with db.open_db(resolved.path, expect_exists=True) as conn:
            result = viewing.detail(conn, message_id=args.message, content_id=args.content)
            if getattr(args, "json", False):
                print(cfg_dump_json(result))
                return 0
            kind = viewing.preflight(result, copy=args.copy)
            if args.command == "show" and kind != "text":
                raise viewing.ViewError(
                    "终端图片展示尚未通过 DST 集成验收；show 当前支持纯文本与 --json。"
                )
            if args.command != "show" or args.copy:
                windows_view.require_windows()
            body = viewing.text_body(result)
            if args.copy and kind == "text" and "\0" in body:
                raise viewing.ViewError("正文含 NUL，无法完整复制到剪贴板")
            images = []
            files = None
            if args.command != "show":
                from pc.config import ingest_limits

                limits = ingest_limits(_load_app_config(_project_root(args)))
                images = view_files.read_images(conn, result, limit=limits.max_media_bytes)
                files = view_files.materialize(
                    result, images, cache_root=paths.data_root / "view-cache", kind=kind
                )
            # 系统操作前再读一次目标，缓存不替代可见性检查。
            current = viewing.detail(conn, message_id=args.message, content_id=args.content)
            if current != result:
                raise viewing.ViewError("目标在准备期间发生变化，请重新执行")
        if args.copy:
            if kind == "text":
                windows_view.copy_text(body)
            else:
                assert files is not None
                windows_view.copy_files(files.files)
            copied = True
        if args.command == "show":
            # 终端正文去除 C0/C1 控制字符，JSON/TXT/剪贴板保留原始正文。
            from pc.view_presentation import terminal_text

            print(terminal_text(body))
        else:
            assert files is not None
            if args.command == "open":
                windows_view.open_files(files.open_files)
            else:
                windows_view.reveal_files(files.open_files)
            print(str(files.directory))
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        suffix = "；剪贴板复制已完成" if copied else ""
        print(f"展示失败：{exc}{suffix}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc",
        description="post-collector 命令行（导入、处理、检索、标签与内容查看）",
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
    root_help = "项目根目录（默认自动查找；找不到仓库文件时用它指定）"
    migrations_dir_help = (
        "迁移目录（默认 <项目根>/migrations/sqlite；按文件名序号顺序应用全部 .sql）"
    )

    p_db_apply = p_db_sub.add_parser(
        "apply", help="应用迁移（逐个文件各自一个事务；已应用则安全短路）"
    )
    p_db_apply.add_argument("--path", help=db_path_help)
    p_db_apply.add_argument("--dir", help=migrations_dir_help)
    p_db_apply.add_argument("--root", help=root_help)
    p_db_apply.set_defaults(func=cmd_db_apply)

    p_db_status = p_db_sub.add_parser("status", help="只读报告库结构与迁移是否一致")
    p_db_status.add_argument("--path", help=db_path_help)
    p_db_status.add_argument("--dir", help=migrations_dir_help)
    p_db_status.add_argument("--root", help=root_help)
    p_db_status.set_defaults(func=cmd_db_status)

    p_db_backup = p_db_sub.add_parser(
        "backup", help="生成事务一致的快照并校验（VACUUM INTO）"
    )
    p_db_backup.add_argument("--path", help=db_path_help)
    p_db_backup.add_argument("--root", help=root_help)
    p_db_backup.add_argument(
        "--to",
        help="快照输出路径（默认 <data_root>/backup/pc-<UTC 时间戳>.sqlite3）；已存在则拒绝覆盖",
    )
    p_db_backup.set_defaults(func=cmd_db_backup)

    p_db_reindex = p_db_sub.add_parser("reindex", help="全量重建两个 FTS5 索引")
    p_db_reindex.add_argument("--path", help=db_path_help)
    p_db_reindex.add_argument("--root", help=root_help)
    p_db_reindex.add_argument(
        "--recompute-derived",
        action="store_true",
        help="重建索引之前先重算 text_search / search_extra（升级后或改过分词规则后跑一次）",
    )
    p_db_reindex.add_argument(
        "--batch-size", type=int, help="重算派生列时每批提交多少行（默认 500）"
    )
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
        choices=["export", "manual", "bot_api", "webhook", "rss", "user_api"],
        help="来源类型（export=本地文件/官方导出目录；manual=root 手工上传）",
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
        default=None,
        choices=["manual_export", "root_self", "owner_token", "admin_consent", "public_feed"],
        help="授权取得方式（留空按 kind 取默认：manual→root_self，其余→manual_export）",
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

    p_upload = sub.add_parser(
        "upload", help="root 手工上传：身份按内容，时间留空（重传不会变成两条）"
    )
    p_upload.add_argument("paths", nargs="+", help="文件或目录（目录会递归扫描）")
    p_upload.add_argument(
        "--source", help="来源名称或 source_id（留空用默认的「manual」来源，必要时自动建）"
    )
    p_upload.add_argument(
        "--note", help="来源备注（可选）：这条内容是从哪儿弄来的，写在 message.origin_note"
    )
    p_upload.add_argument(
        "--process",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="上传后立刻跑 worker 处理（默认开；用 --no-process 分开跑）",
    )
    p_upload.add_argument("--limit", type=int, help="最多扫描这么多条（调试用）")
    p_upload.add_argument("--dry-run", action="store_true", help="只报告会发现什么，不写任何行")
    p_upload.add_argument("--worker-id", help="处理阶段的 worker 标识")
    p_upload.add_argument("--root", help="仓库根目录（默认自动查找，用于读 config/app.yaml）")
    p_upload.add_argument("--path", help=db_path_help)
    p_upload.set_defaults(func=cmd_upload)

    p_work = sub.add_parser("work", help="跑 worker：取字节、扫描、去重、落库")
    p_work.add_argument("--once", action="store_true", help="只处理一条")
    p_work.add_argument("--max", type=int, help="最多处理这么多条")
    p_work.add_argument("--loop", action="store_true", help="常驻轮询（Ctrl+C 退出）")
    p_work.add_argument("--interval", type=int, default=5, help="--loop 时的轮询间隔秒数（默认 5）")
    p_work.add_argument("--worker-id", help="worker 标识（默认 pid@主机名）")
    p_work.add_argument("--root", help="仓库根目录（默认自动查找，用于读 config/app.yaml）")
    p_work.add_argument("--path", help=db_path_help)
    p_work.set_defaults(func=cmd_work)

    p_search = sub.add_parser("search", help="按关键词与过滤器检索内容")
    p_search.add_argument("terms", nargs="*", help="关键词（多个词是 AND；留空则只按过滤器列出）")
    p_search.add_argument("--source", help="只看某个来源（按名字）")
    p_search.add_argument(
        "--type",
        action="append",
        choices=list(domain.TYPE_FILTER_CHOICES),
        help="只看某种类型（可重复）",
    )
    p_search.add_argument(
        "--status",
        choices=["active", "quarantined", "archived", "deleted"],
        help="只看某个状态（默认 active）",
    )
    p_search.add_argument(
        "--tag",
        action="append",
        help="按标签过滤，多个标签是 AND（写法：名字 或 命名空间:名字）",
    )
    p_search.add_argument("--since", help="只看这个时间之后的（按 --time-field 选定的时间字段）")
    p_search.add_argument("--until", help="只看这个时间之前的（按 --time-field 选定的时间字段）")
    p_search.add_argument(
        "--time-field",
        choices=list(TIME_FIELDS),
        default=TIME_FIELD_INGEST,
        help="时间看哪个钟：ingest=入库时间（默认），sent=收到消息的时间",
    )
    p_search.add_argument(
        "--nsfw-level",
        type=int,
        default=0,
        help="允许的最高 NSFW 档位（默认 0=safe；2 及以上要显式给）",
    )
    p_search.add_argument(
        "--include-duplicates", action="store_true", help="不折叠同一段文本的重复项"
    )
    p_search.add_argument("--sort", choices=["relevance", "time"], help="排序（默认：有词按相关度，无词按时间）")
    p_search.add_argument("--limit", type=int, help="每页条数（默认取设置项，硬上限 50）")
    p_search.add_argument("--page", type=int, default=1, help="相关度排序下的页码（默认 1）")
    p_search.add_argument("--cursor", help="时间序翻页游标，取自上一次 --json 的 next_cursor")
    p_search.add_argument("--timings", action="store_true", help="打印各段耗时（排错用）")
    p_search.add_argument("--json", action="store_true", help="输出 JSON（含命中理由与游标）")
    p_search.add_argument("--root", help="仓库根目录（默认自动查找，用于读配置）")
    p_search.add_argument("--path", help=db_path_help)
    p_search.set_defaults(func=cmd_search)

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

    for operation in ("show", "open", "reveal"):
        p_view = sub.add_parser(operation, help="展示内容正文、打开查看文件或定位文件")
        p_view.add_argument("--path", help="数据库路径")
        p_view.add_argument("--root", help="项目根目录")
        target = p_view.add_mutually_exclusive_group(required=True)
        target.add_argument("--message", type=int, help="消息 ID，展示全部可见部件")
        target.add_argument("--content", type=int, help="内容项 ID")
        p_view.add_argument("--copy", action="store_true", help="同时复制纯文本或图片文件")
        if operation == "show":
            p_view.add_argument("--json", action="store_true", help="完整详情 JSON")
        p_view.set_defaults(func=cmd_view)

    p_render = sub.add_parser("render", help="从标准输入渲染 JSON 查询结果")
    p_render_sub = p_render.add_subparsers(dest="render_command", required=True)
    p_render_sub.add_parser("table", help="输出终端表格").set_defaults(func=cmd_render)

    p_tag = sub.add_parser("tag", help="root 标签管理：人工标注、别名与建议确认")
    p_tag_sub = p_tag.add_subparsers(dest="tag_command", required=True)
    for operation in ("add", "remove", "show", "list", "confirm", "reject"):
        p = p_tag_sub.add_parser(operation)
        p.add_argument("--path", help=db_path_help)
        p.add_argument("--root", help=root_help)
        p.add_argument("--json", action="store_true", help="输出完整 JSON")
        if operation in {"add", "remove", "show"}:
            targets = p.add_mutually_exclusive_group(required=True)
            targets.add_argument("--message", type=int, action="append", help="整帖 ID，可重复")
            targets.add_argument("--content", type=int, action="append", help="部件 ID，可重复")
        if operation in {"add", "remove"}:
            p.add_argument("--tag", action="append", required=True, help="自定义名称或别名，可重复")
        if operation == "add":
            from pc.services.tagging import MANUAL_NAMESPACES

            p.add_argument("--namespace", default="general", choices=MANUAL_NAMESPACES)
        if operation in {"confirm", "reject"}:
            p.add_argument("ids", type=int, nargs="+", help="建议关联 ID")
        p.set_defaults(func=cmd_tag)
    p_alias = p_tag_sub.add_parser("alias", help="标签别名")
    p_alias_sub = p_alias.add_subparsers(dest="alias_command", required=True)
    for operation in ("add", "remove", "list"):
        p = p_alias_sub.add_parser(operation)
        p.add_argument("--path", help=db_path_help)
        p.add_argument("--root", help=root_help)
        p.add_argument("--json", action="store_true")
        if operation != "remove":
            p.add_argument("--tag", required=True, help="规范名或已有别名")
        if operation != "list":
            p.add_argument("--alias", required=True)
        p.set_defaults(func=cmd_tag)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
