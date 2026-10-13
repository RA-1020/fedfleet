"""Node service (N): runs at each data-owner site.

Receives the current task head (or full model in baseline mode), trains it on local
data that never leaves the site, and sends back only the trained weights plus metrics.
"""

from __future__ import annotations

import time

import torch
from flwr.app import ArrayRecord, Context, Message, MetricRecord, RecordDict
from flwr.clientapp import ClientApp

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


@app.train()
def train_fn(msg: Message, context: Context) -> Message:
    cfg, head_only, model, part, trainloader, _ = _setup(msg, context)
    t0 = time.perf_counter()
    loss = train(model, trainloader, int(cfg["local-epochs"]), float(cfg["lr"]),
                 head_only, pick_device())
    model.to("cpu")
    arrays = ArrayRecord(torch_state_dict=part.state_dict())
    metrics = MetricRecord({
        "train_loss": loss,
        "train_seconds": time.perf_counter() - t0,
        "bytes_up": arrays.count_bytes(),
        "num-examples": len(trainloader.dataset),
    })
    return Message(RecordDict({"arrays": arrays, "metrics": metrics}), reply_to=msg)


@app.evaluate()
def evaluate_fn(msg: Message, context: Context) -> Message:
    _, _, model, _, _, testloader = _setup(msg, context)
    loss, acc = evaluate(model, testloader, pick_device())
    metrics = MetricRecord({"eval_loss": loss, "eval_acc": acc, "num-examples": len(testloader.dataset)})
    return Message(RecordDict({"metrics": metrics}), reply_to=msg)
