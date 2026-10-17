#!/usr/bin/env bash
# Start a local Flower deployment: one SuperLink and N sites, as separate processes.
# Usage: scripts/run_local.sh [num_sites]   (default 2). Logs go to logs/.
set -euo pipefail
N="${1:-2}"
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
mkdir -p logs
nohup flower-superlink --insecure > logs/superlink.log 2>&1 &
echo "SuperLink started (sites :9092, control :8000)"
sleep 5
"$(dirname "$0")/run_sites.sh" 127.0.0.1 "$N"
echo "Run training with:  flwr run . fedfleet-local --stream"
