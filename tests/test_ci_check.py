"""scripts/ci_check_runs.py passes good runs and catches the faults it exists for."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "ci_check_runs.py"


def make(folder: Path, accs, weights, sites=3, site_extra=None):
    folder.mkdir(parents=True)
    rows = []
    for i, acc in enumerate(accs, 1):
        s = [{"site": k, "train_seconds": 1.0, **(site_extra or {})} for k in range(sites)]
        rows.append({"round": i, "train_round_seconds": 5.0, "global_acc": acc, "nodes_asked": sites,
                     "nodes_replied": sites, "aggregated": True, "sites": s})
    (folder / "rounds.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    torch.save({"w": torch.tensor(weights)}, folder / "final_exchanged.pt")


def check(tmp_path):
    r = subprocess.run([sys.executable, str(CHECK), "--rounds", "2", "--sites", "3",
                        str(tmp_path / "plain"), str(tmp_path / "secagg")], capture_output=True, text=True)
    return r.returncode, r.stdout


def test_quantisation_sized_difference_passes(tmp_path):
    make(tmp_path / "plain", [0.2030, 0.2485], [0.1, -0.05, 0.02])
    make(tmp_path / "secagg", [0.2030, 0.2455], [0.1007, -0.0502, 0.02])   # as measured on 27 Nov
    code, out = check(tmp_path)
    assert code == 0, out


def test_wrong_aggregate_fails(tmp_path):
    make(tmp_path / "plain", [0.2030, 0.2485], [0.1, -0.05, 0.02])
    make(tmp_path / "secagg", [0.2030, 0.2485], [0.13, -0.05, 0.02])       # e.g. a weighting bug
    code, out = check(tmp_path)
    assert code == 1 and "parameters differ" in out


def test_leaked_site_loss_fails(tmp_path):
    make(tmp_path / "plain", [0.2030, 0.2485], [0.1, -0.05, 0.02])
    make(tmp_path / "secagg", [0.2030, 0.2485], [0.1, -0.05, 0.02], site_extra={"train_loss": 1.9})
    code, out = check(tmp_path)
    assert code == 1 and "in the clear" in out


def test_missing_site_reply_fails(tmp_path):
    make(tmp_path / "plain", [0.2030, 0.2485], [0.1, -0.05, 0.02], sites=2)
    make(tmp_path / "secagg", [0.2030, 0.2485], [0.1, -0.05, 0.02])
    code, out = check(tmp_path)
    assert code == 1 and "of 3 sites replied" in out
