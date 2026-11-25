"""FedFleet elasticity controller: the Azure site is started when work needs it and deallocated
when idle, within a daily cost budget (tasks 8.1, 8.3, 8.5).

Runs on the Oracle aggregator as fedfleet-elastic.service and checks every TICK_S seconds:

  scale out  A run is queued (POST /runs with "wait_for_sites": true found too few sites online,
             so the control API wrote ~/fedfleet-elastic/queue/<name>.json) and the Azure VM is
             deallocated: start it, if today's VM minutes are under the budget. The VM boots,
             fedfleet-site.service starts site 2, and the site registers with the aggregator.
  dispatch   The oldest queued run has enough sites online and no run is active: submit it to the
             control API (without wait_for_sites) and remove it from the queue. A queued run that
             has not started within QUEUE_TIMEOUT_S is moved to queue/failed (for example it needs
             the Mac sites, which this controller cannot start).
  scale in   The controller started the VM, nothing is queued, and no run has been active for
             IDLE_S: deallocate it. A VM started by a person (scripts/cloud_up.sh) is never
             deallocated by the controller; it only counts its minutes.
  cost guard VM minutes are counted whenever the VM is seen running (whoever started it). The
             controller does not start the VM once today's minutes reach BUDGET_MIN (UTC day), and
             it never stops a VM in the middle of a run.

Azure is reached through its REST API with a service principal whose custom role allows only
read, instanceView/read, start and deallocate on this one VM (deploy/elastic/README.md). The
secret is a root-only file handed to the service by systemd (LoadCredential=azure-sp), so this
process can read it but the ubuntu user's shell cannot.

Every action is appended to ~/fedfleet-elastic/events.jsonl (for the elasticity measurements)
and the current state is in ~/fedfleet-elastic/state.json (read by /status and the exporter).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
DIR = Path(os.environ.get("FEDFLEET_DIR", str(HOME / "fedfleet"))).expanduser()
CONNECTION = os.environ.get("FEDFLEET_CONNECTION", "fedfleet-vm")
API = os.environ.get("FEDFLEET_API", "http://127.0.0.1:8088")
ELASTIC = Path(os.environ.get("FEDFLEET_ELASTIC_DIR", str(HOME / "fedfleet-elastic"))).expanduser()
SUB = os.environ.get("AZ_SUBSCRIPTION", "")
RG = os.environ.get("AZ_RESOURCE_GROUP", "fedfleet-rg")
VM = os.environ.get("AZ_VM", "fedfleet-site2")
TICK_S = int(os.environ.get("ELASTIC_TICK_S", "20"))
IDLE_S = int(os.environ.get("ELASTIC_IDLE_S", "600"))             # 10 min without work -> deallocate
QUEUE_TIMEOUT_S = int(os.environ.get("ELASTIC_QUEUE_TIMEOUT_S", "900"))
BUDGET_MIN = float(os.environ.get("ELASTIC_BUDGET_MIN_PER_DAY", "180"))   # 3 h of B2s per UTC day
RATE_PER_HOUR = float(os.environ.get("ELASTIC_VM_RATE_PER_HOUR", "0.0448"))  # deploy/rates.toml B2s
ARM = "https://management.azure.com"
API_VERSION = "2024-07-01"


def log(msg: str) -> None:
    print(f"[elastic] {msg}", flush=True)


# ---------------------------------------------------------------- Azure (REST, stdlib only)

class Azure:
    def __init__(self, sp: dict, subscription: str, rg: str, vm: str, opener=urllib.request.urlopen):
        self.sp, self.open = sp, opener
        self.vm_path = (f"/subscriptions/{subscription}/resourceGroups/{rg}"
                        f"/providers/Microsoft.Compute/virtualMachines/{vm}")
        self._token, self._token_until = "", 0.0

    def _http(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        with self.open(req, timeout=60) as resp:
            body = resp.read().decode(errors="replace")
            return resp.status, (json.loads(body) if body.strip() else {})

    def token(self) -> str:
        if time.time() < self._token_until - 120:
            return self._token
        form = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": self.sp["appId"],
            "client_secret": self.sp["password"], "scope": f"{ARM}/.default"}).encode()
        _, body = self._http("POST", f"https://login.microsoftonline.com/{self.sp['tenant']}/oauth2/v2.0/token",
                             form, {"content-type": "application/x-www-form-urlencoded"})
        self._token, self._token_until = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _arm(self, method: str, action: str):
        url = f"{ARM}{self.vm_path}{action}?api-version={API_VERSION}"
        return self._http(method, url, b"" if method == "POST" else None,
                          {"authorization": f"Bearer {self.token()}"})

    def power(self) -> str:
        """'running', 'deallocated', 'starting', 'deallocating', 'stopped', ... or 'unknown'."""
        _, body = self._arm("GET", "/instanceView")
        for s in body.get("statuses", []):
            code = s.get("code", "")
            if code.startswith("PowerState/"):
                return code.split("/", 1)[1]
        return "unknown"

    def start(self) -> int:
        return self._arm("POST", "/start")[0]

    def deallocate(self) -> int:
        return self._arm("POST", "/deallocate")[0]


# ---------------------------------------------------------------- Flower and the control API

def flwr_json(*args: str) -> dict:
    cmd = [str(DIR / ".venv" / "bin" / "flwr"), *args, CONNECTION, "--format", "json"]
    r = subprocess.run(cmd, cwd=HOME, capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout)


def sites_online() -> int:
    nodes = flwr_json("supernode", "ls").get("nodes", [])
    return sum(1 for n in nodes if str(n.get("status", "")).lower() == "online")


def run_active() -> bool:
    runs = flwr_json("ls").get("runs", [])
    return any(str(r.get("status", "")).split(":")[0] in ("pending", "starting", "running") for r in runs)


def submit(body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{API}/runs", data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", "user-agent": "fedfleet-elastic/1.0",
                 "x-fedfleet-origin-key": os.environ.get("FEDFLEET_ORIGIN_KEY", "")})
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


# ---------------------------------------------------------------- the decision, one tick

def utc_day(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")


class Controller:
    def __init__(self, azure, root: Path = ELASTIC, online=sites_online, active=run_active, post=submit,
                 idle_s=IDLE_S, queue_timeout_s=QUEUE_TIMEOUT_S, budget_min=BUDGET_MIN):
        self.az, self.root = azure, root
        self.online, self.active, self.post = online, active, post
        self.idle_s, self.queue_timeout_s, self.budget_min = idle_s, queue_timeout_s, budget_min
        (root / "queue" / "failed").mkdir(parents=True, exist_ok=True)
        try:
            self.st = json.loads((root / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            self.st = {}
        self.st.setdefault("started_by_controller", False)
        self.st.setdefault("minutes", {})
        self.st["budget_minutes_per_day"] = budget_min

    def event(self, now: float, kind: str, **kw) -> None:
        rec = {"t": round(now, 3), "event": kind, **kw}
        with (self.root / "events.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
        self.st["last_action"] = rec
        log(f"{kind} {json.dumps(kw)[:300]}")

    def queue(self) -> list[tuple[Path, dict]]:
        out = []
        for f in sorted((self.root / "queue").glob("*.json")):
            try:
                out.append((f, json.loads(f.read_text())))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(out, key=lambda fe: fe[1].get("queued_at", 0))

    def tick(self, now: float) -> None:
        st = self.st
        try:
            power = self.az.power()
        except Exception as e:  # noqa: BLE001  Azure unreachable: decide nothing this tick
            log(f"azure: {e!r}")
            return
        prev = st.get("vm_power")
        st["vm_power"] = power
        if power != prev:
            self.event(now, "vm_power", power=power, was=prev)
        # Cost meter: count the time since the last tick while the VM is up, whoever started it.
        last = st.get("last_tick")
        if last and prev in ("running", "starting") and now - last < 10 * TICK_S:
            day = utc_day(now)
            st["minutes"][day] = round(st["minutes"].get(day, 0.0) + (now - last) / 60, 3)
        st["last_tick"] = now
        st["minutes"] = {d: m for d, m in st["minutes"].items() if d >= utc_day(now - 14 * 86400)}
        spent = st["minutes"].get(utc_day(now), 0.0)
        st["vm_minutes_today"] = round(spent, 2)
        st["vm_cost_today"] = round(spent / 60 * RATE_PER_HOUR, 5)
        if power == "deallocated" and st["started_by_controller"]:
            st["started_by_controller"] = False   # someone (or Azure) stopped it

        queue = self.queue()
        up = power in ("running", "starting")
        try:
            online = self.online() if up or queue else None
            active = self.active()
        except Exception as e:  # noqa: BLE001  aggregator busy or restarting: try next tick
            log(f"flower: {e!r}")
            return
        st["queue_length"], st["sites_online"], st["run_active"] = len(queue), online, active
        if active or queue:
            st["last_busy"] = now

        # Queue: give up on old entries, dispatch the oldest when it can run.
        for f, e in queue:
            if now - e.get("queued_at", now) > self.queue_timeout_s:
                f.replace(self.root / "queue" / "failed" / f.name)
                self.event(now, "queue_timeout", run=f.stem, online=online,
                           needed=e["request"].get("min_nodes"))
        queue = [(f, e) for f, e in queue if f.exists()]
        if queue and not active:
            f, e = queue[0]
            need = int(e["request"].get("min_nodes", 1))
            if online is not None and online >= need:
                code, text = self.post(e["request"])
                if code == 202:
                    f.unlink()
                    self.event(now, "run_dispatched", run=f.stem, online=online, needed=need,
                               waited_s=round(now - e.get("queued_at", now), 1), answer=text[:200])
                    st["last_busy"] = now
                elif code == 409 and "site(s) online" in text:
                    pass                              # a site dropped in between: next tick
                else:
                    f.replace(self.root / "queue" / "failed" / f.name)
                    self.event(now, "run_refused", run=f.stem, code=code, answer=text[:300])
            elif power in ("deallocated", "stopped"):
                if spent >= self.budget_min:
                    if st.get("budget_blocked_day") != utc_day(now):
                        st["budget_blocked_day"] = utc_day(now)
                        self.event(now, "scale_out_refused_budget", minutes_today=round(spent, 1),
                                   budget=self.budget_min, run=f.stem)
                else:
                    code = self.az.start()
                    st["started_by_controller"] = True
                    st["started_at"] = now
                    self.event(now, "scale_out", reason=f"run {f.stem} needs {need} sites, {online} online",
                               azure_status=code, minutes_today=round(spent, 1))

        # Scale in: only a VM this controller started, never during a run or with work queued.
        if (st["started_by_controller"] and power == "running" and not active and not queue
                and now - st.get("last_busy", st.get("started_at", now)) >= self.idle_s):
            code = self.az.deallocate()
            self.event(now, "scale_in", idle_s=round(now - st.get("last_busy", now), 1), azure_status=code,
                       up_s=round(now - st.get("started_at", now), 1))
            st["started_by_controller"] = False

        self.save()

    def save(self) -> None:
        tmp = self.root / "state.tmp"
        tmp.write_text(json.dumps(self.st, indent=1, sort_keys=True) + "\n")
        tmp.replace(self.root / "state.json")


def load_sp() -> dict:
    cred = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/nonexistent")) / "azure-sp"
    sp = json.loads(cred.read_text())
    missing = [k for k in ("appId", "password", "tenant") if not sp.get(k)]
    if missing:
        raise SystemExit(f"azure-sp credential lacks {missing}")
    return sp


def main() -> int:
    if not SUB:
        log("AZ_SUBSCRIPTION is not set (/etc/fedfleet/elastic.env)")
        return 1
    if not os.environ.get("FEDFLEET_ORIGIN_KEY"):
        log("FEDFLEET_ORIGIN_KEY is not set; cannot submit queued runs")
        return 1
    c = Controller(Azure(load_sp(), SUB, RG, VM))
    log(f"watching {VM} in {RG}; idle {IDLE_S} s, queue timeout {QUEUE_TIMEOUT_S} s, "
        f"budget {BUDGET_MIN} min/day")
    while True:
        try:
            c.tick(time.time())
        except Exception as e:  # noqa: BLE001  never die on one bad tick
            log(f"tick failed: {e!r}")
        time.sleep(TICK_S)


if __name__ == "__main__":
    sys.exit(main())
