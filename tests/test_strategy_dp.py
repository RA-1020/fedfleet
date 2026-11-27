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

from flwr.app import Array, ArrayRecord, Message, MetricRecord, RecordDict  # noqa: E402
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


def run(dp=None, clip=None, rounds=2, sites=3, dp_state=None, offset=0, keep=None):
    d = Path(tempfile.mkdtemp())
    # fraction_train as server_app.py sets it: tiny with DP on, so exactly `sites` are asked
    s = CommsFedAvg(metrics_dir=str(d), head_only=True, dp=dp, dp_state=dp_state, round_offset=offset,
                    fraction_evaluate=0.0,
                    fraction_train=DP_FRACTION if dp else 1.0,
                    min_train_nodes=sites, min_evaluate_nodes=sites, min_available_nodes=sites)
    saved = []
    s._checkpoint = lambda rnd, arrays: saved.append(arrays)
    if keep is not None:
        keep.append(s)
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


def test_dp_ledger_continues_after_resume():
    dp = {"noise_multiplier": 1.0, "clip_norm": 1.0, "sites": 3, "delta": 1e-5}
    _, straight, _ = run(dp=dp, clip=1.0, rounds=3)
    first = []
    run(dp=dp, clip=1.0, rounds=2, keep=first)
    ledger = json.loads(first[0].dp_state_json())             # what the checkpoint stores
    _, resumed, _ = run(dp=dp, clip=1.0, rounds=1, dp_state=ledger, offset=2)
    last = [r for r in resumed if r.get("round", 0) >= 1][-1]
    assert last["round"] == 3 and last["dp"]["releases"] == 3
    assert math.isclose(last["dp"]["epsilon"], straight[-1]["dp"]["epsilon"], rel_tol=1e-9)
    for bad in ({**dp, "delta": 1e-6}, {**dp, "sites": 2}):
        try:
            run(dp=bad, clip=1.0, rounds=1, dp_state=ledger, offset=2, sites=bad["sites"])
            raise AssertionError("changed privacy settings were accepted")
        except ValueError:
            pass
    try:
        run(dp={**dp, "noise_multiplier": 0.0}, clip=1.0, rounds=1, dp_state=ledger, offset=2)
        raise AssertionError("noise 0 on a noisy ledger was accepted")
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_budget_counts_affordable_rounds_from_the_ledger():
    from fedfleet.comms import affordable_releases, write_budget_note
    dp = {"noise_multiplier": 0.2, "clip_norm": 1.5, "sites": 3, "delta": 1e-5}
    # epsilon after 1..6 releases on the live federation: 35.08 57.30 77.37 96.12 114.19 131.69
    assert affordable_releases(dp, None, 30.0, 6)[0] == 0
    k, now, after = affordable_releases(dp, None, 80.0, 6)
    assert (k, now) == (3, 0.0) and math.isclose(after, 77.366, rel_tol=1e-4)
    assert affordable_releases(dp, None, 1e9, 6)[0] == 6                 # capped at the rounds asked for
    # resumed after 3 rounds: the ledger counts, so a budget of 100 leaves one more round
    noisy = {"noise_multiplier": 0.2, "clip_norm": 1.5, "sites": 3, "delta": 1e-5}
    first = []
    run(dp=noisy, clip=1.0, rounds=3, keep=first)
    ledger = json.loads(first[0].dp_state_json())
    k, now, after = affordable_releases(dp, ledger, 100.0, 3)
    assert k == 1 and math.isclose(now, 77.366, rel_tol=1e-4) and math.isclose(after, 96.116, rel_tol=1e-4)
    assert affordable_releases(dp, ledger, 90.0, 3)[0] == 0
    d = Path(tempfile.mkdtemp())
    write_budget_note(str(d), 100.0, 4, 3, after, 6)
    note = json.loads((d / "dp_budget.json").read_text())
    assert note["stopped_by_budget"] and note["last_round_allowed"] == 4


def test_budget_appears_in_rows():
    dp = {"noise_multiplier": 0.2, "clip_norm": 1.0, "sites": 3, "delta": 1e-5, "max_epsilon": 80.0}
    _, rows, _ = run(dp=dp, clip=1.0, rounds=1)
    assert rows[-1]["dp"]["max_epsilon"] == 80.0


