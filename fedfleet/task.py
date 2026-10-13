"""Local training and evaluation, run at each site."""

from __future__ import annotations

import torch
from torch import nn
from torch.utils.data import DataLoader

from fedfleet.model import FedFleetNet, freeze_backbone


def pick_device() -> torch.device:
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def train(model: FedFleetNet, loader: DataLoader, epochs: int, lr: float,
          head_only: bool, device: torch.device) -> float:
    model.to(device)
    if head_only:
        freeze_backbone(model)
        params = model.head.parameters()
        model.head.train()
    else:
        params = model.parameters()
        model.train()
    opt = torch.optim.Adam(params, lr=lr)
    loss_fn = nn.CrossEntropyLoss()
    total, count = 0.0, 0
    for _ in range(epochs):
        for x, y in loader:
            x, y = x.to(device), y.to(device)
            opt.zero_grad()
            loss = loss_fn(model(x), y)
            loss.backward()
            opt.step()
            total += loss.item() * len(y)
            count += len(y)
    return total / max(count, 1)


@torch.no_grad()
def evaluate(model: FedFleetNet, loader: DataLoader, device: torch.device) -> tuple[float, float]:
    model.to(device).eval()
    loss_fn = nn.CrossEntropyLoss(reduction="sum")
    loss, correct, n = 0.0, 0, 0
    for x, y in loader:
        x, y = x.to(device), y.to(device)
        out = model(x)
        loss += loss_fn(out, y).item()
        correct += (out.argmax(1) == y).sum().item()
        n += len(y)
    return loss / max(n, 1), correct / max(n, 1)
