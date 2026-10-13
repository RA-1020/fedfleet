"""Pre-train the backbone once on the public pre-training split, then freeze it.

Runs on Apple Silicon (MPS), CUDA (Kaggle) or CPU. The hold-out split is never part of
any site's data, so this does not leak private data into the shared backbone.

    python scripts/pretrain_backbone.py --data-path data/RML2016.10a_dict.pkl --epochs 15
"""

from __future__ import annotations

import argparse

import torch
from torch.utils.data import DataLoader, random_split

from fedfleet.data import pretrain_set
from fedfleet.model import build_model, count_params
from fedfleet.task import evaluate, pick_device, train


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-path", default="", help="RML2016.10a_dict.pkl (empty = synthetic)")
    ap.add_argument("--num-classes", type=int, default=11)
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--out", default="backbone.pt")
    args = ap.parse_args()

    data = pretrain_set(args.data_path, args.num_classes)
    n_val = max(1, int(0.1 * len(data)))
    tr, va = random_split(data, [len(data) - n_val, n_val], generator=torch.Generator().manual_seed(0))
    trl = DataLoader(tr, batch_size=args.batch_size, shuffle=True)
    val = DataLoader(va, batch_size=512)

    device = pick_device()
    model = build_model(args.num_classes)
    print(f"device={device}, params={count_params(model):,}, hold-out samples={len(data):,}")
    for epoch in range(1, args.epochs + 1):
        loss = train(model, trl, epochs=1, lr=args.lr, head_only=False, device=device)
        vloss, vacc = evaluate(model, val, device)
        print(f"epoch {epoch:2d}: train_loss={loss:.4f} val_loss={vloss:.4f} val_acc={vacc:.4f}")
    torch.save(model.backbone.to("cpu").state_dict(), args.out)
    print(f"saved frozen backbone to {args.out}")


if __name__ == "__main__":
    main()
