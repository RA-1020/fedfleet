"""Runs Flower's own round loop (Strategy.start) over a simulated three-site grid, without
PyTorch, to check the measuring strategy with and without differential privacy."""
import json
import math
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import torch  # noqa: F401
except ImportError:                       # comms.py imports torch only for checkpoints
    _stub = types.ModuleType("torch")
    _stub.Tensor = type("Tensor", (), {})   # SciPy (used by the accountant) probes torch.Tensor
    sys.modules["torch"] = _stub

from flwr.app import Array, ArrayRecord, ConfigRecord, Message, MetricRecord, RecordDict  # noqa: E402
from flwr.supercore.task_identity import TaskIdentity  # noqa: E402

from fedfleet.comms import DP_FRACTION, CommsFedAvg  # noqa: E402
from fedfleet.compress import clip_update, update_norm  # noqa: E402

TaskIdentity.run_id, TaskIdentity.node_id, TaskIdentity.task_id = 1, 1, 1
SIZES = {11: 50000, 12: 30000, 13: 20000}            # node id -> samples (unequal on purpose)
START = ArrayRecord({"w": Array(np.zeros(20, np.float32)), "b": Array(np.zeros(4, np.float32))})


def site_delta(node, rnd):
    rng = np.random.default_rng(node * 100 + rnd)
    return {"w": rng.standard_normal(20).astype(np.float32), "b": rng.standard_normal(4).astype(np.float32)}


class FakeGrid:
    """Answers train messages like three sites; clips like client_app.py when clip is set."""
    def __init__(self, clip=None):
        self.clip = clip
        self.sent_arrays = []

    def get_node_ids(self):
        return list(SIZES)

    def send_and_receive(self, messages, timeout=None):
        out = []
        for m in messages:
            got = m.content["arrays"]
            rnd = int(m.content["config"]["server-round"])
            self.sent_arrays.append(got)
            d = site_delta(m.metadata.dst_node_id, rnd)
            trained = ArrayRecord({k: Array(got[k].numpy() + d[k]) for k in got})
            if self.clip:
                trained, _ = clip_update(trained, got, self.clip)
            metrics = MetricRecord({"num-examples": SIZES[m.metadata.dst_node_id], "dp-weight": 1.0,
                                    "train_loss": 1.0, "site": m.metadata.dst_node_id - 11})
            out.append(Message(RecordDict({"arrays": trained, "metrics": metrics}), reply_to=m))
        return out


def run(dp=None, clip=None, rounds=2, sites=3):
    d = Path(tempfile.mkdtemp())
    # fraction_train as server_app.py sets it: tiny with DP on, so exactly `sites` are asked
    s = CommsFedAvg(metrics_dir=str(d), head_only=True, dp=dp, fraction_evaluate=0.0,
                    fraction_train=DP_FRACTION if dp else 1.0,
                    min_train_nodes=sites, min_evaluate_nodes=sites, min_available_nodes=sites)
    saved = []
    s._checkpoint = lambda rnd, arrays: saved.append(arrays)
    res = s.start(grid=FakeGrid(clip), initial_arrays=START, num_rounds=rounds, timeout=5,
                  evaluate_fn=lambda r, a: (s.log_global(r, 0.5, 1.0), MetricRecord({"acc": 0.5}))[1])
    rows = [json.loads(l) for l in (d / "rounds.jsonl").read_text().splitlines()]
    return res, rows, saved


def flat(a):
    return np.concatenate([a["w"].numpy(), a["b"].numpy()]).astype(np.float64)


def test_plain_fedavg_weights_by_samples():
    res, rows, saved = run(rounds=1)
    total = sum(SIZES.values())
    want = sum(np.concatenate([site_delta(n, 1)["w"], site_delta(n, 1)["b"]]) * w / total for n, w in SIZES.items())
    assert np.allclose(flat(res.arrays), want, atol=1e-5)
    assert "dp" not in rows[1] and rows[1]["nodes_replied"] == 3
    assert np.allclose(flat(saved[-1]), flat(res.arrays))


def test_dp_without_noise_is_equal_weight_average_of_clipped_updates():
    clip = 1.0
    dp = {"noise_multiplier": 0.0, "clip_norm": clip, "sites": 3, "delta": 1e-5}
    res, rows, _ = run(dp=dp, clip=clip, rounds=1)
    clipped = []
    for n in SIZES:
        d = site_delta(n, 1)
        new = ArrayRecord({k: Array(START[k].numpy() + d[k]) for k in d})
        c, was = clip_update(new, START, clip)
        assert was and math.isclose(update_norm(c, START), clip, rel_tol=1e-5)
        clipped.append(flat(c))
    assert np.allclose(flat(res.arrays), np.mean(clipped, axis=0), atol=1e-5)
    assert rows[1]["dp"]["epsilon"] is None and rows[1]["dp"]["aggregated"]


def test_dp_with_noise_reports_epsilon_and_checkpoints_noised_model():
    dp = {"noise_multiplier": 1.0, "clip_norm": 1.0, "sites": 3, "delta": 1e-5}
    res, rows, saved = run(dp=dp, clip=1.0, rounds=3)
    eps = [r["dp"]["epsilon"] for r in rows if r.get("round", 0) >= 1]
    assert len(eps) == 3 and all(e > 0 for e in eps) and eps[0] < eps[1] < eps[2], eps
    assert [r["dp"]["releases"] for r in rows if r.get("round", 0) >= 1] == [1, 2, 3]
    assert np.allclose(flat(saved[-1]), flat(res.arrays))       # checkpoint = released model
    # noise std = 1.0 * 1.0 / 3 per value: far from the clean average
    print(f"   epsilon after 1, 2, 3 rounds: {', '.join(f'{e:.2f}' for e in eps)}")


def test_dp_asks_exactly_min_nodes_when_more_are_online():
    dp = {"noise_multiplier": 1.0, "clip_norm": 1.0, "sites": 2, "delta": 1e-5}
    _, rows, _ = run(dp=dp, clip=1.0, rounds=2, sites=2)        # three sites online
    assert [r["nodes_asked"] for r in rows if r.get("round", 0) >= 1] == [2, 2]
    assert rows[-1]["dp"]["releases"] == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
