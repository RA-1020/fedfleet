"""Round report built from the rounds.jsonl files that the Oracle VM copies to Blob storage.

Standard library only. The container URL with a read-only SAS (Read + List) comes from the
app setting BLOB_SAS_URL. Nothing here ever prints or returns that URL.
"""
from __future__ import annotations

import html
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

API_VERSION = "2021-12-02"
SUFFIX = "rounds.jsonl"


def split_sas(url: str) -> tuple[str, str]:
    """(container URL without query, SAS query string)."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme != "https" or "sig=" not in parts.query or len(parts.path.strip("/").split("/")) != 1:
        raise ValueError("BLOB_SAS_URL must be one https container URL with a SAS token (?sv=...&sig=...)")
    return f"https://{parts.netloc}{parts.path.rstrip('/')}", parts.query


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"x-ms-version": API_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_round_blobs(base: str, sas: str, timeout: float = 20.0) -> list[str]:
    """Names of every blob called rounds.jsonl (in any folder)."""
    names: list[str] = []
    marker = ""
    while True:
        q = f"restype=container&comp=list&maxresults=500&{sas}"
        if marker:
            q += "&marker=" + urllib.parse.quote(marker, safe="")
        root = ET.fromstring(_get(f"{base}?{q}", timeout))
        for el in root.iterfind("./Blobs/Blob/Name"):
            n = el.text or ""
            if n == SUFFIX or n.endswith("/" + SUFFIX):
                names.append(n)
        marker = (root.findtext("NextMarker") or "").strip()
        if not marker:
            return sorted(names)


def run_name(blob: str) -> str:
    return blob[: -len(SUFFIX)].rstrip("/") or "(root)"


def parse_rows(text: str) -> tuple[list[dict], int]:
    rows, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def summarize(rows: list[dict], detail: bool = False) -> dict:
    """One run: rounds, accuracy, resumes, dropouts, traffic and cost (list prices)."""
    train = [r for r in rows if isinstance(r.get("round"), int) and r["round"] >= 1]
    scored = [r for r in rows if isinstance(r.get("round"), int) and "global_acc" in r]
    costed = [r for r in train if isinstance(r.get("cost"), dict)]

    def cost(k: str) -> float:
        return sum(float(r["cost"].get(k, 0.0)) for r in costed)

    stamps = [r["timestamp"] for r in rows if isinstance(r.get("timestamp"), (int, float))]
    out = {
        "rounds": len(train),
        "last_round": max((r["round"] for r in train), default=0),
        "final_acc": scored[-1]["global_acc"] if scored else None,
        "final_loss": scored[-1].get("global_loss") if scored else None,
        "resumes": sum(1 for r in rows if r.get("event") == "resumed"),
        "nodes_dropped": sum(int(r.get("nodes_dropped", 0)) for r in train),
        "bytes_up": sum(int(r.get("bytes_up", 0)) for r in train),
        "bytes_down": sum(int(r.get("bytes_down", 0)) for r in train),
        "costed_rounds": len(costed),
        "provider_usd": cost("provider_usd"),
        "customer_usd": cost("customer_usd"),
        "customer_busy_usd": cost("customer_busy_usd"),
        "total_usd": cost("total_usd"),
        "wall_seconds": cost("wall_seconds"),
        "first_timestamp": min(stamps) if stamps else None,
        "last_timestamp": max(stamps) if stamps else None,
    }
    if detail:
        out["per_round"] = [{
            "round": r["round"], "global_acc": r.get("global_acc"), "global_loss": r.get("global_loss"),
            "replied": r.get("nodes_replied"), "asked": r.get("nodes_asked"),
            "wall_seconds": (r.get("cost") or {}).get("wall_seconds"),
            "provider_usd": (r.get("cost") or {}).get("provider_usd"),
            "customer_usd": (r.get("cost") or {}).get("customer_usd"),
            "total_usd": (r.get("cost") or {}).get("total_usd"),
        } for r in train]
    return out


def build_report(sas_url: str, run: str | None = None, timeout: float = 20.0) -> dict:
    base, sas = split_sas(sas_url)
    blobs = list_round_blobs(base, sas, timeout)
    if run:
        blobs = [b for b in blobs if run_name(b) == run]

    def one(blob: str) -> dict:
        try:
            text = _get(f"{base}/{urllib.parse.quote(blob, safe='/')}?{sas}", timeout).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return {"run": run_name(blob), "error": f"HTTP {e.code}"}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return {"run": run_name(blob), "error": type(e).__name__}
        rows, bad = parse_rows(text)
        return {"run": run_name(blob), "bad_lines": bad, **summarize(rows, detail=bool(run))}

    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(one, blobs))
    runs.sort(key=lambda r: r.get("last_timestamp") or 0, reverse=True)
    return {
        "generated_unix": time.time(),
        "container": base.rsplit("/", 1)[-1],
        "note": "Costs are list prices before free tiers and credits; only rounds that carry a cost field are summed "
                "(older runs have none).",
        "runs": runs,
    }


def _usd(v) -> str:
    return "-" if v is None else f"${v:.4f}"


def render_html(report: dict) -> str:
    e = html.escape
    head = ("run", "rounds", "costed", "final acc", "resumes", "dropped", "wall s", "provider", "customer", "total")
    lines = [
        "<!doctype html><meta charset=utf-8><title>FedFleet round report</title>",
        "<style>body{font:14px system-ui;margin:2rem}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:4px 10px;text-align:right}td:first-child,th:first-child{text-align:left}</style>",
        f"<h1>FedFleet round report</h1><p>{e(report['note'])}</p><table>",
        "<tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>",
    ]
    for r in report["runs"]:
        if "error" in r:
            lines.append(f"<tr><td>{e(r['run'])}</td><td colspan=9>could not read: {e(r['error'])}</td></tr>")
            continue
        acc = "-" if r["final_acc"] is None else f"{r['final_acc']:.4f}"
        cells = [r["run"], r["rounds"], r["costed_rounds"], acc, r["resumes"], r["nodes_dropped"],
                 f"{r['wall_seconds']:.0f}", _usd(r["provider_usd"]), _usd(r["customer_usd"]), _usd(r["total_usd"])]
        lines.append("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in cells) + "</tr>")
    lines.append("</table>")
    return "\n".join(lines)
