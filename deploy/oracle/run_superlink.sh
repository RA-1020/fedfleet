#!/usr/bin/env bash
# Start the FedFleet aggregator (Flower SuperLink), listening ONLY on the Tailscale address.
# The control port (8000) and the site port (9092) are therefore unreachable from the
# public internet, even if a firewall rule is ever opened by mistake.
set -euo pipefail
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
DIR="${FEDFLEET_DIR:-$HOME/fedfleet}"
# The SuperLink starts a helper program (flower-superexec) by name. systemd gives services a
# minimal PATH, so the project's virtual environment must be put on it explicitly.
export PATH="$DIR/.venv/bin:$PATH"

# Wait until Tailscale is up and has an address (it may start a few seconds after boot).
until TS="$(tailscale ip -4 2>/dev/null | head -n1)" && [ -n "$TS" ]; do
  echo "waiting for Tailscale..."; sleep 2
done
cd "$DIR"
CERTS="$DIR/certificates"
if [ -f "$CERTS/server.pem" ] && [ -f "$CERTS/server.key" ] && [ -f "$CERTS/ca.pem" ]; then
  SEC=(--ssl-certfile "$CERTS/server.pem" --ssl-keyfile "$CERTS/server.key"
       --ssl-ca-certfile "$CERTS/ca.pem" --enable-supernode-auth)
  MODE="TLS on, only registered sites may join"
else
  SEC=(--insecure)
  MODE="WARNING: no certificates found, running without TLS"
fi
echo "starting SuperLink on $TS (control :8000, sites :9092) - $MODE"
exec "$DIR/.venv/bin/flower-superlink" "${SEC[@]}" --host "$TS" --fleet-api-address "$TS:9092"
