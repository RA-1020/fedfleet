"""Aggregation service (Σ): coordinates rounds and averages the returned weights."""

from __future__ import annotations

import json
from pathlib import Path

import torch
from flwr.app import ArrayRecord, Context
from flwr.serverapp import Grid, ServerApp

from flwr.app import MetricRecord
from torch.utils.data import DataLoader, Subset

from fedfleet.comms import DP_FRACTION, CommsFedAvg, affordable_releases, ledger_max_epsilon, write_budget_note
from fedfleet.cost import load_rates
from fedfleet.compress import check_mode
from fedfleet.data import public_test_set
from fedfleet.model import build_model, count_params, exchanged_module
from fedfleet.secagg import run_secagg
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
    if dp is not None and compression != "none":
        # The DP guarantee needs every site's update to stay within the clipping norm. Compression
        # changes the update after clipping: int8 rounding can grow it, and error feedback adds
        # what earlier rounds dropped, so the sent update can exceed the norm and epsilon would
        # be understated. Refused, as SecAgg+ refuses compression.
        print("[FedFleet] dp=true cannot be combined with compression (the clipping bound would not "
              "hold for the update actually sent); not starting")
        return
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

    # Privacy budget: stop before a release would take epsilon past it. Every round composes
    # the same event, so the number of rounds that fit is known now (fedfleet/comms.py).
    budget = float(cfg.get("dp-max-epsilon", 0.0))
    if done_rounds and saved_dp is not None:
        # As in Flower, the budget belongs to the run: the ledger was saved with it, and a
        # resume may neither add, raise, lower nor drop it.
        saved_max = ledger_max_epsilon(json.loads(saved_dp))
        if (saved_max or 0.0) != budget:
            print(f"[FedFleet] this run's privacy ledger was saved with max_epsilon "
                  f"{'none' if saved_max is None else f'{saved_max:g}'}; resume it with the same "
                  f"dp-max-epsilon ({0.0 if saved_max is None else saved_max:g}). Not resuming.")
            return
    if budget > 0:
        if not noisy:
            print("[FedFleet] dp-max-epsilon needs dp=true with a noise multiplier above 0 "
                  "(without noise epsilon is unbounded). Not starting.")
            return
        dp["max_epsilon"] = budget
        ledger = json.loads(saved_dp) if saved_dp else None
        allowed, eps_now, eps_after = affordable_releases(dp, ledger, budget, remaining)
        if allowed == 0:
            print(f"[FedFleet] privacy budget reached: epsilon is {eps_now:.4g} and one more round "
                  f"would exceed the budget of {budget:g}. Not starting.")
            write_budget_note(metrics_dir, budget, done_rounds, done_rounds, eps_now, num_rounds)
            return
        if allowed < remaining:
            print(f"[FedFleet] privacy budget {budget:g} allows {allowed} of the {remaining} remaining "
                  f"rounds: the run stops after round {done_rounds + allowed} at epsilon "
                  f"{eps_after:.4g} (at most; a round without all sites releases nothing)")
            remaining = allowed
        else:
            print(f"[FedFleet] privacy budget {budget:g}: all {remaining} remaining rounds fit "
                  f"(epsilon at most {eps_after:.4g} at the end)")
        write_budget_note(metrics_dir, budget, done_rounds + remaining, done_rounds, eps_after, num_rounds)

    # Fixed public test set, disjoint from both the sites' data and the backbone's
    # pre-training data: one consistent convergence curve whichever sites are online.
    public = public_test_set(str(cfg["data-path"]), int(cfg["num-classes"]),
                             str(cfg["public-data-path"]))
    public = Subset(public, range(min(len(public), 4000)))
    public_loader = DataLoader(public, batch_size=512)

    if bool(cfg.get("secagg", False)):
        # Secure aggregation runs on Flower's legacy workflow (fedfleet/secagg.py). SecAgg+
        # quantises updates itself, so FedFleet's compression is refused. DP works under it
        # (clipping at the sites before masking, noise and accounting at the aggregator).
        if compression != "none":
            print("[FedFleet] secagg=true cannot be combined with compression; not starting")
            return
        max_weight = float(cfg.get("secagg-max-weight", 60000.0))
        clipping_range = float(cfg.get("secagg-clipping-range", 8.0))
        print(f"[FedFleet] secure aggregation (SecAgg+): {min_nodes} sites minimum, shares to all "
              f"selected sites, threshold 2/3, "
              + ("equal weights (DP)" if dp else f"max weight {max_weight:g}")
              + f", clipping range {clipping_range:g}"
              + ("; site evaluation off under SecAgg+" if bool(cfg["site-eval"]) else ""))
        recorder = CommsFedAvg(metrics_dir=metrics_dir, head_only=head_only,
                               round_offset=done_rounds, rates=rates, dp=dp,
                               dp_state=json.loads(saved_dp) if (saved_dp and noisy) else None)

        def evaluate_global(server_round: int, sd: dict) -> tuple[float, float]:
            part.load_state_dict(sd)
            return evaluate(model, public_loader, pick_device())

        final = run_secagg(grid, context, recorder=recorder, state=state, num_rounds=remaining,
                           round_offset=done_rounds, min_nodes=min_nodes,
                           fraction_train=float(cfg["fraction-train"]),
                           timeout=float(cfg["round-timeout"]), evaluate_global=evaluate_global,
                           max_weight=max_weight, clipping_range=clipping_range, dp=dp)
        torch.save(final, Path(metrics_dir) / "final_exchanged.pt")
        print(f"[FedFleet] done; metrics in {metrics_dir}/rounds.jsonl")
        return

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
        # Sites' own evaluation reports per-site accuracy and test-set size in clear text; under
        # DP that is side information about a site, so it is off (the public test set remains).
        fraction_evaluate=1.0 if (bool(cfg["site-eval"]) and dp is None) else 0.0,
        min_train_nodes=min_nodes,
        min_evaluate_nodes=min_nodes,
        min_available_nodes=min_nodes,
    )
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
