"""CommsFedAvg: FedAvg plus the measurements FedFleet is about.

For every round it records bytes sent to nodes, bytes received back, how many nodes
were asked and how many answered (dropouts), round duration and accuracy, writes one
JSON line per round, and saves a checkpoint so a restarted aggregator can resume.

Each round also lists every site that replied ("sites"): its sample count, its own
measured setup and training time (site clock only, so exact), and its turnaround: the
time from the aggregator creating the instruction to the site creating its reply. The
turnaround compares two machines' clocks, so it is only as exact as their clock sync.
Nodes that were asked but sent no valid reply are listed in "nodes_missing".
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from pathlib import Path

import torch
from flwr.app import ArrayRecord, ConfigRecord, Message, MetricRecord
from flwr.serverapp import Grid
from flwr.serverapp.strategy import FedAvg
from flwr.serverapp.strategy.strategy_utils import aggregate_metricrecords

# Metrics that describe one site and mean nothing when averaged across sites.
PER_SITE_ONLY = ("site",)


def aggregate_train_metrics(records, weighting_key: str) -> MetricRecord:
    """Flower's weighted average of the sites' metrics, minus the per-site-only keys."""
    metrics = aggregate_metricrecords(records, weighting_key)
    for key in PER_SITE_ONLY:
        if key in metrics:
            del metrics[key]
    return metrics


def _seconds(value) -> float | None:
    return None if value is None else round(float(value), 3)


class CommsFedAvg(FedAvg):
    def __init__(self, metrics_dir: str, head_only: bool, round_offset: int = 0, **kwargs) -> None:
        kwargs.setdefault("train_metrics_aggr_fn", aggregate_train_metrics)
        super().__init__(**kwargs)
        # Rounds already done before a resume. Flower always counts from 1, so every round
        # number written to the metrics file and the checkpoint adds this offset.
        self.round_offset = round_offset
        self.dir = Path(metrics_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.metrics_file = self.dir / "rounds.jsonl"
        self.head_only = head_only
        self._row: dict = {}
        self._t0 = 0.0
        self._sent: dict[int, float] = {}   # node id -> when its instruction was created

    # ---------------------------------------------------------------- training
    def configure_train(self, server_round: int, arrays: ArrayRecord,
                        config: ConfigRecord, grid: Grid) -> Iterable[Message]:
        messages = list(super().configure_train(server_round, arrays, config, grid))
        payload = arrays.count_bytes()
        online = len(list(grid.get_node_ids()))
        self.max_seen = max(getattr(self, "max_seen", 0), online)
        self._sent = {m.metadata.dst_node_id: m.metadata.created_at for m in messages}
        self._t0 = time.perf_counter()
        self._row = {
            "round": server_round + self.round_offset,
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
            "sites": self._site_rows(ok),
            "nodes_missing": sorted(set(self._sent) - {m.metadata.src_node_id for m in ok}),
        })
        # Errors the SuperLink creates itself (for example "Node Unavailable" when a site
        # stops sending heartbeats) come from the SuperLink's own id, not the failed site's,
        # so only a sender that was actually asked is named; the failed site is always the
        # one listed in nodes_missing.
        errors = [{"node": m.metadata.src_node_id if m.metadata.src_node_id in self._sent else None,
                   "code": int(m.error.code), "reason": str(m.error.reason)[:200]}
                  for m in replies if m.has_error()]
        if errors:
            self._row["errors"] = errors
        arrays, metrics = super().aggregate_train(server_round, replies)
        if metrics is not None:
            self._row["train_loss"] = metrics.get("train_loss")
        if arrays is not None:
            self._checkpoint(server_round + self.round_offset, arrays)
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
            if self.round_offset:
                # After a resume this is the restored model, not a new round: log it as an
                # event without a "round" key so it can never be mistaken for a training round.
                row = {"event": "resumed", "resumed_from_round": self.round_offset,
                       "global_acc": acc, "global_loss": loss, "timestamp": time.time()}
                print(f"[FedFleet] resumed from round {self.round_offset}: global_acc={acc:.4f}")
            else:
                row = {"round": 0, "global_acc": acc, "global_loss": loss,
                       "timestamp": time.time()}
                print(f"[FedFleet] round 0: global_acc={acc:.4f}")
            with self.metrics_file.open("a") as f:
                f.write(json.dumps(row) + "\n")
            return
        self._row["global_acc"] = acc
        self._row["global_loss"] = loss
        self._flush()

    # ---------------------------------------------------------------- helpers
    def _site_rows(self, ok: list[Message]) -> list[dict]:
        rows = []
        for m in ok:
            rec = next(iter(m.content.metric_records.values()), None) or {}
            node = m.metadata.src_node_id
            sent = self._sent.get(node)
            site = rec.get("site")
            rows.append({
                "site": None if site is None else int(site),
                "node": node,
                "num_examples": None if rec.get("num-examples") is None else int(rec["num-examples"]),
                "train_loss": rec.get("train_loss"),
                "setup_seconds": _seconds(rec.get("setup_seconds")),
                "train_seconds": _seconds(rec.get("train_seconds")),
                "turnaround_seconds": None if sent is None else round(m.metadata.created_at - sent, 3),
            })
        rows.sort(key=lambda r: (r["site"] is None, r["site"] if r["site"] is not None else 0, r["node"]))
        return rows

    def _checkpoint(self, server_round: int, arrays: ArrayRecord) -> None:
        state = {"round": server_round, "head_only": self.head_only,
                 "state_dict": arrays.to_torch_state_dict()}
        # Write to a temporary file, then rename: a crash in the middle of saving can
        # never leave a half-written latest_checkpoint.pt behind.
        tmp = self.dir / "latest_checkpoint.pt.tmp"
        torch.save(state, tmp)
        os.replace(tmp, self.dir / "latest_checkpoint.pt")

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
        for s in r.get("sites", []):
            name = f"site {s['site']}" if s["site"] is not None else f"node {s['node']}"
            n = f"{s['num_examples']:,}" if s["num_examples"] is not None else "?"
            print(f"[FedFleet]   {name}: {n} samples, setup {s['setup_seconds']} s, "
                  f"train {s['train_seconds']} s, turnaround {s['turnaround_seconds']} s")
        for node in r.get("nodes_missing", []):
            print(f"[FedFleet]   no reply from node {node}")
        self._row = {}
