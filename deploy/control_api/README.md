# FedFleet control API (WBS 11.1, minimal)

A small FastAPI service on the Oracle aggregator VM, bound to `127.0.0.1:8088` only. The
public entry point is the Cloudflare Worker gateway, which checks the caller's token and rate
limit and forwards through an outbound-only tunnel, adding the origin key. No inbound port is
opened on the VM.

| Method | Path | What it does |
|---|---|---|
| GET | `/healthz` | liveness (no key) |
| GET | `/status` | state of the aggregator, exporter, backup, Prometheus and Grafana; latest run and round |
| GET | `/runs` | every run: rounds, final accuracy, resumes, dropouts, bytes, cost (list prices) |
| GET | `/runs/{name}` | one run with per-round detail |
| POST | `/runs` | start a run: `{"name", "rounds" 1-20, "min_nodes" 1-3, "round_timeout" 30-900, "head_only", "compression" none/int8/topk/topk-int8, "topk_ratio" (0,1], "error_feedback", "seed" -1 or 0-1000000}` |

`POST /runs` calls `flwr run ~/fedfleet fedfleet-vm` on the VM with fixed data, backbone and
rate-card paths; only the fields above can be set. Metrics go to `~/fedfleet-runs-<name>`, so
Grafana and the Blob backup pick the run up like any other. A name that already exists is
refused. The sites must already be running (`scripts/cloud_up.sh` on the Mac).

Run summaries use the same code as the Azure Function round report
(`deploy/azure_function/fedfleet_report.py`) and the rate card in `deploy/rates.toml`.

Not in this minimal version (the plan's full 11.1): tenants, Supabase sign-in, Kubernetes
custom resources. Those are described in D2 as designed work.

Install steps are in the chat log of 21 Nov and the main plan doc; tests: `python tests/test_control_api.py`.
