#!/usr/bin/env bash
# Start N simulated sites (SuperNodes) on this machine, pointed at an aggregator.
#   scripts/run_sites.sh fedfleet-agg 2     cloud aggregator, over Tailscale
#   scripts/run_sites.sh 127.0.0.1 2        local aggregator from scripts/run_local.sh
# Each site gets its own Flower folder (~/.flwr-sites/site-<i>), so sites on the same
# machine never race each other while unpacking the app on their first round.
# With certificates/ca.pem and keys/site-<i>.key present, sites use TLS and authenticate.
set -euo pipefail
HOST="${1:-fedfleet-agg}"
N="${2:-2}"
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
source "$(dirname "$0")/_site_security.sh"
mkdir -p logs
for ((i = 0; i < N; i++)); do
  SEC="$(site_security_flags "$i")"
  FLWR_HOME="$HOME/.flwr-sites/site-$i" nohup flower-supernode $SEC \
    --superlink "$HOST:9092" --node-config "partition-id=$i num-partitions=$N" \
    --port $((9094 + i)) > "logs/site-$i.log" 2>&1 &
  if [ "$SEC" = "--insecure" ]; then MODE="insecure"; else MODE="TLS + authenticated"; fi
  echo "site $i started -> $HOST:9092 ($MODE, log: logs/site-$i.log)"
done
