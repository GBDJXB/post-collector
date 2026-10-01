"""基准脚本自身的守卫：``tools/bench_search.py`` 要能跑通并在小规模上达标。

为什么要测一个工具脚本：它的结论是**性能红线的判据**（DESIGN §5.6），而它现在驱动的是
真实检索实现（`run_search`）。脚本一旦腐坏——比如改了函数签名、场景引用了不存在的字段——
它可能"跑出一个漂亮的数字"却什么都没测到。这里用 300 条合成数据跑一遍全部场景，
断言退出码为 0，保证脚本与实现始终对得上。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

BENCH_PATH = Path(__file__).resolve().parents[1] / "tools" / "bench_search.py"


def _load_bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pc_bench_search", BENCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    return _load_bench()


def test_small_scale_run_passes(bench: ModuleType) -> None:
    """小规模全场景跑一遍：既验证脚本能跑，也验证真实检索路径没退化到超预算。"""
    assert bench.main(["--scale", "300", "--runs", "3", "--assert"]) == 0


def test_scenarios_all_go_through_the_real_search_layer(bench: ModuleType) -> None:
    """场景函数必须调用真实实现，而不是自己写 SQL。

    判据很直接：脚本里不该再出现 ``content_fts`` / ``content_word`` 的 MATCH 语句——
    基准若走旁路，就可能"基准达标而线上很慢"。
    """
    source = BENCH_PATH.read_text(encoding="utf-8")
    assert "run_search(" in source
    assert "MATCH ?" not in source, "基准脚本不该自己构造 FTS5 查询"


def test_synthetic_data_uses_the_production_tokenizer(bench: ModuleType) -> None:
    """合成数据的派生列必须由生产函数算出，否则词级索引里躺的不是真实分词结果。"""
    source = BENCH_PATH.read_text(encoding="utf-8")
    assert "search_tokens(" in source
    assert "search_extra_value(" in source


def test_synthetic_data_carries_names_and_message_times(
    bench: ModuleType, tmp_path: Path
) -> None:
    """两个检索维度都要在合成数据里真实存在，否则新场景量的是一条退化路径。

    没有 ``message.sent_at`` 时"按消息时间排序"量的是全 NULL；没有 ``original_name``
    时文件名检索恒为空。两者都会让基准看起来很漂亮却什么都没测到。
    """
    import sqlite3

    path = tmp_path / "bench-seed.sqlite3"
    bench.build_database(path, 300)
    conn = sqlite3.connect(path)
    rows = {
        "messages": conn.execute("SELECT count(*) FROM message").fetchone()[0],
        "with_sent_at": conn.execute(
            "SELECT count(*) FROM message WHERE sent_at IS NOT NULL"
        ).fetchone()[0],
        "named": conn.execute(
            "SELECT count(*) FROM content_item WHERE original_name IS NOT NULL"
        ).fetchone()[0],
        "names_in_index": conn.execute(
            "SELECT count(*) FROM content_item WHERE search_extra LIKE '%.jpg%'"
        ).fetchone()[0],
    }
    conn.close()
    assert rows["with_sent_at"] > 0, "合成数据里必须有消息时间"
    assert rows["with_sent_at"] < rows["messages"], "也要留一部分空值（手工上传那种）"
    assert rows["named"] == rows["names_in_index"] == rows["messages"] > 0
