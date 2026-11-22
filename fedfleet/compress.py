"""Update compression for what the sites send back (WBS 3.10).

A site sends its *update* (trained weights minus the weights it received) instead of the
weights, compressed by one of:

  none        the trained weights unchanged (the Layer 1 behaviour, and the default)
  int8        every value of the update as an 8-bit integer with one float scale per tensor
              (symmetric, round to nearest): about 4x fewer bytes than float32
  topk        only the k largest-magnitude values of each tensor (k = topk-ratio of its
              size, at least 1) with their positions; the rest count as zero
  topk-int8   top-k, with the kept values as 8-bit integers

The aggregator rebuilds full weights as (weights it sent) + (decoded update) before
averaging, so FedAvg itself is unchanged. With error feedback on, each site keeps what
compression dropped and adds it to its next update, so small changes are delayed rather
than lost.

Compression and secure aggregation (Layer 3): quantised integers can pass through SecAgg+
masking, but top-k cannot, because each site keeps different positions. This is the plan's
understanding, to confirm when SecAgg+ is added.

Everything here works on NumPy arrays inside Flower ArrayRecords, so it needs no PyTorch.
"""

from __future__ import annotations

import math

import numpy as np
from flwr.app import Array, ArrayRecord, ConfigRecord

MODES = ("none", "int8", "topk", "topk-int8")
IDX = "::idx"      # suffix of the array that holds a tensor's kept positions (top-k modes)


def check_mode(mode: str, ratio: float) -> None:
    if mode not in MODES:
        raise ValueError(f"compression must be one of {MODES}, not {mode!r}")
    if mode.startswith("topk") and not 0.0 < ratio <= 1.0:
        raise ValueError(f"topk-ratio must be in (0, 1], not {ratio}")


def _index_dtype(n: int):
    return np.uint16 if n <= np.iinfo(np.uint16).max + 1 else np.uint32


def _q8(values: np.ndarray) -> tuple[np.ndarray, float]:
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    scale = peak / 127.0 if peak > 0 else 1.0
    return np.clip(np.rint(values / scale), -127, 127).astype(np.int8), scale


def compress(new: ArrayRecord, old: ArrayRecord, mode: str, ratio: float = 0.1,
             residual: dict[str, np.ndarray] | None = None
             ) -> tuple[ArrayRecord, ConfigRecord, dict[str, np.ndarray]]:
    """Encode new - old (+ residual). Returns (arrays to send, metadata, new residual)."""
    check_mode(mode, ratio)
    if mode == "none":
        return new, ConfigRecord({"mode": "none"}), {}
    out: dict[str, Array] = {}
    keys, scales, new_residual = [], [], {}
    for key, arr in new.items():
        cur = arr.numpy().astype(np.float32)
        delta = cur - old[key].numpy().astype(np.float32)
        if residual and key in residual:
            delta = delta + residual[key]
        flat = delta.ravel()
        sent = np.zeros_like(flat)                       # what the aggregator will decode
        if mode == "int8":
            q, scale = _q8(flat)
            out[key] = Array(q)
            sent = q.astype(np.float32) * scale
        else:
            k = max(1, math.ceil(ratio * flat.size))
            idx = np.sort(np.argpartition(np.abs(flat), -k)[-k:]) if k < flat.size else np.arange(flat.size)
            vals = flat[idx]
            if mode == "topk-int8":
                q, scale = _q8(vals)
                out[key] = Array(q)
                sent[idx] = q.astype(np.float32) * scale
            else:
                scale = 1.0
                out[key] = Array(vals.astype(np.float32))
                sent[idx] = vals
            out[key + IDX] = Array(idx.astype(_index_dtype(flat.size)))
        keys.append(key)
        scales.append(float(scale))
        new_residual[key] = (flat - sent).reshape(delta.shape)
    meta = ConfigRecord({"mode": mode, "keys": keys, "scales": scales})
    return ArrayRecord(out), meta, new_residual


def decompress(rec: ArrayRecord, meta: ConfigRecord | None, old: ArrayRecord) -> ArrayRecord:
    """Full weights old + decoded update, same keys and order as old. No meta = uncompressed."""
    mode = "none" if meta is None else str(meta["mode"])
    if mode == "none":
        return rec
    scales = dict(zip(meta["keys"], meta["scales"]))
    out: dict[str, Array] = {}
    for key, arr in old.items():
        base = arr.numpy()
        flat = np.zeros(base.size, dtype=np.float32)
        if key in scales:
            payload = rec[key].numpy()
            vals = payload.astype(np.float32) * (scales[key] if payload.dtype == np.int8 else 1.0)
            if key + IDX in rec:
                flat[rec[key + IDX].numpy().astype(np.int64)] = vals
            else:
                flat = vals.reshape(-1)
        out[key] = Array((base.astype(np.float32) + flat.reshape(base.shape)).astype(base.dtype))
    return ArrayRecord(out)


def decode_content(content, sent: ArrayRecord) -> str:
    """Aggregator side: replace a reply's compressed "arrays" with full weights, in place.

    content is the reply's RecordDict; sent is the ArrayRecord the aggregator sent this
    round. Removes the "compress" ConfigRecord so the reply looks like an uncompressed one
    to FedAvg. Returns the mode used.
    """
    meta = content["compress"] if "compress" in content else None
    mode = "none" if meta is None else str(meta["mode"])
    if meta is not None:
        content["arrays"] = decompress(content["arrays"], meta, sent)
        del content["compress"]
    return mode


def update_norm(new: ArrayRecord, old: ArrayRecord) -> float:
    """L2 norm of new - old over every array (the update a site sends, uncompressed)."""
    total = 0.0
    for key, arr in new.items():
        d = arr.numpy().astype(np.float64) - old[key].numpy().astype(np.float64)
        total += float(np.sum(d * d))
    return math.sqrt(total)


def clip_update(new: ArrayRecord, old: ArrayRecord, clip_norm: float) -> tuple[ArrayRecord, bool]:
    """Scale the update new - old down to L2 norm clip_norm if it is larger.

    Returns (old + clipped update, whether it was clipped). Used by the sites for central
    differential privacy, before any compression, so the norm bound holds for what is sent.
    """
    if clip_norm <= 0:
        raise ValueError("clip_norm must be positive")
    norm = update_norm(new, old)
    if norm <= clip_norm:
        return new, False
    scale = clip_norm / norm
    out = {}
    for key, arr in new.items():
        base = old[key].numpy()
        delta = arr.numpy().astype(np.float64) - base.astype(np.float64)
        out[key] = Array((base.astype(np.float64) + delta * scale).astype(arr.numpy().dtype))
    return ArrayRecord(out), True
