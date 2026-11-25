"""Checks for deploy/control_api/app.py with a fake home folder and a stubbed flwr (no VM needed)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(tempfile.mkdtemp())
os.environ["HOME"] = str(HOME)
os.environ["FEDFLEET_DIR"] = str(ROOT)
os.environ["FEDFLEET_ORIGIN_KEY"] = "test-origin-key"
os.environ["FEDFLEET_AUDIT"] = str(HOME / "audit.jsonl")
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "deploy" / "control_api"))
from test_cost import ROWS  # noqa: E402

(HOME / "fedfleet-runs-crash").mkdir()
(HOME / "fedfleet-runs-crash" / "rounds.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS) + "\n")
(HOME / "fedfleet-runs-e9" / "a").mkdir(parents=True)
(HOME / "fedfleet-runs-e9" / "a" / "rounds.jsonl").write_text(json.dumps(ROWS[0]) + "\n")

import app as api  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

c = TestClient(api.app)
# No aggregator in tests: three sites online and no run in progress, unless a test says otherwise.
api._online_sites = lambda: 3
api._run_active = lambda run_id: False
KEY = {"X-FedFleet-Origin-Key": "test-origin-key"}


def test_healthz_open_rest_needs_key():
    assert c.get("/healthz").status_code == 200
    for path in ("/status", "/runs", "/runs/fedfleet-runs-crash"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"X-FedFleet-Origin-Key": "wrong"}).status_code == 401
    assert c.post("/runs", json={"name": "x"}).status_code == 401


def test_runs_and_detail():
    r = c.get("/runs", headers=KEY).json()
    names = {x["run"] for x in r["runs"]}
    assert names == {"fedfleet-runs-crash", "fedfleet-runs-e9/a"}
    d = c.get("/runs/fedfleet-runs-crash", headers=KEY).json()
    assert d["rounds"] == 3 and d["cost_basis"] == "computed" and len(d["per_round"]) == 3
    assert c.get("/runs/nope", headers=KEY).status_code == 404
    assert c.get("/runs/fedfleet-runs-e9/a", headers=KEY).status_code == 200


def test_status():
    s = c.get("/status", headers=KEY).json()
    assert s["runs"] == 2 and "fedfleet-superlink" in s["services"]
    assert s["latest_round"]["run"] in {"fedfleet-runs-crash", "fedfleet-runs-e9/a"}


def test_start_run_validation_and_command():
    for bad in ({"name": "Bad Name"}, {"name": "ok", "rounds": 50}, {"name": "ok", "min_nodes": 9},
                {"name": "ok", "round_timeout": 5}, {"name": "ok", "compression": "zip"},
                {"name": "ok", "topk_ratio": 0}):
        assert c.post("/runs", json=bad, headers=KEY).status_code == 422, bad
    assert c.post("/runs", json={"name": "crash"}, headers=KEY).status_code == 409   # folder exists
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "Successfully started run 123456\n", "")
    orig = api.subprocess.run
    api.subprocess.run = fake_run
    try:
        r = c.post("/runs", json={"name": "demo-1", "rounds": 2, "min_nodes": 3, "compression": "topk-int8",
                                  "topk_ratio": 0.05}, headers=KEY)
        assert r.status_code == 202 and r.json()["run_id"] == "123456", r.text
        cmd = seen["cmd"]
        assert cmd[1:4] == ["run", str(ROOT), "fedfleet-vm"] and cmd[4] == "--run-config"
        cfg = cmd[5]
        for part in ("num-server-rounds=2", "min-nodes=3", 'metrics-dir="~/fedfleet-runs-demo-1"',
                     "head-only=true", "site-eval=false", "rates-path=", 'compression="topk-int8"',
                     "topk-ratio=0.05", "error-feedback=true", "seed=-1"):
            assert part in cfg, (part, cfg)
        api.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Connection unavailable")
        r = c.post("/runs", json={"name": "demo-2"}, headers=KEY)
        assert r.status_code == 502 and "Connection unavailable" in r.json()["detail"]
    finally:
        api.subprocess.run = orig


def test_run_config_carries_dp_settings():
    cfg = api._run_config(api.RunRequest(name="dp-z05", dp=True, dp_noise_multiplier=0.05), "/tmp/m")
    assert "dp=true" in cfg and "dp-noise-multiplier=0.05" in cfg
    assert "dp-clip-norm=1.5" in cfg and "dp-delta=1e-05" in cfg
    off = api._run_config(api.RunRequest(name="plain"), "/tmp/m")
    assert "dp=false" in off


def test_resume_rules_and_command():
    # no folder / no checkpoint
    assert c.post("/runs", json={"name": "nothing-here", "rounds": 8, "resume": True}, headers=KEY).status_code == 409
    assert c.post("/runs", json={"name": "crash", "rounds": 9, "resume": True}, headers=KEY).status_code == 409
    restored = HOME / "fedfleet-runs-restored"
    restored.mkdir()
    (restored / "latest_checkpoint.pt").write_bytes(b"x")
    (restored / "rounds.jsonl").write_text("".join(json.dumps({"round": r, "global_acc": 0.5}) + "\n" for r in range(6)))
    assert c.post("/runs", json={"name": "restored", "rounds": 5, "resume": True}, headers=KEY).status_code == 409
    r = c.post("/runs", json={"name": "restored", "rounds": 8, "resume": True, "dp": True}, headers=KEY)
    assert r.status_code == 409 and "without DP noise" in r.json()["detail"]
    noisy = HOME / "fedfleet-runs-noisy"
    noisy.mkdir()
    (noisy / "latest_checkpoint.pt").write_bytes(b"x")
    dp = {"noise_multiplier": 0.2, "clip_norm": 1.5, "sites": 3, "delta": 1e-5, "epsilon": 57.3}
    (noisy / "rounds.jsonl").write_text("".join(json.dumps({"round": r, "dp": dp}) + "\n" for r in (1, 2)))
    for body, word in (({}, "with DP noise"), ({"dp": True, "dp_noise_multiplier": 1.0}, "original noise"),
                       ({"dp": True, "dp_noise_multiplier": 0.2, "dp_delta": 1e-6}, "original noise")):
        r = c.post("/runs", json={"name": "noisy", "rounds": 5, "resume": True, **body}, headers=KEY)
        assert r.status_code == 409 and word in r.json()["detail"], (body, r.text)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "Successfully started run 42\n", "")
    orig = api.subprocess.run
    api.subprocess.run = fake_run
    try:
        r = c.post("/runs", json={"name": "restored", "rounds": 8, "resume": True}, headers=KEY)
        assert r.status_code == 202 and r.json()["rounds_done_before"] == 5, r.text
        assert "resume=true" in seen["cmd"][5] and "num-server-rounds=8" in seen["cmd"][5]
        r = c.post("/runs", json={"name": "noisy", "rounds": 5, "resume": True, "dp": True,
                                  "dp_noise_multiplier": 0.2}, headers=KEY)
        assert r.status_code == 202 and r.json()["rounds_done_before"] == 2, r.text
        r = c.post("/runs", json={"name": "fresh-one"}, headers=KEY)
        assert r.status_code == 202 and "resume=false" in seen["cmd"][5]
    finally:
        api.subprocess.run = orig


def test_secagg_settings_and_refusals():
    r = c.post("/runs", json={"name": "sa-bad", "secagg": True, "compression": "int8"}, headers=KEY)
    assert r.status_code == 422 and "secagg" in r.json()["detail"]
    cfg = api._run_config(api.RunRequest(name="sa", secagg=True, dp=True, dp_noise_multiplier=0.2), "/tmp/m")
    assert "secagg=true" in cfg and "dp=true" in cfg
    cfg = api._run_config(api.RunRequest(name="sa"), "/tmp/m")
    assert "secagg=false" in cfg
    assert "secagg=true" in api._run_config(api.RunRequest(name="sa", secagg=True), "/tmp/m")
    # resume must keep the aggregation mode of the original run
    folder = HOME / "fedfleet-runs-sa-run"
    folder.mkdir()
    (folder / "latest_checkpoint.pt").write_bytes(b"x")
    (folder / "rounds.jsonl").write_text("".join(json.dumps({"round": r, "secagg": {"aggregated": True}}) + "\n" for r in (1, 2)))
    r = c.post("/runs", json={"name": "sa-run", "rounds": 4, "resume": True}, headers=KEY)
    assert r.status_code == 409 and "it ran with secagg" in r.json()["detail"]
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "Successfully started run 7\n", "")
    orig = api.subprocess.run
    api.subprocess.run = fake_run
    try:
        r = c.post("/runs", json={"name": "sa-run", "rounds": 4, "resume": True, "secagg": True}, headers=KEY)
        assert r.status_code == 202 and r.json()["secagg"] is True and "secagg=true" in seen["cmd"][5], r.text
    finally:
        api.subprocess.run = orig


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_epsilon_budget_rules():
    for body in ({"dp_max_epsilon": 50}, {"dp": True, "dp_noise_multiplier": 0.0, "dp_max_epsilon": 50}):
        r = c.post("/runs", json={"name": "budget-bad", **body}, headers=KEY)
        assert r.status_code == 422 and "unbounded" in r.json()["detail"], (body, r.text)
    cfg = api._run_config(api.RunRequest(name="b", dp=True, dp_noise_multiplier=0.2, dp_max_epsilon=80), "/tmp/m")
    assert "dp-max-epsilon=80.0" in cfg
    spent = HOME / "fedfleet-runs-spent"
    spent.mkdir()
    (spent / "latest_checkpoint.pt").write_bytes(b"x")
    dp = {"noise_multiplier": 0.2, "clip_norm": 1.5, "sites": 3, "delta": 1e-5, "epsilon": 77.37, "releases": 3, "max_epsilon": 70.0}
    (spent / "rounds.jsonl").write_text("".join(json.dumps({"round": r, "dp": dp}) + "\n" for r in (1, 2, 3)))
    base = {"name": "spent", "rounds": 6, "resume": True, "dp": True, "dp_noise_multiplier": 0.2}
    r = c.post("/runs", json={**base, "dp_max_epsilon": 70}, headers=KEY)
    assert r.status_code == 409 and "budget already spent" in r.json()["detail"], r.text
    for other in (0, 100):                                   # the run's budget cannot change
        r = c.post("/runs", json={**base, "dp_max_epsilon": other}, headers=KEY)
        assert r.status_code == 409 and "original dp_max_epsilon" in r.json()["detail"], (other, r.text)
    within = HOME / "fedfleet-runs-within"
    within.mkdir()
    (within / "latest_checkpoint.pt").write_bytes(b"x")
    (within / "rounds.jsonl").write_text(json.dumps({"round": 3, "dp": {**dp, "max_epsilon": 100.0}}) + "\n")
    base = {**base, "name": "within"}
    orig = api.subprocess.run
    api.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Successfully started run 7\n", "")
    try:
        r = c.post("/runs", json={**base, "dp_max_epsilon": 100}, headers=KEY)
        assert r.status_code == 202 and r.json()["dp_max_epsilon"] == 100, r.text
    finally:
        api.subprocess.run = orig


def test_dp_with_compression_refused():
    r = c.post("/runs", json={"name": "dp-comp", "dp": True, "compression": "int8"}, headers=KEY)
    assert r.status_code == 422 and "clipping bound" in r.json()["detail"], r.text


def test_resume_refused_when_next_round_passes_the_budget():
    # the live case of 24 Nov: 4 releases at z 0.2 (epsilon 96.12), max_epsilon 100, a 5th would be 114.19
    d = HOME / "fedfleet-runs-at-limit"
    d.mkdir()
    (d / "latest_checkpoint.pt").write_bytes(b"x")
    dp = {"noise_multiplier": 0.2, "clip_norm": 1.5, "sites": 3, "delta": 1e-5, "epsilon": 96.12,
          "releases": 4, "max_epsilon": 100.0}
    (d / "rounds.jsonl").write_text("".join(json.dumps({"round": r, "dp": dp}) + "\n" for r in (1, 2, 3, 4)))
    r = c.post("/runs", json={"name": "at-limit", "rounds": 6, "resume": True, "dp": True,
                              "dp_noise_multiplier": 0.2, "dp_max_epsilon": 100}, headers=KEY)
    assert r.status_code == 409 and "one more round would pass" in r.json()["detail"], r.text


def test_busy_run_and_too_few_sites_refused():
    d = HOME / "fedfleet-runs-busy"
    d.mkdir()
    (d / "latest_checkpoint.pt").write_bytes(b"x")
    (d / "rounds.jsonl").write_text(json.dumps({"round": 1}) + "\n")
    (d / "active_run.json").write_text(json.dumps({"run_id": "99"}))
    api._run_active = lambda run_id: run_id == "99"
    try:
        r = c.post("/runs", json={"name": "busy", "rounds": 4, "resume": True}, headers=KEY)
        assert r.status_code == 409 and "still in progress" in r.json()["detail"], r.text
    finally:
        api._run_active = lambda run_id: False
    api._online_sites = lambda: 2
    try:
        r = c.post("/runs", json={"name": "needs-three", "min_nodes": 3}, headers=KEY)
        assert r.status_code == 409 and "only 2 site(s) online" in r.json()["detail"], r.text
        assert not (HOME / "fedfleet-runs-needs-three").exists()
    finally:
        api._online_sites = lambda: 3
    orig = api.subprocess.run
    api.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Successfully started run 55\n", "")
    try:
        r = c.post("/runs", json={"name": "marked"}, headers=KEY)
        assert r.status_code == 202, r.text
        assert json.loads((HOME / "fedfleet-runs-marked" / "active_run.json").read_text())["run_id"] == "55"
    finally:
        api.subprocess.run = orig


def test_audit_log_records_who_what_and_outcome():
    log = Path(os.environ["FEDFLEET_AUDIT"])
    before = len(log.read_text().splitlines()) if log.exists() else 0
    gw = {**KEY, "x-fedfleet-client-ip": "203.0.113.7", "x-fedfleet-client-agent": "curl/8",
          "x-fedfleet-token-id": "a1b2c3d4e5f6"}
    c.get("/healthz")                                               # not logged
    c.get("/status", headers={"x-fedfleet-client-ip": "198.51.100.1"})   # no key: 401, faked IP ignored
    orig = api.subprocess.run
    api.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Successfully started run 77\n", "")
    try:
        assert c.post("/runs", json={"name": "audited", "rounds": 2}, headers=gw).status_code == 202
    finally:
        api.subprocess.run = orig
    rows = [json.loads(l) for l in log.read_text().splitlines()[before:]]
    assert [r["path"] for r in rows] == ["/status", "/runs"]
    denied, started = rows
    assert denied["status"] == 401 and not denied["via_gateway"] and denied["client_ip"] != "198.51.100.1"
    assert started["status"] == 202 and started["via_gateway"] and started["client_ip"] == "203.0.113.7"
    assert started["token_id"] == "a1b2c3d4e5f6" and started["run"] == "audited" and started["run_id"] == "77"
    assert started["request"]["rounds"] == 2 and "test-origin-key" not in log.read_text()


def test_marker_keeps_the_request_for_the_watchdog():
    orig = api.subprocess.run
    api.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "Successfully started run 88\n", "")
    try:
        assert c.post("/runs", json={"name": "wdog", "rounds": 4, "dp": True, "dp_noise_multiplier": 0.3},
                      headers=KEY).status_code == 202
        m = json.loads((HOME / "fedfleet-runs-wdog" / "active_run.json").read_text())
        assert m["run_id"] == "88" and m["request"]["rounds"] == 4
        assert m["request"]["dp_noise_multiplier"] == 0.3 and m["request"]["auto_resume"] is True
        assert c.post("/runs", json={"name": "wdog2", "auto_resume": False},
                      headers=KEY).status_code == 202
        m = json.loads((HOME / "fedfleet-runs-wdog2" / "active_run.json").read_text())
        assert m["request"]["auto_resume"] is False
    finally:
        api.subprocess.run = orig


def test_second_run_request_while_one_is_in_progress_is_refused():
    assert api._start_lock.acquire(blocking=False)
    try:
        r = c.post("/runs", json={"name": "concurrent"}, headers=KEY)
        assert r.status_code == 409 and "another run request" in r.json()["detail"]
        assert not (HOME / "fedfleet-runs-concurrent").exists()
    finally:
        api._start_lock.release()


def test_wait_for_sites_queues_the_run():
    api._online_sites = lambda: 2
    try:
        r = c.post("/runs", json={"name": "elastic-q", "min_nodes": 3, "wait_for_sites": True}, headers=KEY)
        assert r.status_code == 202 and r.json()["queued"] is True and r.json()["needed"] == 3
        e = json.loads((HOME / "fedfleet-elastic" / "queue" / "elastic-q.json").read_text())
        assert e["request"]["wait_for_sites"] is False and e["request"]["min_nodes"] == 3
        assert c.post("/runs", json={"name": "elastic-q", "min_nodes": 3, "wait_for_sites": True},
                      headers=KEY).status_code == 409
        assert c.post("/runs", json={"name": "no-wait", "min_nodes": 3}, headers=KEY).status_code == 409
        assert [q["run"] for q in c.get("/status", headers=KEY).json()["queue"]] == ["elastic-q"]
    finally:
        api._online_sites = lambda: 3
