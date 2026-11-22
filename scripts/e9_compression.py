"""E9 compression arms (WBS 14.9): the same federation run once per compression setting,
started and read back through the FedFleet gateway, then one comparison table.

    python scripts/e9_compression.py --gateway https://fedfleet-gateway.<sub>.workers.dev
        [--token-file ~/.fedfleet-api-token] [--rounds 5] [--arms none,int8,topk,topk-int8]
        [--tag 22nov] [--topk-ratio 0.1]

Bring the sites up first (scripts/cloud_up.sh) and down afterwards (scripts/cloud_down.sh).
Every arm uses the same settings and the default fixed seed, so with all sites replying the
only difference between arms is the compression. One run per arm: a single run cannot show
run-to-run spread.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(gw: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(gw.rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", required=True)
    p.add_argument("--token-file", default="~/.fedfleet-api-token")
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--arms", default="none,int8,topk,topk-int8")
    p.add_argument("--topk-ratio", type=float, default=0.1)
    p.add_argument("--tag", default=time.strftime("%d%b").lower())
    p.add_argument("--wait-minutes", type=float, default=20)
    a = p.parse_args()
    token = Path(a.token_file).expanduser().read_text().strip()
    results = []
    for arm in a.arms.split(","):
        name = f"e9c-{a.tag}-{arm}"
        run = f"fedfleet-runs-{name}"
        started = call(a.gateway, token, "POST", "/runs", {
            "name": name, "rounds": a.rounds, "min_nodes": 3, "compression": arm, "topk_ratio": a.topk_ratio})
        print(f"[{arm}] started run {started['run_id']} -> {run}", flush=True)
        deadline = time.time() + a.wait_minutes * 60
        detail = {}
        while time.time() < deadline:
            time.sleep(20)
            try:
                detail = call(a.gateway, token, "GET", f"/runs/{run}")
            except SystemExit:
                continue                      # the folder appears after the first round
            print(f"[{arm}]   round {detail['rounds']}/{a.rounds}", flush=True)
            if detail["rounds"] >= a.rounds:
                break
        else:
            raise SystemExit(f"[{arm}] did not finish within {a.wait_minutes} minutes; check the sites")
        results.append((arm, detail))
        time.sleep(15)                         # let the aggregator finish the run before the next
    print()
    print(f"{'arm':<10} {'B up/round':>11} {'vs none':>8} {'final acc':>9} {'acc per round':<34} {'cost $':>9} {'dropped':>7}")
    base = None
    for arm, d in results:
        up = d["bytes_up"] / max(d["rounds"], 1)
        base = up if arm == "none" else base
        rel = f"{up / base:.1%}" if base else "-"
        accs = " ".join(f"{r['global_acc']:.4f}" for r in d["per_round"])
        cost = "-" if d["total_usd"] is None else f"{d['total_usd']:.5f}"
        print(f"{arm:<10} {up:>11,.0f} {rel:>8} {d['final_acc']:>9.4f} {accs:<34} {cost:>9} {d['nodes_dropped']:>7}")
    print("\nCosts are list prices before free tiers and credits. One run per arm.")


if __name__ == "__main__":
    main()
