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
from flwr.client.mod import secaggplus_mod
from flwr.common import Code, FitRes, Status, ndarrays_to_parameters, parameters_to_ndarrays
from flwr.common.secure_aggregation.secaggplus_constants import RECORD_KEY_CONFIGS, Key, Stage
from flwr.compat.common import recorddict_compat as compat

from fedfleet.compress import clip_update, compress, update_norm
from fedfleet.data import site_loaders
from fedfleet.model import build_model, exchanged_module
from fedfleet.task import evaluate, pick_device, train

app = ClientApp()
# Per-site metrics that describe the site's data or update; not sent under DP or SecAgg+.
PRIVATE_METRICS = ("train_loss", "update_norm", "num-examples")
torch.set_num_threads(2)


def _setup(msg: Message, context: Context):
    cfg, head_only, model, part, trainloader, testloader = _build(context)
    part.load_state_dict(msg.content["arrays"].to_torch_state_dict())
    return cfg, head_only, model, part, trainloader, testloader


def _build(context: Context):
    """Model, the exchanged part of it, and this site's data loaders (no weights loaded)."""
    cfg = context.run_config
    head_only = bool(cfg["head-only"])
    model = build_model(int(cfg["num-classes"]), str(cfg["backbone-path"]))
    part = exchanged_module(model, head_only)
    trainloader, testloader = site_loaders(
        partition_id=int(context.node_config["partition-id"]),
        num_partitions=int(context.node_config["num-partitions"]),
        data_path=str(cfg["data-path"]),
        num_classes=int(cfg["num-classes"]),
        alpha=float(cfg["dirichlet-alpha"]),
        batch_size=int(cfg["batch-size"]),
    )
    return cfg, head_only, model, part, trainloader, testloader


def _reseed(cfg, msg: Message, context: Context, server_round: int | None = None) -> None:
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
    if server_round is None:
        server_round = int(msg.content["config"]["server-round"])
    torch.manual_seed(seed * 1_000_003 + site * 1_009 + server_round)


def _secagg_train(msg: Message, context: Context) -> Message:
    """Training step of a SecAgg+ round, in the legacy FitIns/FitRes format SecAgg+ expects.

    Same training as train_fn; the reply carries the trained weights, the sample count and
    the metrics. secaggplus_mod then weights, quantises and masks the weights before anything
    leaves the site, so the aggregator only ever sees the masked vector.
    """
    t_setup = time.perf_counter()
    cfg, head_only, model, part, trainloader, _ = _build(context)
    fitins = compat.recorddict_to_fitins(msg.content, keep_input=True)
    received = parameters_to_ndarrays(fitins.parameters)
    keys = list(part.state_dict().keys())
    like = part.state_dict()
    part.load_state_dict({k: torch.from_numpy(np.asarray(a).astype(like[k].numpy().dtype))
                          for k, a in zip(keys, received, strict=True)})
    setup_seconds = time.perf_counter() - t_setup
    _reseed(cfg, msg, context, int(fitins.config.get("server-round", 0)))
    t0 = time.perf_counter()
    # The training loss is not reported under SecAgg+ (it would leave the site unmasked).
    train(model, trainloader, int(cfg["local-epochs"]), float(cfg["lr"]),
          head_only, pick_device())
    model.to("cpu")
    trained = [part.state_dict()[k].detach().numpy() for k in keys]
    norm = float(np.sqrt(sum(float(np.sum((t.astype(np.float64) - r.astype(np.float64)) ** 2))
                             for t, r in zip(trained, received, strict=True))))
    dp = bool(cfg.get("dp", False))
    clipped = False
    if dp:
        # Central DP with client-side clipping, before secaggplus_mod masks anything.
        clip = float(cfg["dp-clip-norm"])
        if norm > clip:
            scale = clip / norm
            trained = [(r.astype(np.float64) + (t.astype(np.float64) - r.astype(np.float64)) * scale).astype(t.dtype)
                       for t, r in zip(trained, received, strict=True)]
            clipped = True
    samples = len(trainloader.dataset)
    # Only what describes the round, not this site's data or model: under SecAgg+ the
    # aggregator must not learn anything about one site's contribution, and metrics travel
    # unmasked. Loss, update norm and sample count therefore stay here (PRIVATE_METRICS).
    metrics = {
        "train_seconds": time.perf_counter() - t0,
        "setup_seconds": setup_seconds,
        "site": int(context.node_config["partition-id"]),
        "clipped": int(clipped),
    }
    # The weight SecAgg+ applies inside the masked vector: the sample count, or 1 for every site
    # under DP (equal weights). secagg_switch hides the clear-text copy Flower sends with it.
    fitres = FitRes(Status(Code.OK, "ok"), ndarrays_to_parameters(trained), 1 if dp else samples, metrics)
    return Message(compat.fitres_to_recorddict(fitres, keep_input=True), reply_to=msg)


