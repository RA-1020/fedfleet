"""Write the server's public test split to a small file, separate from all site data.

The aggregator only needs the public test split (5% of the data) to draw the
convergence curve. Giving it this file instead of the full dataset keeps every site's
raw data off the aggregator, which is the point of federated learning.

    python scripts/export_public_test.py --data-path ~/fedfleet/data/RML2016.10a_dict.pkl
    rsync -av data/radioml_public_test.pt ubuntu@fedfleet-agg:~/fedfleet/data/
"""

from __future__ import annotations

import argparse
import os

import torch

from fedfleet.data import public_test_set


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-path", required=True)
    ap.add_argument("--num-classes", type=int, default=11)
    ap.add_argument("--out", default="data/radioml_public_test.pt")
    args = ap.parse_args()
    ds = public_test_set(args.data_path, args.num_classes)
    x, y = ds.tensors
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    torch.save({"x": x, "y": y}, args.out)
    size_mb = os.path.getsize(args.out) / 1e6
    print(f"wrote {len(y):,} public test samples to {args.out} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
