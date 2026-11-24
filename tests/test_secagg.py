"""Checks for fedfleet/secagg.py helpers (no federation needed; the live path is tested with
a local SuperLink and three SuperNodes, see the SecAgg+ notes)."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from flwr.app import Array, ArrayRecord, ConfigRecord, Message, MetricRecord, RecordDict  # noqa: E402
from flwr.supercore.task_identity import TaskIdentity  # noqa: E402

TaskIdentity.run_id, TaskIdentity.node_id, TaskIdentity.task_id = 1, 1, 1

torch = __import__("pytest").importorskip("torch")
from fedfleet.secagg import CountingGrid, _record_bytes, content_bytes, ndarrays_to_state  # noqa: E402


def test_record_bytes_handles_empty_lists_and_matches_flower():
    with_empty = ConfigRecord({"a": [], "b": [b"xyz", b"12"], "c": 3, "d": "hi"})
    assert _record_bytes(with_empty) == 1 + 1 + 5 + 1 + 8 + 1 + 2
    plain = ConfigRecord({"b": [b"xyz", b"12"], "c": 3, "d": "hi"})
    assert _record_bytes(plain) == plain.count_bytes()
    m = MetricRecord({"x": 1.5, "y": [1.0, 2.0]})
    assert _record_bytes(m) == m.count_bytes()


class FakeGrid:
    def __init__(self):
        self.calls = 0

    def send_and_receive(self, messages, timeout=None):
        self.calls += 1
        out = []
        for i, m in enumerate(messages):
            reply = Message(RecordDict({"c": ConfigRecord({"v": [b"x" * (10 * (i + 1))]})}), reply_to=m)
            out.append(reply)
        return out

    def get_node_ids(self):
        return [11, 12]


def test_counting_grid_counts_both_directions_and_passes_through():
    inner = FakeGrid()
    grid = CountingGrid(inner)
    msgs = [Message(RecordDict({"arrays": ArrayRecord({"w": Array(np.zeros(4, np.float32))})}),
                    dst_node_id=n, message_type="train") for n in (11, 12)]
    replies = grid.send_and_receive(msgs, timeout=5)
    assert len(replies) == 2 and inner.calls == 1 and grid.exchanges == 1
    assert grid.down == sum(content_bytes(m) for m in msgs) > 0
    assert grid.up == (1 + 10) + (1 + 20)
    assert grid.get_node_ids() == [11, 12]          # passed through
    grid.reset()
    assert grid.up == grid.down == grid.exchanges == 0 and grid.up_by_node == {}


def test_ndarrays_to_state_restores_original_dtypes():
    like = {"w": torch.zeros(3, dtype=torch.float32), "b": torch.zeros(2, dtype=torch.float32)}
    sd = ndarrays_to_state(["w", "b"], [np.ones(3, np.float64), np.full(2, 0.5)], like)
    assert all(v.dtype == torch.float32 for v in sd.values()) and float(sd["b"][0]) == 0.5


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
