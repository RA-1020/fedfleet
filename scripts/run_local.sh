#!/usr/bin/env bash
# Start a local Flower deployment: one SuperLink and N SuperNodes, as separate processes.
# Usage: scripts/run_local.sh [num_nodes]   (default 2). Logs go to logs/.
set -euo pipefail
N="${1:-2}"
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1   # use this venv, don't reinstall per run
mkdir -p logs
nohup flower-superlink --insecure > logs/superlink.log 2>&1 &
echo "SuperLink started (Fleet API :9092, Control API :8000)"
sleep 5
for ((i = 0; i < N; i++)); do
  nohup flower-supernode --insecure --superlink 127.0.0.1:9092 \
    --node-config "partition-id=$i num-partitions=$N" --port $((9094 + i)) \
    > "logs/supernode-$i.log" 2>&1 &
  echo "SuperNode $i started"
done
echo "Run training with:  flwr run . fedfleet-local --stream"
