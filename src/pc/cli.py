"""命令行入口（``pc``）。

设计目标（DESIGN §6.2.3、§6.2.4）：让不懂技术的人**不需要碰 docker 命令、不需要读
堆栈**就能知道「现在是什么状态、下一步该做什么」；同时让开发者能回答
「这个值为什么是这样」。

本模块**只依赖标准库**：在还没执行 ``uv sync`` 的环境里，``pc doctor``
也应当能跑起来并告诉你缺了什么。

退出码约定：
    0  成功
    2  环境问题（缺依赖、缺配置文件）—— 提示里会给出可操作的动作
    3  功能尚未实现（骨架阶段已知的未完成项）
    1  argparse 用法错误（由 argparse 自行返回）
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

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
    "psycopg": "PostgreSQL 访问（实现阶段）",
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
    """显示设置项的生效值与来源（``--explain`` 时附带含义与影响的解释）。"""
    from pc import config as config_mod

    try:
        specs = config_mod.load_registry()
    except config_mod.SettingsRegistryError as exc:
        print(f"无法读取设置项注册表：{exc}", file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print(f"该功能尚未实现（骨架阶段）：{exc}", file=sys.stderr)
        print("实现指引：DESIGN §6.2.1（注册表）与 §6.2.4（来源可解释性）。", file=sys.stderr)
        return 3

    for spec in specs:
        print(f"{spec.key} = {spec.default!r}  (来源: default)")
        if args.explain:
            print(f"    {spec.label}")
            print(f"    说明：{spec.help}")
            print(f"    影响：{spec.impact}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pc",
        description="post-collector 命令行（骨架阶段，仅 doctor 与 config show 已接线）",
    )
    parser.add_argument("--version", action="version", version=f"post-collector {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="体检：报告环境、文件、依赖与运行时目录")
    p_doctor.add_argument("--root", help="指定仓库根目录（默认自动查找）")
    p_doctor.set_defaults(func=cmd_doctor)

    p_config = sub.add_parser("config", help="配置相关操作")
    p_config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_show = p_config_sub.add_parser("show", help="显示设置项及其来源")
    p_show.add_argument("--explain", action="store_true", help="附带含义与影响的解释")
    p_show.set_defaults(func=cmd_config_show)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
