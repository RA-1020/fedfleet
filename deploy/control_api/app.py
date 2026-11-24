"""FedFleet control API (minimal Layer 1 version of WBS 11.1).

Runs on the Oracle aggregator VM, bound to 127.0.0.1 only. The public way in is the
Cloudflare Worker gateway (deploy/gateway/), which checks the caller's token and rate limit,
then forwards here through an outbound-only tunnel with a second, shared origin key. So
neither the tunnel nor anyone on the VM's network can call this API without that key.

    GET  /healthz            liveness, no key needed (the tunnel's health check)
    GET  /status             aggregator, monitoring and backup services; the latest run
    GET  /runs               every run: rounds, accuracy, resumes, dropouts, cost
    GET  /runs/{name}        one run, with per-round detail
    POST /runs               start a federation run with allow-listed settings, or resume one
                             ("resume": true: continue an existing folder from its checkpoint,
                             e.g. one restored from the Blob backup by scripts/restore_run.sh)

Settings come from the environment (systemd EnvironmentFile, mode 600):
    FEDFLEET_ORIGIN_KEY   required; the gateway sends it as X-FedFleet-Origin-Key
    FEDFLEET_DIR          default ~/fedfleet (the project, its venv and data on this VM)
    FEDFLEET_CONNECTION   default fedfleet-vm (the [superlink.*] entry in ~/.flwr/config.toml)
Costs are list prices before free tiers and credits, from deploy/rates.toml.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from typing import Literal

from pydantic import BaseModel, Field

HOME = Path.home()
DIR = Path(os.environ.get("FEDFLEET_DIR", str(HOME / "fedfleet"))).expanduser()
CONNECTION = os.environ.get("FEDFLEET_CONNECTION", "fedfleet-vm")
SERVICES = ("fedfleet-superlink", "fedfleet-exporter", "fedfleet-backup", "prometheus", "grafana-server")
RUN_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# The run summary and cost code is shared with the Azure Function round report.
sys.path.insert(0, str(DIR))
sys.path.insert(0, str(DIR / "deploy" / "azure_function"))
from fedfleet.cost import load_rates, run_costs  # noqa: E402
from fedfleet_report import parse_rows, summarize  # noqa: E402

app = FastAPI(title="FedFleet control API", version="0.1.0",
              description="Status, runs and cost of the FedFleet federation; starts runs.")


def require_key(x_fedfleet_origin_key: str = Header(default="")) -> None:
    want = os.environ.get("FEDFLEET_ORIGIN_KEY", "")
    if not want:
        raise HTTPException(503, "FEDFLEET_ORIGIN_KEY is not set on the server")
    if not hmac.compare_digest(x_fedfleet_origin_key.encode(), want.encode()):
        raise HTTPException(401, "missing or wrong origin key")


def _rates():
    try:
        return load_rates(DIR / "deploy" / "rates.toml")
    except (OSError, ValueError):
        return None


def _run_files() -> dict[str, Path]:
    """Run name (folder path under home) -> its rounds.jsonl, as the exporter and backup see them."""
    found = {}
    for pattern in ("fedfleet-runs*/rounds.jsonl", "fedfleet-runs*/*/rounds.jsonl"):
        for p in HOME.glob(pattern):
            found[str(p.parent.relative_to(HOME))] = p
    return found


def _summary(name: str, path: Path, detail: bool = False) -> dict:
    rows, bad = parse_rows(path.read_text(errors="replace"))
    rates = _rates()
    out = summarize(rows, detail=detail, rates=rates, costing=run_costs if rates else None)
    return {"run": name, "bad_lines": bad, **out}


def _service_state(name: str) -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/status", dependencies=[Depends(require_key)])
def status() -> dict:
    runs = _run_files()
    latest = max(runs.items(), key=lambda kv: kv[1].stat().st_mtime, default=None)
    last = None
    if latest:
        rows, _ = parse_rows(latest[1].read_text(errors="replace"))
        train = [r for r in rows if isinstance(r.get("round"), int) and r["round"] >= 1]
        last = {"run": latest[0], "round": None}
        if train:
            r = train[-1]
            ts = r.get("timestamp")
            last.update({"round": r["round"], "global_acc": r.get("global_acc"),
                         "sites_online": r.get("nodes_online"), "sites_replied": r.get("nodes_replied"),
                         "sites_asked": r.get("nodes_asked"), "timestamp": ts,
                         "age_seconds": round(time.time() - ts, 1) if ts else None})
    return {"services": {s: _service_state(s) for s in SERVICES}, "runs": len(runs), "latest_round": last,
            "note": "Costs are list prices before free tiers and credits."}


@app.get("/runs", dependencies=[Depends(require_key)])
def runs() -> dict:
    out = [_summary(n, p) for n, p in _run_files().items()]
    out.sort(key=lambda r: r.get("last_timestamp") or 0, reverse=True)
    return {"runs": out, "note": "Costs are list prices before free tiers and credits."}


@app.get("/runs/{name:path}", dependencies=[Depends(require_key)])
def run(name: str) -> dict:
    path = _run_files().get(name)
    if path is None:
        raise HTTPException(404, "no such run")
    return _summary(name, path, detail=True)


class RunRequest(BaseModel):
    name: str = Field(description="new run name: lower-case letters, digits and hyphens; "
                                  "metrics go to ~/fedfleet-runs-<name>")
    rounds: int = Field(5, ge=1, le=20)
    min_nodes: int = Field(3, ge=1, le=3)
    round_timeout: float = Field(600.0, ge=30.0, le=900.0)
    head_only: bool = True
    compression: Literal["none", "int8", "topk", "topk-int8"] = "none"
    topk_ratio: float = Field(0.1, gt=0.0, le=1.0)
    error_feedback: bool = True
    seed: int = Field(-1, ge=-1, le=1_000_000)
    dp: bool = False
    dp_noise_multiplier: float = Field(1.0, ge=0.0, le=10.0)
    dp_clip_norm: float = Field(1.5, gt=0.0, le=100.0)
    dp_delta: float = Field(1e-5, gt=0.0, lt=1.0)
    dp_max_epsilon: float = Field(0.0, ge=0.0, le=1e6, description="Flower's max_epsilon: > 0 stops the run "
                                     "before a round would take epsilon past it; needs dp and noise above 0; "
                                     "fixed for the life of a run")
    secagg: bool = Field(False, description="secure aggregation (SecAgg+); works with dp, not with compression")
    resume: bool = Field(False, description="continue fedfleet-runs-<name> from its latest_checkpoint.pt; "
                                             "rounds is then the total to reach, not the number to add")


def _done_rounds(path: Path) -> tuple[int, dict | None, bool]:
    """Highest round logged in a rounds.jsonl (0 if none; the checkpoint matches it), the
    privacy settings of that round's row (None for a run without DP) and whether that round
    used secure aggregation."""
    done, dp, secagg = 0, None, False
    try:
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row.get("round"), int) and row["round"] >= done:
                done, dp, secagg = row["round"], row.get("dp"), "secagg" in row
    except FileNotFoundError:
        pass
    return done, dp, secagg


def _run_config(req: RunRequest, metrics_dir: str) -> str:
    cfg = {
        "num-server-rounds": req.rounds, "min-nodes": req.min_nodes, "round-timeout": req.round_timeout,
        "head-only": req.head_only, "site-eval": False, "lr": 0.001 if req.head_only else 0.0005,
        "data-path": "~/fedfleet/data/RML2016.10a_dict.pkl",            # read on each site
        "public-data-path": "~/fedfleet/data/radioml_public_test.pt",  # read on the aggregator
        "backbone-path": "~/fedfleet/backbone.pt",
        "metrics-dir": metrics_dir,
        "rates-path": str(DIR / "deploy" / "rates.toml"),
        "compression": req.compression, "topk-ratio": req.topk_ratio,
        "error-feedback": req.error_feedback, "seed": req.seed,
        "dp": req.dp, "dp-noise-multiplier": req.dp_noise_multiplier,
        "dp-clip-norm": req.dp_clip_norm, "dp-delta": req.dp_delta,
        "dp-max-epsilon": req.dp_max_epsilon,
        "resume": req.resume, "secagg": req.secagg,
    }

    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return json.dumps(v)
    return " ".join(f"{k}={val(v)}" for k, v in cfg.items())


@app.post("/runs", status_code=202, dependencies=[Depends(require_key)])
def start_run(req: RunRequest) -> dict:
    if not RUN_NAME.match(req.name):
        raise HTTPException(422, "name must be 1-40 lower-case letters, digits or hyphens")
    if req.secagg and req.compression != "none":
        raise HTTPException(422, "secagg cannot be combined with compression: SecAgg+ quantises "
                                 "updates itself, and masked values do not compress")
    if req.dp and req.compression != "none":
        raise HTTPException(422, "dp cannot be combined with compression: compression and error "
                                 "feedback change the update after clipping, so the clipping bound "
                                 "and epsilon would not hold")
    if req.dp_max_epsilon > 0 and not (req.dp and req.dp_noise_multiplier > 0):
        raise HTTPException(422, "dp_max_epsilon needs dp=true and dp_noise_multiplier above 0: "
                                 "without noise epsilon is unbounded")
    metrics_dir = HOME / f"fedfleet-runs-{req.name}"
    done = None
    if req.resume:
        if not (metrics_dir / "latest_checkpoint.pt").is_file():
            raise HTTPException(409, f"resume needs fedfleet-runs-{req.name}/latest_checkpoint.pt on the aggregator")
        done, last_dp, last_secagg = _done_rounds(metrics_dir / "rounds.jsonl")
        if last_secagg != req.secagg:
            raise HTTPException(409, "resume with the same aggregation as the original run: "
                                     + ("it ran with secagg" if last_secagg else "it ran without secagg"))
        # Epsilon covers every released model only if a DP run resumes with the same privacy
        # settings (the aggregator checks the saved ledger too; this refuses early and says why).
        was_noisy = bool(last_dp) and (last_dp.get("noise_multiplier") or 0) > 0
        now_noisy = req.dp and req.dp_noise_multiplier > 0
        if was_noisy != now_noisy:
            raise HTTPException(409, "resume with the same privacy settings as the original run: "
                                     + ("it ran with DP noise" if was_noisy else "it ran without DP noise"))
        if was_noisy and (abs(float(last_dp.get("noise_multiplier", 0)) - req.dp_noise_multiplier) > 1e-12
                          or abs(float(last_dp.get("delta", 0)) - req.dp_delta) > 1e-15):
            raise HTTPException(409, "resume a DP run with its original noise multiplier and delta "
                                     f"({last_dp.get('noise_multiplier')}, {last_dp.get('delta')})")
        # As in Flower, max_epsilon belongs to the run (its saved ledger keeps it): a resume must
        # use the run's own value, and one that has spent it cannot continue.
        saved_max = float((last_dp or {}).get("max_epsilon") or 0.0)
        if was_noisy and saved_max != req.dp_max_epsilon:
            raise HTTPException(409, "resume a DP run with its original dp_max_epsilon "
                                     f"({saved_max:g}; 0 = none)")
        spent = (last_dp or {}).get("epsilon")
        if req.dp_max_epsilon > 0 and spent is not None and spent >= req.dp_max_epsilon:
            raise HTTPException(409, f"privacy budget already spent: epsilon is {spent:.4g}, "
                                     f"max_epsilon {req.dp_max_epsilon:g}")
        if done >= req.rounds:
            raise HTTPException(409, f"fedfleet-runs-{req.name} already has {done} rounds; ask for more than that")
    elif metrics_dir.exists():
        raise HTTPException(409, f"run folder fedfleet-runs-{req.name} already exists; pick a new name")
    cmd = [str(DIR / ".venv" / "bin" / "flwr"), "run", str(DIR), CONNECTION,
           "--run-config", _run_config(req, f"~/fedfleet-runs-{req.name}")]
    env = {**os.environ, "FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION": "1",
           "PATH": f"{DIR / '.venv' / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    try:
        r = subprocess.run(cmd, cwd=DIR, env=env, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "flwr run did not return within 120 s")
    text = r.stdout + r.stderr
    m = re.search(r"Successfully started run (\d+)", text)
    if r.returncode != 0 or not m:
        tail = "\n".join(text.strip().splitlines()[-5:])
        raise HTTPException(502, f"flwr run failed (exit {r.returncode}): {tail}")
    return {"run_id": m.group(1), "run": f"fedfleet-runs-{req.name}", "rounds": req.rounds,
            "min_nodes": req.min_nodes, "compression": req.compression, "dp": req.dp,
            "dp_max_epsilon": req.dp_max_epsilon or None, "resume": req.resume, "secagg": req.secagg, "rounds_done_before": done, "watch": f"/runs/fedfleet-runs-{req.name}"}
