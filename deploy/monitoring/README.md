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

## Alerts

`fedfleet-alerts.yml` holds six Prometheus alerting rules: a FedFleet service down
(`fedfleet_service_up`, from `systemctl is-active` in the exporter), the exporter itself down,
a site missing from an active run's latest round, an active run above 80% of its privacy budget,
a round over 120 s, and a round over $0.005 at list prices. A run is active for 10 minutes after
its latest round. Alerts appear in Prometheus (Alerts) and in Grafana (Alerting), and Alertmanager
(`alertmanager.yml`, listening on 127.0.0.1 only) sends them to a Telegram chat. Tests: `promtool test rules fedfleet-alerts.test.yml` in this folder.

## Service-level objectives

`python3 scripts/slo_report.py` on the aggregator measures the round-level objectives from every
run's rounds.jsonl: 95% of plain rounds within 60 s, 95% of SecAgg+ rounds within 90 s, 99% of
rounds complete outside fault-injection runs, and no DP round past its max_epsilon. The recovery
objectives (aggregator serving again within 30 s of a crash, first new round within 120 s, a
partitioned site back within 30 s of the heal) are measured by the chaos drills.
