#!/usr/bin/env bash
# Bring the 3-site cloud federation up from the Mac, ready for `flwr run`:
#   1. start the Azure site VM (deallocated between tests, so it bills only while up),
#   2. wait for it on Tailscale, 3. check the aggregator answers on 8000 and 9092,
#   4. restart the two Mac sites, 5. start site 2 on the Azure VM.
#   scripts/cloud_up.sh                 (defaults below; override with environment variables)
# If `az` fails with connection resets, start the VM in the portal and rerun with SKIP_AZ=1.
# If the aggregator check fails but `tailscale ping` works, quit and reopen Tailscale.
set -euo pipefail
cd "$(dirname "$0")/.."
RG="${RG:-fedfleet-rg}"; VM="${VM:-fedfleet-site2}"
AGG="${AGG:-fedfleet-agg}"; SITE_HOST="${SITE_HOST:-fedfleet-site2}"; SITE_USER="${SITE_USER:-ubuntu}"

if [ "${SKIP_AZ:-0}" != 1 ]; then
  echo "== starting Azure VM $VM (resource group $RG)"
  az vm start -g "$RG" -n "$VM" --output none
fi
az_state() { az vm get-instance-view -g "$RG" -n "$VM" --query "instanceView.statuses[1].displayStatus" -o tsv 2>/dev/null || echo "unknown"; }
[ "${SKIP_AZ:-0}" != 1 ] && echo "   power state: $(az_state)"

echo "== waiting for $SITE_HOST on Tailscale (up to 3 min)"
for _ in $(seq 1 36); do
  if nc -z -w 5 "$SITE_HOST" 22 2>/dev/null; then echo "   reachable"; break; fi
  sleep 5
done
nc -z -w 5 "$SITE_HOST" 22 || { echo "!! $SITE_HOST not reachable on port 22"; exit 1; }

echo "== checking the aggregator $AGG"
for port in 8000 9092; do
  nc -z -w 10 "$AGG" "$port" || { echo "!! $AGG:$port not reachable (if 'tailscale ping $AGG' works, restart Tailscale)"; exit 1; }
done
echo "   8000 and 9092 reachable"

echo "== restarting the Mac sites (0 and 1 of 3)"
scripts/stop_local.sh >/dev/null
sleep 2
out="$(scripts/run_sites.sh "$AGG" 2 3)"; echo "$out"
if grep -q "(insecure" <<<"$out"; then
  echo "!! a Mac site started without TLS keys; the aggregator would reject it. Run from ~/fedfleet with certificates/ and keys/ present."
  scripts/stop_local.sh >/dev/null; exit 1
fi

echo "== starting site 2 on $SITE_HOST"
ssh -o ConnectTimeout=20 "$SITE_USER@$SITE_HOST" \
  "cd ~/fedfleet && scripts/stop_local.sh >/dev/null; . .venv/bin/activate && scripts/run_sites.sh $AGG 1 3 2"

echo "== ready. Wait about 15 s for the sites to register, then run flwr run."
