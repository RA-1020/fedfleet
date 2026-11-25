#!/usr/bin/env bash
# One site in the foreground, for systemd (deploy/site/fedfleet-site.service on the Azure VM).
# Same flags as scripts/run_sites.sh; the log goes to the journal and to logs/site-<id>.log.
#   SITE_ID=2 SITE_TOTAL=3 SITE_AGG=fedfleet-agg scripts/site_service.sh
set -euo pipefail
cd "$(dirname "$0")/.."
i="${SITE_ID:?}"; total="${SITE_TOTAL:?}"; agg="${SITE_AGG:-fedfleet-agg}"
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1 FLWR_HOME="$HOME/.flwr-sites/site-$i"
# shellcheck source=scripts/_site_security.sh
source scripts/_site_security.sh
# Wait for Tailscale to resolve the aggregator after boot (up to 2 min).
for _ in $(seq 1 24); do getent hosts "$agg" >/dev/null && break; sleep 5; done
mkdir -p logs
# shellcheck disable=SC2046  # the flags are separate words on purpose
exec .venv/bin/flower-supernode $(site_security_flags "$i") \
  --superlink "$agg:9092" --node-config "partition-id=$i num-partitions=$total" \
  --port $((9094 + i)) > >(tee -a "logs/site-$i.log") 2>&1
