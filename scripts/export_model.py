"""Export a finished run's model to ONNX with a model card (part of WBS 13.7, scripted teardown).

    python scripts/export_model.py ~/fedfleet-runs-<name> [--backbone ~/fedfleet/backbone.pt]
        [--public ~/fedfleet/data/radioml_public_test.pt] [--samples 2000]

Runs on the aggregator VM, where each run folder holds final_exchanged.pt (the head, or the
whole model in full-model runs) and the frozen backbone and the public test file live.
Writes model.onnx and model_card.md into the run folder; the Blob backup service copies
both to Azure Blob storage like the other run files.

The export is checked: the ONNX model and the PyTorch model are run on the same public test
samples, and the card records the largest output difference, whether they agree on every
predicted class, and the accuracy of each. Needs onnx and onnxruntime in the venv.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fedfleet.model import IN_CHANNELS, IN_H, IN_W, build_model, count_params  # noqa: E402

CLASSES = ["8PSK", "AM-DSB", "AM-SSB", "BPSK", "CPFSK", "GFSK", "PAM4", "QAM16", "QAM64", "QPSK", "WBFM"]


def load_rows(run: Path) -> list[dict]:
    f = run / "rounds.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


def export(model: torch.nn.Module, path: Path) -> str:
    model.eval()
    # Keep gradients on: PyTorch's fast inference path for transformer layers (taken under
    # no_grad in eval mode) cannot be exported, the ordinary path can.
    for p in model.parameters():
        p.requires_grad_(True)
    x = torch.randn(1, IN_CHANNELS, IN_H, IN_W)
    kw = dict(input_names=["spectrogram"], output_names=["logits"], opset_version=17,
              dynamic_axes={"spectrogram": {0: "batch"}, "logits": {0: "batch"}})
    try:
        torch.onnx.export(model, (x,), str(path), dynamo=False, **kw)
        return "torch.onnx.export (TorchScript exporter), opset 17"
    except TypeError:                      # older torch without the dynamo argument
        torch.onnx.export(model, (x,), str(path), **kw)
        return "torch.onnx.export, opset 17"


def check(model: torch.nn.Module, path: Path, public: Path, samples: int) -> dict:
    import numpy as np
    import onnxruntime as ort
    data = torch.load(public, map_location="cpu", weights_only=True)
    x, y = data["x"][:samples].float(), data["y"][:samples].long()
    with torch.no_grad():
        ref = model(x).numpy()
    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    out = sess.run(["logits"], {"spectrogram": x.numpy()})[0]
    return {
        "samples": int(len(y)),
        "max_abs_logit_difference": float(np.max(np.abs(out - ref))),
        "same_predicted_class": float(np.mean(out.argmax(1) == ref.argmax(1))),
        "accuracy_pytorch": float(np.mean(ref.argmax(1) == y.numpy())),
        "accuracy_onnx": float(np.mean(out.argmax(1) == y.numpy())),
        "onnxruntime": ort.__version__,
    }


def card(run: Path, rows: list[dict], mode: str, n_params: int, n_exchanged: int, how: str,
         chk: dict | None, size: int) -> str:
    train = [r for r in rows if isinstance(r.get("round"), int) and r["round"] >= 1]
    last = train[-1] if train else {}
    first = next((r for r in rows if r.get("round") == 0), {})
    costs = [r["cost"]["total_usd"] for r in train if isinstance(r.get("cost"), dict)]
    lines = [
        f"# Model card: {run.name}",
        "",
        f"Exported {time.strftime('%Y-%m-%d %H:%M %Z')} by scripts/export_model.py ({how}); "
        f"torch {torch.__version__}, Python {platform.python_version()}. File: model.onnx, {size:,} bytes.",
        "",
        "## Model",
        f"- Tiny Vision Transformer for radio modulation classification: frozen pre-trained backbone "
        f"(patch embedding, 4 transformer blocks, 96-dim) and a 96-64-11 MLP head; {n_params:,} parameters.",
        f"- Input `spectrogram`: float32, shape (batch, {IN_CHANNELS}, {IN_H}, {IN_W}), a normalised "
        "log-magnitude spectrogram of 128 I/Q samples (fedfleet/data.py iq_to_spectrogram).",
        f"- Output `logits`: float32, shape (batch, 11), classes in this order: {', '.join(CLASSES)}.",
        "",
        "## Training",
        f"- Federated with Flower 1.38 (FedAvg), {mode}: {n_exchanged:,} parameters trained at the sites "
        "and exchanged; site data never left the sites.",
        f"- Rounds: {len(train)}; sites replying in the last round: {last.get('nodes_replied', '?')}"
        f"/{last.get('nodes_asked', '?')}; update compression: {last.get('compression', 'none')}.",
        "- Data: RadioML 2016.10a, split across sites with a Dirichlet(0.5) non-IID partition; "
        "evaluation on a fixed public test split held out from sites and backbone pre-training.",
        f"- Global accuracy on the public test split: {first.get('global_acc', float('nan')):.4f} before "
        f"training, {last.get('global_acc', float('nan')):.4f} after the last round.",
    ]
    if costs:
        lines.append(f"- Cost of training (list prices before free tiers and credits): ${sum(costs):.5f} "
                     f"over {len(costs)} rounds.")
    lines += ["", "## Export check"]
    if chk:
        lines += [
            f"- ONNX Runtime {chk['onnxruntime']} against PyTorch on {chk['samples']:,} public test samples: "
            f"largest logit difference {chk['max_abs_logit_difference']:.2e}; same predicted class for "
            f"{chk['same_predicted_class']:.2%} of samples.",
            f"- Accuracy on those samples: PyTorch {chk['accuracy_pytorch']:.4f}, ONNX {chk['accuracy_onnx']:.4f}.",
        ]
    else:
        lines.append("- Not checked (onnxruntime or the public test file not available).")
    lines += [
        "",
        "## Intended use and limits",
        "- Course project model (SE-315, FedFleet). Classifies the 11 RadioML 2016.10a modulations from "
        "short I/Q captures; overall accuracy on the public split is about 50%. Accuracy per SNR level "
        "was not measured for this export.",
        "- Not for operational spectrum monitoring. Trained on synthetic benchmark data (RadioML), "
        "not on captured signals.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("run")
    p.add_argument("--backbone", default="~/fedfleet/backbone.pt")
    p.add_argument("--public", default="~/fedfleet/data/radioml_public_test.pt")
    p.add_argument("--samples", type=int, default=2000)
    a = p.parse_args()
    run = Path(a.run).expanduser()
    final = run / "final_exchanged.pt"
    if not final.exists():
        raise SystemExit(f"{final} not found: only finished runs can be exported")
    state = torch.load(final, map_location="cpu", weights_only=True)
    head_only = all(k.startswith("net.") for k in state)
    model = build_model(11, a.backbone)
    (model.head if head_only else model).load_state_dict(state)
    mode = "head-only" if head_only else "full model"
    out = run / "model.onnx"
    how = export(model, out)
    chk = None
    public = Path(a.public).expanduser()
    try:
        if public.exists():
            chk = check(model, out, public, a.samples)
    except ImportError:
        print("onnxruntime not installed: export not checked")
    n_ex = count_params(model.head) if head_only else count_params(model)
    (run / "model_card.md").write_text(card(run, load_rows(run), mode, count_params(model), n_ex, how,
                                            chk, out.stat().st_size))
    print(f"wrote {out} ({out.stat().st_size:,} bytes) and {run / 'model_card.md'}")
    if chk:
        print(json.dumps(chk, indent=2))
        if chk["same_predicted_class"] < 1.0 or chk["max_abs_logit_difference"] > 1e-3:
            raise SystemExit("export check FAILED: ONNX and PyTorch disagree")


if __name__ == "__main__":
    main()
