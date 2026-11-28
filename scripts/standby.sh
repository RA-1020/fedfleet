#!/usr/bin/env bash
# Cold standby aggregator on a second cloud (task 2.10a), run from the Mac.
#
# The primary aggregator is the Oracle VM in Mumbai. The standby does not exist until it is needed:
# `up` creates it in Azure (its own resource group, B2s, Ubuntu 24.04, no inbound ports), joins it
# to the tailnet with a one-off key, installs Docker and pulls the FedFleet image CI built.
# `failover` restores the replicated state onto it and moves the three sites there:
#   - the SuperLink state database (registered site keys, runs) from the 5-minute snapshot in Blob,
#   - the run folder (checkpoint, rounds.jsonl, privacy ledger) from the 15-second Blob backup,
#   - the backbone, the public test set and the rate card from this Mac,
#   - a new TLS certificate for the standby's name, signed here by the CA that never leaves this
#     Mac; its private key goes straight to the standby over SSH and is never stored anywhere else.
# Then it starts the SuperLink in the image, points the sites at it and resumes the run there.
#
#   scripts/standby.sh up                      create the standby and wait until it is ready
#   scripts/standby.sh failover <run> <new> --rounds <total>
#                                              resume fedfleet-runs-<run> on the standby as
#                                              fedfleet-runs-<new> (plain head-only runs)
#   scripts/standby.sh drill <name> [--rounds 8] [--after 3]
#                                              measured recovery: start <name> on the primary,
#                                              declare the primary lost after round <after> (the run
#                                              is stopped, the service is not touched), then up +
#                                              failover + resume, with a timeline from that moment
#   scripts/standby.sh failback <new>          sites back to the primary; the standby's run folder
#                                              is copied to the primary (so it is backed up too)
#   scripts/standby.sh down                    delete the standby's resource group (asks first)
#   scripts/standby.sh status
#
# Before `up` (each time; the key is spent and deleted by `up`): in the Tailscale admin console,
# Settings > Keys > Generate auth key: Reusable OFF, Expiration 1 day, Ephemeral ON, Pre-approved ON,
# Tags tag:aggregator and tag:region-ap-indiasouthcentral. Save it to ~/.fedfleet-ts-standby-key
# with mode 600. Start the sites first (scripts/cloud_up.sh). Needs `az login` and certificates/ca.key.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="$PWD/.venv/bin:$PATH"
CMD="${1:-}"; shift || true

