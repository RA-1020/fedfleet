"""Tiny Vision Transformer for radio spectrograms, split into backbone and task head.

The backbone (patch embedding + transformer blocks + final norm) is pre-trained once
and then frozen. Only the small task head is trained at the sites and sent over the
network, which is the communication saving FedFleet is built around.
"""

from __future__ import annotations

import torch
from torch import nn

# Input: one log-magnitude spectrogram per sample, shape (1, 32, 16).
IN_CHANNELS, IN_H, IN_W = 1, 32, 16
PATCH = 4
EMBED_DIM = 96
DEPTH = 4
HEADS = 4
MLP_RATIO = 4
HEAD_HIDDEN = 64


class Backbone(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.patch_embed = nn.Conv2d(IN_CHANNELS, EMBED_DIM, kernel_size=PATCH, stride=PATCH)
        n_tokens = (IN_H // PATCH) * (IN_W // PATCH)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, EMBED_DIM))
        self.pos_embed = nn.Parameter(torch.zeros(1, n_tokens + 1, EMBED_DIM))
        layer = nn.TransformerEncoderLayer(
            d_model=EMBED_DIM,
            nhead=HEADS,
            dim_feedforward=EMBED_DIM * MLP_RATIO,
            dropout=0.0,
            activation="gelu",
            batch_first=True,
            norm_first=True,
        )
        self.blocks = nn.TransformerEncoder(layer, num_layers=DEPTH, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(EMBED_DIM)
        nn.init.trunc_normal_(self.pos_embed, std=0.02)
        nn.init.trunc_normal_(self.cls_token, std=0.02)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.patch_embed(x).flatten(2).transpose(1, 2)        # (B, tokens, D)
        cls = self.cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat([cls, x], dim=1) + self.pos_embed
        x = self.blocks(x)
        return self.norm(x[:, 0])                                 # CLS embedding


class TaskHead(nn.Module):
    """96-64-11 MLP: the only part exchanged in head-only mode."""

    def __init__(self, num_classes: int = 11) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(EMBED_DIM, HEAD_HIDDEN), nn.GELU(), nn.Linear(HEAD_HIDDEN, num_classes)
        )

    def forward(self, z: torch.Tensor) -> torch.Tensor:
        return self.net(z)


class FedFleetNet(nn.Module):
    def __init__(self, num_classes: int = 11) -> None:
        super().__init__()
        self.backbone = Backbone()
        self.head = TaskHead(num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.head(self.backbone(x))


def build_model(num_classes: int, backbone_path: str = "", seed: int = 1234) -> FedFleetNet:
    """Create the model with an identical backbone on every node.

    With a pre-trained backbone file, every node loads the same weights. Without one,
    a fixed seed gives every node the same random backbone, which is enough to test
    the plumbing but not to get good accuracy.
    """
    torch.manual_seed(seed)
    model = FedFleetNet(num_classes)
    if backbone_path:
        state = torch.load(backbone_path, map_location="cpu", weights_only=True)
        model.backbone.load_state_dict(state)
    return model


def freeze_backbone(model: FedFleetNet) -> None:
    for p in model.backbone.parameters():
        p.requires_grad = False
    model.backbone.eval()


def exchanged_module(model: FedFleetNet, head_only: bool) -> nn.Module:
    """The part of the model that travels between nodes and the aggregator."""
    return model.head if head_only else model


def count_params(module: nn.Module) -> int:
    return sum(p.numel() for p in module.parameters())
