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
    FEDFLEET_AUDIT        default ~/fedfleet-audit/audit.jsonl: one JSON line per request (who,
                          what, outcome); the gateway supplies the caller's address, client and
                          a fingerprint of the token used, never the token
Costs are list prices before free tiers and credits, from deploy/rates.toml.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
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

AUDIT = Path(os.environ.get("FEDFLEET_AUDIT", str(HOME / "fedfleet-audit" / "audit.jsonl"))).expanduser()


def _audit(entry: dict) -> None:
    """Append one line to the audit log. A failure to write is reported, never fatal."""
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"audit log not written: {e}", file=sys.stderr)


@app.middleware("http")
async def audit_requests(request: Request, call_next):
    """Record every request except /healthz: when, who (as the gateway saw them), what, outcome.

    Caller details come from headers the gateway sets; they count only when the request also
    carried the right origin key, so a caller that bypassed the gateway cannot fake them."""
    t0 = time.time()
    response = await call_next(request)
    if request.url.path == "/healthz":
        return response
    try:   # the audit log must never break a request
        want = os.environ.get("FEDFLEET_ORIGIN_KEY", "")
        via_gateway = bool(want) and hmac.compare_digest(
            request.headers.get("x-fedfleet-origin-key", "").encode(), want.encode())
        h = request.headers
        agent = h.get("x-fedfleet-client-agent") if via_gateway else h.get("user-agent")
        entry = {
            "time": round(t0, 3), "method": request.method, "path": request.url.path,
            "status": response.status_code, "via_gateway": via_gateway,
            "client_ip": h.get("x-fedfleet-client-ip") if via_gateway else (request.client.host if request.client else None),
            "client_agent": (agent or "")[:200] or None,
            "token_id": h.get("x-fedfleet-token-id") if via_gateway else None,
            "ms": round((time.time() - t0) * 1000),
        }
        entry.update(getattr(request.state, "audit", None) or {})
        _audit(entry)
    except Exception as e:  # noqa: BLE001
        print(f"audit log not written: {e!r}", file=sys.stderr)
    return response


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
    auto_resume: bool = Field(True, description="let the aggregator's watchdog resume this run by itself if the "
                                                 "aggregator crashes (Flower then marks it failed for lack of heartbeat)")
    resume: bool = Field(False, description="continue fedfleet-runs-<name> from its latest_checkpoint.pt; "
                                             "rounds is then the total to reach, not the number to add")


ACTIVE = ("pending", "starting", "running")


def _flwr_json(*args: str) -> dict:
    """Run `flwr <args> <connection> --format json` on this machine and parse its output."""
    cmd = [str(DIR / ".venv" / "bin" / "flwr"), *args, CONNECTION, "--format", "json"]
    r = subprocess.run(cmd, cwd=Path.home(), capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout)


def _online_sites() -> int:
    """Sites the aggregator lists as online (current heartbeat)."""
    nodes = _flwr_json("supernode", "ls").get("nodes", [])
    return sum(1 for n in nodes if n.get("status") == "online")


def _run_active(run_id: str) -> bool:
    """Whether Flower still lists this run as pending, starting or running."""
    runs = _flwr_json("ls", "--run-id", str(run_id)).get("runs", [])
    return any(str(r.get("status", "")).split(":")[0] in ACTIVE for r in runs)


