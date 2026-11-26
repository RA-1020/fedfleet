#!/usr/bin/env bash
# Bring the 3-site cloud federation up from the Mac, ready for `flwr run`:
#   0. point the peer relay's firewall rule at this Mac's current public address,
#   1. start the Azure site VM (deallocated between tests, so it bills only while up),
#   2. wait for it on Tailscale, 3. check the aggregator answers on 8000 and 9092,
#   4. restart the two Mac sites, 5. start site 2 on the Azure VM.
#   scripts/cloud_up.sh                 (defaults below; override with environment variables)
# If `az` fails with connection resets, start the VM in the portal and rerun with SKIP_AZ=1.
# If the aggregator check fails but `tailscale ping` works, quit and reopen Tailscale.
# The Mac reaches the aggregator through a Tailscale peer relay on the Azure VM, which needs
# inbound UDP 40000 there. The Azure firewall rule admits only the Oracle VM and the Mac's
# current public address, which step 0 reads from `tailscale netcheck` and writes into the rule
# (any older address is dropped). RELAY_RULE=0 skips it; the Mac then falls back to Tailscale's
# public DERP relay, which works but is slower and has dropped rounds.
set -euo pipefail
cd "$(dirname "$0")/.."
RG="${RG:-fedfleet-rg}"; VM="${VM:-fedfleet-site2}"
NSG="${NSG:-fedfleet-site2-nsg}"; RELAY_RULE_NAME="${RELAY_RULE_NAME:-tailscale-peer-relay}"
AGG_PUBLIC_IP="${AGG_PUBLIC_IP:-137.23.50.204}"   # the Oracle aggregator (fixed)
AGG="${AGG:-fedfleet-agg}"; SITE_HOST="${SITE_HOST:-fedfleet-site2}"; SITE_USER="${SITE_USER:-ubuntu}"

if [ "${SKIP_AZ:-0}" != 1 ] && [ "${RELAY_RULE:-1}" != 0 ]; then
  echo "== peer relay firewall rule: following this Mac's public address"
  MAC_IP=$(tailscale netcheck 2>/dev/null | sed -nE 's/^[[:space:]]*\* IPv4: yes, ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+):[0-9]+.*/\1/p' | head -1 || true)
  if [ -z "$MAC_IP" ]; then
    echo "   !! could not read this Mac's public IPv4 from 'tailscale netcheck'; rule left as it is"
  else
    want="$AGG_PUBLIC_IP/32 $MAC_IP/32"
    have=$(az network nsg rule show -g "$RG" --nsg-name "$NSG" -n "$RELAY_RULE_NAME" \
             --query "sourceAddressPrefixes" -o tsv 2>/dev/null | tr '\t\n' '  ' | xargs -n1 2>/dev/null | sort | xargs || true)
    if [ "$have" = "$(echo "$want" | xargs -n1 | sort | xargs)" ]; then
      echo "   already allows $MAC_IP (and the aggregator)"
    else
      # shellcheck disable=SC2086  # two prefixes, split on purpose
      if az network nsg rule update -g "$RG" --nsg-name "$NSG" -n "$RELAY_RULE_NAME" \
           --source-address-prefixes $want --output none; then
        echo "   was: ${have:-none}"
        echo "   now: $want"
      else
        echo "   !! could not update the rule; the Mac will use the slower DERP path"
      fi
    fi
  fi
fi

# Tell the elasticity controller this VM is now in a person's hands (it never stops a held VM).
ssh -o ConnectTimeout=10 "ubuntu@$AGG" 'mkdir -p ~/fedfleet-elastic && touch ~/fedfleet-elastic/hold' 2>/dev/null \
  || echo "   (could not set the elasticity hold on $AGG; the controller may stop a VM it started)"

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
# With deploy/site/fedfleet-site.service installed (needed for elasticity) the site is a
# service that starts at boot; otherwise it is started by hand as before.
# shellcheck disable=SC2029  # AGG expands here on purpose
ssh -o ConnectTimeout=20 "$SITE_USER@$SITE_HOST" \
  "if systemctl cat fedfleet-site >/dev/null 2>&1; then sudo systemctl restart fedfleet-site && echo 'site 2 of 3 restarted (service fedfleet-site, log: journalctl -u fedfleet-site)'; \
   else cd ~/fedfleet && scripts/stop_local.sh >/dev/null; . .venv/bin/activate && scripts/run_sites.sh $AGG 1 3 2; fi"

echo "== Mac -> aggregator path (the peer relay can take a minute to be offered after the VM boots)"
path=""
for _ in 1 2 3 4 5 6; do
  path=$(tailscale ping -c 4 "$AGG" 2>&1 || true)
  grep -q "via peer-relay" <<<"$path" && break
  sleep 10
done
if grep -q "via peer-relay" <<<"$path"; then
  echo "   peer relay: $(grep -m1 -Eo 'via peer-relay[^ ]* in [0-9.]+ms' <<<"$path")"
elif grep -q "via DERP" <<<"$path"; then
  echo "   !! public DERP relay ($(grep -m1 -Eo 'via DERP\([a-z]+\) in [0-9.]+ms' <<<"$path")), not the peer relay."
  echo "      Fine for a quick run; for a measured run check the rule above (a network that sends"
  echo "      traffic out from more than one public address also defeats it)."
else
  echo "   $(tail -1 <<<"$path")"
fi

echo "== ready. Wait about 15 s for the sites to register, then run flwr run."
