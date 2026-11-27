"""Checks for the CI federation (scripts/ci_federation.sh, task 15.2).

    python3 scripts/ci_check_runs.py --rounds 2 --sites 3 <plain run folder> <secagg run folder>

For each run: every round is logged, aggregated, and answered by every site, with a finite
global accuracy. Then the two properties the live system was shown to have:
  * SecAgg+ gives the model plain averaging gives, up to its quantisation: Flower's SecAgg+
    rounds each value up or down at random (stochastic rounding), so the two runs are not
    bit-identical. Measured on 27 Nov (3 sites, 2 rounds, synthetic data): largest parameter
    difference 7.3e-4, mean 4.3e-5, accuracy identical or one test sample apart. The tolerances
    below are about seven times that; a real fault in masking or weighting is far larger;
  * under SecAgg+ no site reports its training loss or update norm in the clear.
Exit code 1 with a message on the first failure.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

PRIVATE = ("train_loss", "update_norm")
MAX_PARAM_DIFF = 5e-3      # largest |plain - secagg| over the final exchanged parameters
MAX_ACC_DIFF = 0.01        # per-round global accuracy, about three test samples


def fail(msg: str) -> None:
    print(f"!! {msg}")
    sys.exit(1)


def rounds(folder: Path) -> list[dict]:
    f = folder / "rounds.jsonl"
    if not f.is_file():
        fail(f"{f} missing")
    rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if "train_round_seconds" in r]


def param_diff(a: Path, b: Path) -> float:
    import torch                     # only here: the other checks need no torch

    def flat(obj):
        vals = obj.values() if isinstance(obj, dict) else obj
        return torch.cat([torch.as_tensor(v).flatten().double() for v in vals])
    for f in (a, b):
        if not f.is_file():
            fail(f"{f} missing")
    fa = flat(torch.load(a, weights_only=False))
    fb = flat(torch.load(b, weights_only=False))
    if fa.shape != fb.shape:
        fail(f"parameter shapes differ: {tuple(fa.shape)} against {tuple(fb.shape)}")
    return float((fa - fb).abs().max())


def check_run(name: str, rows: list[dict], want_rounds: int, sites: int) -> None:
    if [r.get("round") for r in rows] != list(range(1, want_rounds + 1)):
        fail(f"{name}: rounds logged {[r.get('round') for r in rows]}, expected 1..{want_rounds}")
    for r in rows:
        n = r["round"]
        if r.get("aggregated") is False:
            fail(f"{name} round {n}: not aggregated ({r.get('skipped')})")
        if r.get("nodes_replied") != sites:
            fail(f"{name} round {n}: {r.get('nodes_replied')} of {sites} sites replied")
        acc = r.get("global_acc")
        if not isinstance(acc, (int, float)) or not math.isfinite(acc):
            fail(f"{name} round {n}: global accuracy {acc!r}")
    print(f"   {name}: {want_rounds} rounds, {sites}/{sites} replies each, accuracy "
          + " / ".join(f"{r['global_acc']:.4f}" for r in rows))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, required=True)
    ap.add_argument("--sites", type=int, required=True)
    ap.add_argument("plain", type=Path)
    ap.add_argument("secagg", type=Path)
    a = ap.parse_args()

    plain, secagg = rounds(a.plain), rounds(a.secagg)
    check_run("plain", plain, a.rounds, a.sites)
    check_run("secagg", secagg, a.rounds, a.sites)

    worst_acc = max(abs(p["global_acc"] - s["global_acc"]) for p, s in zip(plain, secagg))
    if worst_acc > MAX_ACC_DIFF:
        fail(f"SecAgg+ accuracy differs from plain by {worst_acc:.4f} (allowed {MAX_ACC_DIFF})")
    diff = param_diff(a.plain / "final_exchanged.pt", a.secagg / "final_exchanged.pt")
    if diff > MAX_PARAM_DIFF:
        fail(f"SecAgg+ parameters differ from plain by up to {diff:.2e} (allowed {MAX_PARAM_DIFF:.0e})")
    print(f"   SecAgg+ = plain up to quantisation: parameters within {diff:.1e}, "
          f"accuracy within {worst_acc:.4f}")

    for r in secagg:
        for site in r.get("sites") or []:
            leaked = [k for k in PRIVATE if site.get(k) is not None]
            if leaked:
                fail(f"secagg round {r['round']}: site {site.get('site')} reported {leaked} in the clear")
    print("   no per-site loss or update norm under SecAgg+")
    print("ok")


if __name__ == "__main__":
    main()
