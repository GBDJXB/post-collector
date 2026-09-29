"""检索性能基准：DESIGN §5.6 的预算与九类场景。

它回答一个具体问题：**在 10 万条内容上，「数据层收到已结构化的查询 → 返回前 50 条
组装好的结果」要多久**。预算口径见 DESIGN §5.6：P50 < 150 ms、P99 < 500 ms
（热缓存、单用户或 ≤4 并发读者；缩略图字节与 AI 解析不计入）。

刻意说明边界：本脚本跑的是**裸 SQL**——业务检索层还没实现，所以它验证的是
**schema、索引与查询构造策略**，不是端到端检索路径。结论不要说过了头。

用法::

    uv run python tools/bench_search.py --scale 100000            # 只测并打印
    uv run python tools/bench_search.py --scale 100000 --assert   # 超预算则退出码 1
    uv run python tools/bench_search.py --scale 20000 --keep      # 保留合成库以便排查

合成数据的性质（决定了结论的适用范围）：
  * 标签按 Zipf 分布，所以会出现「覆盖 20% 内容的通用标签」这种真实的长尾形态；
  * 文本是中日文混排的伪随机词，`text_search` 是分词结果，`search_extra` 是标签与来源名的拼接；
  * 媒体与内容项通过 `content_media` 多对多关联（与 §3.8 的引用计数口径一致）。
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pc.adapters import sqlite as db

#: 伪随机词表：中文词、日文片假名、拉丁词混排
CJK_WORDS = (
    "蓝发 精灵 雨 银发 法师 夜 猫 耳 少女 骑士 机甲 樱花 雪 海 城市 黄昏 剑 魔法 图书馆 "
    "夏日 制服 眼镜 微笑 眼泪 星 空 风 花火 祭典 猫娘 天使 恶魔 王冠 披风 长枪 弓 阴影 "
)
KANA_WORDS = "ミク カガミネ リン レン サクラ ユキ ツキ ホシ カゼ ハナ"
LATIN_WORDS = "sunset portrait original fanart sketch commission oc digital watercolor"
TAG_NAMES = [
    "blue_hair", "silver_hair", "twintails", "long_hair", "short_hair", "cat_ears",
    "glasses", "school_uniform", "kimono", "armor", "sword", "magic", "rain", "night",
    "sunset", "city", "sky", "flower", "winter", "summer", "smile", "crying",
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


def _make_text(rng: random.Random, words: int) -> tuple[str, str]:
    """返回 ``(正文, 分词结果)``；分词结果用空格连接，模拟应用层分词。"""
    picked: list[str] = []
    for _ in range(words):
        roll = rng.random()
        if roll < 0.7:
            picked.append(rng.choice(CJK_WORDS.split()))
        elif roll < 0.9:
            picked.append(rng.choice(KANA_WORDS.split()))
        else:
            picked.append(rng.choice(LATIN_WORDS.split()))
    text = "".join(picked) if rng.random() < 0.5 else " ".join(picked)
    return text, " ".join(picked)


def build_database(path: Path, scale: int, *, batch: int = 5000) -> dict[str, Any]:
    """造一个合成库；返回耗时与体积统计。"""
    conn = db.connect(path)
    started = time.perf_counter()
    db.apply_migration(conn, db.DEFAULT_MIGRATION_FILE)
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

    for start in range(0, scale, batch):
        stop = min(start + batch, scale)
        conn.execute("BEGIN IMMEDIATE")
        for content_id in range(start + 1, stop + 1):
            message_id = content_id
            text, tokens = _make_text(rng, rng.randint(6, 18))
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
            extra = " ".join(TAG_NAMES[tag_id - 1] for tag_id in picks if tag_id)
            conn.execute(
                "INSERT INTO message (message_id, uuid, source_id, platform_msg_id) "
                "VALUES (?, ?, ?, ?)",
                (message_id, f"msg-{message_id}", rng.randint(1, len(SOURCE_NAMES)), f"pm-{message_id}"),
            )
            conn.execute(
                "INSERT INTO content_item "
                "(content_id, uuid, message_id, item_type, ordinal, text_content, ocr_text, "
                " caption, text_search, search_extra, nsfw_level, status) "
                "VALUES (?, ?, ?, 'image', 0, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    content_id,
                    f"ci-{content_id}",
                    message_id,
                    text,
                    text if rng.random() < 0.3 else None,
                    f"caption {rng.choice(LATIN_WORDS.split())}" if rng.random() < 0.3 else None,
                    tokens,
                    f"{extra} {SOURCE_NAMES[rng.randint(0, len(SOURCE_NAMES) - 1)]}",
                    2 if content_id % 97 == 0 else 0,
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
        conn.execute("COMMIT")

    finished = time.perf_counter()
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
# 查询构造（M1 / M2：候选集 + 三条 SQL 组装，不出现 N+1）
# ---------------------------------------------------------------------------


def _assemble(conn: sqlite3.Connection, ids: list[int]) -> list[dict[str, Any]]:
    """把前 N 个 content_id 组装成结果项：**固定 3 条 SQL**（主查询 + tag + 媒体）。"""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = {
        row["content_id"]: row
        for row in conn.execute(
            "SELECT c.content_id, c.item_type, c.text_content, c.first_seen_at, c.nsfw_level, "
            "       s.name AS source_name "
            "FROM content_item c JOIN message m ON m.message_id = c.message_id "
            "JOIN source s ON s.source_id = m.source_id "
            f"WHERE c.content_id IN ({marks})",
            ids,
        ).fetchall()
    }
    tags: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT ta.content_id, t.name FROM tag_assignment ta JOIN tag t ON t.tag_id = ta.tag_id "
        f"WHERE ta.content_id IN ({marks}) AND ta.revoked_at IS NULL",
        ids,
    ).fetchall():
        tags.setdefault(row["content_id"], []).append(row["name"])
    media: dict[int, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT cm.content_id, m.thumb_key, m.width, m.height FROM content_media cm "
        "JOIN media m ON m.media_id = cm.media_id "
        f"WHERE cm.content_id IN ({marks}) ORDER BY cm.ordinal",
        ids,
    ).fetchall():
        media.setdefault(row["content_id"], {"thumb_key": row["thumb_key"], "w": row["width"]})
    return [
        {
            "content_id": cid,
            "text": (rows[cid]["text_content"] or "")[:120],
            "tags": tags.get(cid, []),
            "thumb": media.get(cid, {}).get("thumb_key"),
            "source": rows[cid]["source_name"],
        }
        for cid in ids
        if cid in rows
    ]


def _keyword_candidates(conn: sqlite3.Connection, term: str, limit: int = 500) -> list[int]:
    """M1：先取 bm25 前 N 条作为候选，再在候选集内做过滤。"""
    if len(term) < 3:
        sql = (
            "SELECT rowid FROM content_word WHERE content_word MATCH ? "
            "ORDER BY bm25(content_word) LIMIT ?"
        )
        rows = conn.execute(sql, (term, limit)).fetchall()
    else:
        sql = (
            "SELECT rowid FROM content_fts WHERE content_fts MATCH ? "
            "ORDER BY bm25(content_fts) LIMIT ?"
        )
        rows = conn.execute(sql, (term, limit)).fetchall()
    return [int(row[0]) for row in rows]


def _tag_ids(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    marks = ",".join("?" * len(names))
    return [
        int(row[0])
        for row in conn.execute(
            f"SELECT tag_id FROM tag WHERE name IN ({marks})", names
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# 九类场景
# ---------------------------------------------------------------------------


def _query_keyword_broad(conn: sqlite3.Connection) -> int:
    ids = _keyword_candidates(conn, "雨", PAGE_SIZE)
    return len(_assemble(conn, ids))


def _query_cjk_mixed(conn: sqlite3.Connection) -> int:
    ids = _keyword_candidates(conn, "蓝发", PAGE_SIZE)
    return len(_assemble(conn, ids))


def _query_short_term(conn: sqlite3.Connection) -> int:
    # 2 字词：trigram 用不上，必须走词级索引（§5.1 的退化点）
    ids = _keyword_candidates(conn, "精灵", PAGE_SIZE)
    return len(_assemble(conn, ids))


def _query_keyword_tag_time(conn: sqlite3.Connection) -> int:
    candidate = _keyword_candidates(conn, "雨", 500)
    if not candidate:
        return 0
    marks = ",".join("?" * len(candidate))
    tag_ids = _tag_ids(conn, ["blue_hair", "rain"])
    tag_marks = ",".join("?" * len(tag_ids))
    rows = conn.execute(
        "SELECT c.content_id FROM content_item c "
        f"WHERE c.content_id IN ({marks}) AND c.first_seen_at >= ? "
        f"  AND EXISTS (SELECT 1 FROM tag_assignment ta WHERE ta.content_id = c.content_id "
        f"              AND ta.tag_id IN ({tag_marks}) AND ta.revoked_at IS NULL) "
        "ORDER BY c.first_seen_at DESC LIMIT ?",
        [*candidate, "2026-01-01T00:00:00.000Z", *tag_ids, PAGE_SIZE],
    ).fetchall()
    return len(_assemble(conn, [int(row[0]) for row in rows]))


def _query_three_tag_intersection(conn: sqlite3.Connection) -> int:
    """多标签交集：按集合大小升序求交，而不是 GROUP BY … HAVING（§5.6 M1）。"""
    tag_ids = _tag_ids(conn, ["rain", "night", "sky"])
    sets: list[set[int]] = []
    for tag_id in tag_ids:
        rows = conn.execute(
            "SELECT content_id FROM tag_assignment WHERE tag_id = ? AND revoked_at IS NULL",
            (tag_id,),
        ).fetchall()
        sets.append({int(row[0]) for row in rows})
    if not sets:
        return 0
    sets.sort(key=len)
    result = sets[0]
    for other in sets[1:]:
        result &= other
        if not result:
            break
    ids = sorted(result)[:PAGE_SIZE]
    return len(_assemble(conn, ids))


def _query_deep_paging(conn: sqlite3.Connection) -> int:
    """深分页：keyset 分页而不是 OFFSET（§5.6 M1）。"""
    cursor: str | None = None
    for _ in range(20):
        if cursor is None:
            rows = conn.execute(
                "SELECT content_id, first_seen_at FROM content_item "
                "WHERE deleted_at IS NULL ORDER BY first_seen_at DESC, content_id DESC LIMIT ?",
                (PAGE_SIZE,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT content_id, first_seen_at FROM content_item "
                "WHERE deleted_at IS NULL AND (first_seen_at, content_id) < (?, ?) "
                "ORDER BY first_seen_at DESC, content_id DESC LIMIT ?",
                (cursor, 0, PAGE_SIZE),
            ).fetchall()
        if not rows:
            return 0
        cursor = str(rows[-1][1])
    return len(_assemble(conn, [int(row[0]) for row in rows]))


def _query_nsfw_filter(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT content_id FROM content_item WHERE nsfw_level <= 1 AND deleted_at IS NULL "
        "ORDER BY first_seen_at DESC LIMIT ?",
        (PAGE_SIZE,),
    ).fetchall()
    return len(_assemble(conn, [int(row[0]) for row in rows]))


def _query_plain_list(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT content_id FROM content_item WHERE deleted_at IS NULL "
        "ORDER BY first_seen_at DESC, content_id DESC LIMIT ?",
        (PAGE_SIZE,),
    ).fetchall()
    return len(_assemble(conn, [int(row[0]) for row in rows]))


SCENARIOS: tuple[tuple[str, Callable[[sqlite3.Connection], int]], ...] = (
    ("纯过滤列表（时间倒序 50 条）", _query_plain_list),
    ("纯关键词宽命中（1 字，走词级索引）", _query_keyword_broad),
    ("中日文混排查询（2 字）", _query_cjk_mixed),
    ("短词（2 字，trigram 用不上）", _query_short_term),
    ("关键词 + 标签 + 时间", _query_keyword_tag_time),
    ("三标签交集", _query_three_tag_intersection),
    ("深分页（第 20 页，keyset）", _query_deep_paging),
    ("受限内容过滤（nsfw_level）", _query_nsfw_filter),
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


def measure_cold(path: Path, func: Callable[[sqlite3.Connection], int]) -> float:
    """冷启动首查：新开连接后的第一次查询（对照 M4 预热是否值得做）。"""
    conn = db.connect(path, expect_exists=True)
    try:
        started = time.perf_counter()
        func(conn)
        return (time.perf_counter() - started) * 1000
    finally:
        conn.close()


def measure_concurrent(path: Path, func: Callable[[sqlite3.Connection], int], readers: int) -> dict[str, Any]:
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
    return {"p50": statistics.median(samples), "p99": _percentile(samples, 99), "samples": len(samples)}


def measure_under_write(path: Path, func: Callable[[sqlite3.Connection], int]) -> dict[str, Any]:
    """后台写入同时发生：验证 WAL 下读不被写阻塞（§5.6 场景 8）。"""
    stop = threading.Event()
    writer_errors: list[str] = []

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
    parser.add_argument("--keep", action="store_true", help="保留合成库以便排查")
    parser.add_argument("--assert", dest="check", action="store_true", help="超预算则退出码 1")
    args = parser.parse_args(argv)

    directory = Path(tempfile.mkdtemp(prefix="pc_bench_")) if not args.path else None
    path = Path(args.path) if args.path else directory / "pc_bench.sqlite3"  # type: ignore[operator]
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
        print(f"\n[2/4] 九类场景（每类 {args.runs} 次采样，{PAGE_SIZE} 条/页）")
        print(f"      {'场景':<34} {'P50':>8} {'P95':>8} {'P99':>8} {'行数':>5}  判定")
        for name, func in SCENARIOS:
            result = measure(conn, func, args.runs)
            verdict = "达标" if result["p99"] < BUDGET_P99_MS else "超预算"
            if result["p99"] >= BUDGET_P99_MS:
                failures.append(f"{name}：P99 {result['p99']:.0f} ms")
            print(
                f"      {name:<34} {result['p50']:>7.1f}ms {result['p95']:>7.1f}ms "
                f"{result['p99']:>7.1f}ms {result['rows']:>5}  {verdict}"
            )

        print("\n[3/4] 冷启动首查（预热前，仅供对照 M4 是否值得做）")
        for name, func in SCENARIOS[:3]:
            cold = measure_cold(path, func)
            print(f"      {name:<34} {cold:>7.1f}ms")

        print(f"\n[4/4] 并发与并发写入（readers={args.readers}）")
        concurrent = measure_concurrent(path, _query_plain_list, args.readers)
        if "error" in concurrent:
            print(f"      并发读者：失败（{concurrent['error']}）")
            failures.append("并发读者失败")
        else:
            print(
                f"      {args.readers} 个并发读者（{concurrent['samples']} 次采样）"
                f"：P50 {concurrent['p50']:.1f}ms，P99 {concurrent['p99']:.1f}ms"
            )
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
