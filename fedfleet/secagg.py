"""Secure aggregation (SecAgg+) for FedFleet runs, on Flower's legacy workflow API.

With SecAgg+ the aggregator never sees one site's update: each site uploads its weighted,
quantised parameters plus pairwise and private masks, and only the sum of all sites' vectors
can be unmasked. Flower 1.38 offers SecAgg+ only as a legacy workflow (`SecAggPlusWorkflow`
under `DefaultWorkflow`, with a `LegacyContext` and a legacy strategy), so a SecAgg+ run takes
this path instead of `CommsFedAvg.start`. It still writes FedFleet's rounds.jsonl rows,
checkpoints and cost per round, by reusing a CommsFedAvg instance as the recorder.

Under SecAgg+:
- the masked vectors travel inside config records, not arrays, so bytes are counted at the
  message level (`CountingGrid`), across all four protocol stages of a round;
- SecAgg+ quantises the parameters itself (clipped to +-clipping_range), so FedFleet's
  compression is off, and DP is not combined with it yet (refused in server_app.py);
- weights are num_examples / max_weight, so max_weight must be at least the largest site's
  sample count, or the sites' weights overflow (Flower warns but carries on);
- per-site turnaround is not available (the legacy results carry no message timestamps).
"""

from __future__ import annotations

import time

import numpy as np
import torch
from flwr.app import ArrayRecord, Message
from flwr.common import ndarrays_to_parameters, parameters_to_ndarrays
from flwr.server import LegacyContext, ServerConfig
from flwr.server.strategy import FedAvg as LegacyFedAvg
from flwr.server.workflow import DefaultWorkflow, SecAggPlusWorkflow


def _value_bytes(v) -> int:
    if isinstance(v, (bytes, bytearray)):
        return len(v)
    if isinstance(v, str):
        return len(v.encode())
    if isinstance(v, (list, tuple)):
        return sum(_value_bytes(x) for x in v)
    if isinstance(v, bool):
        return 1
    return 8   # int or float


def _record_bytes(record) -> int:
    """Keys plus values, like Flower's count_bytes, which fails on empty lists (Flower 1.38),
    and SecAgg+ sends empty lists in its unmask stage when no site dropped out."""
    return sum(len(str(k).encode()) + _value_bytes(v) for k, v in record.items())


def content_bytes(msg: Message) -> int:
    """Bytes of a message's records (arrays, configs, metrics): what the protocol moves."""
    c = msg.content
    return (sum(r.count_bytes() for r in c.array_records.values())
            + sum(_record_bytes(r) for r in c.config_records.values())
            + sum(_record_bytes(r) for r in c.metric_records.values()))


class CountingGrid:
    """Passes everything through to Flower's grid and counts the bytes of every message.

    `down` = bytes sent to sites, `up` = bytes received from them (replies without an error),
    `up_by_node` = the same per sending node. The recorder resets the counts at the start of
    each round, so they cover all SecAgg+ stages of that round.
    """

    def __init__(self, grid) -> None:
        self._grid = grid
        self.reset()

    def reset(self) -> None:
        self.down = 0
        self.up = 0
        self.up_by_node: dict[int, int] = {}
        self.exchanges = 0

    def send_and_receive(self, messages, *args, **kwargs):
        messages = list(messages)
        self.down += sum(content_bytes(m) for m in messages)
        self.exchanges += 1
        replies = list(self._grid.send_and_receive(messages, *args, **kwargs))
        for m in replies:
            if not m.has_error():
                n = content_bytes(m)
                self.up += n
                node = m.metadata.src_node_id
                self.up_by_node[node] = self.up_by_node.get(node, 0) + n
        return replies

    def __getattr__(self, name):   # get_node_ids, run, ...
        return getattr(self._grid, name)


def state_to_ndarrays(state: dict) -> tuple[list[str], list[np.ndarray]]:
    keys = list(state.keys())
    return keys, [state[k].detach().cpu().numpy() for k in keys]


def ndarrays_to_state(keys: list[str], arrays: list[np.ndarray], like: dict) -> dict:
    """Back to a state dict, in the original keys' dtypes (SecAgg+ dequantises to float64)."""
    return {k: torch.from_numpy(np.asarray(a).astype(like[k].detach().cpu().numpy().dtype))
            for k, a in zip(keys, arrays, strict=True)}


