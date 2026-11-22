"""Checks for fedfleet/cost.py on rows shaped like the 19 Nov cloud runs."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fedfleet.cost import load_rates, round_cost, run_costs  # noqa: E402

RATES = load_rates(Path(__file__).resolve().parent.parent / "deploy" / "rates.toml")


def site(i, setup, train):
    return {"site": i, "node": 100 + i, "setup_seconds": setup, "train_seconds": train}


ROWS = [
    {"round": 0, "global_acc": 0.48125, "timestamp": 1795081555.552422},
    {"round": 1, "nodes_asked": 3, "nodes_replied": 3, "bytes_per_update": 28248, "bytes_down": 84744,
     "train_round_seconds": 29.326, "timestamp": 1795081591.4611707,
     "sites": [site(0, 0.502, 8.222), site(1, 0.498, 8.11), site(2, 8.419, 13.632)], "nodes_missing": []},
    {"round": 2, "nodes_asked": 3, "nodes_replied": 3, "bytes_per_update": 28248, "bytes_down": 84744,
     "train_round_seconds": 18.759, "timestamp": 1795081616.885144,
     "sites": [site(0, 0.19, 8.347), site(1, 0.403, 7.729), site(2, 0.381, 11.747)], "nodes_missing": []},
    {"event": "resumed", "global_acc": 0.497, "timestamp": 1795082254.148},
    {"round": 4, "nodes_asked": 3, "nodes_replied": 3, "bytes_per_update": 28248, "bytes_down": 84744,
     "train_round_seconds": 22.863, "timestamp": 1795082283.657008,
     "sites": [site(0, 0.326, 8.301), site(1, 0.204, 7.622), site(2, 0.381, 11.824)], "nodes_missing": []},
]


def test_aggregator_rate_from_shape():
    assert math.isclose(RATES["aggregator"]["usd_per_hour"], 2 * 0.01 + 12 * 0.0015)


def test_round_one_by_hand():
    c = round_cost(ROWS[1], RATES, wall_seconds=35.9087)
    assert math.isclose(c["provider_compute_usd"], 35.9087 * 0.038 / 3600)
    assert math.isclose(c["customer_compute_usd"], 35.9087 * 0.0448 / 3600)       # only site 2 is billed
    assert math.isclose(c["customer_busy_usd"], (8.419 + 13.632) * 0.0448 / 3600)
    assert math.isclose(c["customer_egress_usd"], 28248 * 0.12 / 1e9)
    assert math.isclose(c["total_usd"], c["provider_usd"] + c["customer_usd"])


def test_run_costs_skips_round0_and_event_and_uses_gaps():
    out = run_costs(ROWS, RATES)
    assert [r["round"] for r, _ in out] == [1, 2, 4]
    assert math.isclose(out[0][1]["wall_seconds"], 1795081591.4611707 - 1795081555.552422, abs_tol=1e-3)
    # after a resume the gap is measured from the resumed row, not from the pre-crash round
    assert math.isclose(out[2][1]["wall_seconds"], 1795082283.657008 - 1795082254.148, abs_tol=1e-3)


def test_missing_sites_counted():
    row = {"round": 3, "nodes_asked": 3, "nodes_replied": 1, "bytes_per_update": 28248, "bytes_down": 84744,
           "train_round_seconds": 51.535, "sites": [site(2, 0.378, 11.709)],
           "nodes_missing": [1643855550748340826, 16641682726303163882]}
    c = round_cost(row, RATES)
    assert sum(1 for s in c["sites"] if s.get("missing")) == 2
    assert math.isclose(c["customer_compute_usd"], 51.535 * 0.0448 / 3600)    # default_site rate is 0


def test_unknown_site_uses_default():
    row = {"round": 1, "bytes_per_update": 10, "bytes_down": 10, "train_round_seconds": 10.0,
           "sites": [site(7, 1.0, 1.0)]}
    assert round_cost(row, RATES)["customer_usd"] == 0.0


def test_site_upload_size_used_for_egress():
    """With compression a site sends fewer bytes than the update size; egress follows the site's bytes."""
    row = {"round": 1, "bytes_per_update": 28248, "bytes_down": 3 * 28248, "train_round_seconds": 10.0,
           "sites": [dict(site(2, 1.0, 1.0), bytes_up=3217)]}
    old = {"round": 1, "bytes_per_update": 28248, "bytes_down": 3 * 28248, "train_round_seconds": 10.0,
           "sites": [site(2, 1.0, 1.0)]}
    egress = lambda r: round_cost(r, RATES)["customer_egress_usd"]
    assert egress(old) > 0 and math.isclose(egress(row), egress(old) * 3217 / 28248)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