def test_flower_backstop_and_ledger_keep_max_epsilon():
    import pytest
    from flwr.serverapp.exception import PrivacyBudgetExhausted
    from fedfleet.comms import ledger_max_epsilon
    dp = {"noise_multiplier": 0.2, "clip_norm": 1.0, "sites": 3, "delta": 1e-5, "max_epsilon": 40.0}
    keep = []
    # without FedFleet's early stop, Flower's own check refuses round 2 (epsilon would be 57.3)
    with pytest.raises(PrivacyBudgetExhausted):
        run(dp=dp, clip=1.0, rounds=3, keep=keep)
    ledger = json.loads(keep[0].dp_state_json())
    assert ledger_max_epsilon(ledger) == 40.0 and ledger_max_epsilon(None) is None
    # a ledger saved with one budget cannot be continued under another (Flower checks the config)
    with pytest.raises(ValueError):
        run(dp={**dp, "max_epsilon": 80.0}, clip=1.0, rounds=1, dp_state=ledger, offset=1)


class SilentGrid(FakeGrid):
    """Like FakeGrid, but in the given rounds only the listed nodes reply (as when the
    others miss the round timeout)."""
    def __init__(self, only: dict):
        super().__init__()
        self.only = only

    def send_and_receive(self, messages, timeout=None):
        replies = super().send_and_receive(messages, timeout)
        rnd = int(messages[0].content["config"]["server-round"]) if messages else 0
        keep = self.only.get(rnd)
        return replies if keep is None else [r for r in replies if r.metadata.src_node_id in keep]


def run_silent(only, min_replies, rounds=3):
    d = Path(tempfile.mkdtemp())
    s = CommsFedAvg(metrics_dir=str(d), head_only=True, fraction_evaluate=0.0, fraction_train=1.0,
                    min_replies=min_replies, min_train_nodes=2, min_evaluate_nodes=2, min_available_nodes=2)
    saved = []
    s._checkpoint = lambda rnd, arrays: saved.append((rnd, arrays))
    res = s.start(grid=SilentGrid(only), initial_arrays=START, num_rounds=rounds, timeout=5,
                  evaluate_fn=lambda r, a: (s.log_global(r, 0.5, 1.0), MetricRecord({"acc": 0.5}))[1])
    rows = [json.loads(l) for l in (d / "rounds.jsonl").read_text().splitlines()]
    return res, [r for r in rows if "train_round_seconds" in r], saved


def test_round_with_too_few_replies_keeps_the_previous_model():
    # Round 2: only node 13 replies (as in drill delay-a, round 7). With min_replies 2 the
    # round is logged, not aggregated, not checkpointed, and round 3 starts from round 1's model.
    res, rows, saved = run_silent({2: {13}}, min_replies=2)
    assert [r["aggregated"] for r in rows] == [True, False, True]
    assert rows[1]["nodes_replied"] == 1 and "1 of the 2" in rows[1]["skipped"]
    assert [rnd for rnd, _ in saved] == [1, 3]
    total = sum(SIZES.values())
    want = sum((np.concatenate([site_delta(n, 1)["w"], site_delta(n, 1)["b"]])
                + np.concatenate([site_delta(n, 3)["w"], site_delta(n, 3)["b"]])) * w / total
               for n, w in SIZES.items())
    assert np.allclose(flat(res.arrays), want, atol=1e-5)


def test_without_the_rule_one_reply_becomes_the_global_model():
    # The behaviour seen live before the fix: FedAvg averages whatever arrives.
    res, rows, saved = run_silent({2: {13}}, min_replies=1, rounds=2)
    assert rows[1]["aggregated"] is True and "skipped" not in rows[1]
    one = flat(saved[0][1]) + np.concatenate([site_delta(13, 2)["w"], site_delta(13, 2)["b"]])
    assert np.allclose(flat(res.arrays), one, atol=1e-5)


def test_two_of_three_replies_are_still_aggregated():
    # A partition drill drops one site of three; that round is still aggregated with min 2.
    _, rows, _ = run_silent({2: {11, 12}}, min_replies=2)
    assert all(r["aggregated"] for r in rows) and rows[1]["nodes_replied"] == 2
