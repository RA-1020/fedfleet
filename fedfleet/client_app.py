"""Node service (N): runs at each data-owner site.

Receives the current task head (or full model in baseline mode), trains it on local
data that never leaves the site, and sends back only the trained weights plus metrics.
"""

from __future__ import annotations

import time

import torch
import numpy as np
from flwr.app import Array, ArrayRecord, Context, Message, MetricRecord, RecordDict
from flwr.clientapp import ClientApp

from fedfleet.compress import compress, update_norm
from fedfleet.data import site_loaders
from fedfleet.model import build_model, exchanged_module
from fedfleet.task import evaluate, pick_device, train

app = ClientApp()
torch.set_num_threads(2)


def _setup(msg: Message, context: Context):
    cfg = context.run_config
    head_only = bool(cfg["head-only"])
    model = build_model(int(cfg["num-classes"]), str(cfg["backbone-path"]))
    part = exchanged_module(model, head_only)
    part.load_state_dict(msg.content["arrays"].to_torch_state_dict())
    trainloader, testloader = site_loaders(
        partition_id=int(context.node_config["partition-id"]),
        num_partitions=int(context.node_config["num-partitions"]),
        data_path=str(cfg["data-path"]),
        num_classes=int(cfg["num-classes"]),
        alpha=float(cfg["dirichlet-alpha"]),
        batch_size=int(cfg["batch-size"]),
    )
    return cfg, head_only, model, part, trainloader, testloader


def _reseed(cfg, msg: Message, context: Context) -> None:
    """Optional training seed, so repeated runs can differ.

    build_model() seeds torch with a fixed value, so every site starts every round from
    the same random state (same shuffling): with seed = -1 (default) that stays as it is,
    and identical settings give identical runs. With seed >= 0 each site reseeds here
    from the run's seed, its own site number and the round, so runs with different seeds
    shuffle differently while each one stays repeatable.
    """
    seed = int(cfg.get("seed", -1))
    if seed < 0:
        return
    site = int(context.node_config["partition-id"])
    server_round = int(msg.content["config"]["server-round"])
    torch.manual_seed(seed * 1_000_003 + site * 1_009 + server_round)


@app.train()
def train_fn(msg: Message, context: Context) -> Message:
    t_setup = time.perf_counter()
    cfg, head_only, model, part, trainloader, _ = _setup(msg, context)
    setup_seconds = time.perf_counter() - t_setup   # model build + data load + partitioning
    _reseed(cfg, msg, context)
    t0 = time.perf_counter()
    loss = train(model, trainloader, int(cfg["local-epochs"]), float(cfg["lr"]),
                 head_only, pick_device())
    model.to("cpu")
    trained = ArrayRecord(torch_state_dict=part.state_dict())
    norm = update_norm(trained, msg.content["arrays"])
    arrays, meta, residual_present = _compress_reply(trained, msg.content["arrays"], cfg, context)
    metrics = MetricRecord({
        "train_loss": loss,
        "train_seconds": time.perf_counter() - t0,
        "setup_seconds": setup_seconds,
        "bytes_up": arrays.count_bytes(),
        # Which site this is, so the aggregator can log per-site timing. It is the site's
        # partition number from its own node config, not anything about its data.
        "site": int(context.node_config["partition-id"]),
        "num-examples": len(trainloader.dataset),
        # 1 if this site carried an error-feedback residual into this round (shows that the
        # site's state survived between rounds); 0 otherwise.
        "ef_residual": residual_present,
        # L2 norm of this site's update (trained minus received weights), before any
        # compression or clipping: the scale a differential-privacy clipping norm is set from.
        "update_norm": norm,
    })
    content = {"arrays": arrays, "metrics": metrics}
    if meta is not None:
        content["compress"] = meta
    return Message(RecordDict(content), reply_to=msg)


def _compress_reply(trained: ArrayRecord, received: ArrayRecord, cfg, context: Context):
    """Compress the update if the run asks for it (run config "compression", default none)."""
    mode = str(cfg.get("compression", "none"))
    if mode == "none":
        return trained, None, 0
    ratio = float(cfg.get("topk-ratio", 0.1))
    feedback = bool(cfg.get("error-feedback", True))
    residual = None
    if feedback and "ef_residual" in context.state:
        residual = {k: a.numpy() for k, a in context.state["ef_residual"].items()}
    arrays, meta, new_residual = compress(trained, received, mode, ratio, residual)
    if feedback:
        context.state["ef_residual"] = ArrayRecord({k: Array(np.ascontiguousarray(v)) for k, v in new_residual.items()})
    return arrays, meta, int(residual is not None)


@app.evaluate()
def evaluate_fn(msg: Message, context: Context) -> Message:
    _, _, model, _, _, testloader = _setup(msg, context)
    loss, acc = evaluate(model, testloader, pick_device())
    metrics = MetricRecord({"eval_loss": loss, "eval_acc": acc, "num-examples": len(testloader.dataset)})
    return Message(RecordDict({"metrics": metrics}), reply_to=msg)
