#!/usr/bin/env bash
# Chaos drills for Layer 3 (resilience), run from the Mac against the live federation.
#
#   scripts/chaos.sh kill <name> [--rounds 6] [--after 3] [--dp-noise Z] [--secagg] --yes
#       Starts a run, waits until round <after> is logged, waits 5 s more so the kill lands
#       inside the next round, then SIGKILLs the aggregator (fedfleet-superlink). systemd
#       restarts it (Restart=always, 5 s); the sites reconnect by themselves. As soon as the
#       aggregator lists all 3 sites online again, the drill resumes the run from its checkpoint
#       through the control API and waits for the last round. With --dp-noise the run uses differential privacy and the privacy ledger must
#       continue across the crash. --yes is required: this is the one sanctioned way to kill
#       the aggregator, and only between our own rounds.
#
#   scripts/chaos.sh partition <name> [--rounds 10] [--after 2] [--seconds 90] [--secagg]
#       Starts a run that tolerates a missing site (min_nodes 2, round timeout 60 s), waits
#       until round <after> is logged, then cuts the Azure site off from the aggregator: on
#       that VM only, an iptables rule drops its outgoing traffic to the aggregator's Flower
#       port. The rule is removed by a systemd timer on the VM after <seconds> even if this
#       script or the Mac dies, so the partition always heals. No firewall is opened anywhere,
#       and the Tailscale peer relay on that VM keeps working. After the heal it polls the
#       aggregator's site list until the Azure site is online again and reports how long that took
#       (a silent site goes offline and back online by heartbeat, which Flower does not log).
#
# --secagg runs either drill under secure aggregation (SecAgg+): a partition then tests a site
# dropping out of the protocol (two of three key shares are enough to unmask), a kill tests a
# crash and resume of a SecAgg+ run. Both print a timeline and the per-round table at the end. Start the sites first
# (scripts/cloud_up.sh) and shut them down after (scripts/cloud_down.sh).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
AGG="${AGG:-fedfleet-agg}"
SITE="${SITE:-fedfleet-site2}"
AGG_TS_IP="${AGG_TS_IP:-100.69.230.89}"     # aggregator's Tailscale address
FLOWER_PORT="${FLOWER_PORT:-9092}"            # the port sites connect to
GATEWAY="${GATEWAY:-https://fedfleet-gateway.fedfleet-ra.workers.dev}"
TOKEN_FILE="${TOKEN_FILE:-$HOME/.fedfleet-api-token}"

usage() { sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
MODE="${1:-}"; NAME="${2:-}"
[[ "$MODE" == kill || "$MODE" == partition ]] || usage
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || { echo "name: 1-40 lower-case letters, digits or hyphens"; exit 1; }
shift 2
ROUNDS=""; AFTER=""; DP_NOISE=""; SECONDS_CUT=90; YES=0; SECAGG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --rounds) ROUNDS="$2"; shift 2 ;;
    --after) AFTER="$2"; shift 2 ;;
    --dp-noise) DP_NOISE="$2"; shift 2 ;;
    --seconds) SECONDS_CUT="$2"; shift 2 ;;
    --yes) YES=1; shift ;;
    --secagg) SECAGG=1; shift ;;
    *) usage ;;
  esac
