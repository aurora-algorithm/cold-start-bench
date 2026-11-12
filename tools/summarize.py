#!/usr/bin/env python3
"""Reduces a session's runs.jsonl to the medians in the README and writes summary.json next to it.

usage: python3 tools/summarize.py results/i7win-20261110

Start-up: median over every recorded run of a side (5 rounds x 20 runs). Load: median of the per-round values.
The plain round (no screen recording) is reported beside the recorded medians, never mixed in. MB here is MiB.
"""

import json
import statistics
import sys
from pathlib import Path

MB = 2**20
SIDES = ("spring", "spring-aot", "spring-native", "cs-jit", "cs-aot", "go")


def start_stats(rows: list[dict], side: str) -> dict:
    rec = [r for r in rows if r["kind"] == "start" and r["side"] == side and r["round"] != "plain"]
    plain = [r for r in rows if r["kind"] == "start" and r["side"] == side and r["round"] == "plain"]
    wall = [t * 1000 for r in rec for t in r["wall_s"]]
    return {
        "runs": len(wall),
        "median_ms": round(statistics.median(wall), 1),
        "min_ms": round(min(wall), 1),
        "max_ms": round(max(wall), 1),
        "plain_median_ms": round(statistics.median([t * 1000 for r in plain for t in r["wall_s"]]), 1),
    }


def load_stats(rows: list[dict], side: str) -> dict:
    rec = [r for r in rows if r["kind"] == "load" and r["side"] == side and r["round"] != "plain"]
    plain = next(r for r in rows if r["kind"] == "load" and r["side"] == side and r["round"] == "plain")

    def med(get) -> float:
        return statistics.median([get(r) for r in rec])

    return {
        "rounds": len(rec),
        "idle_working_set_mb": round(med(lambda r: r["memory"]["idle"]["working_set_median"]) / MB, 1),
        "idle_private_mb": round(med(lambda r: r["memory"]["idle"]["private_median"]) / MB, 1),
        "capped_working_set_mb": round(med(lambda r: r["memory"]["capped"]["working_set_median"]) / MB, 1),
        "first_20s_rps": round(med(lambda r: r["ramp"]["rps"])),
        "warm_rps": round(med(lambda r: r["steady"]["rps"])),
        "warm_p99_ms": round(med(lambda r: r["steady"]["p99_ms"]), 2),
        "capped_p99_ms": round(med(lambda r: r["capped"]["p99_ms"]), 2),
        "plain_warm_rps": round(plain["steady"]["rps"]),
    }


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    session = Path(sys.argv[1])
    rows = [json.loads(line) for line in (session / "runs.jsonl").read_text().splitlines() if line.strip()]
    summary = {
        side: {
            "label": next(r["label"] for r in rows if r["side"] == side),
            "start": start_stats(rows, side),
            "load": load_stats(rows, side),
        }
        for side in SIDES
    }
    (session / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for s in summary.values():
        print(
            f"{s['label']:30} start {s['start']['median_ms']:8.1f} ms  warm {s['load']['warm_rps']:>8,} req/s  "
            f"at 5k req/s {s['load']['capped_working_set_mb']:6.1f} MB"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
