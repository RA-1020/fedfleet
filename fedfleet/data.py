"""Data for FedFleet: RadioML 2016.10a as spectrograms, split across sites.

If no RadioML file is given, a synthetic dataset with the same shapes is generated, so
the whole pipeline can be tested before the real dataset is downloaded.

Split, decided once with a fixed seed:
  * 20% "public" hold-out, split again into two disjoint parts:
      - 75% to pre-train the backbone (scripts/pretrain_backbone.py)
      - 25% as the server's fixed test set for the convergence curve
  * 80% private: divided between sites with a Dirichlet label split (non-IID);
    each site keeps 80% for training and 20% for local evaluation.
"""

from __future__ import annotations

import os
import pickle

import numpy as np
import torch
from torch.utils.data import DataLoader, TensorDataset

SEED = 2026
HOLDOUT_FRACTION = 0.2
_CACHE: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}


# ---------------------------------------------------------------- spectrograms
def iq_to_spectrogram(iq: np.ndarray) -> torch.Tensor:
    """(N, 2, 128) I/Q samples -> (N, 1, 32, 16) normalised log-magnitude spectrograms."""
    x = torch.from_numpy(iq[:, 0, :] + 1j * iq[:, 1, :]).to(torch.complex64)
    spec = torch.stft(
        x, n_fft=32, hop_length=8, win_length=32,
        window=torch.hann_window(32), center=True, return_complex=True, onesided=False,
    )                                                    # (N, 32, 17)
    mag = torch.log1p(spec.abs())[:, :, :16]             # (N, 32, 16)
    mean = mag.mean(dim=(1, 2), keepdim=True)
    std = mag.std(dim=(1, 2), keepdim=True) + 1e-6
    return ((mag - mean) / std).unsqueeze(1).float()


# ---------------------------------------------------------------- sources
def _load_radioml(path: str) -> tuple[np.ndarray, np.ndarray]:
    with open(path, "rb") as f:
        raw = pickle.load(f, encoding="latin1")
    mods = sorted({mod for mod, _ in raw})
    xs, ys = [], []
    for (mod, _snr), samples in raw.items():
        xs.append(samples)
        ys.append(np.full(len(samples), mods.index(mod), dtype=np.int64))
    return np.concatenate(xs), np.concatenate(ys)


def _synthetic(num_classes: int, per_class: int = 600) -> tuple[np.ndarray, np.ndarray]:
    """Noisy complex tones; each class sits at its own frequency. Plumbing tests only."""
    rng = np.random.default_rng(SEED)
    t = np.arange(128)
    xs, ys = [], []
    for c in range(num_classes):
        freq = (c + 1) / (2.5 * num_classes)
        phase = rng.uniform(0, 2 * np.pi, size=(per_class, 1))
        sig = np.exp(1j * (2 * np.pi * freq * t + phase))
        sig = sig + 0.8 * (rng.standard_normal((per_class, 128)) + 1j * rng.standard_normal((per_class, 128)))
        xs.append(np.stack([sig.real, sig.imag], axis=1).astype(np.float32))
        ys.append(np.full(per_class, c, dtype=np.int64))
    return np.concatenate(xs), np.concatenate(ys)


def load_all(data_path: str, num_classes: int) -> tuple[torch.Tensor, torch.Tensor]:
    key = data_path or f"synthetic-{num_classes}"
    if key not in _CACHE:
        if data_path:
            if not os.path.exists(data_path):
                raise FileNotFoundError(f"RadioML file not found: {data_path}")
            iq, y = _load_radioml(data_path)
        else:
            iq, y = _synthetic(num_classes)
        x = torch.cat([iq_to_spectrogram(iq[i : i + 8192]) for i in range(0, len(iq), 8192)])
        _CACHE[key] = (x, torch.from_numpy(y))
    return _CACHE[key]


# ---------------------------------------------------------------- splits
def _split_indices(n: int) -> tuple[np.ndarray, np.ndarray]:
    perm = np.random.default_rng(SEED).permutation(n)
    cut = int(n * HOLDOUT_FRACTION)
    return perm[:cut], perm[cut:]


def _holdout_parts(n: int) -> tuple[np.ndarray, np.ndarray]:
    hold, _ = _split_indices(n)
    cut = int(len(hold) * 0.75)
    return hold[:cut], hold[cut:]


def pretrain_set(data_path: str, num_classes: int) -> TensorDataset:
    """Public data for pre-training the backbone. Never used for evaluation."""
    x, y = load_all(data_path, num_classes)
    idx, _ = _holdout_parts(len(y))
    return TensorDataset(x[idx], y[idx])


def public_test_set(data_path: str, num_classes: int) -> TensorDataset:
    """Fixed public test set for the server's convergence curve. Never used for training."""
    x, y = load_all(data_path, num_classes)
    _, idx = _holdout_parts(len(y))
    return TensorDataset(x[idx], y[idx])


def dirichlet_partition(labels: np.ndarray, num_partitions: int, alpha: float) -> list[np.ndarray]:
    rng = np.random.default_rng(SEED + 1)
    parts: list[list[int]] = [[] for _ in range(num_partitions)]
    for c in np.unique(labels):
        idx = np.where(labels == c)[0]
        rng.shuffle(idx)
        props = rng.dirichlet([alpha] * num_partitions)
        cuts = (np.cumsum(props) * len(idx)).astype(int)[:-1]
        for p, chunk in enumerate(np.split(idx, cuts)):
            parts[p].extend(chunk.tolist())
    return [np.array(sorted(p), dtype=np.int64) for p in parts]


def site_loaders(
    partition_id: int, num_partitions: int, data_path: str, num_classes: int,
    alpha: float, batch_size: int,
) -> tuple[DataLoader, DataLoader]:
    x, y = load_all(data_path, num_classes)
    _, private = _split_indices(len(y))
    parts = dirichlet_partition(y[private].numpy(), num_partitions, alpha)
    mine = private[parts[partition_id]]
    rng = np.random.default_rng(SEED + 2 + partition_id)
    rng.shuffle(mine)
    cut = int(len(mine) * 0.8)
    train = TensorDataset(x[mine[:cut]], y[mine[:cut]])
    test = TensorDataset(x[mine[cut:]], y[mine[cut:]])
    return (
        DataLoader(train, batch_size=batch_size, shuffle=True),
        DataLoader(test, batch_size=256),
    )