def _next_release_exceeds(last_dp: dict, max_epsilon: float) -> tuple[bool, float]:
    """Whether one more DP round would take epsilon past max_epsilon.

    Every FedFleet release composes the same Gaussian event, so the ledger is rebuilt from the
    last row's settings and release count with Flower's own accountant, the check its DP
    wrapper and FedFleet's aggregator apply. Returns (exceeds, epsilon so far)."""
    from flwr.serverapp import NeighboringRelation, PrivacyConfig, RdpAccountant, SamplingMethod
    from flwr.supercore.privacy_accounting import GaussianPrivacyEvent
    n = int(last_dp["sites"])
    acc = RdpAccountant(PrivacyConfig(
        target_delta=float(last_dp["delta"]), population_size=n,
        neighboring_relation=NeighboringRelation.ADD_OR_REMOVE_ONE,
        sampling_method=SamplingMethod.NO_AMPLIFICATION))
    event = GaussianPrivacyEvent(noise_multiplier=float(last_dp["noise_multiplier"]),
                                 sample_size=n, population_size=n)
    releases = int(last_dp.get("releases") or 0)
    if releases:
        acc.compose(event, releases)
    spent = float(last_dp.get("epsilon") or 0.0)
    return acc.would_exceed(event, max_epsilon=max_epsilon), spent


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


# One run request at a time. `flwr run` takes about 20 s, and the guard against two runs on one
# folder reads active_run.json, which is written only after it returns; two resumes arriving in
# that window (a person and the watchdog, say) would otherwise both pass and both start.
_start_lock = threading.Lock()


@app.post("/runs", status_code=202, dependencies=[Depends(require_key)])
def start_run(req: RunRequest, request: Request) -> dict:
    if not _start_lock.acquire(blocking=False):
        request.state.audit = {"run": req.name, "request": req.model_dump()}
        raise HTTPException(409, "another run request is being processed; try again in a few seconds")
    try:
        return _start_run(req, request)
    finally:
        _start_lock.release()


def _start_run(req: RunRequest, request: Request) -> dict:
    # What was asked, for the audit log (allow-listed settings only; there are no secrets here).
    request.state.audit = {"run": req.name, "request": req.model_dump()}
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
        if req.dp_max_epsilon > 0 and was_noisy:
            exceeds, spent = _next_release_exceeds(last_dp, req.dp_max_epsilon)
            if exceeds:
                raise HTTPException(409, f"privacy budget already spent: epsilon is {spent:.4g}, and one "
                                         f"more round would pass max_epsilon {req.dp_max_epsilon:g}")
        marker = metrics_dir / "active_run.json"
        if marker.is_file():
            try:
                prev = json.loads(marker.read_text()).get("run_id")
                busy = bool(prev) and _run_active(prev)
            except Exception:
                raise HTTPException(503, "could not check whether this run is still in progress (flwr ls)")
            if busy:
                raise HTTPException(409, f"run {prev} is still in progress in fedfleet-runs-{req.name}; "
                                         "wait for it to finish or stop it first")
        if done >= req.rounds:
            raise HTTPException(409, f"fedfleet-runs-{req.name} already has {done} rounds; ask for more than that")
    elif metrics_dir.exists():
        raise HTTPException(409, f"run folder fedfleet-runs-{req.name} already exists; pick a new name")
    try:
        online = _online_sites()
    except Exception:
        raise HTTPException(503, "could not read the list of sites from the aggregator (flwr supernode ls)")
    if online < req.min_nodes:
        raise HTTPException(409, f"only {online} site(s) online, and this run needs {req.min_nodes}; "
                                 "start the sites first")
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
    # Remember which run uses this folder, so a resume cannot start while it is still going.
    metrics_dir.mkdir(parents=True, exist_ok=True)
    # The request is kept too, so the watchdog can resume exactly this run after a crash.
    (metrics_dir / "active_run.json").write_text(json.dumps({
        "run_id": m.group(1), "resume": req.resume, "started": round(time.time(), 3),
        "request": req.model_dump()}) + "\n")
    request.state.audit["run_id"] = m.group(1)
    return {"run_id": m.group(1), "run": f"fedfleet-runs-{req.name}", "rounds": req.rounds,
            "min_nodes": req.min_nodes, "compression": req.compression, "dp": req.dp,
            "dp_max_epsilon": req.dp_max_epsilon or None, "resume": req.resume, "secagg": req.secagg, "rounds_done_before": done, "watch": f"/runs/fedfleet-runs-{req.name}"}