done
[ -n "$AFTER" ] || { [ "$MODE" = kill ] && AFTER=3 || AFTER=2; }
[ -n "$ROUNDS" ] || { [ "$MODE" = kill ] && ROUNDS=6 || ROUNDS=10; }
for v in "$ROUNDS" "$AFTER" "$SECONDS_CUT"; do [[ "$v" =~ ^[0-9]+$ ]] || { echo "numbers only: $v"; exit 1; }; done
[ "$AFTER" -lt "$ROUNDS" ] || { echo "--after must be below --rounds"; exit 1; }
[ -z "$DP_NOISE" ] || [[ "$DP_NOISE" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "--dp-noise: a number"; exit 1; }
[ "$MODE" = partition ] && [ -n "$DP_NOISE" ] && { echo "--dp-noise is for kill only (a DP run skips rounds with a missing site)"; exit 1; }
[ "$MODE" = kill ] && [ "$YES" != 1 ] && { echo "kill stops the aggregator; add --yes to confirm"; exit 1; }
[ -r "$TOKEN_FILE" ] || { echo "no API token at $TOKEN_FILE"; exit 1; }

now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
agg_now() { ssh "ubuntu@$AGG" 'date +%s.%N'; }
post() {   # $1 = JSON body; prints the reply, fails unless a run was started
  local out
  out=$(curl -sS -X POST "$GATEWAY/runs" -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
        -H 'content-type: application/json' -H 'User-Agent: fedfleet-chaos/1.0' -d "$1")
  echo "   $out"
  [[ "$out" == *'"run_id"'* ]]
}
rounds_logged() {   # training rounds in the run's rounds.jsonl (0 if none yet)
  # shellcheck disable=SC2029  # NAME is checked above
  ssh "ubuntu@$AGG" "grep -c train_round_seconds ~/fedfleet-runs-$NAME/rounds.jsonl 2>/dev/null" || true
}
online_nodes() {    # node ids the aggregator lists as online (heartbeat current), one line
  ssh "ubuntu@$AGG" "cd ~ && ~/fedfleet/.venv/bin/flwr supernode ls fedfleet-vm --format json 2>/dev/null" | python3 -c '
import json, sys
try:
    nodes = json.load(sys.stdin).get("nodes", [])
except Exception:
    nodes = []
print(" ".join(n["node-id"] for n in nodes if n.get("status") == "online"))'
}
wait_online() {     # $1 = count|node, $2 = number of sites or a node id, $3 = timeout in s.
  local t0 on; t0=$(date +%s)   # Echoes the aggregator's time when the condition holds.
  while :; do
    on=$(online_nodes)
    if [ "$1" = count ]; then [ "$(wc -w <<<"$on")" -ge "$2" ] && { agg_now; return 0; }
    else [[ " $on " == *" $2 "* ]] && { agg_now; return 0; }
    fi
    [ $(( $(date +%s) - t0 )) -ge "$3" ] && return 1
    sleep 2
  done
}
wait_rounds() {     # $1 = rounds to wait for, $2 = timeout in s
  local n t0; t0=$(date +%s)
  while :; do
    n=$(rounds_logged); n=${n:-0}
    [ "$n" -ge "$1" ] && return 0
    [ $(( $(date +%s) - t0 )) -ge "$2" ] && { echo "!! only $n of $1 rounds after $2 s"; return 1; }
    sleep 5
  done
}

echo "== checking that no other run is in progress"
active=$(ssh "ubuntu@$AGG" "cd ~ && ~/fedfleet/.venv/bin/flwr ls fedfleet-vm --format json 2>/dev/null" | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("runs", [])
except Exception:
    print("?"); sys.exit()
print(" ".join(r["run-id"] for r in runs if str(r.get("status", "")).split(":")[0] in ("pending", "starting", "running")))')
if [ "$active" = "?" ]; then
  echo "!! could not read the run list (flwr ls); not starting"; exit 1
elif [ -n "$active" ] && [ "${FORCE:-0}" != 1 ]; then
  echo "!! runs still marked active: $active"
  echo "   (a run killed earlier can stay listed as running; check with flwr ls, then FORCE=1 to go ahead)"
  exit 1
fi
echo "   none"

DP_JSON=""
[ -n "$DP_NOISE" ] && DP_JSON=",\"dp\":true,\"dp_noise_multiplier\":$DP_NOISE"
[ "$SECAGG" = 1 ] && DP_JSON="$DP_JSON,\"secagg\":true"
if [ "$MODE" = kill ]; then
  BODY="{\"name\":\"$NAME\",\"rounds\":$ROUNDS,\"min_nodes\":3$DP_JSON}"
else
  BODY="{\"name\":\"$NAME\",\"rounds\":$ROUNDS,\"min_nodes\":2,\"round_timeout\":60$DP_JSON}"
fi
echo "== starting fedfleet-runs-$NAME ($MODE drill, $ROUNDS rounds$([ "$SECAGG" = 1 ] && echo ", SecAgg+"))"
post "$BODY" || { echo "!! the run did not start"; exit 1; }
echo "== waiting for round $AFTER"
wait_rounds "$AFTER" 1200 || exit 1

if [ "$MODE" = kill ]; then
  sleep 5
  echo "== SIGKILL to fedfleet-superlink (inside round $((AFTER + 1)))"
  T_KILL=$(ssh "ubuntu@$AGG" 'date +%s.%N; sudo systemctl kill -s SIGKILL fedfleet-superlink' | head -1)
  echo "   killed at $T_KILL (aggregator clock)"
  T_UP=""
  for _ in $(seq 1 60); do
    sleep 1
    if ssh "ubuntu@$AGG" 'systemctl is-active --quiet fedfleet-superlink && ss -ltn | grep -q ":8000 "'; then
      T_UP=$(agg_now); break
    fi
  done
  [ -n "$T_UP" ] || { echo "!! fedfleet-superlink did not come back within 60 s; check: systemctl status fedfleet-superlink"; exit 1; }
  echo "   back up at $T_UP; restarts so far: $(ssh "ubuntu@$AGG" 'systemctl show -p NRestarts --value fedfleet-superlink')"
  echo "== waiting until the aggregator lists 3 sites online (up to 3 min), then resuming"
  T_SITES=$(wait_online count 3 180) || { echo "!! fewer than 3 sites online after 3 min; resuming anyway (the run waits for them)"; T_SITES=""; }
  [ -n "$T_SITES" ] && echo "   3 sites online at +$(python3 -c "print(f'{$T_SITES - $T_KILL:.1f}')") s"
  T_RESUME=$(agg_now)
  post "{\"name\":\"$NAME\",\"rounds\":$ROUNDS,\"min_nodes\":3,\"resume\":true$DP_JSON}" || { echo "!! resume was refused"; exit 1; }
else
  # shellcheck disable=SC2029
  SITE_NODE=$(ssh "ubuntu@$AGG" "cat ~/fedfleet-runs-$NAME/rounds.jsonl" | python3 -c '
import json, sys
ids = {s.get("node") for l in sys.stdin if l.strip() for s in json.loads(l).get("sites", []) if s.get("site") == 2}
print(next(iter(ids), ""))')
  [ -n "$SITE_NODE" ] || { echo "!! site 2 is not in the logged rounds; is it running?"; exit 1; }
  echo "   site 2 is node $SITE_NODE"
  STAMP=$(date +%s)
  RULE="OUTPUT -d $AGG_TS_IP -p tcp --dport $FLOWER_PORT -j DROP"
  echo "== cutting $SITE off from the aggregator for $SECONDS_CUT s (heal is scheduled on the VM first)"
  # shellcheck disable=SC2029  # all values were checked above
  T_CUT=$(ssh "ubuntu@$SITE" "sudo systemd-run --quiet --unit fedfleet-chaos-heal-$STAMP --on-active=${SECONDS_CUT}s /usr/sbin/iptables -D $RULE \
          && sudo /usr/sbin/iptables -I $RULE && date +%s.%N") || { echo "!! could not apply the partition"; exit 1; }
  echo "   cut at $T_CUT (Azure VM clock), heals at about +$SECONDS_CUT s"
  sleep "$SECONDS_CUT"; sleep 2
  # shellcheck disable=SC2029
  if ssh "ubuntu@$SITE" "sudo /usr/sbin/iptables -C $RULE 2>/dev/null"; then
    echo "!! the rule is still there; removing it now"
    # shellcheck disable=SC2029
    ssh "ubuntu@$SITE" "sudo /usr/sbin/iptables -D $RULE"
  else
    echo "   healed: the rule is gone"
  fi
  echo "== waiting for the aggregator to list site 2 online again (up to 5 min)"
  T_BACK=$(wait_online node "$SITE_NODE" 300) || { echo "!! site 2 not online 5 min after the heal"; T_BACK=""; }
  [ -n "$T_BACK" ] && echo "   online again at $T_BACK"
fi

echo "== waiting for round $ROUNDS"
wait_rounds "$ROUNDS" 1800 || true

echo "== timeline (aggregator clock unless noted)"
REPORT="$(mktemp)"; trap 'rm -f "$REPORT"' EXIT
cat > "$REPORT" <<'PY'
import json, os, sys
rows = [json.loads(l) for l in sys.stdin if l.strip()]
def env(k):
    v = os.environ.get(k)
    return float(v) if v else None
kill, up, resume, cut = env("T_KILL"), env("T_UP"), env("T_RESUME"), env("T_CUT")
sites_up, t_back = env("T_SITES"), env("T_BACK")
heal = cut + float(os.environ["CUT"]) if cut else None
def site_ids(r):
    return sorted(s.get("site") for s in r.get("sites", []) if s.get("site") is not None)
seen_after = False
for r in rows:
    ts = r.get("timestamp")
    if r.get("event"):
        line = "   event %s from round %s" % (r["event"], r.get("resumed_from_round"))
        if kill and ts:
            line += ", %.1f s after the kill" % (ts - kill)
        print(line)
        continue
    if "train_round_seconds" not in r:
        continue
    dp = r.get("dp") or {}
    eps = ""
    if dp.get("epsilon") is not None:
        eps = ", epsilon %.2f (%s releases)" % (dp["epsilon"], dp.get("releases"))
    note = ""
    if kill and ts and ts > kill and not seen_after:
        seen_after = True
        note = "  <- first round after the kill, %.1f s after it" % (ts - kill)
    sa = r.get("secagg")
    if sa is not None:
        note = (", SecAgg+" + ("" if sa.get("aggregated") else " not aggregated")) + note
    print("   round %s: %s/%s replied, sites %s, %.1f s, accuracy %.4f%s%s" % (
        r["round"], r.get("nodes_replied"), r.get("nodes_asked"), site_ids(r),
        r["train_round_seconds"], r["global_acc"], eps, note))
if kill:
    print("   aggregator back %.1f s after the kill; resume requested at +%.1f s" % (up - kill, resume - kill))
    if sites_up:
        print("   all 3 sites online %.1f s after the kill" % (sites_up - kill))
if cut:
    trained = [r for r in rows if "train_round_seconds" in r]
    miss = [r["round"] for r in trained if r.get("timestamp", 0) > cut and 2 not in site_ids(r)]
    back = [r for r in trained if r.get("timestamp", 0) > heal and 2 in site_ids(r)]
    print("   site 2 missing in rounds %s" % miss)
    if t_back:
        print("   site 2 online again %.1f s after the heal (aggregator's site list; the heal time is the VM's clock + %s s)" % (t_back - heal, os.environ["CUT"]))
    if back:
        print("   site 2 back in round %s, logged %.1f s after the heal" % (back[0]["round"], back[0]["timestamp"] - heal))
    else:
        print("   site 2 did not come back before the run ended")
PY
# shellcheck disable=SC2029
ssh "ubuntu@$AGG" "cat ~/fedfleet-runs-$NAME/rounds.jsonl" | \
  T_KILL="${T_KILL:-}" T_UP="${T_UP:-}" T_SITES="${T_SITES:-}" T_RESUME="${T_RESUME:-}" T_CUT="${T_CUT:-}" \
  T_BACK="${T_BACK:-}" CUT="$SECONDS_CUT" python3 "$REPORT"
