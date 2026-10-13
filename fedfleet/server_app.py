"""Aggregation service (Σ): coordinates rounds and averages the returned weights."""

from __future__ import annotations

from pathlib import Path

import torch
from flwr.app import ArrayRecord, Context
from flwr.serverapp import Grid, ServerApp

from flwr.app import MetricRecord
from torch.utils.data import DataLoader, Subset

from fedfleet.comms import CommsFedAvg
from fedfleet.data import public_test_set
from fedfleet.model import build_model, count_params, exchanged_module
from fedfleet.task import evaluate, pick_device

app = ServerApp()


@app.main()
def main(grid: Grid, context: Context) -> None:
    cfg = context.run_config
    head_only = bool(cfg["head-only"])
    num_rounds = int(cfg["num-server-rounds"])
    metrics_dir = str(Path(str(cfg["metrics-dir"])).expanduser())

    model = build_model(int(cfg["num-classes"]), str(cfg["backbone-path"]))
    part = exchanged_module(model, head_only)
    state = part.state_dict()

    ckpt = Path(metrics_dir) / "latest_checkpoint.pt"
    if bool(cfg["resume"]) and ckpt.exists():
        saved = torch.load(ckpt, map_location="cpu", weights_only=True)
        if saved["head_only"] == head_only:
            state = saved["state_dict"]
            print(f"[FedFleet] resuming from checkpoint of round {saved['round']}")

    print(f"[FedFleet] mode={'head-only' if head_only else 'full-model'}, "
          f"exchanging {count_params(part):,} of {count_params(model):,} parameters")

    min_nodes = int(cfg["min-nodes"])
    strategy = CommsFedAvg(
        metrics_dir=metrics_dir,
        head_only=head_only,
        fraction_train=float(cfg["fraction-train"]),
        fraction_evaluate=1.0 if bool(cfg["site-eval"]) else 0.0,
        min_train_nodes=min_nodes,
        min_evaluate_nodes=min_nodes,
        min_available_nodes=min_nodes,
    )
    # Fixed public test set, disjoint from both the sites' data and the backbone's
    # pre-training data: one consistent convergence curve whichever sites are online.
    public = public_test_set(str(cfg["data-path"]), int(cfg["num-classes"]))
    public = Subset(public, range(min(len(public), 4000)))
    public_loader = DataLoader(public, batch_size=512)

    def global_eval(server_round: int, arrays: ArrayRecord) -> MetricRecord:
        part.load_state_dict(arrays.to_torch_state_dict())
        loss, acc = evaluate(model, public_loader, pick_device())
        strategy.log_global(server_round, acc, loss)
        return MetricRecord({"global_acc": acc, "global_loss": loss})

    result = strategy.start(
        grid=grid,
        initial_arrays=ArrayRecord(torch_state_dict=state),
        num_rounds=num_rounds,
        timeout=float(cfg["round-timeout"]),
        evaluate_fn=global_eval,
    )
    torch.save(result.arrays.to_torch_state_dict(), Path(metrics_dir) / "final_exchanged.pt")
    print(f"[FedFleet] done; metrics in {metrics_dir}/rounds.jsonl")
