"""Checks for fedfleet/compress.py with head-sized NumPy arrays (no PyTorch needed)."""
import math
import sys
from pathlib import Path

import numpy as np
from flwr.app import Array, ArrayRecord

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fedfleet.compress import compress, decode_content, decompress, update_norm  # noqa: E402
from flwr.app import MetricRecord, RecordDict  # noqa: E402

rng = np.random.default_rng(0)
# The exchanged task head (fedfleet/model.py TaskHead, 96-64-11 MLP): 6,923 float32 parameters.
SHAPES = {"net.0.weight": (64, 96), "net.0.bias": (64,), "net.2.weight": (11, 64), "net.2.bias": (11,)}
assert sum(math.prod(s) for s in SHAPES.values()) == 6923


def rec(scale=1.0):
    return ArrayRecord({k: Array((rng.standard_normal(s) * scale).astype(np.float32)) for k, s in SHAPES.items()})


OLD = rec()
NEW = ArrayRecord({k: Array(OLD[k].numpy() + rng.standard_normal(SHAPES[k]).astype(np.float32) * 0.01)
                   for k in SHAPES})


def err(a, b):
    return max(float(np.max(np.abs(a[k].numpy() - b[k].numpy()))) for k in SHAPES)


def test_none_is_identity():
    sent, meta, res = compress(NEW, OLD, "none")
    assert sent is NEW and meta["mode"] == "none" and res == {}
    assert decompress(sent, meta, OLD) is NEW and decompress(sent, None, OLD) is NEW


def test_int8_size_and_error():
    sent, meta, res = compress(NEW, OLD, "int8")
    back = decompress(sent, meta, OLD)
    assert sent.count_bytes() < NEW.count_bytes() / 3, (sent.count_bytes(), NEW.count_bytes())
    peak = max(float(np.max(np.abs(NEW[k].numpy() - OLD[k].numpy()))) for k in SHAPES)
    assert err(back, NEW) <= peak / 127 / 2 + 1e-6            # at most half a quantisation step
    for k in SHAPES:
        assert back[k].numpy().shape == SHAPES[k] and back[k].numpy().dtype == np.float32
        assert np.allclose(res[k], NEW[k].numpy() - back[k].numpy(), atol=1e-6)


def test_topk_keeps_largest():
    sent, meta, res = compress(NEW, OLD, "topk", 0.1)
    back = decompress(sent, meta, OLD)
    for k, s in SHAPES.items():
        n = math.prod(s)
        delta = (NEW[k].numpy() - OLD[k].numpy()).ravel()
        got = (back[k].numpy() - OLD[k].numpy()).ravel()
        kept = np.flatnonzero(np.abs(got) > 0)
        assert len(kept) == max(1, math.ceil(0.1 * n))
        assert np.abs(delta[kept]).min() >= np.sort(np.abs(delta))[-len(kept)] - 1e-7
        assert np.allclose(got[kept], delta[kept])
    assert sent.count_bytes() < NEW.count_bytes() / 3
    assert sent["net.0.weight::idx"].numpy().dtype == np.uint16


def test_topk_int8_smallest():
    sizes = {m: compress(NEW, OLD, m, 0.1)[0].count_bytes() for m in ("none", "int8", "topk", "topk-int8")}
    assert sizes["topk-int8"] < sizes["topk"] < sizes["int8"] < sizes["none"], sizes
    assert sizes["none"] == 28248, sizes        # the 28,248 B per update measured on 19-20 Nov
    print("   bytes:", sizes)


def test_error_feedback_carries_dropped_part():
    """Two rounds of the same update: with the residual, round 2 sends what round 1 dropped."""
    _, _, res1 = compress(NEW, OLD, "topk", 0.1)
    sent2, meta2, _ = compress(NEW, OLD, "topk", 0.1, residual=res1)
    got2 = decompress(sent2, meta2, OLD)
    sent_plain, meta_plain, _ = compress(NEW, OLD, "topk", 0.1)
    plain = decompress(sent_plain, meta_plain, OLD)
    k = "net.0.weight"
    assert not np.allclose(got2[k].numpy(), plain[k].numpy())     # residual changed what was sent


def test_bad_settings():
    for mode, ratio in (("zip", 0.1), ("topk", 0.0), ("topk", 1.5)):
        try:
            compress(NEW, OLD, mode, ratio)
        except ValueError:
            continue
        raise AssertionError((mode, ratio))


def test_decode_content_like_the_aggregator():
    """A reply as a site builds it, decoded as CommsFedAvg does before FedAvg sees it."""
    for mode in ("none", "int8", "topk-int8"):
        sent, meta, _ = compress(NEW, OLD, mode, 0.1)
        content = {"arrays": sent, "metrics": MetricRecord({"num-examples": 10})}
        if mode != "none":
            content["compress"] = meta
        rd = RecordDict(content)
        assert decode_content(rd, OLD) == mode
        assert "compress" not in rd and list(rd["arrays"].keys()) == list(SHAPES)
        assert err(rd["arrays"], decompress(sent, meta, OLD) if mode != "none" else NEW) == 0.0, mode


def test_update_norm():
    want = math.sqrt(sum(float(np.sum((NEW[k].numpy().astype(np.float64) - OLD[k].numpy()) ** 2)) for k in SHAPES))
    assert math.isclose(update_norm(NEW, OLD), want) and update_norm(OLD, OLD) == 0.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
