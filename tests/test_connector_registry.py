"""连接器工厂：kind 分派、roots 解析、以及"不支持时要说清支持什么"。

用户真正会遇到的错误只有几种：kind 写错、目录没配、凭证还没实现。这三种都必须
给出**可操作**的提示（支持哪些、规划中哪些、怎么补目录），而不是一句 "unsupported"。
这也是"给适配器留够接口"这件事唯一能被测试的部分。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pc.adapters.local_files import LocalFilesConnector
from pc.adapters.registry import (
    PLANNED_KINDS,
    SUPPORTED_KINDS,
    SourceConfigError,
    UnsupportedSourceKindError,
    connector_for,
    source_config,
)


def _source(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_id": 1,
        "name": "samples",
        "platform": "local",
        "kind": "export",
        "config": "{}",
        "enabled": 1,
        "deleted_at": None,
    }
    base.update(overrides)
    return base


class TestSourceConfig:
    def test_empty_config_is_an_empty_mapping(self) -> None:
        assert source_config(_source(config="")) == {}
        assert source_config(_source(config=None)) == {}
        assert source_config(_source(config="  ")) == {}

    def test_json_object_is_parsed(self) -> None:
        assert source_config(_source(config='{"roots": ["D:/dump"]}')) == {"roots": ["D:/dump"]}

    def test_already_decoded_mapping_is_accepted(self) -> None:
        assert source_config(_source(config={"roots": ["D:/dump"]})) == {"roots": ["D:/dump"]}

    def test_invalid_json_is_a_human_error(self) -> None:
        with pytest.raises(SourceConfigError, match="不是合法 JSON"):
            source_config(_source(config="{not json"))

    def test_json_array_is_rejected(self) -> None:
        with pytest.raises(SourceConfigError, match="必须是 JSON 对象"):
            source_config(_source(config="[1, 2]"))


class TestConnectorFor:
    def test_export_kind_builds_the_local_connector(self, tmp_path: Path) -> None:
        connector = connector_for(_source(), roots=[tmp_path])
        assert isinstance(connector, LocalFilesConnector)

    def test_roots_come_from_the_source_config(self, tmp_path: Path) -> None:
        source = _source(config=f'{{"roots": ["{tmp_path.as_posix()}"]}}')
        connector = connector_for(source)
        assert isinstance(connector, LocalFilesConnector)
        assert str(tmp_path) in connector.capabilities()["roots"][0]

    def test_explicit_roots_win_over_config(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        configured = tmp_path / "configured"
        configured.mkdir()
        source = _source(config=f'{{"roots": ["{configured.as_posix()}"]}}')
        connector = connector_for(source, roots=[explicit])
        assert connector.capabilities()["roots"] == [str(explicit)]

    def test_single_root_as_a_string_is_tolerated(self, tmp_path: Path) -> None:
        source = _source(config=f'{{"roots": "{tmp_path.as_posix()}"}}')
        assert isinstance(connector_for(source), LocalFilesConnector)

    def test_missing_roots_is_an_actionable_error(self) -> None:
        with pytest.raises(SourceConfigError) as excinfo:
            connector_for(_source())
        message = str(excinfo.value)
        assert "roots" in message and "pc import" in message

    def test_limits_are_passed_through(self, tmp_path: Path) -> None:
        connector = connector_for(_source(), roots=[tmp_path], max_media_bytes=1234)
        assert isinstance(connector, LocalFilesConnector)
        assert connector.capabilities()["max_attachment_bytes"] == 1234


class TestUnsupportedKinds:
    @pytest.mark.parametrize("kind", ["bot_api", "webhook", "rss", "user_api"])
    def test_planned_kinds_name_the_plan(self, kind: str) -> None:
        with pytest.raises(UnsupportedSourceKindError) as excinfo:
            connector_for(_source(kind=kind), roots=["."])
        message = str(excinfo.value)
        assert "暂不支持" in message
        assert "设计上支持但尚未实现" in message, "报错必须说清'规划中'与'不存在'的区别"
        assert kind in message

    def test_nonsense_kind_is_called_out(self) -> None:
        with pytest.raises(UnsupportedSourceKindError, match="不是合法的 kind 取值"):
            connector_for(_source(kind="telepathy"), roots=["."])

    def test_the_two_kind_tables_are_disjoint(self) -> None:
        assert not (set(SUPPORTED_KINDS) & set(PLANNED_KINDS))

    def test_error_lists_what_is_supported(self) -> None:
        with pytest.raises(UnsupportedSourceKindError) as excinfo:
            connector_for(_source(kind="bot_api"), roots=["."])
        assert "已实现" in str(excinfo.value)
