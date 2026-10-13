"""CommsFedAvg: FedAvg plus the measurements FedFleet is about.

For every round it records bytes sent to nodes, bytes received back, how many nodes
were asked and how many answered (dropouts), round duration and accuracy, writes one
JSON line per round, and saves a checkpoint so a restarted aggregator can resume.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path

import torch
from flwr.app import ArrayRecord, ConfigRecord, Message, MetricRecord
from flwr.serverapp import Grid
from flwr.serverapp.strategy import FedAvg


class CommsFedAvg(FedAvg):
    def __init__(self, metrics_dir: str, head_only: bool, **kwargs) -> None:
        super().__init__(**kwargs)
        self.dir = Path(metrics_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.metrics_file = self.dir / "rounds.jsonl"
        self.head_only = head_only
        self._row: dict = {}
        self._t0 = 0.0

    # ---------------------------------------------------------------- training
    def configure_train(self, server_round: int, arrays: ArrayRecord,
                        config: ConfigRecord, grid: Grid) -> Iterable[Message]:
        messages = list(super().configure_train(server_round, arrays, config, grid))
        payload = arrays.count_bytes()
        online = len(list(grid.get_node_ids()))
        self.max_seen = max(getattr(self, "max_seen", 0), online)
        self._t0 = time.perf_counter()
        self._row = {
            "round": server_round,
            "mode": "head-only" if self.head_only else "full-model",
            "nodes_online": online,
            "nodes_registered": self.max_seen,
            "nodes_asked": len(messages),
            "bytes_per_update": payload,
            "bytes_down": payload * len(messages),
        }
        return messages

    def aggregate_train(self, server_round: int, replies: Iterable[Message]):
        replies = list(replies)
        ok = [m for m in replies if not m.has_error()]
        self._row.update({
            "nodes_replied": len(ok),
            "nodes_dropped": self._row.get("nodes_asked", 0) - len(ok),
            "bytes_up": sum(m.content["arrays"].count_bytes() for m in ok),
            "train_round_seconds": round(time.perf_counter() - self._t0, 3),
        })
        arrays, metrics = super().aggregate_train(server_round, replies)
        if metrics is not None:
            self._row["train_loss"] = metrics.get("train_loss")
        if arrays is not None:
            self._checkpoint(server_round, arrays)
        return arrays, metrics

    # ---------------------------------------------------------------- evaluation
    def aggregate_evaluate(self, server_round: int, replies: Iterable[Message]) -> MetricRecord | None:
        metrics = super().aggregate_evaluate(server_round, replies)
        if metrics is not None:
            self._row["eval_acc"] = metrics.get("eval_acc")
            self._row["eval_loss"] = metrics.get("eval_loss")
        return metrics  # the row is written after the server-side evaluation (log_global)

    # ---------------------------------------------------------------- global eval
    def log_global(self, server_round: int, acc: float, loss: float) -> None:
        """Record accuracy on the fixed public test set (round 0 = before training)."""
        if server_round == 0:
            with self.metrics_file.open("a") as f:
                f.write(json.dumps({"round": 0, "global_acc": acc, "global_loss": loss,
                                    "timestamp": time.time()}) + "\n")
            print(f"[FedFleet] round 0: global_acc={acc:.4f}")
            return
        self._row["global_acc"] = acc
        self._row["global_loss"] = loss
        self._flush()

    # ---------------------------------------------------------------- helpers
    def _checkpoint(self, server_round: int, arrays: ArrayRecord) -> None:
        state = {"round": server_round, "head_only": self.head_only,
                 "state_dict": arrays.to_torch_state_dict()}
        torch.save(state, self.dir / "latest_checkpoint.pt")

    def _flush(self) -> None:
        if not self._row:
            return
        self._row["timestamp"] = time.time()
        with self.metrics_file.open("a") as f:
            f.write(json.dumps(self._row) + "\n")
        r = self._row
        fmt = lambda v: "n/a" if v is None else f"{v:.4f}"
        print(
            f"[FedFleet] round {r['round']}: {r.get('nodes_online')}/{r.get('nodes_registered')} sites online, "
            f"{r.get('nodes_replied')}/{r.get('nodes_asked')} replied, "
            f"{r['bytes_per_update']:,} B per update, {r.get('bytes_up', 0):,} B up, "
            f"{r.get('train_round_seconds')} s, global_acc={fmt(r.get('global_acc'))}, "
            f"site_acc={fmt(r.get('eval_acc'))}"
        )
        self._row = {}
