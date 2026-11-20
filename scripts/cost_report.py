"""Cost per round for a finished run, from its rounds.jsonl and a rate card.

    python scripts/cost_report.py RUN_DIR_OR_ROUNDS_JSONL [--rates deploy/rates.toml] [--json]

Prints one line per training round (provider, customer, total, wall time) and the run
total. Prices are list prices before free tiers and credits (see fedfleet/cost.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fedfleet.cost import load_rates, run_costs  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run")
    p.add_argument("--rates", default=str(Path(__file__).resolve().parent.parent / "deploy" / "rates.toml"))
    p.add_argument("--json", action="store_true", help="one JSON object per round instead of a table")
    a = p.parse_args()
    path = Path(a.run).expanduser()
    if path.is_dir():
        path = path / "rounds.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rates = load_rates(a.rates)
    costs = run_costs(rows, rates)
    if a.json:
        for row, c in costs:
            print(json.dumps({"round": row["round"], **c}))
        return
    usd = lambda v: f"{v * 1e6:9.1f}"
    print(f"{path}  (micro-USD, list prices before free tiers and credits)")
    print(f"{'round':>5} {'wall s':>7} {'sites':>5} {'provider':>9} {'customer':>9} {'busy':>9} {'total':>9}")
    tot = {"provider_usd": 0.0, "customer_usd": 0.0, "customer_busy_usd": 0.0, "total_usd": 0.0, "wall_seconds": 0.0}
    for row, c in costs:
        print(f"{row['round']:>5} {c['wall_seconds']:>7.1f} {row.get('nodes_replied', 0):>2}/{row.get('nodes_asked', 0):<2} "
              f"{usd(c['provider_usd'])} {usd(c['customer_usd'])} {usd(c['customer_busy_usd'])} {usd(c['total_usd'])}")
        for k in tot:
            tot[k] += c[k]
    if costs:
        n = len(costs)
        print(f"{'total':>5} {tot['wall_seconds']:>7.1f} {'':>5} {usd(tot['provider_usd'])} {usd(tot['customer_usd'])} "
              f"{usd(tot['customer_busy_usd'])} {usd(tot['total_usd'])}")
        print(f"{'mean':>5} {tot['wall_seconds'] / n:>7.1f} {'':>5} {usd(tot['provider_usd'] / n)} "
              f"{usd(tot['customer_usd'] / n)} {usd(tot['customer_busy_usd'] / n)} {usd(tot['total_usd'] / n)}")
    agg = rates["aggregator"]
    print(f"aggregator {agg.get('name')}: ${agg['usd_per_hour']:.4f}/h list; "
          + "; ".join(f"site {k} {v.get('name')}: ${v.get('usd_per_hour', 0):.4f}/h"
                      for k, v in sorted(rates.get('sites', {}).items())))


if __name__ == "__main__":
    main()
