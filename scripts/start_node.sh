#!/usr/bin/env bash
# Bring site <i> back after scripts/kill_node.sh; it rejoins at the next round.
#   scripts/start_node.sh 1 2                 local aggregator
#   scripts/start_node.sh 1 2 fedfleet-agg    cloud aggregator
i="${1:-1}"; N="${2:-2}"; HOST="${3:-127.0.0.1}"
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
source "$(dirname "$0")/_site_security.sh"
mkdir -p logs
SEC="$(site_security_flags "$i")"
FLWR_HOME="$HOME/.flwr-sites/site-$i" nohup flower-supernode $SEC \
  --superlink "$HOST:9092" --node-config "partition-id=$i num-partitions=$N" \
  --port $((9094 + i)) >> "logs/site-$i.log" 2>&1 &
echo "site $i is back online -> $HOST:9092"
