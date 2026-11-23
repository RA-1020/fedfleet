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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
