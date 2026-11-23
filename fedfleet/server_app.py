"""Aggregation service (Σ): coordinates rounds and averages the returned weights."""

from __future__ import annotations

import json
from pathlib import Path

import torch
from flwr.app import ArrayRecord, Context
from flwr.serverapp import Grid, ServerApp

from flwr.app import MetricRecord
from torch.utils.data import DataLoader, Subset

from fedfleet.comms import DP_FRACTION, CommsFedAvg
from fedfleet.cost import load_rates
from fedfleet.compress import check_mode
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
    done_rounds = 0
    saved_dp: str | None = None   # privacy ledger saved with the checkpoint (DP runs with noise)
    if bool(cfg["resume"]):
        if not ckpt.exists():
            print(f"[FedFleet] resume requested but {ckpt} not found; starting from round 1")
        else:
            saved = torch.load(ckpt, map_location="cpu", weights_only=True)
            if saved["head_only"] == head_only:
                state = saved["state_dict"]
                done_rounds = int(saved["round"])
                saved_dp = saved.get("dp_accountant")
                print(f"[FedFleet] resuming from checkpoint of round {done_rounds}")
            else:
                print("[FedFleet] checkpoint was saved in the other mode (head-only or full "
                      "model); starting from round 1")
    remaining = num_rounds - done_rounds
    if remaining <= 0:
        print(f"[FedFleet] checkpoint is already at round {done_rounds} of {num_rounds}; "
              "nothing to do")
        return

    print(f"[FedFleet] mode={'head-only' if head_only else 'full-model'}, "
          f"exchanging {count_params(part):,} of {count_params(model):,} parameters")

    rates = None
    rates_path = str(cfg.get("rates-path", ""))
    if rates_path:
        if Path(rates_path).expanduser().exists():
            rates = load_rates(rates_path)
        else:
            print(f"[FedFleet] rate card {rates_path} not found; cost per round is off")

    compression = str(cfg.get("compression", "none"))
    check_mode(compression, float(cfg.get("topk-ratio", 0.1)))
    if compression != "none":
        print(f"[FedFleet] update compression: {compression}"
              + (f", top-k ratio {float(cfg.get('topk-ratio', 0.1))}" if compression.startswith("topk") else "")
              + f", error feedback {'on' if bool(cfg.get('error-feedback', True)) else 'off'}")
    min_nodes = int(cfg["min-nodes"])
    dp = None
    if bool(cfg.get("dp", False)):
        dp = {"noise_multiplier": float(cfg.get("dp-noise-multiplier", 1.0)),
              "clip_norm": float(cfg.get("dp-clip-norm", 1.5)),
              "sites": min_nodes, "delta": float(cfg.get("dp-delta", 1e-5))}
        print(f"[FedFleet] differential privacy: noise multiplier {dp['noise_multiplier']}, "
              f"clip norm {dp['clip_norm']}, {min_nodes} sites, equal weights, delta {dp['delta']:g}")
    # A resumed run keeps its privacy settings: epsilon covers every released model only if the
    # ledger continues. Refuse the combinations that would silently break that.
    noisy = dp is not None and dp["noise_multiplier"] > 0
    if done_rounds and noisy and saved_dp is None:
        print("[FedFleet] this checkpoint has no privacy ledger (saved without DP noise); "
              "resuming it with DP would report epsilon for the new rounds only. Not resuming.")
        return
    if done_rounds and saved_dp is not None and not noisy:
        print("[FedFleet] this checkpoint comes from a DP run with noise; resume it with the same "
              "dp settings (dp=true, same noise multiplier, delta and sites). Not resuming.")
        return
    if done_rounds and saved_dp is not None:
        print("[FedFleet] continuing the privacy ledger of the earlier rounds")
    strategy = CommsFedAvg(
        metrics_dir=metrics_dir,
        head_only=head_only,
        round_offset=done_rounds,
        rates=rates,
        compression=compression,
        dp=dp,
        dp_state=json.loads(saved_dp) if (saved_dp and noisy) else None,
        # With DP on, ask exactly min-nodes sites each round: the privacy accounting needs a
        # fixed sample size, and FedAvg samples max(int(online * fraction), min_train_nodes).
        # A fraction of exactly 0 would make FedAvg skip training, so use a tiny one.
        fraction_train=DP_FRACTION if dp else float(cfg["fraction-train"]),
        fraction_evaluate=1.0 if bool(cfg["site-eval"]) else 0.0,
        min_train_nodes=min_nodes,
        min_evaluate_nodes=min_nodes,
        min_available_nodes=min_nodes,
    )
    # Fixed public test set, disjoint from both the sites' data and the backbone's
    # pre-training data: one consistent convergence curve whichever sites are online.
    public = public_test_set(str(cfg["data-path"]), int(cfg["num-classes"]),
                             str(cfg["public-data-path"]))
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
        num_rounds=remaining,
        timeout=float(cfg["round-timeout"]),
        evaluate_fn=global_eval,
    )
    torch.save(result.arrays.to_torch_state_dict(), Path(metrics_dir) / "final_exchanged.pt")
    print(f"[FedFleet] done; metrics in {metrics_dir}/rounds.jsonl")
