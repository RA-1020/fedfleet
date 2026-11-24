"""Prometheus exporter for FedFleet's per-round metrics (standard library only).

Reads every rounds.jsonl under the metrics folders and serves the latest round of each run,
plus running totals, in Prometheus text format on /metrics:

    python deploy/monitoring/fedfleet_exporter.py --listen 100.69.230.89:9101

It runs beside the aggregator, not inside it: a run's ServerApp is a short-lived process,
while this keeps serving between runs. It only reads files, so it cannot disturb training.
Rows without a "cost" column (runs before cost metering) are costed with the rate card.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from fedfleet.cost import load_rates, round_cost  # noqa: E402

DEFAULT_GLOBS = ["~/fedfleet-runs*/rounds.jsonl", "~/fedfleet-runs*/*/rounds.jsonl"]


class Run:
    """Incremental reader for one rounds.jsonl: remembers where it stopped."""

    def __init__(self, path: Path, name: str):
        self.path, self.name = path, name
        self.offset = 0
        self.inode = None
        self.prev_ts = None
        self.last: dict | None = None
        self.last_cost: dict | None = None
        self.totals = {"rounds": 0, "bytes_up": 0, "bytes_down": 0,
                       "provider_usd": 0.0, "customer_usd": 0.0, "nodes_dropped": 0}
        self.resumes = 0

    def reset(self):
        self.__init__(self.path, self.name)

    def poll(self, rates: dict | None) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.reset()                      # file replaced or truncated: start again
        self.inode = st.st_ino
        with self.path.open("rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return                            # no complete line yet
        self.offset += end + 1
        for line in data[:end].splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._add(row, rates)

    def _add(self, row: dict, rates: dict | None) -> None:
        ts = row.get("timestamp")
        if row.get("event") == "resumed":
            self.resumes += 1
        elif "round" in row and row["round"] != 0 and "train_round_seconds" in row:
            cost = row.get("cost")
            if cost is None and rates is not None:
                wall = ts - self.prev_ts if (ts is not None and self.prev_ts is not None) else None
                cost = round_cost(row, rates, wall)
            self.last, self.last_cost = row, cost
            t = self.totals
            t["rounds"] += 1
            t["bytes_up"] += row.get("bytes_up", 0) or 0
            t["bytes_down"] += row.get("bytes_down", 0) or 0
            t["nodes_dropped"] += row.get("nodes_dropped", 0) or 0
            if cost:
                t["provider_usd"] += cost["provider_usd"]
                t["customer_usd"] += cost["customer_usd"]
        if ts is not None:
            self.prev_ts = ts


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(runs: list[Run]) -> str:
    # Prometheus wants every sample of one metric together, under one HELP and TYPE.
    families: dict[str, tuple[str, str, list[str]]] = {}

    def metric(name, kind, help_, labels, value):
        if value is None:
            return
        lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        families.setdefault(name, (kind, help_, []))[2].append(f"{name}{{{lab}}} {float(value)!r}")

    for r in sorted(runs, key=lambda r: r.name):
        L = {"run": r.name}
        t = r.totals
        metric("fedfleet_rounds_total", "counter", "Training rounds recorded", L, t["rounds"])
        metric("fedfleet_resumes_total", "counter", "Resumes from a checkpoint", L, r.resumes)
        metric("fedfleet_bytes_up_total", "counter", "Bytes sent by sites to the aggregator", L, t["bytes_up"])
        metric("fedfleet_bytes_down_total", "counter", "Bytes sent by the aggregator to sites", L, t["bytes_down"])
        metric("fedfleet_nodes_dropped_total", "counter", "Asked sites that sent no valid reply", L, t["nodes_dropped"])
        for side in ("provider", "customer"):
            metric("fedfleet_cost_usd_total", "counter",
                   "Cost at list prices before free tiers and credits", {**L, "side": side}, t[f"{side}_usd"])
        row = r.last
        if not row:
            continue
        metric("fedfleet_round", "gauge", "Latest training round", L, row.get("round"))
        metric("fedfleet_round_timestamp_seconds", "gauge", "When the latest round was written", L, row.get("timestamp"))
        metric("fedfleet_nodes_online", "gauge", "Sites online at the latest round", L, row.get("nodes_online"))
        metric("fedfleet_nodes_asked", "gauge", "Sites asked in the latest round", L, row.get("nodes_asked"))
        metric("fedfleet_nodes_replied", "gauge", "Sites that replied in the latest round", L, row.get("nodes_replied"))
        metric("fedfleet_round_bytes_up", "gauge", "Bytes up in the latest round", L, row.get("bytes_up"))
        metric("fedfleet_round_bytes_down", "gauge", "Bytes down in the latest round", L, row.get("bytes_down"))
        metric("fedfleet_bytes_per_update", "gauge", "Size of one model update", L, row.get("bytes_per_update"))
        metric("fedfleet_round_seconds", "gauge", "Training exchange time of the latest round", L, row.get("train_round_seconds"))
        metric("fedfleet_global_accuracy", "gauge", "Accuracy on the public test set", L, row.get("global_acc"))
        metric("fedfleet_global_loss", "gauge", "Loss on the public test set", L, row.get("global_loss"))
        dp = row.get("dp") or {}
        metric("fedfleet_dp_epsilon", "gauge", "Privacy spent so far (epsilon at fedfleet_dp_delta)", L, dp.get("epsilon"))
        metric("fedfleet_dp_max_epsilon", "gauge", "Privacy budget: the run stops before epsilon would pass it", L, dp.get("max_epsilon"))
        metric("fedfleet_dp_delta", "gauge", "Delta at which epsilon is reported", L, dp.get("delta") if dp else None)
        metric("fedfleet_dp_noise_multiplier", "gauge", "Gaussian noise multiplier", L, dp.get("noise_multiplier"))
        metric("fedfleet_dp_clip_norm", "gauge", "L2 clipping bound on each site update", L, dp.get("clip_norm"))
        clipped = [s.get("clipped") for s in row.get("sites", []) if s.get("clipped") is not None]
        metric("fedfleet_dp_sites_clipped", "gauge", "Sites whose update was clipped in the latest round", L,
               sum(clipped) if clipped else None)
        c = r.last_cost
        if c:
            for side in ("provider", "customer"):
                metric("fedfleet_round_cost_usd", "gauge", "Cost of the latest round at list prices",
                       {**L, "side": side}, c.get(f"{side}_usd"))
            metric("fedfleet_round_wall_seconds", "gauge", "Wall time of the latest round, row to row", L, c.get("wall_seconds"))
        for s in row.get("sites") or []:
            SL = {**L, "site": "unknown" if s.get("site") is None else s["site"]}
            metric("fedfleet_site_train_seconds", "gauge", "Site training time, latest round", SL, s.get("train_seconds"))
            metric("fedfleet_site_turnaround_seconds", "gauge",
                   "Instruction created to reply created, latest round (two clocks)", SL, s.get("turnaround_seconds"))
    out: list[str] = []
    for name, (kind, help_, lines) in families.items():
        out += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}", *lines]
    return "\n".join(out) + "\n"


def run_name(path: Path) -> str:
    """Folder of the run, relative to home: fedfleet-runs-crash, fedfleet-runs-e9/head-lr0.001-r1."""
    try:
        return str(path.parent.resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return path.parent.name


class Collector:
    def __init__(self, patterns: list[str], rates: dict | None):
        self.patterns, self.rates = patterns, rates
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()

    def scrape(self) -> str:
        with self.lock:
            for pat in self.patterns:
                for p in glob.glob(os.path.expanduser(pat)):
                    path = Path(p)
                    key = str(path.resolve())
                    if key not in self.runs:
                        self.runs[key] = Run(path, run_name(path))
            for r in self.runs.values():
                r.poll(self.rates)
            return render(list(self.runs.values()))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", default="127.0.0.1:9101", help="host:port; on the VM use the Tailscale address")
    p.add_argument("--glob", action="append", help="rounds.jsonl pattern (repeatable)")
    p.add_argument("--rates", default=str(Path(__file__).resolve().parent.parent / "rates.toml"))
    a = p.parse_args()
    rates = load_rates(a.rates) if Path(a.rates).exists() else None
    col = Collector(a.glob or DEFAULT_GLOBS, rates)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/metrics":
                self.send_error(404)
                return
            body = col.scrape().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    host, port = a.listen.rsplit(":", 1)
    print(f"fedfleet exporter on http://{host}:{port}/metrics, rate card: {'yes' if rates else 'no'}", flush=True)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main()