class SecAggFedAvg(LegacyFedAvg):
    """Legacy FedAvg that writes FedFleet's round rows through a CommsFedAvg recorder."""

    def __init__(self, recorder, grid: CountingGrid, keys: list[str], like: dict,
                 secagg_info: dict, round_offset: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rec = recorder
        self.grid = grid
        self.keys = keys
        self.like = like
        self.info = secagg_info
        self.offset = round_offset
        self._asked: list[int] = []
        self._t0 = 0.0

    def configure_fit(self, server_round, parameters, client_manager):
        pairs = super().configure_fit(server_round, parameters, client_manager)
        self.grid.reset()
        self._asked = [int(proxy.cid) for proxy, _ in pairs]
        self._t0 = time.perf_counter()
        payload = sum(a.nbytes for a in parameters_to_ndarrays(parameters))
        online = client_manager.num_available()
        self.rec.max_seen = max(getattr(self.rec, "max_seen", 0), online)
        self.rec._row = {
            "round": server_round + self.offset,
            "mode": "head-only" if self.rec.head_only else "full-model",
            "compression": "none",
            "nodes_online": online,
            "nodes_registered": self.rec.max_seen,
            "nodes_asked": len(pairs),
            "bytes_per_update": payload,
            "secagg": dict(self.info, aggregated=False),
        }
        return pairs

    def aggregate_fit(self, server_round, results, failures):
        params, metrics = super().aggregate_fit(server_round, results, failures)
        if params is not None:
            # SecAgg+ dequantises to float64; send the next round in the model's own dtypes
            # (float32), or every later download doubles in size.
            state = ndarrays_to_state(self.keys, parameters_to_ndarrays(params), self.like)
            params = ndarrays_to_parameters([state[k].numpy() for k in self.keys])
        replied = [int(proxy.cid) for proxy, _ in results]
        row = self.rec._row
        row.update({
            "nodes_replied": len(results),
            "nodes_dropped": len(self._asked) - len(results),
            # Every byte of the round's protocol messages (setup, key sharing, masked vectors,
            # unmasking), counted at the message level.
            "bytes_down": self.grid.down,
            "bytes_up": self.grid.up,
            "train_round_seconds": round(time.perf_counter() - self._t0, 3),
            "nodes_missing": sorted(set(self._asked) - set(replied)),
            "sites": self._site_rows(results),
        })
        row["secagg"].update({"aggregated": params is not None, "exchanges": self.grid.exchanges})
        if failures:
            row["errors"] = [{"node": None, "code": None, "reason": str(f)[:200]} for f in failures]
        losses = [(fr.num_examples, fr.metrics.get("train_loss")) for _, fr in results
                  if fr.metrics.get("train_loss") is not None]
        if losses:
            row["train_loss"] = sum(n * l for n, l in losses) / sum(n for n, _ in losses)
        if params is not None:
            self.rec._checkpoint(server_round + self.offset, ArrayRecord(torch_state_dict=state))
        return params, metrics

    def _site_rows(self, results) -> list[dict]:
        rows = []
        for proxy, fr in results:
            m = fr.metrics
            node = int(proxy.cid)
            rows.append({
                "site": None if m.get("site") is None else int(m["site"]),
                "node": node,
                "num_examples": int(fr.num_examples),
                "train_loss": m.get("train_loss"),
                "setup_seconds": None if m.get("setup_seconds") is None else round(float(m["setup_seconds"]), 3),
                "train_seconds": None if m.get("train_seconds") is None else round(float(m["train_seconds"]), 3),
                # This site's protocol bytes in the round (all stages), so customer egress
                # in the cost model reflects what SecAgg+ really sends.
                "bytes_up": self.grid.up_by_node.get(node),
                "update_norm": None if m.get("update_norm") is None else round(float(m["update_norm"]), 6),
                "turnaround_seconds": None,
            })
        rows.sort(key=lambda r: (r["site"] is None, r["site"] if r["site"] is not None else 0, r["node"]))
        return rows


def run_secagg(grid, context, *, recorder, state: dict, num_rounds: int, round_offset: int,
               min_nodes: int, fraction_train: float, timeout: float, evaluate_global,
               max_weight: float, clipping_range: float) -> dict:
    """Run `num_rounds` SecAgg+ rounds; returns the final exchanged state dict.

    evaluate_global(server_round, state_dict) -> (loss, acc) on the public test set; its
    result is written through the recorder exactly as in the plain path (round 0 row or
    resumed event, then one row per round).
    """
    keys, arrays = state_to_ndarrays(state)
    counting = CountingGrid(grid)
    info = {"num_shares": "all selected", "reconstruction_threshold": "2/3 of shares, at least 2",
            "max_weight": max_weight, "clipping_range": clipping_range}
    final: dict = {"state": state}

    def evaluate_fn(server_round, ndarrays, config):
        sd = ndarrays_to_state(keys, ndarrays, state)
        final["state"] = sd
        loss, acc = evaluate_global(server_round, sd)
        recorder.log_global(server_round, acc, loss)
        return loss, {"global_acc": acc}

    strategy = SecAggFedAvg(
        recorder, counting, keys, state, info, round_offset,
        fraction_fit=fraction_train, fraction_evaluate=0.0,
        min_fit_clients=min_nodes, min_evaluate_clients=min_nodes, min_available_clients=min_nodes,
        initial_parameters=ndarrays_to_parameters(arrays),
        evaluate_fn=evaluate_fn,
        fit_metrics_aggregation_fn=lambda metrics: {},   # FedFleet's row carries per-site metrics
        on_fit_config_fn=lambda r: {"server-round": r + round_offset},
    )
    legacy = LegacyContext(context=context, config=ServerConfig(num_rounds=num_rounds, round_timeout=timeout),
                           strategy=strategy)
    workflow = DefaultWorkflow(fit_workflow=SecAggPlusWorkflow(
        num_shares=1.0, reconstruction_threshold=2 / 3, max_weight=max_weight,
        clipping_range=clipping_range, timeout=timeout))
    workflow(counting, legacy)
    return final["state"]
