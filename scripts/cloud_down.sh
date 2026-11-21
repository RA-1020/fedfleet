#!/usr/bin/env bash
# Stop everything that bills or runs after a cloud test, from the Mac:
#   1. stop site 2's SuperNode on the Azure VM, 2. deallocate the Azure VM (compute billing
#   stops; its disk and public IP still bill a little), 3. stop the Mac sites.
# The Oracle aggregator, Prometheus, Grafana and the Blob backup stay up: they are Always Free
# and must keep running between tests.
#   scripts/cloud_down.sh
# If `az` fails with connection resets, stop the VM in the portal (Stop = deallocate).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
RG="${RG:-fedfleet-rg}"; VM="${VM:-fedfleet-site2}"
SITE_HOST="${SITE_HOST:-fedfleet-site2}"; SITE_USER="${SITE_USER:-ubuntu}"
status=0

echo "== stopping site 2 on $SITE_HOST"
if nc -z -w 5 "$SITE_HOST" 22 2>/dev/null; then
  ssh -o ConnectTimeout=20 "$SITE_USER@$SITE_HOST" "cd ~/fedfleet && scripts/stop_local.sh" || status=1
else
  echo "   not reachable (VM already stopped?), skipping"
fi

if [ "${SKIP_AZ:-0}" != 1 ]; then
  echo "== deallocating Azure VM $VM"
  if az vm deallocate -g "$RG" -n "$VM" --output none; then
    echo "   power state: $(az vm get-instance-view -g "$RG" -n "$VM" --query "instanceView.statuses[1].displayStatus" -o tsv 2>/dev/null || echo unknown)"
  else
    echo "!! az deallocate failed: stop $VM in the portal"; status=1
  fi
fi

echo "== stopping the Mac sites"
scripts/stop_local.sh

[ $status = 0 ] && echo "== done" || echo "== done, with a step to finish by hand (see above)"
exit $status
