"""运行时目录的解析（``resolve_paths``）。

这里钉的核心是一条容易被写错、写错之后**很难发现**的规则：**媒体目录跟着数据根
目录走，不跟着数据库文件走**。``PC__DB__PATH`` 只挪数据库文件本身；如果它顺带把
媒体也挪走，那么「把库放到 SSD 上」这个动作就会静默改变对象存储的落点——对象键
虽然不变，老内容照样找不到。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pc.adapters import sqlite as db


def _write_env(path: Path, **values: str) -> Path:
    env_file = path / ".env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
    )
    return env_file


def _write_app_config(path: Path, body: str) -> Path:
    app_config = path / "app.yaml"
    app_config.write_text(body, encoding="utf-8")
    return app_config


def _resolve(tmp_path: Path, *, env: dict[str, str] | None = None, **kwargs: Any) -> Any:
    return db.resolve_paths(
        env=env or {},
        env_file=tmp_path / ".env",
        app_config=tmp_path / "app.yaml",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 默认与来源
# ---------------------------------------------------------------------------


def test_falls_back_to_platform_default_root(tmp_path: Path) -> None:
    resolved = _resolve(tmp_path)
    assert resolved.data_root == db.default_data_root(env={})
    assert resolved.root_source == "default"
    for name, suffix in (
        ("media", "media"),
        ("thumb", "thumbs"),
        ("cold", "cold"),
        ("quarantine", "quarantine"),
        ("backup", "backup"),
        ("models", "models"),
    ):
        assert resolved.paths[name] == resolved.data_root / suffix
        assert resolved.sources[name].startswith("default + /")


def test_attribute_shortcuts(tmp_path: Path) -> None:
    resolved = _resolve(tmp_path)
    assert resolved.media_root == resolved.paths["media"]
    assert resolved.thumb_root == resolved.paths["thumb"]
    # 未定义的以 _root 结尾的名字仍然按属性错误处理，而不是静默返回 None
    assert not hasattr(resolved, "nope_root")
    with pytest.raises(AttributeError):
        _ = resolved.nope_root


def test_explain_is_human_readable(tmp_path: Path) -> None:
    text = _resolve(tmp_path).explain()
    assert "数据根目录" in text
    assert "media_root" in text


# ---------------------------------------------------------------------------
# 数据根目录的优先级：进程环境 > .env > app.yaml > 平台默认
# ---------------------------------------------------------------------------


def test_process_env_wins_over_env_file_and_yaml(tmp_path: Path) -> None:
    _write_env(tmp_path, PC_DATA_ROOT="/from-env-file")
    _write_app_config(tmp_path, "schema_version: 1\ndb:\n  data_root: /from-yaml\n")
    resolved = _resolve(tmp_path, env={"PC_DATA_ROOT": "/from-process-env"})
    assert resolved.data_root == Path("/from-process-env")
    assert resolved.root_source == "env:PC_DATA_ROOT"


def test_env_file_wins_over_yaml(tmp_path: Path) -> None:
    _write_env(tmp_path, PC_DATA_ROOT="/from-env-file")
    _write_app_config(tmp_path, "schema_version: 1\ndb:\n  data_root: /from-yaml\n")
    resolved = _resolve(tmp_path)
    assert resolved.data_root == Path("/from-env-file")
    assert resolved.root_source == "env-file:PC_DATA_ROOT"


def test_yaml_used_when_nothing_else_is_set(tmp_path: Path) -> None:
    app_config = _write_app_config(tmp_path, "schema_version: 1\ndb:\n  data_root: /from-yaml\n")
    resolved = _resolve(tmp_path)
    assert resolved.data_root == Path("/from-yaml")
    assert resolved.root_source.endswith("app.yaml:data_root")
    assert app_config.exists()


# ---------------------------------------------------------------------------
# 单项目录的覆盖
# ---------------------------------------------------------------------------


def test_single_dir_override_from_process_env(tmp_path: Path) -> None:
    resolved = _resolve(tmp_path, env={"PC__PATHS__MEDIA_ROOT": "/mnt/big/media"})
    assert resolved.media_root == Path("/mnt/big/media")
    assert resolved.sources["media"] == "env:PC__PATHS__MEDIA_ROOT"
    # 只覆盖一项时，其余仍从数据根目录派生
    assert resolved.paths["backup"] == resolved.data_root / "backup"


def test_single_dir_override_from_yaml(tmp_path: Path) -> None:
    _write_app_config(
        tmp_path,
        "schema_version: 1\npaths:\n  backup_root: /mnt/other/backups\n",
    )
    resolved = _resolve(tmp_path)
    assert resolved.backup_root == Path("/mnt/other/backups")
    assert resolved.sources["backup"].endswith("app.yaml:backup_root")


def test_env_file_dir_override(tmp_path: Path) -> None:
    _write_env(tmp_path, PC__PATHS__COLD_ROOT="/mnt/nas/cold")
    resolved = _resolve(tmp_path)
    assert resolved.cold_root == Path("/mnt/nas/cold")
    assert resolved.sources["cold"] == "env-file:PC__PATHS__COLD_ROOT"


# ---------------------------------------------------------------------------
# 与数据库文件路径的关系（本条是这块最容易写错的地方）
# ---------------------------------------------------------------------------


def test_db_path_override_does_not_move_media(tmp_path: Path) -> None:
    """``PC__DB__PATH`` 只挪数据库文件：媒体仍留在数据根目录下。"""
    env = {
        "PC_DATA_ROOT": str(tmp_path / "root"),
        "PC__DB__PATH": str(tmp_path / "ssd" / "pc.sqlite3"),
    }
    paths = db.resolve_paths(env=env, env_file=tmp_path / "none", app_config=tmp_path / "none")
    database = db.resolve_db_path(env=env, env_file=tmp_path / "none", app_config=tmp_path / "none")

    assert database.path == tmp_path / "ssd" / "pc.sqlite3"
    assert paths.media_root == tmp_path / "root" / "media", (
        "把库放到别处不该顺带挪走媒体本体"
    )


def test_root_and_db_path_agree_on_the_data_root(tmp_path: Path) -> None:
    """只设置数据根目录时，库文件与运行时目录必须落在同一个根下。"""
    env = {"PC_DATA_ROOT": str(tmp_path / "root")}
    paths = db.resolve_paths(env=env, env_file=tmp_path / "none", app_config=tmp_path / "none")
    database = db.resolve_db_path(env=env, env_file=tmp_path / "none", app_config=tmp_path / "none")
    assert paths.data_root == tmp_path / "root"
    assert database.path == tmp_path / "root" / db.DB_FILENAME


def test_blank_values_are_ignored(tmp_path: Path) -> None:
    """空值等于没写：``PC_DATA_ROOT=`` 不该解析成当前目录。"""
    _write_env(tmp_path, PC_DATA_ROOT="", PC__PATHS__MEDIA_ROOT="")
    resolved = _resolve(tmp_path)
    assert resolved.data_root == db.default_data_root(env={})
    assert resolved.root_source == "default"
