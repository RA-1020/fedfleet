"""Summarise FedFleet runs: one line per run, then mean and spread per arm.

Each run is a metrics folder holding rounds.jsonl. Folders named <arm>-r<N> (for example
head-lr0.001-r2) are grouped into arm <arm>; repetitions are what gives the spread.

    python scripts/summarize_runs.py ~/fedfleet-runs-e9/*
    python scripts/summarize_runs.py ~/fedfleet-runs-e9/* --csv logs/e9-summary.csv

Accuracy is the global accuracy on the aggregator's fixed public test set. Round time is
the aggregator's train_round_seconds, which moves in steps of Flower's 3 s polling.
"Site overhead" is turnaround minus setup minus training: starting the training process,
receiving the model and polling. "Return" is round time minus the slowest site's
turnaround: sending the reply back and the aggregator's next poll. Both compare two
machines' clocks, so they are only as exact as the clock sync.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics as st
from pathlib import Path


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def _sd(xs):
    xs = [x for x in xs if x is not None]
    return st.stdev(xs) if len(xs) > 1 else None


def summarize(folder: Path) -> dict | None:
    f = folder / "rounds.jsonl"
    if not f.exists():
        return None
    rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    rounds = [r for r in rows if "round" in r and r["round"] > 0]   # skips round 0 and "resumed" events
    start = next((r for r in rows if r.get("round") == 0), None)
    if not rounds:
        return None
    acc = [r.get("global_acc") for r in rounds]
    site_over, ret, site_train = [], [], []
    for r in rounds:
        sites = r.get("sites") or []
        for s in sites:
            if None not in (s.get("turnaround_seconds"), s.get("setup_seconds"), s.get("train_seconds")):
                site_over.append(s["turnaround_seconds"] - s["setup_seconds"] - s["train_seconds"])
            site_train.append(s.get("train_seconds"))
        turns = [s["turnaround_seconds"] for s in sites if s.get("turnaround_seconds") is not None]
        if turns and r.get("train_round_seconds") is not None:
            ret.append(r["train_round_seconds"] - max(turns))
    arm = re.sub(r"-r\d+$", "", folder.name)
    return {
        "run": folder.name, "arm": arm, "mode": rounds[0].get("mode"),
        "rounds": len(rounds), "acc_start": None if start is None else start.get("global_acc"),
        "acc_final": acc[-1], "acc_peak": max(a for a in acc if a is not None),
        "bytes_per_update": rounds[0].get("bytes_per_update"),
        "round_s": _mean([r.get("train_round_seconds") for r in rounds]),
        "site_train_s": _mean(site_train), "site_overhead_s": _mean(site_over), "return_s": _mean(ret),
        "replied": f"{sum(r.get('nodes_replied', 0) for r in rounds)}/{sum(r.get('nodes_asked', 0) for r in rounds)}",
    }


def _pct(x):
    return "   n/a" if x is None else f"{100 * x:6.2f}"


def _sec(x):
    return "   n/a" if x is None else f"{x:6.1f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+", type=Path)
    ap.add_argument("--csv", default="", help="also write the per-run lines to this CSV file")
    args = ap.parse_args()
    runs = [s for s in (summarize(p) for p in sorted(args.folders)) if s]
    if not runs:
        raise SystemExit("no rounds.jsonl found in the given folders")

    print(f"{'run':28} {'mode':10} {'rnds':>4} {'start%':>6} {'final%':>6} {'peak%':>6} "
          f"{'bytes/upd':>10} {'round s':>7} {'train s':>7} {'site ov':>7} {'return':>7} {'replied':>7}")
    for s in runs:
        print(f"{s['run']:28} {str(s['mode']):10} {s['rounds']:>4} {_pct(s['acc_start'])} {_pct(s['acc_final'])} "
              f"{_pct(s['acc_peak'])} {s['bytes_per_update'] or 0:>10,} {_sec(s['round_s']):>7} "
              f"{_sec(s['site_train_s']):>7} {_sec(s['site_overhead_s']):>7} {_sec(s['return_s']):>7} {s['replied']:>7}")

    print("\nper arm (mean, with standard deviation across repetitions where there are 2 or more):")
    for arm in sorted({s["arm"] for s in runs}):
        g = [s for s in runs if s["arm"] == arm]
        fin = [s["acc_final"] for s in g]
        sd = _sd(fin)
        print(f"  {arm:24} n={len(g)}  final {100 * _mean(fin):.2f}%"
              + (f" ± {100 * sd:.2f}" if sd is not None else "")
              + f"  peak {100 * _mean([s['acc_peak'] for s in g]):.2f}%"
              + f"  round {_mean([s['round_s'] for s in g]):.1f} s"
              + f"  bytes/update {g[0]['bytes_per_update']:,}")

    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(runs[0].keys()))
            w.writeheader()
            w.writerows(runs)
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
