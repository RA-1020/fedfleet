"""Cost per training round, for the provider and for the customer.

Clouds bill by the hour and by the gigabyte, not by the round, so a cost per round is an
attribution. This module turns one row of rounds.jsonl into money with a rate card
(deploy/rates.toml) and these rules:

  Provider (runs the aggregator):
    aggregator   = round wall time x the aggregator VM's hourly rate
    egress down  = bytes sent to the sites x the aggregator's egress rate
  Customer (owns the sites):
    site compute = round wall time x each asked site's hourly rate (a VM is billed while it
                   waits, so occupancy, not busy time, is what the bill sees)
    egress up    = bytes each site sent back x that site's egress rate

The round wall time is the time from the previous row to this one (training, aggregation,
global evaluation and checkpoint), so the rounds of a run add up to the run. Busy time
(setup + training) is reported beside it, as the share a perfectly elastic site would pay.

Prices are list prices, before free tiers and credits: the Oracle aggregator is an Always
Free shape and the first 100 GB of Azure egress each month is free, so the bill can be
lower than the list-price figure. Every rate comes from the rate card, with its source.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

GB = 1e9  # cloud price lists bill decimal gigabytes


def load_rates(path: str | Path) -> dict:
    with open(Path(path).expanduser(), "rb") as f:
        rates = tomllib.load(f)
    agg = rates.get("aggregator", {})
    if "usd_per_hour" not in agg:
        agg["usd_per_hour"] = (agg.get("ocpus", 0) * agg.get("usd_per_ocpu_hour", 0)
                               + agg.get("memory_gb", 0) * agg.get("usd_per_gb_hour", 0))
    rates["aggregator"] = agg
    return rates


def _site_rate(rates: dict, site) -> dict:
    sites = rates.get("sites", {})
    if site is not None and str(site) in sites:
        return sites[str(site)]
    return rates.get("default_site", {"name": "unknown", "usd_per_hour": 0.0})


def round_cost(row: dict, rates: dict, wall_seconds: float | None = None,
               asked_sites: list | None = None) -> dict:
    """Cost of one metrics row. wall_seconds defaults to the row's train_round_seconds.

    asked_sites: site ids that were asked this round. Sites that replied are known from
    the row; a site that was asked but never replied still occupied its machine, but the
    row only has its node id, so it is charged at the default_site rate and counted.
    """
    wall = float(wall_seconds if wall_seconds is not None else row.get("train_round_seconds") or 0.0)
    agg = rates["aggregator"]
    sites_out = []
    customer_compute = customer_egress = busy_total = 0.0
    replied = row.get("sites") or []
    for s in replied:
        r = _site_rate(rates, s.get("site"))
        compute = wall * r.get("usd_per_hour", 0.0) / 3600
        busy_s = (s.get("setup_seconds") or 0.0) + (s.get("train_seconds") or 0.0)
        busy = busy_s * r.get("usd_per_hour", 0.0) / 3600
        # The site's own upload size (smaller with compression); older rows only have the
        # update size, which equals the upload when nothing is compressed.
        sent = s.get("bytes_up") if s.get("bytes_up") is not None else row.get("bytes_per_update", 0)
        up = sent * r.get("egress_usd_per_gb", 0.0) / GB
        customer_compute += compute
        customer_egress += up
        busy_total += busy
        sites_out.append({"site": s.get("site"), "name": r.get("name"),
                          "compute_usd": compute, "busy_usd": busy, "egress_usd": up})
    missing = row.get("nodes_missing") or []
    if missing:
        r = rates.get("default_site", {"name": "unknown", "usd_per_hour": 0.0})
        for node in missing:
            compute = wall * r.get("usd_per_hour", 0.0) / 3600
            customer_compute += compute
            sites_out.append({"node": node, "name": r.get("name"), "missing": True,
                              "compute_usd": compute, "busy_usd": 0.0, "egress_usd": 0.0})
    provider_compute = wall * agg["usd_per_hour"] / 3600
    provider_egress = row.get("bytes_down", 0) * agg.get("egress_usd_per_gb", 0.0) / GB
    return {
        "wall_seconds": round(wall, 3),
        "provider_usd": provider_compute + provider_egress,
        "provider_compute_usd": provider_compute,
        "provider_egress_usd": provider_egress,
        "customer_usd": customer_compute + customer_egress,
        "customer_compute_usd": customer_compute,
        "customer_busy_usd": busy_total,
        "customer_egress_usd": customer_egress,
        "total_usd": provider_compute + provider_egress + customer_compute + customer_egress,
        "sites": sites_out,
        "basis": "list prices before free tiers and credits",
    }


def run_costs(rows: list[dict], rates: dict) -> list[tuple[dict, dict]]:
    """Cost every training round of a run; wall time = gap to the previous row."""
    out, prev_ts = [], None
    for row in rows:
        ts = row.get("timestamp")
        if "round" in row and row["round"] != 0 and "train_round_seconds" in row:
            wall = ts - prev_ts if (ts is not None and prev_ts is not None) else None
            out.append((row, round_cost(row, rates, wall)))
        prev_ts = ts
    return out
