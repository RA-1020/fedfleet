# Monitoring: Prometheus and Grafana for FedFleet

The exporter reads every `rounds.jsonl` under `~/fedfleet-runs*` (read-only) and serves the
latest round of each run, plus running totals, for Prometheus. Grafana shows them on the
"FedFleet rounds" dashboard. All three listen on the aggregator's Tailscale address only.

| Part | Port | Runs as |
| --- | --- | --- |
| `fedfleet_exporter.py` | 9101 | systemd `fedfleet-exporter` (user ubuntu) |
| Prometheus | 9090 | Ubuntu package `prometheus` |
| Grafana | 3000 | Grafana's APT package |

Metrics (labels: `run` = the metrics folder name, `side` = provider or customer, `site`):
round, sites online/asked/replied, bytes up/down per round and in total, bytes per update,
round time and wall time, global accuracy and loss, cost per round and in total (list prices
before free tiers and credits), per-site training and turnaround times, resumes, dropped nodes.

Files: `fedfleet-exporter.service`, `prometheus.yml`, `grafana-datasource.yaml`,
`grafana-dashboards.yaml`, `fedfleet-dashboard.json`. Installation steps are run one at a time
with the aggregator idle; they are recorded in the project plan as they are done.
