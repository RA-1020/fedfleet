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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
