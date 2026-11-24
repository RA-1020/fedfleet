"""fedfleet_service_up from systemctl, without systemd (stubbed)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy" / "monitoring"))
import fedfleet_exporter as ex  # noqa: E402


def test_service_up_reports_each_unit():
    def run(cmd, **kw):
        state = "active" if cmd[-1] == "fedfleet-superlink" else "failed"
        return subprocess.CompletedProcess(cmd, 0, state + "\n", "")
    text = ex.services_text(("fedfleet-superlink", "fedfleet-backup"), run=run)
    assert 'fedfleet_service_up{service="fedfleet-superlink"} 1.0' in text
    assert 'fedfleet_service_up{service="fedfleet-backup"} 0.0' in text
    assert "# TYPE fedfleet_service_up gauge" in text


def test_no_systemctl_reports_nothing():
    def run(cmd, **kw):
        raise FileNotFoundError("systemctl")
    assert ex.services_text(run=run) == ""
