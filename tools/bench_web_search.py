"""本机统一检索的可重跑规模烟测；帖级基准仍使用 bench_search.py。"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

from bench_search import build_database

from pc.adapters import sqlite as db
from pc.services.web_search import run_web_search


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Web v2 unified search scale smoke benchmark")
    parser.add_argument("--scale", type=int, default=100000, help="content item count, default 100000")
    parser.add_argument("--compositions", type=int, default=1000, help="single-member composition count, default 1000")
    parser.add_argument("--runs", type=int, default=20, help="repeated queries per case, default 20")
    parser.add_argument("--assert", dest="enforce", action="store_true", help="fail if P50 or P99 exceeds budget")
    args = parser.parse_args(argv)
    if args.scale < 1 or args.compositions < 1 or args.compositions > args.scale or args.runs < 2:
        parser.error("scale and compositions must be positive, compositions <= scale, runs >= 2")

    try:
        with tempfile.TemporaryDirectory(prefix="pc_web_search_bench_") as directory:
            database = Path(directory) / "bench.sqlite3"
            build_database(database, args.scale)
            with db.open_db(database) as conn:
                with db.transaction(conn):
                    for ident in range(1, args.compositions + 1):
                        created = "2026-10-14T00:00:00.000Z"
                        conn.execute(
                            "INSERT INTO composition(composition_id,name,created_at,updated_at) VALUES (?,?,?,?)",
                            (ident, f"组合 {ident}", created, created),
                        )
                        conn.execute(
                            "INSERT INTO composition_member(composition_id,position,content_id) VALUES (?,0,?)",
                            (ident, ident),
                        )
                failures = []
                for keyword in ("", "蓝发"):
                    durations = []
                    for _ in range(args.runs):
                        started = time.perf_counter()
                        result = run_web_search(conn, q=keyword, limit=50)
                        durations.append((time.perf_counter() - started) * 1000)
                        if result["result_version"] != 2 or len(result["hits"]) != 50:
                            raise AssertionError("查询结果版本或页长不符合预期")
                    # 首次查询单独报告，P50/P99 只从后续热缓存查询估算。
                    hot = sorted(durations[1:])
                    p50 = statistics.median(hot)
                    p99 = hot[max(0, (len(hot) * 99 + 99) // 100 - 1)]
                    passed = p50 < 150 and p99 < 500
                    label = keyword.encode("unicode_escape").decode("ascii") or "empty"
                    print(
                        f"[{'PASS' if passed else 'FAIL'}] Web v2 content+composition "
                        f"scale={args.scale}+{args.compositions} query={label} "
                        f"cold={durations[0]:.1f}ms P50={p50:.1f}ms P99={p99:.1f}ms "
                        "budget=P50<150ms/P99<500ms",
                        flush=True,
                    )
                    if not passed:
                        failures.append(keyword)
                if args.enforce and failures:
                    labels = [keyword.encode("unicode_escape").decode("ascii") or "empty" for keyword in failures]
                    print(f"[FAIL] queries over budget: {labels!r}", file=sys.stderr)
                    return 1
                return 0
    except Exception as exc:
        reason = str(exc).encode("ascii", "backslashreplace").decode("ascii")
        print(f"[FAIL] Web v2 benchmark could not complete: {type(exc).__name__}: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