AGG="${AGG:-fedfleet-agg}"
SITE="${SITE:-fedfleet-site2}"; SITE_RG="${SITE_RG:-fedfleet-rg}"; SITE_VM="${SITE_VM:-fedfleet-site2}"
GATEWAY="${GATEWAY:-https://fedfleet-gateway.fedfleet-ra.workers.dev}"
TOKEN_FILE="${TOKEN_FILE:-$HOME/.fedfleet-api-token}"
ACCOUNT="${ACCOUNT:-fedfleetruns429}"; CONTAINER="${CONTAINER:-fedfleet-runs}"
RG="fedfleet-standby-rg"                       # fixed: `down` deletes exactly this group
VM="${STANDBY_VM:-fedfleet-standby}"; SIZE="${STANDBY_SIZE:-Standard_B2s}"
OS_IMAGE="${STANDBY_OS_IMAGE:-Ubuntu2404}"
TS_HOSTNAME="${TS_HOSTNAME:-fedfleet-standby}"
IMAGE="${FEDFLEET_IMAGE:-ghcr.io/ra-1020/fedfleet:latest}"
SSH_PUB="${SSH_PUB:-$HOME/.ssh/fedfleet_ed25519.pub}"
KEY_FILE="${TS_KEY_FILE:-$HOME/.fedfleet-ts-standby-key}"
ENV_FILE="$HOME/.fedfleet-standby.env"         # what `up` learnt: the standby's tailnet name and IP
KH="$HOME/.ssh/fedfleet_standby_known_hosts"   # its own file: every new standby has a new host key
CONN="fedfleet-standby"                        # [superlink.fedfleet-standby] in ~/.flwr/config.toml
EXPECTED_REGION="indiasouthcentral"            # what tag:region-ap-indiasouthcentral claims
# One private scratch folder for the whole command; the standby's private key and the Tailscale key
# pass through it, so it is removed on every exit.
WORK=$(mktemp -d) || exit 1
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
since() { python3 -c "print(f'{$(now) - $1:.1f}')"; }
die() { echo "!! $*"; exit 1; }
# shellcheck disable=SC1090  # written by `up` with printf %q
load_env() { [ -f "$ENV_FILE" ] || die "no standby recorded ($ENV_FILE); run: scripts/standby.sh up"; . "$ENV_FILE"; }
sb() {   # run a command on the standby
  ssh -o ConnectTimeout=15 -o BatchMode=no -o StrictHostKeyChecking=accept-new \
      -o UserKnownHostsFile="$KH" "ubuntu@$STANDBY_HOST" "$@"
}
check_name() { [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || die "name: 1-40 lower-case letters, digits or hyphens: $1"; }
mac_ip() {
  tailscale netcheck 2>/dev/null | sed -nE 's/^[[:space:]]*\* IPv4: yes, ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+):[0-9]+.*/\1/p' | head -1
}
standby_runs() {    # training rounds logged on the standby for run folder $1
  # shellcheck disable=SC2029  # $1 was checked by check_name
  # grep -c prints 0 and exits 1 when nothing matches: keep its output, ignore the status.
  local c; c=$(sb "grep -c train_round_seconds /srv/fedfleet/fedfleet-runs-$1/rounds.jsonl 2>/dev/null || true" 2>/dev/null)
  echo "${c:-0}"
}
online_count() {    # sites the standby lists as online
  flwr supernode ls "$CONN" --format json 2>/dev/null | python3 -c '
import json, sys
try:
    print(sum(1 for n in json.load(sys.stdin).get("nodes", []) if n.get("status") == "online"))
except Exception:
    print(0)'
}

# ---------------------------------------------------------------------------------------- up
cmd_up() {
  local t0 work="$WORK/up" loc site_ip mip prefixes host ip
  t0=$(now)
  echo "== checks"
  az account show --output none 2>/dev/null || die "not signed in to Azure: az login"
  [ -f "$KEY_FILE" ] || die "no Tailscale key at $KEY_FILE (see the top of this script)"
  [ "$(stat -f %Lp "$KEY_FILE" 2>/dev/null || stat -c %a "$KEY_FILE")" = 600 ] || die "$KEY_FILE must be chmod 600"
  grep -Eq '^tskey-auth-[A-Za-z0-9-]+$' "$KEY_FILE" || die "$KEY_FILE does not hold one tskey-auth-... line"
  [ -f "$SSH_PUB" ] || die "no SSH public key at $SSH_PUB (set SSH_PUB=...)"
  [ "$(az group exists -n "$RG")" = false ] || die "$RG already exists (a standby is up?): scripts/standby.sh status"
  # The VM pulls the image without credentials, so the package must be public.
  local tok code repo
  repo="${IMAGE#ghcr.io/}"; repo="${repo%%:*}"; local tag="${IMAGE##*:}"
  tok=$(curl -fsS "https://ghcr.io/token?scope=repository:$repo:pull" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null) || tok=""
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $tok" \
         -H 'Accept: application/vnd.oci.image.index.v1+json' "https://ghcr.io/v2/$repo/manifests/$tag")
  [ "$code" = 200 ] || die "$IMAGE cannot be pulled anonymously (HTTP $code): make the package public on GitHub, or set FEDFLEET_IMAGE"
  echo "   image $IMAGE pullable without credentials"
  loc=$(az vm show -g "$SITE_RG" -n "$SITE_VM" --query location -o tsv 2>/dev/null) || loc=""
  loc="${STANDBY_LOCATION:-$loc}"
  [ -n "$loc" ] || die "could not read site 2's region; set STANDBY_LOCATION"
  [ "$loc" = "$EXPECTED_REGION" ] || echo "   !! region $loc, but the standby's tag says $EXPECTED_REGION: fix the tag or set STANDBY_LOCATION"
  echo "   region $loc (same as site 2: Azure for Students allows it; another region than the primary)"

  mkdir -p "$work"
  python3 - "$KEY_FILE" "$TS_HOSTNAME" "$IMAGE" deploy/standby/user-data.sh "$work/user-data" <<'PY' || die "could not prepare the first-boot script"
import sys
key = open(sys.argv[1]).read().strip()
s = open(sys.argv[4]).read()
for k, v in (("__TS_AUTHKEY__", key), ("__TS_HOSTNAME__", sys.argv[2]), ("__IMAGE__", sys.argv[3])):
    assert k in s, k
    s = s.replace(k, v)
open(sys.argv[5], "w").write(s)
PY
  echo "== creating $RG and $VM ($SIZE, $loc)"
  az group create -n "$RG" -l "$loc" --tags fedfleet-role=standby --output none || die "group create failed"
  az vm create -g "$RG" -n "$VM" --image "$OS_IMAGE" --size "$SIZE" --admin-username ubuntu \
     --ssh-key-values "$SSH_PUB" --public-ip-sku Standard --nsg-rule NONE \
     --custom-data "$work/user-data" --tags fedfleet-role=standby \
     --os-disk-delete-option Delete --nic-delete-option Delete --output none \
     || die "vm create failed (the group stays; scripts/standby.sh down removes it)"
  rm -f "$work/user-data"
  # The key is spent by the first boot; a copy has no further use, so do not keep one.
  rm -f "$KEY_FILE"
  local t_vm; t_vm=$(now)
  echo "   created at +$(python3 -c "print(f'{$t_vm - $t0:.1f}')") s; Tailscale key file deleted (one-off, now spent)"

  # Direct Tailscale paths: admit WireGuard UDP from this Mac and from site 2 only.
  mip=$(mac_ip)
  site_ip=$(az vm list-ip-addresses -g "$SITE_RG" -n "$SITE_VM" \
            --query "[0].virtualMachine.network.publicIpAddresses[0].ipAddress" -o tsv 2>/dev/null)
  prefixes=""
  [ -n "$mip" ] && prefixes="$mip/32"
  [ -n "$site_ip" ] && prefixes="$prefixes $site_ip/32"
  if [ -n "$prefixes" ]; then
    # shellcheck disable=SC2086  # one or two prefixes, split on purpose
    az network nsg rule create -g "$RG" --nsg-name "${VM}NSG" -n tailscale-direct --priority 1000 \
       --direction Inbound --access Allow --protocol Udp --destination-port-ranges 41641 \
       --source-address-prefixes $prefixes --output none \
      && echo "   UDP 41641 admitted from: $prefixes" \
      || echo "   !! could not add the Tailscale rule; paths will be relayed (slower, still works)"
  fi

  echo "== waiting for $TS_HOSTNAME on the tailnet (up to 5 min)"
  host=""; ip=""
  for _ in $(seq 1 60); do
    read -r host ip < <(tailscale status --json 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1]
peers = [p for p in json.load(sys.stdin).get("Peer", {}).values()
         if p.get("HostName") == want and p.get("Online")]
peers.sort(key=lambda p: p.get("Created", ""))
if peers:
    p = peers[-1]
    print(p["DNSName"].split(".")[0], [a for a in p["TailscaleIPs"] if "." in a][0])' "$TS_HOSTNAME") || true
    [ -n "$host" ] && break
    sleep 5
  done
  [ -n "$host" ] || die "$TS_HOSTNAME did not join the tailnet; see the boot log: az vm boot-diagnostics get-boot-log -g $RG -n $VM"
  local t_ts; t_ts=$(now)
  [ "$host" = "$TS_HOSTNAME" ] || echo "   !! joined as $host (an older $TS_HOSTNAME is still listed; it is removed when it expires)"
  echo "   $host ($ip) online at +$(python3 -c "print(f'{$t_ts - $t0:.1f}')") s"
  printf 'STANDBY_HOST=%q\nSTANDBY_IP=%q\nSTANDBY_LOCATION=%q\nT_UP_START=%q\nT_UP_VM=%q\nT_UP_TAILNET=%q\n' \
    "$host" "$ip" "$loc" "$t0" "$t_vm" "$t_ts" > "$ENV_FILE"
  STANDBY_HOST="$host"

  echo "== waiting for Docker and the image (up to 10 min; Tailscale SSH may ask you to approve in the browser)"
  local ok=0
  for _ in $(seq 1 60); do
    if sb 'test -f /var/lib/fedfleet-standby/ready' 2>/dev/null; then ok=1; break; fi
    sleep 10
  done
  [ "$ok" = 1 ] || { sb 'sudo tail -20 /var/log/fedfleet-standby.log' 2>/dev/null; die "standby not ready after 10 min"; }
  local t_ready; t_ready=$(now)
  printf 'T_UP_READY=%q\nIMAGE_DIGEST=%q\n' "$t_ready" "$(sb 'cat /var/lib/fedfleet-standby/image_digest')" >> "$ENV_FILE"
  echo "   ready at +$(python3 -c "print(f'{$t_ready - $t0:.1f}')") s; image $(sb 'cat /var/lib/fedfleet-standby/image_digest')"
  echo "   path from this Mac: $(tailscale ping -c 3 "$host" 2>&1 | tail -1)"
}

# --------------------------------------------------------------------------------- failover
cmd_failover() {
  local src="${1:-}" new="${2:-}"; shift 2 || usage
  local rounds=""
  while [ $# -gt 0 ]; do case "$1" in --rounds) rounds="$2"; shift 2 ;; *) usage ;; esac; done
  [[ "$src" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || die "run name: 1-40 lower-case letters, digits or hyphens"
  check_name "$new"
  [[ "$rounds" =~ ^[0-9]+$ ]] && [ "$rounds" -ge 1 ] && [ "$rounds" -le 20 ] || die "--rounds <total, 1-20> is required"
  load_env
  local t0 work="$WORK/failover" stage; t0=$(now)
  mkdir -p "$work"
  stage="$work/stage"; mkdir -p "$stage/fedfleet-state" "$stage/certificates" "$stage/fedfleet/data" \
                                "$stage/fedfleet/deploy" "$stage/fedfleet-runs-$new"

  echo "== checks"
  [ -f certificates/ca.key ] && [ -f certificates/ca.pem ] || die "certificates/ca.key and ca.pem are needed (this Mac's CA)"
  [ -f backbone.pt ] || die "backbone.pt not found here"
  [ -f data/radioml_public_test.pt ] || die "data/radioml_public_test.pt not found here (scripts/export_public_test.py)"
  sb 'test -f /var/lib/fedfleet-standby/ready' || die "standby $STANDBY_HOST not reachable or not ready"
  # shellcheck disable=SC2029
  sb "test ! -e /srv/fedfleet/fedfleet-runs-$new" || die "fedfleet-runs-$new already exists on the standby"

  echo "== downloading from Blob: fedfleet-runs-$src and the state snapshot"
  az storage blob download-batch --account-name "$ACCOUNT" --source "$CONTAINER" --destination "$work" \
     --pattern "fedfleet-runs-$src/*" --auth-mode key --only-show-errors --output none || die "run download failed"
  az storage blob download-batch --account-name "$ACCOUNT" --source "$CONTAINER" --destination "$work" \
     --pattern "fedfleet-state-backup/*" --auth-mode key --only-show-errors --output none || die "state download failed"
  local t_dl; t_dl=$(now)
  [ -f "$work/fedfleet-runs-$src/latest_checkpoint.pt" ] && [ -f "$work/fedfleet-runs-$src/rounds.jsonl" ] \
    || die "the backup of fedfleet-runs-$src has no checkpoint or rounds.jsonl"
  [ -f "$work/fedfleet-state-backup/state.db" ] || die "no state snapshot in Blob (is fedfleet-state-snapshot.timer installed?)"
  python3 - "$work/fedfleet-state-backup" "$work/fedfleet-runs-$src/rounds.jsonl" <<'PY' || die "the state snapshot or the run failed its checks"
import hashlib, json, sqlite3, sys, time
d, rows = sys.argv[1], sys.argv[2]
meta = json.load(open(f"{d}/state_snapshot.json"))
digest = hashlib.sha256(open(f"{d}/state.db", "rb").read()).hexdigest()
ok = sqlite3.connect(f"file:{d}/state.db?mode=ro", uri=True).execute("PRAGMA integrity_check").fetchone()[0]
print(f"   state snapshot: {meta['size']:,} B, sha256 {'matches' if digest == meta['sha256'] else 'DIFFERS'}, "
      f"integrity {ok}, content {time.time() - meta['taken']:.0f} s old, checked {time.time() - meta['checked']:.0f} s ago")
r = [json.loads(l) for l in open(rows) if l.strip()]
t = [x for x in r if isinstance(x.get("round"), int) and x["round"] > 0 and "train_round_seconds" in x]
if any("dp" in x or "secagg" in x for x in r):
    print("   this run used DP or SecAgg+; failover here supports plain runs only"); sys.exit(1)
print(f"   run backup: {len(t)} rounds, last global_acc {t[-1].get('global_acc') if t else '-'}")
sys.exit(0 if digest == meta["sha256"] and ok == "ok" else 1)
PY
  local restored; restored=$(grep -c train_round_seconds "$work/fedfleet-runs-$src/rounds.jsonl" || true)
  [ "$restored" -lt "$rounds" ] || die "the backup already holds $restored of $rounds rounds; nothing to resume"

  echo "== TLS certificate for $STANDBY_HOST ($STANDBY_IP), signed by this Mac's CA (valid 30 days)"
  local fqdn; fqdn=$(tailscale status --json | python3 -c '
import json, sys
for p in json.load(sys.stdin).get("Peer", {}).values():
    if p["DNSName"].split(".")[0] == sys.argv[1]:
        print(p["DNSName"].rstrip(".")); break' "$STANDBY_HOST")
  ( cd "$stage/certificates" || exit 1
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-384 -nodes -keyout server.key -out server.csr \
      -subj "/CN=$STANDBY_HOST" 2>/dev/null || exit 1
    printf 'subjectAltName = DNS:%s%s, DNS:localhost, IP:%s, IP:127.0.0.1\nbasicConstraints = CA:FALSE\nkeyUsage = digitalSignature, keyEncipherment\nextendedKeyUsage = serverAuth\n' \
      "$STANDBY_HOST" "${fqdn:+, DNS:$fqdn}" "$STANDBY_IP" > server.ext
    openssl x509 -req -in server.csr -CA "$OLDPWD/certificates/ca.pem" -CAkey "$OLDPWD/certificates/ca.key" \
      -set_serial "0x$(openssl rand -hex 16)" -days 30 -out server.pem -extfile server.ext 2>/dev/null || exit 1
    rm -f server.csr server.ext; cp "$OLDPWD/certificates/ca.pem" ca.pem; chmod 600 server.key
  ) || die "certificate failed"
  openssl verify -CAfile certificates/ca.pem "$stage/certificates/server.pem" >/dev/null || die "certificate does not verify"

  cp -p "$work/fedfleet-state-backup/state.db" "$stage/fedfleet-state/state.db"
  cp -p "$work/fedfleet-runs-$src"/* "$stage/fedfleet-runs-$new/"
  cp -p backbone.pt "$stage/fedfleet/backbone.pt"
  cp -p data/radioml_public_test.pt "$stage/fedfleet/data/radioml_public_test.pt"
  cp -p deploy/rates.toml "$stage/fedfleet/deploy/rates.toml"
  echo "== copying to the standby (one tar stream over Tailscale SSH)"
  # COPYFILE_DISABLE: macOS tar would add ._ files otherwise.
  COPYFILE_DISABLE=1 tar -C "$stage" -cf - . | \
    sb 'sudo tar -C /srv/fedfleet --warning=no-unknown-keyword -xf - --no-same-owner && sudo chown -R 10001:10001 /srv/fedfleet && sudo chmod 700 /srv/fedfleet/certificates && sudo chmod 600 /srv/fedfleet/certificates/server.key /srv/fedfleet/fedfleet-state/state.db' \
    || die "copy failed"
  rm -rf "$stage/certificates"                  # the standby's private key now exists only there
  local t_copy; t_copy=$(now)

  echo "== starting the SuperLink on the standby (image, Tailscale address only)"
  # shellcheck disable=SC2029  # IMAGE expands here on purpose
  sb "sudo docker rm -f fedfleet-superlink >/dev/null 2>&1; TS=\$(tailscale ip -4 | head -1); \
      sudo docker run -d --name fedfleet-superlink --restart unless-stopped --network host \
        -v /srv/fedfleet:/home/fedfleet '$IMAGE' flower-superlink \
        --ssl-certfile /home/fedfleet/certificates/server.pem --ssl-keyfile /home/fedfleet/certificates/server.key \
        --ssl-ca-certfile /home/fedfleet/certificates/ca.pem --enable-supernode-auth \
        --host \$TS --fleet-api-address \$TS:9092 --database /home/fedfleet/fedfleet-state/state.db >/dev/null" \
    || die "docker run failed"
  local up=0
  for _ in $(seq 1 30); do
    sb 'ss -ltn | grep -q ":8000 " && ss -ltn | grep -q ":9092 "' 2>/dev/null && { up=1; break; }
    sleep 2
  done
  [ "$up" = 1 ] || { sb 'sudo docker logs --tail 20 fedfleet-superlink' ; die "SuperLink not listening on 8000 and 9092"; }
  local t_link; t_link=$(now)

  # A connection for this Mac's flwr CLI (the old block, if any, is replaced; a backup is kept).
  python3 - "$HOME/.flwr/config.toml" "$CONN" "$STANDBY_HOST:8000" "$PWD/certificates/ca.pem" <<'PY' || die "could not write ~/.flwr/config.toml"
import re, shutil, sys
from pathlib import Path
p, name, addr, ca = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
text = p.read_text() if p.exists() else ""
if p.exists():
    shutil.copy2(p, p.with_suffix(".toml.bak"))
text = re.sub(rf"(?ms)^\[superlink\.{re.escape(name)}\]\n.*?(?=^\[|\Z)", "", text).rstrip("\n")
text += f'\n\n[superlink.{name}]\naddress = "{addr}"\nroot-certificates = "{ca}"\n'
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(text.lstrip("\n"))
PY
  # Runs restored from the snapshot may still be marked active; nothing serves them here.
  local stale
  stale=$(flwr ls "$CONN" --format json 2>/dev/null | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("runs", [])
except Exception:
    runs = []
print(" ".join(r["run-id"] for r in runs if str(r.get("status", "")).split(":")[0] in ("pending", "starting", "running")))')
  for id in $stale; do flwr stop "$id" "$CONN" >/dev/null 2>&1 && echo "   stopped run $id restored as active"; done

  echo "== moving the sites to $STANDBY_HOST"
  scripts/stop_local.sh >/dev/null; sleep 2
  out=$(scripts/run_sites.sh "$STANDBY_HOST" 2 3); echo "$out" | sed 's/^/   /'
  grep -q "(insecure" <<<"$out" && die "a Mac site started without TLS keys; run from ~/fedfleet with certificates/ and keys/"
  # shellcheck disable=SC2029  # STANDBY_HOST expands here on purpose
  ssh -o ConnectTimeout=20 "ubuntu@$SITE" "sudo mkdir -p /etc/systemd/system/fedfleet-site.service.d && \
     printf '[Service]\nEnvironment=SITE_AGG=$STANDBY_HOST\n' | sudo tee /etc/systemd/system/fedfleet-site.service.d/failover.conf >/dev/null && \
     sudo systemctl daemon-reload && sudo systemctl restart fedfleet-site" \
    && echo "   site 2 restarted against $STANDBY_HOST (drop-in failover.conf)" || echo "   !! could not move site 2"

  echo "== waiting for 3 sites on the standby (up to 3 min)"
  local n=0 registered=0 t_sites="" out
  for i in $(seq 1 90); do
    n=$(online_count); [ "$n" -ge 3 ] && { t_sites=$(now); break; }
    if [ "$i" = 30 ] && [ "$registered" = 0 ]; then
      # After 60 s: if the snapshot lacks a site key, register the public keys found here.
      registered=1
      for pub in keys/site-*.pub; do
        [ -f "$pub" ] || continue
        flwr supernode register "$pub" "$CONN" --name "$(basename "$pub" .pub)" >/dev/null 2>&1 \
          && echo "   registered $(basename "$pub") (it was missing from the snapshot)"
      done
    fi
    sleep 2
  done
  [ -n "$t_sites" ] || die "only $n sites online on the standby after 3 min"
  echo "   3 sites online"

  echo "== resuming as fedfleet-runs-$new ($rounds rounds in total, $restored restored)"
  local cfg out
  cfg="num-server-rounds=$rounds min-nodes=3 round-timeout=120.0 head-only=true site-eval=false lr=0.001"
  cfg="$cfg data-path=\"~/fedfleet/data/RML2016.10a_dict.pkl\" public-data-path=\"~/fedfleet/data/radioml_public_test.pt\""
  cfg="$cfg backbone-path=\"~/fedfleet/backbone.pt\" metrics-dir=\"~/fedfleet-runs-$new\" rates-path=\"~/fedfleet/deploy/rates.toml\""
  cfg="$cfg compression=\"none\" seed=-1 dp=false secagg=false resume=true"
  out=$(FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1 flwr run . "$CONN" --run-config "$cfg" 2>&1)
  grep -q "Successfully started run" <<<"$out" || { echo "$out" | tail -5; die "flwr run failed"; }
  local t_run; t_run=$(now)
  echo "   $(grep -m1 -o 'Successfully started run [0-9]*' <<<"$out")"
  printf 'FO_SRC=%q\nFO_NEW=%q\nFO_ROUNDS=%q\nFO_RESTORED=%q\nT_FO_START=%q\nT_FO_DOWNLOAD=%q\nT_FO_COPY=%q\nT_FO_LINK=%q\nT_FO_SITES=%q\nT_FO_RUN=%q\n' \
    "$src" "$new" "$rounds" "$restored" "$t0" "$t_dl" "$t_copy" "$t_link" "$t_sites" "$t_run" >> "$ENV_FILE"
  echo "== failover done in $(since "$t0") s (download $(python3 -c "print(f'{$t_dl-$t0:.1f}')"), copy $(python3 -c "print(f'{$t_copy-$t_dl:.1f}')"), SuperLink $(python3 -c "print(f'{$t_link-$t_copy:.1f}')"), sites $(python3 -c "print(f'{$t_sites-$t_link:.1f}')"), run $(python3 -c "print(f'{$t_run-$t_sites:.1f}')"))"
}

# ------------------------------------------------------------------------------------ drill
cmd_drill() {
  local name="${1:-}"; shift || usage
  check_name "$name"
  local rounds=8 after=3
  while [ $# -gt 0 ]; do
    case "$1" in --rounds) rounds="$2"; shift 2 ;; --after) after="$2"; shift 2 ;; *) usage ;; esac
  done
  [[ "$rounds" =~ ^[0-9]+$ ]] && [[ "$after" =~ ^[0-9]+$ ]] && [ "$after" -ge 1 ] && [ "$after" -lt "$rounds" ] && [ "$rounds" -le 20 ] \
    || die "--after must be at least 1 and below --rounds (at most 20)"
  check_name "$name-sb"
  [ -r "$TOKEN_FILE" ] || die "no API token at $TOKEN_FILE"
  [ -f "$KEY_FILE" ] || die "no Tailscale key at $KEY_FILE (needed by up; see the top of this script)"
  [ "$(az group exists -n "$RG" 2>/dev/null)" = false ] || die "$RG exists already; a cold drill starts without a standby (scripts/standby.sh down)"
  mkdir -p logs
  local log="logs/failover-$name.txt"
  echo "== drill $name: $rounds rounds on the primary, lost after round $after; log $log"
  {
  echo "== starting fedfleet-runs-$name on the primary"
  local out
  out=$(curl -sS -X POST "$GATEWAY/runs" -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
        -H 'content-type: application/json' -H 'User-Agent: fedfleet-standby/1.0' \
        -d "{\"name\":\"$name\",\"rounds\":$rounds,\"min_nodes\":3,\"round_timeout\":120,\"auto_resume\":false}")
  echo "   $out"
  [[ "$out" == *'"run_id"'* ]] || die "the run did not start"
  echo "== waiting for round $after"
  local n=0 t
  t=$(date +%s)
  while :; do
    # shellcheck disable=SC2029
    n=$(ssh "ubuntu@$AGG" "grep -c train_round_seconds ~/fedfleet-runs-$name/rounds.jsonl 2>/dev/null || true"); n=${n:-0}
    [ "${n:-0}" -ge "$after" ] && break
    [ $(( $(date +%s) - t )) -ge 1200 ] && die "only $n rounds after 20 min"
    sleep 3
  done
  # Give the 15-second Blob backup one cycle for the round's checkpoint. Round $((after + 1)) is
  # by then under way and is the work this failover loses.
  sleep 20
  local id
  # shellcheck disable=SC2029
  id=$(ssh "ubuntu@$AGG" "cat ~/fedfleet-runs-$name/active_run.json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
  local T0; T0=$(now)
  # shellcheck disable=SC2029
  ssh "ubuntu@$AGG" "cd ~ && ~/fedfleet/.venv/bin/flwr stop $id fedfleet-vm" >/dev/null 2>&1 \
    || echo "   !! flwr stop failed; the primary's run may keep going on its own folder (harmless: the standby uses a new name)"
  # shellcheck disable=SC2029
  local n0; n0=$(ssh "ubuntu@$AGG" "grep -c train_round_seconds ~/fedfleet-runs-$name/rounds.jsonl || true"); n0=${n0:-?}
  echo "== T0 $(date -u +%H:%M:%SZ): primary declared lost (run $id stopped with $n0 rounds; the service was not touched)"
  cmd_up || die "standby up failed"
  cmd_failover "$name" "$name-sb" --rounds "$rounds" || die "failover failed"
  load_env
  echo "== waiting for the first new round on the standby"
  local t_first="" t_all=""
  t=$(date +%s)
  while :; do
    n=$(standby_runs "$name-sb"); n=${n:-0}
    [ -z "$t_first" ] && [ "$n" -gt "$FO_RESTORED" ] && t_first=$(now) && echo "   round $n done at +$(python3 -c "print(f'{$t_first-$T0:.1f}')") s"
    [ "$n" -ge "$rounds" ] && { t_all=$(now); break; }
    [ $(( $(date +%s) - t )) -ge 1800 ] && { echo "!! $n of $rounds rounds after 30 min"; break; }
    sleep 2
  done
  echo
  echo "== timeline (seconds after T0, this Mac's clock)"
  python3 - "$T0" "$T_UP_VM" "$T_UP_TAILNET" "$T_UP_READY" "$T_FO_DOWNLOAD" "$T_FO_COPY" "$T_FO_LINK" "$T_FO_SITES" "$T_FO_RUN" "${t_first:-0}" "${t_all:-0}" <<'PY'
import sys
t0, *ts = map(float, sys.argv[1:])
names = ["VM created", "standby on tailnet", "Docker + image ready", "Blob download done", "state copied",
         "SuperLink listening", "3 sites online", "run resumed", "first new round done", "all rounds done"]
for n, t in zip(names, ts):
    print(f"   {n:24s} {'-' if t == 0 else f'{t - t0:8.1f}'}")
PY
  echo "   restored: $FO_RESTORED rounds (primary had $n0 when lost); image $IMAGE_DIGEST; region $STANDBY_LOCATION"
  echo "== rounds on the standby"
  # shellcheck disable=SC2029
  sb "cat /srv/fedfleet/fedfleet-runs-$name-sb/rounds.jsonl" | python3 -c '
import json, sys
for l in sys.stdin:
    if not l.strip():
        continue
    r = json.loads(l)
    acc, rnd, rf = r.get("global_acc"), r.get("round"), r.get("resumed_from_round")
    if r.get("event") == "resumed":
        print(f"   resumed from round {rf}  acc {acc}")
    elif "train_round_seconds" in r:
        secs, got, asked = r["train_round_seconds"], r.get("nodes_replied"), r.get("nodes_asked")
        print(f"   round {rnd:2d}  acc {acc}  {secs} s  replies {got}/{asked}")'
  echo "   path Mac -> standby: $(tailscale ping -c 3 "$STANDBY_HOST" 2>&1 | tail -1)"
  echo
  echo "next: scripts/standby.sh failback $name-sb, then scripts/standby.sh down"
  } 2>&1 | tee "$log"
}

# --------------------------------------------------------------------------------- failback
cmd_failback() {
  local new="${1:-}"; check_name "$new"; load_env
  echo "== copying fedfleet-runs-$new from the standby to the primary (Blob backup picks it up there)"
  # shellcheck disable=SC2029
  if ssh "ubuntu@$AGG" "test -e ~/fedfleet-runs-$new"; then
    echo "   already on the primary; not copied"
  else
    # shellcheck disable=SC2029
    sb "sudo tar -C /srv/fedfleet -cf - fedfleet-runs-$new" | ssh "ubuntu@$AGG" "tar -C ~ -xf -" \
      && echo "   copied" || echo "   !! copy failed; the folder is still on the standby"
  fi
  mkdir -p "logs/failover-$new"
  # shellcheck disable=SC2029
  sb "cat /srv/fedfleet/fedfleet-runs-$new/rounds.jsonl" > "logs/failover-$new/rounds.jsonl" 2>/dev/null \
    && echo "   rounds.jsonl also kept here: logs/failover-$new/"
  echo "== sites back to $AGG"
  scripts/stop_local.sh >/dev/null; sleep 2
  scripts/run_sites.sh "$AGG" 2 3 | sed 's/^/   /'
  ssh -o ConnectTimeout=20 "ubuntu@$SITE" "sudo rm -f /etc/systemd/system/fedfleet-site.service.d/failover.conf && \
     sudo systemctl daemon-reload && sudo systemctl restart fedfleet-site" \
    && echo "   site 2 restarted against $AGG" || echo "   !! could not move site 2 back"
  sb 'sudo docker stop fedfleet-superlink >/dev/null' && echo "   SuperLink on the standby stopped"
  echo "== sites online on the primary (up to 2 min)"
  for _ in $(seq 1 24); do
    n=$(ssh "ubuntu@$AGG" "cd ~ && ~/fedfleet/.venv/bin/flwr supernode ls fedfleet-vm --format json 2>/dev/null" | python3 -c '
import json, sys
try:
    print(sum(1 for n in json.load(sys.stdin).get("nodes", []) if n.get("status") == "online"))
except Exception:
    print(0)')
    [ "$n" -ge 3 ] && break
    sleep 5
  done
  echo "   $n online"
}

# ------------------------------------------------------------------------------------- down
cmd_down() {
  [ "$(az group exists -n "$RG")" = true ] || { echo "$RG does not exist"; rm -f "$ENV_FILE" "$KH"; return 0; }
  local role; role=$(az group show -n "$RG" --query "tags.\"fedfleet-role\"" -o tsv)
  [ "$role" = standby ] || die "$RG is not tagged fedfleet-role=standby; not deleting it"
  echo "== resources in $RG:"
  az resource list -g "$RG" --query "[].{type:type, name:name}" -o tsv | sed 's/^/   /'
  if [ "${YES:-0}" != 1 ]; then
    printf 'delete all of them? type the group name to confirm: '
    local ans; read -r ans
    [ "$ans" = "$RG" ] || die "not confirmed; nothing deleted"
  fi
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    # Logging out cuts the Tailscale SSH session it runs in, so schedule it 2 s later.
    sb 'sudo systemd-run --quiet --on-active=2 /usr/bin/tailscale logout' >/dev/null 2>&1 \
      && echo "   standby leaves the tailnet in 2 s (ephemeral node removed)" \
      || echo "   standby not reachable; its ephemeral node disappears from the tailnet on its own"
    sleep 4
  fi
  local t0; t0=$(now)
  az group delete -n "$RG" --yes --output none || die "delete failed; check the portal"
  echo "   $RG deleted in $(since "$t0") s"
  rm -f "$ENV_FILE" "$KH"
}

cmd_status() {
  if [ "$(az group exists -n "$RG" 2>/dev/null)" = true ]; then
    echo "$RG: $(az vm get-instance-view -g "$RG" -n "$VM" --query "instanceView.statuses[1].displayStatus" -o tsv 2>/dev/null)"
  else
    echo "$RG: does not exist (no standby, nothing billing)"
  fi
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"; echo "recorded: $STANDBY_HOST $STANDBY_IP ($STANDBY_LOCATION)"
    sb 'sudo docker ps --format "{{.Names}} {{.Status}}"' 2>/dev/null | sed 's/^/   /'
  fi
}

case "$CMD" in
  up) cmd_up ;;
  failover) cmd_failover "$@" ;;
  drill) cmd_drill "$@" ;;
  failback) cmd_failback "$@" ;;
  down) cmd_down ;;
  status) cmd_status ;;
  *) usage ;;
esac
