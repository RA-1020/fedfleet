"""The elasticity controller, with a fake Azure and a fake aggregator."""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "elastic"))
import fedfleet_elastic as el  # noqa: E402


class FakeAzure:
    def __init__(self, power="deallocated"):
        self.p, self.calls, self.fail = power, [], False

    def power(self):
        if self.fail:
            raise OSError("azure down")
        return self.p

    def start(self):
        self.calls.append("start"); self.p = "starting"; return 202

    def deallocate(self):
        self.calls.append("deallocate"); self.p = "deallocating"; return 202


class World:
    def __init__(self, online=2, active=False):
        self.online, self.active, self.posted, self.answer = online, active, [], (202, '{"run_id":"9"}')

    def post(self, body):
        self.posted.append(body); return self.answer


def make(tmp_path, power="deallocated", online=2, active=False, **kw):
    az, w = FakeAzure(power), World(online, active)
    c = el.Controller(az, tmp_path, online=lambda: w.online, active=lambda: w.active, post=w.post,
                      idle_s=600, queue_timeout_s=900, budget_min=180, **kw)
    return c, az, w


def queue(tmp_path, name="q", need=3, at=1000.0):
    (tmp_path / "queue").mkdir(exist_ok=True)
    (tmp_path / "queue" / f"{name}.json").write_text(json.dumps(
        {"queued_at": at, "request": {"name": name, "rounds": 3, "min_nodes": need, "wait_for_sites": False}}))


def events(tmp_path):
    return [json.loads(l)["event"] for l in (tmp_path / "events.jsonl").read_text().splitlines()]


def test_full_cycle_scale_out_dispatch_scale_in(tmp_path):
    c, az, w = make(tmp_path)
    queue(tmp_path)
    c.tick(1000)                                   # 2 online, run needs 3, VM off -> start it
    assert az.calls == ["start"] and c.st["started_by_controller"]
    az.p = "running"; c.tick(1060)                 # booting, site not yet registered
    assert w.posted == []
    w.online = 3; c.tick(1120)                     # site 2 online -> submit the run
    assert w.posted[0]["name"] == "q" and not list((tmp_path / "queue").glob("*.json"))
    w.active = True
    for t in range(1140, 1500, 20):
        c.tick(t)                                  # run in progress: never scale in
    assert az.calls == ["start"]
    w.active = False
    c.tick(1500); c.tick(1480 + 590)               # idle since the last busy tick (1480), not 10 min yet
    assert az.calls == ["start"]
    c.tick(1480 + 610)                             # 10 min idle -> deallocate
    assert az.calls == ["start", "deallocate"] and not c.st["started_by_controller"]
    assert events(tmp_path) == ["vm_power", "scale_out", "vm_power", "run_dispatched", "scale_in"]


def test_vm_started_by_a_person_is_never_stopped(tmp_path):
    c, az, w = make(tmp_path, power="running", online=3)
    for t in range(1000, 5000, 20):
        c.tick(t)
    assert az.calls == []
    assert c.st["vm_minutes_today"] > 60            # but its minutes are counted


def test_budget_blocks_scale_out(tmp_path):
    c, az, w = make(tmp_path)
    c.st["minutes"] = {el.utc_day(1000): 180.0}
    queue(tmp_path)
    c.tick(1000); c.tick(1020)
    assert az.calls == [] and events(tmp_path).count("scale_out_refused_budget") == 1


def test_queue_timeout_and_refusal_move_entries_to_failed(tmp_path):
    c, az, w = make(tmp_path, power="running", online=1)
    queue(tmp_path, "old", at=0.0)
    c.tick(1000)
    assert (tmp_path / "queue" / "failed" / "old.json").exists() and w.posted == []
    w.online = 3; w.answer = (409, "run folder fedfleet-runs-new already exists")
    queue(tmp_path, "new", at=990.0)
    c.tick(1020)
    assert (tmp_path / "queue" / "failed" / "new.json").exists()
    assert "run_refused" in events(tmp_path)


def test_too_few_sites_answer_keeps_the_entry(tmp_path):
    c, az, w = make(tmp_path, power="running", online=3)
    w.answer = (409, "only 2 site(s) online, and this run needs 3")
    queue(tmp_path)
    c.tick(1000)
    assert (tmp_path / "queue" / "q.json").exists()


def test_azure_unreachable_changes_nothing(tmp_path):
    c, az, w = make(tmp_path)
    az.fail = True
    queue(tmp_path)
    c.tick(1000)
    assert az.calls == [] and w.posted == []


def test_no_dispatch_while_a_run_is_active(tmp_path):
    c, az, w = make(tmp_path, power="running", online=3, active=True)
    queue(tmp_path)
    c.tick(1000)
    assert w.posted == []


def test_azure_rest_calls(tmp_path):
    seen = []

    class Resp(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout=60):
        seen.append((req.get_method(), req.full_url))
        if "oauth2" in req.full_url:
            return Resp(json.dumps({"access_token": "tok", "expires_in": 3600}).encode())
        if req.full_url.split("?")[0].endswith("/instanceView"):
            assert req.headers["Authorization"] == "Bearer tok"
            return Resp(json.dumps({"statuses": [{"code": "ProvisioningState/succeeded"},
                                                 {"code": "PowerState/deallocated"}]}).encode())
        r = Resp(b""); r.status = 202; return r

    az = el.Azure({"appId": "a", "password": "p", "tenant": "t"}, "sub", "rg", "vm", opener=opener)
    assert az.power() == "deallocated" and az.start() == 202 and az.deallocate() == 202
    paths = [u.split("?")[0] for _, u in seen]
    assert paths[0] == "https://login.microsoftonline.com/t/oauth2/v2.0/token"
    assert paths[1:] == [f"https://management.azure.com/subscriptions/sub/resourceGroups/rg/providers/"
                         f"Microsoft.Compute/virtualMachines/vm/{a}" for a in ("instanceView", "start", "deallocate")]
    assert sum("oauth2" in u for _, u in seen) == 1   # token reused


def test_site_that_never_joins_is_flagged_once(tmp_path):
    # 25 Nov: the VM booted but site 2's service could not start flower-superexec.
    c, az, w = make(tmp_path)
    queue(tmp_path)
    c.tick(1000)                                   # scale out
    az.p = "running"; c.tick(1060)                 # up, site missing
    for t in range(1080, 1060 + el.JOIN_GRACE_S, 20):
        c.tick(t)
    assert "site_not_joined" not in events(tmp_path)
    for t in range(1060 + el.JOIN_GRACE_S, 1060 + el.JOIN_GRACE_S + 200, 20):
        c.tick(t)
    assert events(tmp_path).count("site_not_joined") == 1 and c.st["site_not_joined"]
    w.online = 3; c.tick(1600)                     # site fixed (within the queue timeout): the run goes out
    assert w.posted and events(tmp_path)[-1] == "run_dispatched"
