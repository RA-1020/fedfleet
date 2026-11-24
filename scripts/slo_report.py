#!/usr/bin/env python3
"""FedFleet service-level objectives, measured from every run's rounds.jsonl (standard library only).

    python3 scripts/slo_report.py                 on the aggregator (reads ~/fedfleet-runs*)
    python3 scripts/slo_report.py --root <dir>    any folder holding fedfleet-runs-* copies
    python3 scripts/slo_report.py --since 2026-11-24   only rounds written on or after that day (UTC)

Round-level objectives are computed over every training round on record. Runs that injected a
fault on purpose (chaos drills, residency and capacity checks) are counted separately for the
completeness objective, because missing sites are their point. Recovery objectives come from the
drills (D2 section 9) and are not recomputed here. Folders ending in -void are ignored.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import statistics

DRILL = re.compile(r"(part|kill|crash|res-eu|nosites|drop|chaos)")

# (name, target, unit) - the numbers D2 states as FedFleet's objectives.
ROUND_PLAIN_S = 60.0     # 95% of plain rounds within this
ROUND_SECAGG_S = 90.0    # 95% of SecAgg+ rounds within this (three extra exchanges per round)
COMPLETE_FRACTION = 0.99 # outside fault-injection runs, rounds where every asked site replied


def load(root: str) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = {}
    for pat in ("fedfleet-runs*/rounds.jsonl", "fedfleet-runs*/*/rounds.jsonl"):
        for path in glob.glob(os.path.join(os.path.expanduser(root), pat)):
            name = os.path.relpath(os.path.dirname(path), os.path.expanduser(root))
            if name.split("/")[0].endswith("-void"):
                continue
            rows = []
            for line in open(path, errors="replace"):
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r.get("round"), int) and r["round"] >= 1 and "nodes_asked" in r:
                    rows.append(r)
            if rows:
                runs[name] = rows
    return runs


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(q * (len(s) - 1))))
    return s[k]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default="~")
    p.add_argument("--since", help="YYYY-MM-DD (UTC): only rounds written on or after this day")
    a = p.parse_args()
    runs = load(a.root)
    rows = [(n, r) for n, rs in runs.items() for r in rs]
    if a.since:
        t0 = datetime.datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp()
        rows = [(n, r) for n, r in rows if (r.get("timestamp") or 0) >= t0]
        runs = {n: None for n, _ in rows}
    if not rows:
        raise SystemExit("no training rounds found")
    print(f"{len(runs)} runs, {len(rows)} training rounds\n")

    print("| Objective | Target | Measured | Met |")
    print("|---|---|---|---|")
    for label, sel, limit in (("Plain round time, head-only (the production mode)",
                               lambda r: "secagg" not in r and r.get("mode") != "full-model", ROUND_PLAIN_S),
                              ("(for reference) plain round time, full model (E9 baseline)",
                               lambda r: "secagg" not in r and r.get("mode") == "full-model", ROUND_PLAIN_S),
                              ("SecAgg+ round time (aggregated rounds)",
                               lambda r: "secagg" in r and r["secagg"].get("aggregated"), ROUND_SECAGG_S)):
        t = [float(r["train_round_seconds"]) for _, r in rows if sel(r) and r.get("train_round_seconds") is not None]
        if t:
            share = sum(x <= limit for x in t) / len(t)
            print(f"| {label} | 95% within {limit:g} s | {share:.1%} within; median {statistics.median(t):.1f} s, "
                  f"p95 {pct(t, 0.95):.1f} s, max {max(t):.1f} s ({len(t)} rounds) | {'yes' if share >= 0.95 else 'NO'} |")

    normal = [r for n, r in rows if not DRILL.search(n)]
    drills = [r for n, r in rows if DRILL.search(n)]
    full = sum(r["nodes_replied"] == r["nodes_asked"] for r in normal)
    if normal:
        share = full / len(normal)
        print(f"| Round completeness (every asked site replied), outside fault-injection runs | "
              f"{COMPLETE_FRACTION:.0%} | {share:.1%} ({full} of {len(normal)} rounds) | {'yes' if share >= COMPLETE_FRACTION else 'NO'} |")
    if drills:
        d = sum(r["nodes_replied"] == r["nodes_asked"] for r in drills)
        print(f"| (for reference) completeness in fault-injection runs | none | {d / len(drills):.1%} ({d} of {len(drills)}) | n/a |")

    budgeted = [r for _, r in rows if (r.get("dp") or {}).get("max_epsilon")]
    over = [r for r in budgeted if (r["dp"].get("epsilon") or 0) > r["dp"]["max_epsilon"]]
    print(f"| Privacy budget never exceeded | 100% of rounds | {len(budgeted) - len(over)} of {len(budgeted)} "
          f"rounds with a budget within it | {'yes' if not over else 'NO'} |")
    worst = [(n, r["round"], r["train_round_seconds"]) for n, r in rows
             if "secagg" not in r and (r.get("train_round_seconds") or 0) > ROUND_PLAIN_S]
    if worst:
        print("\nPlain rounds over the target (run, round, seconds):")
        for w in sorted(worst, key=lambda x: -x[2])[:10]:
            print(f"  {w[0]} round {w[1]}: {w[2]:.1f} s")


if __name__ == "__main__":
    main()
