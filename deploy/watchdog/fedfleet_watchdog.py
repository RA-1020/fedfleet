"""FedFleet watchdog: resume a run that the aggregator lost in a crash, without a person.

systemd restarts the aggregator (fedfleet-superlink) after a crash, and the sites reconnect by
themselves, but the run that was in progress is gone: Flower marks it finished:failed once its
ServerApp misses its heartbeats, with the details "No heartbeat received from the task". This
script, run every 30 s by fedfleet-watchdog.timer, finds such runs and resumes them from their
checkpoint through the control API, exactly as a person would (POST /runs with resume=true and
the run's original settings). The control API keeps every guard it has for a manual resume:
same privacy settings, the privacy budget, enough sites online, no second run on the folder.

A run is resumed only when ALL of these hold:
  * it was started through the control API after this watchdog was deployed (its
    active_run.json holds the original request) and not with auto_resume=false;
  * Flower lists it as finished:failed with "No heartbeat" in the details. A run that
    completed, was stopped (flwr stop), or failed with an error in the ServerApp (for example
    a privacy budget that ran out) is never resumed;
  * it has a checkpoint and fewer rounds than it asked for;
  * fewer than MAX_RESUMES automatic resumes of this run folder so far, and the crash was seen
    less than GIVE_UP_S ago (after that a person decides).
If the API refuses (for example too few sites online yet), the watchdog tries again on the next
tick until GIVE_UP_S. Everything it does is printed to the journal
(journalctl -u fedfleet-watchdog) and kept in ~/fedfleet-watchdog/state.json; the API's audit log
records each resume with the client "fedfleet-watchdog/1.0".

Environment: FEDFLEET_ORIGIN_KEY (from /etc/fedfleet/control-api.env), FEDFLEET_DIR,
FEDFLEET_CONNECTION, FEDFLEET_API (default http://127.0.0.1:8088), FEDFLEET_WATCHDOG_STATE.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
DIR = Path(os.environ.get("FEDFLEET_DIR", str(HOME / "fedfleet"))).expanduser()
CONNECTION = os.environ.get("FEDFLEET_CONNECTION", "fedfleet-vm")
API = os.environ.get("FEDFLEET_API", "http://127.0.0.1:8088")
STATE = Path(os.environ.get("FEDFLEET_WATCHDOG_STATE",
                            str(HOME / "fedfleet-watchdog" / "state.json"))).expanduser()
MAX_RESUMES = 3          # automatic resumes per run folder; then a person decides
GIVE_UP_S = 900          # stop retrying 15 min after the crash was first seen
RETRY_S = 25             # at least this long between two attempts for one folder
CRASH = "No heartbeat"   # Flower's details for a run whose ServerApp died with the aggregator


def log(msg: str) -> None:
    print(f"[watchdog] {msg}", flush=True)


def flwr_run_status(run_id: str) -> tuple[str, str] | None:
    """(status, details) of one run as Flower lists it, e.g. ("finished:failed", "No heartbeat
    received from the task"); None if Flower does not answer or does not know the run."""
    cmd = [str(DIR / ".venv" / "bin" / "flwr"), "ls", CONNECTION, "--run-id", str(run_id),
           "--format", "json"]
    try:
        r = subprocess.run(cmd, cwd=HOME, capture_output=True, text=True, timeout=30)
        runs = json.loads(r.stdout).get("runs", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    for run in runs:
        if str(run.get("run-id")) == str(run_id):
            return str(run.get("status", "")), str(run.get("status-details", ""))
    return None


def post_resume(body: dict) -> tuple[int, str]:
    """POST /runs on the local control API; returns (HTTP status, response text)."""
    req = urllib.request.Request(
        f"{API}/runs", data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "user-agent": "fedfleet-watchdog/1.0",
                 "x-fedfleet-origin-key": os.environ.get("FEDFLEET_ORIGIN_KEY", "")})
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def done_rounds(folder: Path) -> int:
    done = 0
    try:
        for line in (folder / "rounds.jsonl").read_text().splitlines():
            try:
                rnd = json.loads(line).get("round")
            except json.JSONDecodeError:
                continue
            if isinstance(rnd, int):
                done = max(done, rnd)
    except FileNotFoundError:
        pass
    return done


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    tmp.replace(STATE)


def check(folder: Path, state: dict, now: float, status_of=flwr_run_status, post=post_resume) -> None:
    """Look at one run folder and resume its run if it was lost in a crash."""
    try:
        marker = json.loads((folder / "active_run.json").read_text())
    except (OSError, json.JSONDecodeError):
        return
    run_id, request = str(marker.get("run_id", "")), marker.get("request")
    if not run_id or not isinstance(request, dict):
        return                                   # started before the watchdog existed
    rec = state.setdefault(folder.name, {"resumes": 0, "final": []})
    if run_id in rec["final"]:
        return                                   # already judged: completed, stopped, error, ...
    if not request.get("auto_resume", True):
        rec["final"].append(run_id)
        return
    got = status_of(run_id)
    if got is None:
        return                                   # Flower did not answer; try on the next tick
    status, details = got
    kind = status.split(":")[0]
    if kind in ("pending", "starting", "running"):
        return
    if status != "finished:failed" or CRASH not in details:
        rec["final"].append(run_id)              # completed, stopped, or the app itself failed
        log(f"{folder.name}: run {run_id} {status}; nothing to do ({details[:120] or 'no details'})")
        return
    # Lost in a crash.
    first = rec.setdefault("crash_seen", {}).setdefault(run_id, now)
    want = int(request.get("rounds", 0))
    done = done_rounds(folder)
    if done >= want or not (folder / "latest_checkpoint.pt").is_file():
        rec["final"].append(run_id)
        log(f"{folder.name}: run {run_id} lost in a crash, but {done}/{want} rounds and "
            f"{'a' if (folder / 'latest_checkpoint.pt').is_file() else 'no'} checkpoint; not resuming")
        return
    if rec["resumes"] >= MAX_RESUMES:
        rec["final"].append(run_id)
        log(f"{folder.name}: run {run_id} lost in a crash again; already resumed {rec['resumes']} "
            "times automatically, leaving it to a person")
        return
    if now - first > GIVE_UP_S:
        rec["final"].append(run_id)
        log(f"{folder.name}: run {run_id} could not be resumed within {GIVE_UP_S} s; giving up "
            f"(last answer: {rec.get('last_answer', 'none')})")
        return
    if now - rec.get("last_try", 0) < RETRY_S:
        return
    rec["last_try"] = now
    body = {**request, "resume": True}
    log(f"{folder.name}: run {run_id} lost in a crash after round {done} of {want}; resuming")
    code, text = post(body)
    rec["last_answer"] = f"{code} {text[:200]}"
    if code == 202:
        rec["resumes"] += 1
        rec["final"].append(run_id)
        rec.setdefault("history", []).append(
            {"lost_run": run_id, "crash_seen": round(first, 3), "resumed": round(now, 3),
             "from_round": done, "answer": text[:300]})
        log(f"{folder.name}: resumed ({text[:200]})")
    else:
        log(f"{folder.name}: resume refused or failed ({code}): {text[:200]}; will retry")


def main() -> int:
    if not os.environ.get("FEDFLEET_ORIGIN_KEY"):
        log("FEDFLEET_ORIGIN_KEY is not set; cannot call the control API")
        return 1
    state, now = load_state(), time.time()
    for marker in sorted(HOME.glob("fedfleet-runs-*/active_run.json")):
        try:
            check(marker.parent, state, now)
        except Exception as e:  # noqa: BLE001  one bad folder must not stop the others
            log(f"{marker.parent.name}: {e!r}")
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
