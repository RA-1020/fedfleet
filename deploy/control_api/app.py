"""FedFleet control API (minimal Layer 1 version of WBS 11.1).

Runs on the Oracle aggregator VM, bound to 127.0.0.1 only. The public way in is the
Cloudflare Worker gateway (deploy/gateway/), which checks the caller's token and rate limit,
then forwards here through an outbound-only tunnel with a second, shared origin key. So
neither the tunnel nor anyone on the VM's network can call this API without that key.

    GET  /healthz            liveness, no key needed (the tunnel's health check)
    GET  /status             aggregator, monitoring and backup services; the latest run
    GET  /runs               every run: rounds, accuracy, resumes, dropouts, cost
    GET  /runs/{name}        one run, with per-round detail
    POST /runs               start a federation run with allow-listed settings

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


def _run_config(req: RunRequest, metrics_dir: str) -> str:
    cfg = {
        "num-server-rounds": req.rounds, "min-nodes": req.min_nodes, "round-timeout": req.round_timeout,
        "head-only": req.head_only, "site-eval": False, "lr": 0.001 if req.head_only else 0.0005,
        "data-path": "~/fedfleet/data/RML2016.10a_dict.pkl",            # read on each site
        "public-data-path": "~/fedfleet/data/radioml_public_test.pt",  # read on the aggregator
        "backbone-path": "~/fedfleet/backbone.pt",
        "metrics-dir": metrics_dir,
        "rates-path": str(DIR / "deploy" / "rates.toml"),
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
    metrics_dir = HOME / f"fedfleet-runs-{req.name}"
    if metrics_dir.exists():
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
            "min_nodes": req.min_nodes, "watch": f"/runs/fedfleet-runs-{req.name}"}
