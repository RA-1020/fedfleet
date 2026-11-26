"""Checks for deploy/azure_function/fedfleet_report.py against a mock Blob endpoint (no Azure needed)."""
import json
import math
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy" / "azure_function"))
sys.path.insert(0, str(ROOT / "tests"))
from fedfleet.cost import load_rates, run_costs  # noqa: E402
from test_cost import ROWS  # noqa: E402
import fedfleet_report as fr  # noqa: E402

RATES = load_rates(ROOT / "deploy" / "rates.toml")


def with_cost(rows):
    """Attach the live-style cost dict that comms.py writes, using the offline calculator."""
    out = [dict(r) for r in rows]
    costs = {id(r): c for r, c in run_costs(rows, RATES)}
    for src, dst in zip(rows, out):
        c = costs.get(id(src))
        if c:
            dst["cost"] = {k: c[k] for k in ("wall_seconds", "provider_usd", "customer_usd", "customer_busy_usd", "total_usd")}
    return out


RUN_A = with_cost(ROWS)
for _r, _acc in zip([r for r in RUN_A if isinstance(r.get("round"), int) and r["round"] >= 1], (0.41, 0.45, 0.5)):
    _r["global_acc"] = _acc          # live rows carry the round's global accuracy; the abbreviated test rows do not
RUN_B = ROWS                       # an older run: no cost fields


def jsonl(rows, extra=""):
    return ("\n".join(json.dumps(r) for r in rows) + "\n" + extra).encode()


FILES = {
    "fedfleet-runs-crash/rounds.jsonl": jsonl(RUN_A),
    "fedfleet-runs-old/rounds.jsonl": jsonl(RUN_B, "not json\n"),
    "fedfleet-runs-crash/latest_checkpoint.pt": b"\x00\x01",
    "rounds.jsonl": jsonl(RUN_B[:2]),
}
SEEN = []


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        SEEN.append(u.query)
        if "sig" not in q:
            self.send_response(403); self.end_headers(); return
        if q.get("comp") == ["list"]:
            names = sorted(FILES)
            page = names[:2] if "marker" not in q else names[2:]
            nxt = "m1" if "marker" not in q else ""
            body = ("<?xml version='1.0'?><EnumerationResults><Blobs>" +
                    "".join(f"<Blob><Name>{n}</Name></Blob>" for n in page) +
                    f"</Blobs><NextMarker>{nxt}</NextMarker></EnumerationResults>").encode()
        else:
            name = urllib.parse.unquote(u.path.split("/", 2)[2])
            if name not in FILES:
                self.send_response(404); self.end_headers(); return
            body = FILES[name]
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)


def serve():
    srv = HTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_split_sas_rejects_bad_urls():
    for bad in ("http://x/c?sig=1", "https://x/c", "https://x/a/b?sig=1", ""):
        try:
            fr.split_sas(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert fr.split_sas("https://acct.blob.core.windows.net/c/?sv=1&sig=abc") == (
        "https://acct.blob.core.windows.net/c", "sv=1&sig=abc")


def test_summarize_matches_offline_calculator():
    s = fr.summarize(RUN_A)
    costs = [c for _, c in run_costs(ROWS, RATES)]
    assert s["rounds"] == 3 and s["last_round"] == 4 and s["resumes"] == 1
    assert s["costed_rounds"] == 3
    assert math.isclose(s["total_usd"], sum(c["total_usd"] for c in costs))
    assert math.isclose(s["wall_seconds"], sum(c["wall_seconds"] for c in costs))
    assert s["final_acc"] == 0.5          # last round's accuracy, not the 0.497 on the "resumed" event row


def test_old_run_has_no_cost_without_rate_card():
    s = fr.summarize(RUN_B)
    assert s["costed_rounds"] == 0 and s["total_usd"] is None and s["cost_basis"] == "none" and s["rounds"] == 3


def test_old_run_is_costed_from_rate_card():
    rates, costing = fr.rate_card()
    assert rates is not None
    s = fr.summarize(RUN_B, rates=rates, costing=costing)
    want = sum(c["total_usd"] for _, c in run_costs(ROWS, RATES))
    assert s["costed_rounds"] == 3 and s["cost_basis"] == "computed" and math.isclose(s["total_usd"], want)
    assert fr.summarize(RUN_A, rates=rates, costing=costing)["cost_basis"] == "logged"
    assert math.isclose(fr.summarize(RUN_A, rates=rates, costing=costing)["total_usd"], want)


def test_build_report_against_mock_blob():
    srv = serve()
    url = "https://x/c?sv=1&sig=abc"          # shape check only; the real URL is http://127.0.0.1 below
    fr.split_sas(url)
    base = f"http://127.0.0.1:{srv.server_port}/c"
    # split_sas insists on https, so call the lower layers the same way build_report does
    names = fr.list_round_blobs(base, "sv=1&sig=abc")
    assert names == ["fedfleet-runs-crash/rounds.jsonl", "fedfleet-runs-old/rounds.jsonl", "rounds.jsonl"], names
    text = fr._get(f"{base}/fedfleet-runs-crash/rounds.jsonl?sv=1&sig=abc", 5).decode()
    rows, bad = fr.parse_rows(text)
    assert len(rows) == len(RUN_A) and bad == 0
    rows, bad = fr.parse_rows(FILES["fedfleet-runs-old/rounds.jsonl"].decode())
    assert bad == 1
    assert fr.run_name("rounds.jsonl") == "(root)" and fr.run_name("a/b/rounds.jsonl") == "a/b"
    srv.shutdown()


def test_full_build_report_over_mock(monkeypatch=None):
    srv = serve()
    orig = fr.split_sas
    fr.split_sas = lambda u: (f"http://127.0.0.1:{srv.server_port}/c", "sv=1&sig=abc")
    try:
        rep = fr.build_report("ignored")
        assert [r["run"] for r in rep["runs"]][0] == "fedfleet-runs-crash"     # newest last_timestamp first
        assert len(rep["runs"]) == 3 and rep["runs"][0]["costed_rounds"] == 3
        assert {r["run"]: r["cost_basis"] for r in rep["runs"]}["fedfleet-runs-old"] == "computed" and rep["rate_card_loaded"]
        one = fr.build_report("ignored", run="fedfleet-runs-crash")
        assert len(one["runs"]) == 1 and len(one["runs"][0]["per_round"]) == 3
        page = fr.render_html(rep)
        assert "fedfleet-runs-crash" in page and "sig=" not in page and "abc" not in page
    finally:
        fr.split_sas = orig
        srv.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
