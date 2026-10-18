#!/usr/bin/env bash
# Start simulated sites (SuperNodes) on this machine, pointed at an aggregator.
#   scripts/run_sites.sh HOST COUNT [TOTAL] [FIRST]
# COUNT = how many sites to start here, TOTAL = how many sites the whole federation has
# (default COUNT), FIRST = id of the first site started here (default 0).
#   scripts/run_sites.sh fedfleet-agg 2        sites 0 and 1 here, 2 partitions in total
#   scripts/run_sites.sh fedfleet-agg 2 3      sites 0 and 1 here, 3 sites in the federation
#   scripts/run_sites.sh fedfleet-agg 1 3 2    site 2 only (for example on the Azure VM)
#   scripts/run_sites.sh 127.0.0.1 2           local aggregator from scripts/run_local.sh
# Each site gets its own Flower folder (~/.flwr-sites/site-<i>), so sites on the same
# machine never race each other while unpacking the app on their first round.
# With certificates/ca.pem and keys/site-<i>.key present, sites use TLS and authenticate.
# Run it from the project folder: the certificate and key paths are relative.
set -euo pipefail
HOST="${1:-fedfleet-agg}"
COUNT="${2:-2}"
TOTAL="${3:-$COUNT}"
FIRST="${4:-0}"
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
source "$(dirname "$0")/_site_security.sh"
mkdir -p logs
for ((i = FIRST; i < FIRST + COUNT; i++)); do
  SEC="$(site_security_flags "$i")"
  FLWR_HOME="$HOME/.flwr-sites/site-$i" nohup flower-supernode $SEC \
    --superlink "$HOST:9092" --node-config "partition-id=$i num-partitions=$TOTAL" \
    --port $((9094 + i)) > "logs/site-$i.log" 2>&1 < /dev/null &
  if [ "$SEC" = "--insecure" ]; then MODE="insecure"; else MODE="TLS + authenticated"; fi
  echo "site $i of $TOTAL started -> $HOST:9092 ($MODE, log: logs/site-$i.log)"
done
