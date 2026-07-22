"""Summarize a frametime.jsonl pulled from the device.

Usage: python analyze_frametime.py <file> [--since EPOCH]

Prints per-scene-type aggregates and the worst offenders, so runs before/
after each enhancement can be compared like-for-like.
"""
import json
import sys
from collections import defaultdict


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, (len(sorted_vals) * p) // 100)]


def main():
    path = sys.argv[1]
    since = 0.0
    if "--since" in sys.argv:
        since = float(sys.argv[sys.argv.index("--since") + 1])
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("ts", 0) >= since:
                rows.append(d)
    if not rows:
        print("no rows")
        return

    span_s = max(r["ts"] for r in rows) - min(r["ts"] for r in rows)
    print("rows=%d span=%.1fmin" % (len(rows), span_s / 60.0))

    groups = defaultdict(list)
    for r in rows:
        kind = r["id"].split(":")[0] if ":" in r["id"] else r["scene"]
        groups[kind].append(r)

    print("\n%-10s %6s %8s | %7s %7s %7s %7s | %7s %7s | %7s %s" % (
        "kind", "runs", "frames", "lateP50", "lateP95", "lateMax", ">40ms%",
        "showP50", "showMax", "resyncs", "drops"))
    for kind in sorted(groups):
        g = groups[kind]
        frames = sum(r["frames"] for r in g)
        lp50 = sorted(r["late_p50"] for r in g)
        lp95 = sorted(r["late_p95"] for r in g)
        lmax = max(r["late_max"] for r in g)
        n40 = sum(r["late_n40"] for r in g)
        sp50 = sorted(r["show_p50"] for r in g)
        smax = max(r["show_max"] for r in g)
        rs = sum(r["resyncs"] for r in g)
        dr = sum(r.get("dropped", 0) for r in g)
        print("%-10s %6d %8d | %7d %7d %7d %6.1f%% | %7d %7d | %7d %d" % (
            kind, len(g), frames, pct(lp50, 50), pct(lp95, 50), lmax,
            100.0 * n40 / max(1, frames), pct(sp50, 50), smax, rs, dr))

    print("\nworst runs by late_p95:")
    for r in sorted(rows, key=lambda r: -r["late_p95"])[:12]:
        stretch = r["wall_ms"] / max(1.0, float(r["planned_ms"]))
        print("  %-46s frames=%-5d lateP95=%-5d lateMax=%-6d "
              "showP50=%-3d resync=%d stretch=%.2f" % (
                  r["id"][:46], r["frames"], r["late_p95"], r["late_max"],
                  r["show_p50"], r["resyncs"], stretch))

    print("\nworst runs by resyncs:")
    for r in sorted(rows, key=lambda r: (-r["resyncs"], -r["late_max"]))[:8]:
        if r["resyncs"] == 0:
            break
        print("  %-46s frames=%-5d resyncs=%-3d resyncMax=%dms" % (
            r["id"][:46], r["frames"], r["resyncs"], r["resync_max"]))


if __name__ == "__main__":
    main()
