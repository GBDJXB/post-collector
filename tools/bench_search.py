"""检索性能基准：DESIGN §5.6 的预算与验收场景。

它回答一个具体问题：**在 10 万条内容上，「数据层收到已结构化的查询 → 返回前 50 条
组装好的结果」要多久**。预算口径见 DESIGN §5.6：P50 < 150 ms、P99 < 500 ms
（热缓存、单用户或 ≤4 并发读者；缩略图字节与 AI 解析不计入）。

**它驱动的是真实检索路径**：场景调用 ``pc.services.search.run_search`` /
``pc.adapters.fts_search.SqliteFtsSearch``，不再是脚本自己写的一份 SQL 副本。这一点很
重要——基准若走旁路，就可能"基准达标而线上很慢"，而 §5.6 的预算正是要约束线上那条路。
合成数据的派生列也由生产的同一套函数算出（``pc.domain.search_tokens`` /
``search_extra_value``），所以词级索引里躺的就是真实分词结果。

用法::

    uv run python tools/bench_search.py --scale 100000            # 只测并打印
    uv run python tools/bench_search.py --scale 100000 --assert   # 超预算则退出码 1
    uv run python tools/bench_search.py --scale 20000 --keep      # 保留合成库以便排查

与 DESIGN §5.6 验收清单的对应关系（清单共九项）：

* 场景（第 1~6 项）：纯过滤列表、1 字宽命中、2 字混排、2 字短词、关键词+标签+时间、
  三标签交集、深分页、受限内容过滤（两项各测一次）
* 冷启动首查（第 7 项，单独记录，作为 M4 是否值得做的判据）
* 后台写入同时读（第 8 项）与 4 个并发读者（第 9 项）

合成数据的性质（决定了结论的适用范围）：
  * 标签按 Zipf 分布，所以会出现「覆盖 20% 内容的通用标签」这种真实的长尾形态；
  * 文本是中日文混排的伪随机词，`text_search` 与 `search_extra` 由生产函数算出；
  * 媒体与内容项通过 `content_media` 多对多关联（与 §3.8 的引用计数口径一致）。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pc.adapters import sqlite as db
from pc.domain import ITEM_TYPE_LABELS, search_extra_value, search_tokens
from pc.services.search import SearchQuery, run_search

#: 伪随机词表：中文词、日文片假名、拉丁词混排
CJK_WORDS = (
    "蓝发 精灵 雨 银发 法师 夜 猫 耳 少女 骑士 机甲 樱花 雪 海 城市 黄昏 剑 魔法 图书馆 "
    "夏日 制服 眼镜 微笑 眼泪 星 空 风 花火 祭典 猫娘 天使 恶魔 王冠 披风 长枪 弓 阴影 "
)
KANA_WORDS = "ミク カガミネ リン レン サクラ ユキ ツキ ホシ カゼ ハナ"
LATIN_WORDS = "sunset portrait original fanart sketch commission oc digital watercolor"
TAG_NAMES = [
    "blue_hair",
    "silver_hair",
    "twintails",
    "long_hair",
    "short_hair",
    "cat_ears",
    "glasses",
    "school_uniform",
    "kimono",
    "armor",
    "sword",
    "magic",
    "rain",
    "night",
    "sunset",
    "city",
    "sky",
    "flower",
    "winter",
    "summer",
    "smile",
    "crying",
]
SOURCE_NAMES = ["telegram-channel-a", "telegram-channel-b", "rss-feed", "local-export"]

#: 预算（DESIGN §5.6）
BUDGET_P50_MS = 150.0
BUDGET_P99_MS = 500.0

#: 每页条数（与 search.default_page_size 的上限一致）
PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# 合成数据
# ---------------------------------------------------------------------------


def _zipf_weights(count: int, exponent: float = 1.07) -> list[float]:
    raw = [1.0 / ((rank + 1) ** exponent) for rank in range(count)]
    total = sum(raw)
    return [value / total for value in raw]


def _make_text(rng: random.Random, words: int) -> str:
    """生成一段中日文混排的伪随机文本（正文）。派生列由生产函数从它算出来。"""
    picked: list[str] = []
    for _ in range(words):
        roll = rng.random()
        if roll < 0.7:
            picked.append(rng.choice(CJK_WORDS.split()))
        elif roll < 0.9:
            picked.append(rng.choice(KANA_WORDS.split()))
        else:
            picked.append(rng.choice(LATIN_WORDS.split()))
    return "".join(picked) if rng.random() < 0.5 else " ".join(picked)


def build_database(path: Path, scale: int, *, batch: int = 5000) -> dict[str, Any]:
    """造一个合成库；返回耗时与体积统计。"""
    conn = db.connect(path)
    started = time.perf_counter()
    db.apply_migration(conn)
    migrated = time.perf_counter()

    tag_ids: list[int] = []
    for index, name in enumerate(TAG_NAMES, start=1):
        conn.execute(
            "INSERT INTO tag (tag_id, uuid, name, name_norm, namespace, usage_count) "
            "VALUES (?, ?, ?, ?, 'general', 0)",
            (index, f"tag-{index}", name, name),
        )
        tag_ids.append(index)
    for index, name in enumerate(SOURCE_NAMES, start=1):
        conn.execute(
            "INSERT INTO source (source_id, uuid, name, platform, kind) "
            "VALUES (?, ?, ?, 'telegram', 'export')",
            (index, f"src-{index}", name),
        )

    weights = _zipf_weights(len(tag_ids))
    rng = random.Random(20260929)
    inserted_tags = 0
    tag_usage: dict[int, int] = {}

    for start in range(0, scale, batch):
        stop = min(start + batch, scale)
        conn.execute("BEGIN IMMEDIATE")
        for content_id in range(start + 1, stop + 1):
            message_id = content_id
            text = _make_text(rng, rng.randint(6, 18))
            ocr_text = text if rng.random() < 0.3 else None
            caption = f"caption {rng.choice(LATIN_WORDS.split())}" if rng.random() < 0.3 else None
            # 每 20 条造一个"热门标签"覆盖：让同一个标签下的内容量出现长尾
            hot = (content_id % 5) == 0
            picks = [tag_ids[0] if hot else None]
            for _ in range(rng.randint(3, 6)):
                roll = rng.random()
                acc = 0.0
                chosen = tag_ids[-1]
                for tag_id, weight in zip(tag_ids, weights, strict=True):
                    acc += weight
                    if roll <= acc:
                        chosen = tag_id
                        break
                picks.append(chosen)
            source_name = SOURCE_NAMES[rng.randint(0, len(SOURCE_NAMES) - 1)]
            # 文件名：只有媒体项才有（这一批全是 image），照生产规则只留 basename
            file_name = f"素材-{content_id:06d}.jpg"
            # 消息时间：按内容 id 递减，模拟"越新的内容消息时间越晚"；每 11 条留一条空值
            # （对应手工上传那种没有消息时间的内容），让"空值排最后/被时间范围排除"这两条
            # 也能在真实规模上被量到，而不是只在小测试里成立。
            sent_at = (
                None
                if content_id % 11 == 0
                else f"2026-{(content_id % 12) + 1:02d}-{(content_id % 28) + 1:02d}"
                f"T{content_id % 24:02d}:00:00.000Z"
            )
            # 派生列按生产的同一套规则算：分词结果 + 检索补充字段
            # （标签名 + 来源名 + 类型 + 文件名）
            extra = search_extra_value(
                " ".join(TAG_NAMES[tag_id - 1] for tag_id in picks if tag_id),
                source_name,
                "image",
                ITEM_TYPE_LABELS["image"],
                file_name,
            )
            tokens = search_tokens(text, ocr_text, caption, extra)
            conn.execute(
                "INSERT INTO message (message_id, uuid, source_id, platform_msg_id, sent_at,"
                " media_refs) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    f"msg-{message_id}",
                    rng.randint(1, len(SOURCE_NAMES)),
                    f"pm-{message_id}",
                    sent_at,
                    json.dumps([f"D:/素材/{file_name}"]),
                ),
            )
            conn.execute(
                "INSERT INTO content_item "
                "(content_id, uuid, message_id, item_type, ordinal, text_content, ocr_text, "
                " caption, text_search, search_extra, original_name, sent_at, nsfw_level,"
                " status, first_seen_at) "
                "VALUES (?, ?, ?, 'image', 0, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    content_id,
                    f"ci-{content_id}",
                    message_id,
                    text,
                    ocr_text,
                    caption,
                    tokens,
                    extra,
                    file_name,
                    # 消息时间的派生副本（0006）：真实落库路径两处都写，基准也一样
                    sent_at,
                    2 if content_id % 97 == 0 else 0,
                    f"2026-01-01T00:00:{content_id % 60:02d}.000Z",
                ),
            )
            conn.execute(
                "INSERT INTO media (media_id, uuid, sha256, size_bytes, object_key, thumb_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    content_id,
                    f"md-{content_id}",
                    f"{content_id:064x}",
                    rng.randint(50_000, 3_000_000),
                    f"{content_id % 256:02x}/{(content_id // 256) % 256:02x}/{content_id:064x}",
                    f"thumbs/{content_id % 256:02x}/{content_id}.webp",
                ),
            )
            conn.execute(
                "INSERT INTO content_media (content_id, media_id, role, ordinal) "
                "VALUES (?, ?, 'primary', 0)",
                (content_id, content_id),
            )
            for tag_id in dict.fromkeys(tag for tag in picks if tag):
                conn.execute(
                    "INSERT INTO tag_assignment (content_id, tag_id, source) "
                    "VALUES (?, ?, 'human')",
                    (content_id, tag_id),
                )
                inserted_tags += 1
                tag_usage[tag_id] = tag_usage.get(tag_id, 0) + 1
        conn.execute("COMMIT")

    # usage_count 是"标签维度的总数"的来源（§5.6 M1b 的查询路径要用它）。
    # 生产里由打标切片维护；合成数据必须自己维护，否则那条路径永远拿不到值。
    for tag_id, count in tag_usage.items():
        conn.execute("UPDATE tag SET usage_count = ? WHERE tag_id = ?", (count, tag_id))

    db.sync_post_rows(conn)
    finished = time.perf_counter()
    # 造完数据先把 WAL 合并回主库：不这么做，首轮读取要跨着一个几百 MB 的 WAL 找页，
    # 量出来的就不是"热缓存的查询耗时"而是"刚写完十万行的 WAL 有多厚"。
    # 预算的口径是热缓存（§5.6），这里把库收拾成稳定状态再开测。
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = path.stat().st_size
    counts = {
        "content_item": conn.execute("SELECT count(*) FROM content_item").fetchone()[0],
        "tag_assignment": conn.execute("SELECT count(*) FROM tag_assignment").fetchone()[0],
        "content_media": conn.execute("SELECT count(*) FROM content_media").fetchone()[0],
    }
    conn.close()
    return {
        "migration_seconds": migrated - started,
        "insert_seconds": finished - migrated,
        "size_bytes": size,
        "counts": counts,
        "tag_rows": inserted_tags,
    }


# ---------------------------------------------------------------------------
# 验收场景（全部走真实检索实现，DESIGN §5.6 清单第 1~6 项）
# ---------------------------------------------------------------------------


def _hits(conn: sqlite3.Connection, query: SearchQuery) -> int:
    """跑一次真实检索，返回这一页取回多少条。"""
    return len(run_search(conn, query)["hits"])


def _query_plain_list(conn: sqlite3.Connection) -> int:
    """纯过滤列表：B 树索引 + 键集分页（M1 的第一条路径）。"""
    return _hits(conn, SearchQuery(limit=PAGE_SIZE))


def _query_keyword_broad(conn: sqlite3.Connection) -> int:
    """1 字宽命中：trigram 命中 0，必须走词级索引。"""
    return _hits(conn, SearchQuery(q="雨", limit=PAGE_SIZE))


def _query_cjk_mixed(conn: sqlite3.Connection) -> int:
    """2 字中文：中文最常见的查询形态。"""
    return _hits(conn, SearchQuery(q="蓝发", limit=PAGE_SIZE))


def _query_short_term(conn: sqlite3.Connection) -> int:
    """另一个 2 字词，且它在词表里是完整词（对照上一条）。"""
    return _hits(conn, SearchQuery(q="精灵", limit=PAGE_SIZE))


def _query_keyword_tag_time(conn: sqlite3.Connection) -> int:
    """首选交互路径：关键词 + 标签 + 时间范围（在候选集内过滤）。"""
    return _hits(
        conn,
        SearchQuery(
            q="雨",
            tags=("blue_hair", "rain"),
            since="2026-01-01T00:00:00.000Z",
            limit=PAGE_SIZE,
        ),
    )


def _query_three_tag_intersection(conn: sqlite3.Connection) -> int:
    """三标签交集：按集合大小升序取最小的那个当候选集，其余用 EXISTS 过滤。"""
    return _hits(conn, SearchQuery(tags=("rain", "night", "sky"), limit=PAGE_SIZE))


def _query_deep_paging(conn: sqlite3.Connection) -> int:
    """深分页：第 20 页的取数耗时（§5.6 M1 的键集分页，不是 OFFSET）。

    走到第 20 页的游标只算一次（第一次调用时算好并缓存）：预算约束的是"取第 20 页"
    这件事本身，而不是"从第一页翻到第二十页"——键集分页的关键性质正是**第 20 页与
    第 1 页同价**，把 20 次取数都算进一次采样反而看不清这条性质。
    """
    global _PAGE_20_CURSOR
    if _PAGE_20_CURSOR is None:
        cursor: str | None = None
        for _ in range(19):
            cursor = run_search(conn, SearchQuery(limit=PAGE_SIZE, cursor=cursor))["next_cursor"]
            if cursor is None:
                break
        _PAGE_20_CURSOR = cursor or ""
    cursor = _PAGE_20_CURSOR or None
    return len(run_search(conn, SearchQuery(limit=PAGE_SIZE, cursor=cursor))["hits"])


#: 第 20 页的键集游标（首次调用时算好，见 :func:`_query_deep_paging`）
_PAGE_20_CURSOR: str | None = None


def _query_nsfw_filter(conn: sqlite3.Connection) -> int:
    """受限内容过滤：允许到 questionable（默认只允许 safe 那一档）。"""
    return _hits(conn, SearchQuery(nsfw_max_level=1, limit=PAGE_SIZE))


def _query_sent_time_page(conn: sqlite3.Connection) -> int:
    """按「收到消息时间」过滤 + 排序，并翻到第 20 页（§5.6 的第二个钟）。

    这条路径与入库时间那条**不是同一个查询形状**：它由 message 驱动、沿
    ``message_sent_idx`` 走，否则十万行一次排序。时间范围取一段能命中约十分之一的区间，
    排序与游标都按消息时间——这正是 `--time-field sent` 的真实形态。
    """
    global _SENT_20_CURSOR
    query = SearchQuery(
        since="2026-06-01T00:00:00.000Z",
        time_field="sent",
        sort="time",
        limit=PAGE_SIZE,
    )
    if _SENT_20_CURSOR is None:
        cursor: str | None = None
        for _ in range(19):
            cursor = run_search(conn, replace(query, cursor=cursor))["next_cursor"]
            if cursor is None:
                break
        _SENT_20_CURSOR = cursor or ""
    return _hits(conn, replace(query, cursor=_SENT_20_CURSOR or None))


#: 按消息时间翻到第 20 页的游标（首次调用时算好，理由同 :func:`_query_deep_paging`）
_SENT_20_CURSOR: str | None = None


SCENARIOS: tuple[tuple[str, Callable[[sqlite3.Connection], int]], ...] = (
    ("纯过滤列表（时间倒序 50 条）", _query_plain_list),
    ("纯关键词宽命中（1 字，走词级索引）", _query_keyword_broad),
    ("中日文混排查询（2 字）", _query_cjk_mixed),
    ("短词（2 字，trigram 用不上）", _query_short_term),
    ("关键词 + 标签 + 时间", _query_keyword_tag_time),
    ("三标签交集", _query_three_tag_intersection),
    ("深分页（连续 20 页，keyset）", _query_deep_paging),
    ("受限内容过滤（nsfw_level）", _query_nsfw_filter),
    ("按消息时间过滤并翻到第 20 页", _query_sent_time_page),
)


# ---------------------------------------------------------------------------
# 度量
# ---------------------------------------------------------------------------


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return float("nan")
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[index]


def measure(
    conn: sqlite3.Connection, func: Callable[[sqlite3.Connection], int], runs: int
) -> dict[str, Any]:
    samples: list[float] = []
    rows = 0
    for _ in range(runs):
        started = time.perf_counter()
        rows = func(conn)
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "p50": statistics.median(samples),
        "p95": _percentile(samples, 95),
        "p99": _percentile(samples, 99),
        "max": max(samples),
        "rows": rows,
    }


def _breakdown(conn: sqlite3.Connection, name: str) -> str:
    """跑一次某个场景，把 ``run_search`` 报的各段耗时拼成一行（M6 的耗时分解）。"""
    queries = {
        "纯关键词宽命中（1 字，走词级索引）": SearchQuery(q="雨", limit=PAGE_SIZE),
        "中日文混排查询（2 字）": SearchQuery(q="蓝发", limit=PAGE_SIZE),
    }
    query = queries.get(name)
    if query is None:
        return "（无）"
    result = run_search(conn, query)
    timings = result["timings"]
    return "  ".join(
        f"{stage}={timings[stage]:.1f}ms"
        for stage in ("candidates", "filter", "main", "tags", "media", "total")
        if stage in timings
    )


def measure_cold(path: Path, func: Callable[[sqlite3.Connection], int]) -> float:
    """冷启动首查：新开连接后的第一次查询（对照 M4 预热是否值得做）。"""
    conn = db.connect(path, expect_exists=True)
    try:
        started = time.perf_counter()
        func(conn)
        return (time.perf_counter() - started) * 1000
    finally:
        conn.close()


def measure_concurrent(
    path: Path, func: Callable[[sqlite3.Connection], int], readers: int
) -> dict[str, Any]:
    """4 个并发读者各自持一条连接，重复查询。"""
    samples: list[float] = []
    lock = threading.Lock()
    errors: list[str] = []

    def worker() -> None:
        conn = db.connect(path, expect_exists=True)
        try:
            local: list[float] = []
            for _ in range(40):
                started = time.perf_counter()
                func(conn)
                local.append((time.perf_counter() - started) * 1000)
            with lock:
                samples.extend(local)
        except Exception as exc:
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        return {"error": errors[0]}
    return {
        "p50": statistics.median(samples),
        "p99": _percentile(samples, 99),
        "samples": len(samples),
    }


def measure_under_write(path: Path, func: Callable[[sqlite3.Connection], int]) -> dict[str, Any]:
    """后台写入同时发生：验证 WAL 下读不被写阻塞（§5.6 场景 8）。"""
    stop = threading.Event()
    writer_errors: list[str] = []
    created_messages: list[int] = []

    def writer() -> None:
        try:
            conn = db.connect(path, expect_exists=True)
            try:
                tag_id = int(conn.execute("SELECT max(tag_id) FROM tag").fetchone()[0] or 1)
                row = conn.execute("SELECT max(message_id) FROM message").fetchone()
                batch_index = int(row[0] or 0)
                while not stop.is_set():
                    # 每批用一个独立消息，避免撞上 content_item 的 UNIQUE(message_id, ordinal)
                    batch_index += 1
                    message_id = 10_000_000 + batch_index
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO message (message_id, uuid, source_id, platform_msg_id) "
                        "VALUES (?, ?, 1, ?)",
                        (message_id, f"wmsg-{message_id}", f"wpm-{message_id}"),
                    )
                    for offset in range(1, 51):
                        conn.execute(
                            "INSERT INTO content_item (uuid, message_id, item_type, ordinal, "
                            "text_search) VALUES (?, ?, 'image', ?, '雨 夜')",
                            (f"w-{message_id}-{offset}", message_id, offset),
                        )
                        conn.execute(
                            "INSERT INTO tag_assignment (content_id, tag_id, source) "
                            "SELECT content_id, ?, 'human' FROM content_item "
                            "WHERE message_id = ? AND ordinal = ?",
                            (tag_id, message_id, offset),
                        )
                    conn.execute("COMMIT")
                    created_messages.append(message_id)
            finally:
                conn.close()
        except Exception as exc:
            writer_errors.append(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.3)  # 让写事务先跑起来
    samples: list[float] = []
    reader = db.connect(path, expect_exists=True)
    try:
        for _ in range(60):
            started = time.perf_counter()
            func(reader)
            samples.append((time.perf_counter() - started) * 1000)
    finally:
        reader.close()
        stop.set()
        thread.join(timeout=5)
        if thread.is_alive():
            writer_errors.append("后台写者未按时停止")
        elif created_messages:
            # 只清理本次写者成功提交的消息，复测不能改变合成库规模。
            with db.open_db(path, expect_exists=True) as cleanup, db.transaction(cleanup):
                # canonical_id 自引用外键没有索引；临时覆盖避免删除时逐条扫描全库。
                cleanup.execute("CREATE INDEX _pc_bench_cleanup_idx ON content_item(canonical_id)")
                for start in range(0, len(created_messages), 500):
                    ids = created_messages[start : start + 500]
                    marks = ",".join("?" for _ in ids)
                    cleanup.execute(
                        "DELETE FROM tag_assignment WHERE content_id IN (SELECT content_id "
                        f"FROM content_item WHERE message_id IN ({marks}))",
                        ids,
                    )
                    cleanup.execute(f"DELETE FROM content_item WHERE message_id IN ({marks})", ids)
                    cleanup.execute(f"DELETE FROM message WHERE message_id IN ({marks})", ids)
                cleanup.execute("DROP INDEX _pc_bench_cleanup_idx")
    result = {
        "p50": statistics.median(samples),
        "p99": _percentile(samples, 99),
        "samples": len(samples),
    }
    if writer_errors:
        result["writer_error"] = writer_errors[0]
    return result


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索性能基准（DESIGN §5.6）")
    parser.add_argument("--scale", type=int, default=100_000, help="合成内容条数（默认 10 万）")
    parser.add_argument("--runs", type=int, default=200, help="每个场景的采样次数（默认 200）")
    parser.add_argument("--readers", type=int, default=4, help="并发读者数（默认 4）")
    parser.add_argument("--path", help="合成库路径（默认临时目录，跑完删除）")
    parser.add_argument("--reuse", action="store_true", help="复用 --path 指定的既有合成库，不重建")
    parser.add_argument("--keep", action="store_true", help="保留合成库以便排查")
    parser.add_argument("--assert", dest="check", action="store_true", help="超预算则退出码 1")
    args = parser.parse_args(argv)

    directory = Path(tempfile.mkdtemp(prefix="pc_bench_")) if not args.path else None
    path = Path(args.path) if args.path else directory / "pc_bench.sqlite3"  # type: ignore[operator]
    if args.reuse:
        if not args.path or not path.is_file():
            parser.error("--reuse 必须配合 --path 指定已存在的合成库")
        with db.open_db(path, expect_exists=True) as reused:
            counts = {
                name: reused.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("content_item", "tag_assignment", "content_media")
            }
            if counts["content_item"] != args.scale:
                parser.error("合成库内容数量与 --scale 不符，拒绝复用")
        stats = {"counts": counts}
        print(f"[1/4] 复用合成库：{args.scale:,} 条内容，{path}")
    else:
        if path.exists():
            path.unlink()
        print(f"[1/4] 造数据：{args.scale:,} 条内容 → {path}")
        stats = build_database(path, args.scale)
        print(
            f"      迁移 {stats['migration_seconds']:.2f}s，写入 {stats['insert_seconds']:.2f}s"
            f"（{args.scale / max(stats['insert_seconds'], 1e-6):,.0f} 条/秒），"
            f"库 {stats['size_bytes'] / 1024 / 1024:.1f} MB"
        )
    print(
        f"      行数：content_item={stats['counts']['content_item']:,} "
        f"tag_assignment={stats['counts']['tag_assignment']:,} "
        f"content_media={stats['counts']['content_media']:,}"
    )

    conn = db.connect(path, expect_exists=True)
    failures: list[str] = []
    try:
        print(f"\n[2/4] 验收场景（第 1~6 项，每类 {args.runs} 次采样，{PAGE_SIZE} 条/页）")
        print("      采样前先各跑一次预热（预算口径本来就是热缓存，见 §5.6）")
        print(f"      {'场景':<34} {'P50':>8} {'P95':>8} {'P99':>8} {'行数':>5}  判定")
        for name, func in SCENARIOS:
            func(conn)  # 预热：把懒加载的游标与页缓存填上，别算进采样
            result = measure(conn, func, args.runs)
            # 两个口径都要判：P99 管尾部，P50 管"随手一查"的体感。只判 P99 会漏掉
            # "每次都慢但慢得很稳定"的形状——那正是实际使用里最难受的一种。
            over = []
            if result["p50"] >= BUDGET_P50_MS:
                over.append(f"P50 {result['p50']:.0f} ms")
            if result["p99"] >= BUDGET_P99_MS:
                over.append(f"P99 {result['p99']:.0f} ms")
            verdict = "达标" if not over else "超预算"
            if over:
                failures.append(f"{name}：" + "、".join(over))
            print(
                f"      {name:<34} {result['p50']:>7.1f}ms {result['p95']:>7.1f}ms "
                f"{result['p99']:>7.1f}ms {result['rows']:>5}  {verdict}"
            )

        print("\n      耗时分解（关键词一页，M6：慢查询一眼看出是哪一段）")
        for name, _func in SCENARIOS[1:3]:
            print(f"      {name:<34} {_breakdown(conn, name)}")

        print("\n[3/4] 冷启动首查（第 7 项，预热前，仅供对照 M4 是否值得做）")
        for name, func in SCENARIOS[:3]:
            cold = measure_cold(path, func)
            print(f"      {name:<34} {cold:>7.1f}ms")

        print(f"\n[4/4] 并发与并发写入（第 8、9 项，readers={args.readers}）")
        concurrent = measure_concurrent(path, _query_plain_list, args.readers)
        if "error" in concurrent:
            print(f"      并发读者：失败（{concurrent['error']}）")
            failures.append("并发读者失败")
        else:
            print(
                f"      {args.readers} 个并发读者（{concurrent['samples']} 次采样）"
                f"：P50 {concurrent['p50']:.1f}ms，P99 {concurrent['p99']:.1f}ms"
            )
            if concurrent["p50"] >= BUDGET_P50_MS or concurrent["p99"] >= BUDGET_P99_MS:
                failures.append("并发读者超预算")
        under_write = measure_under_write(path, _query_plain_list)
        if "error" in under_write:
            print(f"      写入并发下读：失败（{under_write['error']}）")
            failures.append("写入并发下读失败")
        else:
            print(
                f"      后台写入同时读（{under_write['samples']} 次采样）"
                f"：P50 {under_write['p50']:.1f}ms，P99 {under_write['p99']:.1f}ms"
            )
            if "writer_error" in under_write:
                print(f"      写者报错：{under_write['writer_error']}")
                failures.append("后台写者失败")
            if under_write["p50"] >= BUDGET_P50_MS or under_write["p99"] >= BUDGET_P99_MS:
                failures.append("后台写入同时读超预算")
    finally:
        conn.close()

    print(f"\n预算（DESIGN §5.6）：P50 < {BUDGET_P50_MS:.0f} ms、P99 < {BUDGET_P99_MS:.0f} ms")
    if failures:
        print("结论：有场景超预算 —— " + "；".join(failures))
        if args.check:
            if not args.keep and directory is not None:
                for leftover in directory.glob("*"):
                    leftover.unlink()
                directory.rmdir()
            return 1
    else:
        print("结论：全部场景在预算内。")
    if args.keep:
        print(f"合成库保留在：{path}")
    elif directory is not None:
        for leftover in directory.glob("*"):
            leftover.unlink()
        directory.rmdir()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