def _chaos_secagg_drop(msg: Message, context: Context) -> bool:
    """True if this message is the masked-vector stage that the drop drill targets."""
    run_config = getattr(context, "run_config", None) or {}
    target = str(run_config.get("chaos-secagg-drop", "") or "").strip()
    if not target:
        return False
    try:
        site, rnd = (int(x) for x in target.split(":"))
    except ValueError:
        return False                      # malformed: never inject a fault by accident
    stage = msg.content.config_records[RECORD_KEY_CONFIGS].get(Key.STAGE)
    fit_cfg = msg.content.config_records.get("fitins.config")
    this_round = int(fit_cfg.get("server-round", -1)) if fit_cfg is not None else -1
    this_site = int(context.node_config.get("partition-id", -1))
    return stage == Stage.COLLECT_MASKED_VECTORS and this_site == site and this_round == rnd


def secagg_switch(msg: Message, context: Context, call_next) -> Message:
    """Hand SecAgg+ rounds to Flower's secaggplus_mod; leave every other message alone.

    A SecAgg+ round sends four messages (setup, key sharing, masked vectors, unmasking), each
    with the protocol's config record; only the masked-vector stage trains (_secagg_train).
    """
    if RECORD_KEY_CONFIGS in msg.content.config_records:
        if _chaos_secagg_drop(msg, context):
            # Fault injection for the drop-after-key-sharing drill (run config
            # chaos-secagg-drop = "<site>:<round>"; empty = off): this site has already sent its
            # key shares and now fails at the masked-vector stage, exactly as a site that crashes
            # mid-round. Flower records the error as a dropout and SecAgg+ must recover.
            raise RuntimeError("chaos: site dropping out after key sharing (chaos-secagg-drop)")
        out = secaggplus_mod(msg, context, _secagg_train)
        # Flower 1.38 returns the masked-vector stage's reply with the site's FitRes beside the
        # masked vector, including num_examples in clear text. The weight is already inside
        # the masked vector, and the aggregate does not depend on this copy (every site's
        # FitRes gets the same unmasked parameters), so send 1 instead of the sample count.
        rec = out.content.metric_records.get("fitres.num_examples")
        if rec is not None and "num_examples" in rec:
            rec["num_examples"] = 1
        return out
    return call_next(msg, context)


@app.train(mods=[secagg_switch])
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
    clipped = False
    if bool(cfg.get("dp", False)):
        # Central DP with client-side clipping: bound this site's influence before sending.
        # The DP wrapper sends its clipping norm with every instruction; prefer that value.
        clip = msg.content["config"].get("clipping_norm", cfg["dp-clip-norm"])
        trained, clipped = clip_update(trained, msg.content["arrays"], float(clip))
    arrays, meta, residual_present = _compress_reply(trained, msg.content["arrays"], cfg, context)
    private = bool(cfg.get("dp", False))
    metrics = {
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
        "clipped": int(clipped),
        # Equal weight for every site; FedAvg uses it only when DP is on (see comms.py).
        "dp-weight": 1.0,
    }
    if private:
        # The DP guarantee covers the released model, not side information: keep this site's
        # loss, update norm and sample count here (as under SecAgg+).
        for key in PRIVATE_METRICS:
            metrics.pop(key, None)
    content = {"arrays": arrays, "metrics": MetricRecord(metrics)}
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
