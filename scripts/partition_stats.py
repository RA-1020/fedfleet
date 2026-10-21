"""Label counts per site, computed offline with the exact split the sites use.

Each site trains on its own Dirichlet share of the private data (fedfleet/data.py).
This script rebuilds every share on one machine and counts the labels, so the skew
between sites can be checked without sending any label information over the network.
The training counts must equal the "num_examples" each site reports in rounds.jsonl.

    python scripts/partition_stats.py --data-path ~/fedfleet/data/RML2016.10a_dict.pkl --num-partitions 3
    python scripts/partition_stats.py --num-partitions 2             # synthetic data
    python scripts/partition_stats.py ... --out logs/partitions-3.json

Class numbers follow fedfleet/data.py: the modulation names from the RadioML file,
sorted alphabetically.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import torch
from fedfleet.data import public_test_set, site_loaders


def _counts(labels: torch.Tensor, num_classes: int) -> list[int]:
    return np.bincount(labels.numpy(), minlength=num_classes).tolist()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-path", default="", help="RadioML pickle; empty = synthetic data")
    ap.add_argument("--num-partitions", type=int, required=True)
    ap.add_argument("--num-classes", type=int, default=11)
    ap.add_argument("--alpha", type=float, default=0.5, help="dirichlet-alpha of the run")
    ap.add_argument("--public-data-path", default="",
                    help="the aggregator's public test file, to compare its label mix")
    ap.add_argument("--out", default="", help="also write the counts to this JSON file")
    args = ap.parse_args()

    k = args.num_classes
    sites = []
    for i in range(args.num_partitions):
        train, test = site_loaders(i, args.num_partitions, args.data_path, k, args.alpha, 64)
        tr = _counts(train.dataset.tensors[1], k)
        te = _counts(test.dataset.tensors[1], k)
        n = sum(tr)
        shares = [c / n if n else 0.0 for c in tr]
        sites.append({
            "site": i, "train_examples": n, "test_examples": sum(te),
            "train_counts": tr, "test_counts": te,
            "largest_class": int(np.argmax(tr)), "largest_share": round(max(shares), 4),
            "classes_under_2pct": [c for c, s in enumerate(shares) if s < 0.02],
        })

    # The aggregator scores on the first 4000 samples of the public test split.
    ds = public_test_set(args.data_path, k, args.public_data_path)
    public = _counts(ds.tensors[1][: min(len(ds), 4000)], k)

    width = 9
    head = "class " + "".join(f"{'site ' + str(s['site']):>{width}}" for s in sites)
    if public is not None:
        head += f"{'public':>{width}}"
    print(f"Training samples per class (num-partitions={args.num_partitions}, alpha={args.alpha})")
    print(head)
    for c in range(k):
        line = f"{c:>5} " + "".join(f"{s['train_counts'][c]:>{width},}" for s in sites)
        if public is not None:
            line += f"{public[c]:>{width},}"
        print(line)
    total = "total " + "".join(f"{s['train_examples']:>{width},}" for s in sites)
    if public is not None:
        total += f"{sum(public):>{width},}"
    print(total)
    print()
    for s in sites:
        print(f"site {s['site']}: {s['train_examples']:,} train / {s['test_examples']:,} local test; "
              f"largest class {s['largest_class']} = {s['largest_share']:.1%}; "
              f"classes under 2%: {s['classes_under_2pct'] or 'none'}")

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump({"num_partitions": args.num_partitions, "alpha": args.alpha,
                       "data_path": args.data_path, "sites": sites, "public_test": public}, f, indent=1)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
