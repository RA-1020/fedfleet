"""The watchdog resumes a run only when Flower says it died with the aggregator."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy" / "watchdog"))
import fedfleet_watchdog as wd  # noqa: E402

CRASH = ("finished:failed", "No heartbeat received from the task")
REQ = {"name": "w", "rounds": 6, "min_nodes": 3, "dp": True, "dp_noise_multiplier": 0.2,
       "dp_max_epsilon": 100.0, "secagg": False, "resume": False, "auto_resume": True}


def folder(tmp_path, rounds_done=3, request=REQ, checkpoint=True, run_id="111"):
    f = tmp_path / "fedfleet-runs-w"
    f.mkdir()
    marker = {"run_id": run_id, "resume": False}
    if request is not None:
        marker["request"] = request
    (f / "active_run.json").write_text(json.dumps(marker))
    (f / "rounds.jsonl").write_text("".join(json.dumps({"round": r, "train_round_seconds": 20}) + "\n"
                                            for r in range(1, rounds_done + 1)))
    if checkpoint:
        (f / "latest_checkpoint.pt").write_bytes(b"x")
    return f


class Api:
    def __init__(self, answers):
        self.answers, self.bodies = list(answers), []

    def __call__(self, body):
        self.bodies.append(body)
        return self.answers.pop(0)


def test_crashed_run_is_resumed_once_with_its_own_settings(tmp_path):
    f, state, api = folder(tmp_path), {}, Api([(202, '{"run_id":"222"}')])
    wd.check(f, state, 1000.0, status_of=lambda r: CRASH, post=api)
    assert api.bodies == [{**REQ, "resume": True}]          # same DP settings and budget
    assert state["fedfleet-runs-w"]["resumes"] == 1
    wd.check(f, state, 1100.0, status_of=lambda r: CRASH, post=api)   # same lost run: not again
    assert len(api.bodies) == 1


def test_the_resumed_run_can_be_resumed_again_up_to_the_limit(tmp_path):
    f, state = folder(tmp_path), {}
    api = Api([(202, "ok")] * 5)
    for i in range(5):
        (f / "active_run.json").write_text(json.dumps({"run_id": str(500 + i), "request": REQ}))
        wd.check(f, state, 1000.0 + 100 * i, status_of=lambda r: CRASH, post=api)
    assert len(api.bodies) == wd.MAX_RESUMES


def test_runs_that_did_not_crash_are_left_alone(tmp_path):
    for status in [("finished:completed", ""), ("finished:stopped", ""),
                   ("finished:failed", "ServerApp failed with exception: privacy budget exhausted"),
                   ("running", "")]:
        f, state, api = folder(tmp_path), {}, Api([])
        wd.check(f, state, 1000.0, status_of=lambda r, s=status: s, post=api)
        assert api.bodies == [], status
        for p in f.iterdir():
            p.unlink()
        f.rmdir()


def test_a_running_run_is_checked_again_later(tmp_path):
    f, state = folder(tmp_path), {}
    wd.check(f, state, 1000.0, status_of=lambda r: ("running", ""), post=Api([]))
    api = Api([(202, "ok")])
    wd.check(f, state, 1030.0, status_of=lambda r: CRASH, post=api)
    assert len(api.bodies) == 1


def test_refusal_is_retried_then_given_up(tmp_path):
    f, state = folder(tmp_path), {}
    api = Api([(409, "only 2 site(s) online")] * 100)
    wd.check(f, state, 1000.0, status_of=lambda r: CRASH, post=api)
    wd.check(f, state, 1010.0, status_of=lambda r: CRASH, post=api)        # too soon
    assert len(api.bodies) == 1
    wd.check(f, state, 1030.0, status_of=lambda r: CRASH, post=api)
    assert len(api.bodies) == 2
    wd.check(f, state, 1000.0 + wd.GIVE_UP_S + 1, status_of=lambda r: CRASH, post=api)
    assert len(api.bodies) == 2 and "111" in state["fedfleet-runs-w"]["final"]


def test_not_resumed_without_request_checkpoint_rounds_left_or_consent(tmp_path):
    cases = [dict(request=None), dict(checkpoint=False), dict(rounds_done=6),
             dict(request={**REQ, "auto_resume": False})]
    for kw in cases:
        f, api = folder(tmp_path, **kw), Api([])
        wd.check(f, {}, 1000.0, status_of=lambda r: CRASH, post=api)
        assert api.bodies == [], kw
        for p in f.iterdir():
            p.unlink()
        f.rmdir()


def test_flower_not_answering_is_not_a_crash(tmp_path):
    f, state, api = folder(tmp_path), {}, Api([])
    wd.check(f, state, 1000.0, status_of=lambda r: None, post=api)
    assert api.bodies == [] and state["fedfleet-runs-w"]["final"] == []
