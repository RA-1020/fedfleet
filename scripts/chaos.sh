#!/usr/bin/env bash
# Chaos drills for Layer 3 (resilience), run from the Mac against the live federation.
#
#   scripts/chaos.sh kill <name> [--rounds 6] [--after 3] [--dp-noise Z] [--secagg] [--auto] --yes
#       Starts a run, waits until round <after> is logged, waits 5 s more so the kill lands
#       inside the next round, then SIGKILLs the aggregator (fedfleet-superlink). systemd
#       restarts it (Restart=always, 5 s); the sites reconnect by themselves. As soon as the
#       aggregator lists all 3 sites online again, the drill resumes the run from its checkpoint
#       through the control API (retrying until Flower has marked the lost run failed, about a
#       minute after the kill) and waits for the last round. With --auto the drill does not resume:
#       the aggregator's watchdog must do it alone, and the drill times it. With --dp-noise the run uses differential privacy and the privacy ledger must
#       continue across the crash. --yes is required: this is the one sanctioned way to kill
#       the aggregator, and only between our own rounds.
#
#   scripts/chaos.sh delay <name> [--rounds 12] [--after 2] [--seconds 150] [--delay-ms 200]
#                    [--jitter-ms 50] [--loss 2] [--secagg]
#       A bad network instead of a dead one: same run settings as partition, then on the Azure VM
#       only, netem delays the site's packets to the aggregator's Flower port by <delay-ms> (+/-
#       <jitter-ms>) and drops <loss> percent of them. A tc filter picks just that traffic, so SSH
#       and the peer relay are untouched. As with partition, the heal (tc qdisc del) is scheduled
#       on the VM first. Reports round times before, during and after, whether site 2 kept up, and
#       netem's own packet counters as proof the impairment was applied.
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
#   scripts/chaos.sh stop-site <name> [--rounds 10] [--after 2] [--seconds 90]
#       A site process dies instead of its network: same run settings as partition, then the Azure
#       site's service (fedfleet-site) is stopped; a timer on the VM, scheduled first, starts it
#       again after <seconds>. A restarted site may register under a new node id, so the drill
#       waits for three sites online, not for the old id.
#
#   scripts/chaos.sh cpu <name> [--rounds 12] [--after 2] [--seconds 150] [--workers N]
#       A slow site: same run settings as partition, then busy loops on the Azure VM (N, default
#       one per CPU) compete with its training for <seconds>. systemd ends them at that limit
#       (RuntimeMaxSec), whatever happens to this script. Reports rounds before, during, after.
#
#   scripts/chaos.sh secagg-drop <name> [--rounds 5] [--drop-round 3]
#       SecAgg+ run, 3 sites; in round <drop-round> site 2 drops out after sharing its keys
#       (the run setting chaos_secagg_drop): SecAgg+ must unmask the other two from the shares
#       and the round must still be aggregated. Needs the control API with chaos_secagg_drop.
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

usage() { sed -n '3,50p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
MODE="${1:-}"; NAME="${2:-}"
[[ "$MODE" =~ ^(kill|partition|delay|stop-site|cpu|secagg-drop)$ ]] || usage
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || { echo "name: 1-40 lower-case letters, digits or hyphens"; exit 1; }
shift 2
ROUNDS=""; AFTER=""; DP_NOISE=""; SECONDS_CUT=""; YES=0; SECAGG=0
DELAY_MS=200; JITTER_MS=50; LOSS=2; AUTO=0; WORKERS=0; DROP_ROUND=3
while [ $# -gt 0 ]; do
  case "$1" in
    --rounds) ROUNDS="$2"; shift 2 ;;
    --after) AFTER="$2"; shift 2 ;;
    --dp-noise) DP_NOISE="$2"; shift 2 ;;
    --seconds) SECONDS_CUT="$2"; shift 2 ;;
    --delay-ms) DELAY_MS="$2"; shift 2 ;;
    --jitter-ms) JITTER_MS="$2"; shift 2 ;;
    --loss) LOSS="$2"; shift 2 ;;
    --yes) YES=1; shift ;;
    --secagg) SECAGG=1; shift ;;
    --auto) AUTO=1; shift ;;
    --workers) WORKERS="$2"; shift 2 ;;
    --drop-round) DROP_ROUND="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[ -n "$AFTER" ] || case "$MODE" in kill) AFTER=3 ;; secagg-drop) AFTER=1 ;; *) AFTER=2 ;; esac
[ -n "$ROUNDS" ] || case "$MODE" in kill) ROUNDS=6 ;; delay|cpu) ROUNDS=12 ;; secagg-drop) ROUNDS=5 ;; *) ROUNDS=10 ;; esac
[ -n "$SECONDS_CUT" ] || case "$MODE" in delay|cpu) SECONDS_CUT=150 ;; *) SECONDS_CUT=90 ;; esac
for v in "$ROUNDS" "$AFTER" "$SECONDS_CUT" "$DELAY_MS" "$JITTER_MS" "$WORKERS" "$DROP_ROUND"; do [[ "$v" =~ ^[0-9]+$ ]] || { echo "numbers only: $v"; exit 1; }; done
[ "$AFTER" -lt "$ROUNDS" ] || { echo "--after must be below --rounds"; exit 1; }
[[ "$LOSS" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "--loss: a percentage"; exit 1; }
python3 -c "import sys; sys.exit(0 if float('$LOSS') <= 50 else 1)" || { echo "--loss: at most 50"; exit 1; }
[ "$DELAY_MS" -le 5000 ] && [ "$JITTER_MS" -le "$DELAY_MS" ] || { echo "--delay-ms at most 5000, --jitter-ms at most --delay-ms"; exit 1; }
[ -z "$DP_NOISE" ] || [[ "$DP_NOISE" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "--dp-noise: a number"; exit 1; }
[ "$MODE" != kill ] && [ -n "$DP_NOISE" ] && { echo "--dp-noise is for kill only (a DP run skips rounds with a missing site)"; exit 1; }
[ "$MODE" != kill ] && [ "$AUTO" = 1 ] && { echo "--auto is for kill only"; exit 1; }
[ "$MODE" = secagg-drop ] && { [ "$DROP_ROUND" -ge 1 ] && [ "$DROP_ROUND" -le "$ROUNDS" ] || { echo "--drop-round must be within the run's rounds"; exit 1; }; }
[ "$WORKERS" -le 16 ] || { echo "--workers: at most 16"; exit 1; }
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
  # Without --auto the drill resumes by itself, so the watchdog must stay out of it.
  BODY="{\"name\":\"$NAME\",\"rounds\":$ROUNDS,\"min_nodes\":3,\"auto_resume\":$([ "$AUTO" = 1 ] && echo true || echo false)$DP_JSON}"
elif [ "$MODE" = secagg-drop ]; then
  BODY="{\"name\":\"$NAME\",\"rounds\":$ROUNDS,\"min_nodes\":3,\"secagg\":true,\"chaos_secagg_drop\":\"2:$DROP_ROUND\"}"
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
  if [ "$AUTO" = 1 ]; then
    echo "== waiting for the watchdog to resume the run (up to 5 min)"
    # shellcheck disable=SC2029  # NAME is checked above
    OLD_ID=$(ssh "ubuntu@$AGG" "cat ~/fedfleet-runs-$NAME/active_run.json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
    T_RESUME=""
    for _ in $(seq 1 60); do
      sleep 5
      # shellcheck disable=SC2029
      m=$(ssh "ubuntu@$AGG" "cat ~/fedfleet-runs-$NAME/active_run.json" 2>/dev/null)
      new=$(python3 -c 'import json,sys; m=json.loads(sys.argv[1]); print(m["run_id"], m.get("started", ""))' "$m" 2>/dev/null)
      if [ -n "$new" ] && [ "${new%% *}" != "$OLD_ID" ]; then T_RESUME="${new##* }"; break; fi
    done
    [ -n "$T_RESUME" ] || { echo "!! the watchdog did not resume within 5 min; see: ssh ubuntu@$AGG journalctl -u fedfleet-watchdog -n 30"; exit 1; }
    echo "   resumed by the watchdog as run ${new%% *}, +$(python3 -c "print(f'{$T_RESUME - $T_KILL:.1f}')") s after the kill"
    # shellcheck disable=SC2029  # NAME is checked above
    ssh "ubuntu@$AGG" "journalctl -u fedfleet-watchdog --since '-6 min' --no-pager -o cat | grep -F 'fedfleet-runs-$NAME'" | tail -4 | sed 's/^/   /'
  else
    T_RESUME=$(agg_now)
    RESUME_BODY="{\"name\":\"$NAME\",\"rounds\":$ROUNDS,\"min_nodes\":3,\"resume\":true,\"auto_resume\":false$DP_JSON}"
    # Flower releases the lost run (finished:failed, "No heartbeat") about a minute after the kill;
    # until then the API answers "still in progress". Retry for up to 3 min.
    ok=0
    for _ in $(seq 1 36); do
      if post "$RESUME_BODY"; then ok=1; break; fi
      sleep 5
    done
    [ "$ok" = 1 ] || { echo "!! resume was refused for 3 min"; exit 1; }
    echo "   resume accepted +$(python3 -c "print(f'{$(agg_now) - $T_KILL:.1f}')") s after the kill (first try at +$(python3 -c "print(f'{$T_RESUME - $T_KILL:.1f}')") s)"
  fi
elif [ "$MODE" != secagg-drop ]; then
  # shellcheck disable=SC2029
  SITE_NODE=$(ssh "ubuntu@$AGG" "cat ~/fedfleet-runs-$NAME/rounds.jsonl" | python3 -c '
import json, sys
ids = {s.get("node") for l in sys.stdin if l.strip() for s in json.loads(l).get("sites", []) if s.get("site") == 2}
print(next(iter(ids), ""))')
  [ -n "$SITE_NODE" ] || { echo "!! site 2 is not in the logged rounds; is it running?"; exit 1; }
  echo "   site 2 is node $SITE_NODE"
  STAMP=$(date +%s)
  RULE="OUTPUT -d $AGG_TS_IP -p tcp --dport $FLOWER_PORT -j DROP"
fi
if [ "$MODE" = delay ]; then
  DEV=tailscale0
  echo "== preflight on $SITE: netem/prio/u32 modules, $DEV, no custom root qdisc"
  # shellcheck disable=SC2029
  pre=$(ssh "ubuntu@$SITE" "sudo modprobe -a sch_netem sch_prio cls_u32 2>&1 && echo netem-ok; ip link show $DEV >/dev/null 2>&1 && echo dev-ok; tc qdisc show dev $DEV root") || true
  # shellcheck disable=SC2001
  echo "$pre" | sed 's/^/   /'
  grep -q netem-ok <<<"$pre" || { echo "!! sch_netem, sch_prio or cls_u32 is missing on $SITE (Azure kernels keep it in linux-modules-extra-\$(uname -r))"; exit 1; }
  grep -q dev-ok <<<"$pre" || { echo "!! no $DEV on $SITE"; exit 1; }
  grep -Eq "qdisc (prio|netem|htb|tbf) " <<<"$pre" && { echo "!! $DEV already has a custom root qdisc (left over?); clear it with: sudo tc qdisc del dev $DEV root"; exit 1; }
  NETEM="delay ${DELAY_MS}ms ${JITTER_MS}ms distribution normal loss ${LOSS}%"
  echo "== impairing $SITE -> aggregator:$FLOWER_PORT for $SECONDS_CUT s: $NETEM (heal is scheduled on the VM first)"
  # Band 4 is used only by the filter (priomap sends ordinary traffic to bands 1-3), so only
  # packets to the aggregator's Flower port see netem.
  # shellcheck disable=SC2029
  T_CUT=$(ssh "ubuntu@$SITE" "sudo systemd-run --quiet --unit fedfleet-chaos-heal-$STAMP --on-active=${SECONDS_CUT}s /usr/sbin/tc qdisc del dev $DEV root \
          && sudo tc qdisc add dev $DEV root handle 1: prio bands 4 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1 \
          && sudo tc qdisc add dev $DEV parent 1:4 handle 40: netem $NETEM \
          && sudo tc filter add dev $DEV parent 1: protocol ip prio 1 u32 match ip dst $AGG_TS_IP/32 match ip dport $FLOWER_PORT 0xffff flowid 1:4 \
          && date +%s.%N") || { echo "!! could not apply the impairment; clearing"; ssh "ubuntu@$SITE" "sudo tc qdisc del dev $DEV root" 2>/dev/null; exit 1; }
  echo "   impaired at $T_CUT (Azure VM clock), heals at about +$SECONDS_CUT s"
  sleep $(( SECONDS_CUT / 2 ))
  # shellcheck disable=SC2029
  NETEM_MID=$(ssh "ubuntu@$SITE" "tc -s qdisc show dev $DEV parent 1:4" | tr '\n' ' ' | grep -Eo 'Sent [0-9]+ bytes [0-9]+ pkt \(dropped [0-9]+' | head -1)
  echo "   netem halfway: ${NETEM_MID:-no counters (was the filter matched?)}"
  # How this Mac (sites 0 and 1) reaches the aggregator right now: direct, via the peer relay
  # on the Azure VM, or via a DERP relay. Informational only.
  PATH_MID=$(tailscale status 2>/dev/null | grep -E "[[:space:]]${AGG}[[:space:]]" | sed -E 's/.*(active|idle);[[:space:]]*//' | cut -c1-80)
  echo "   Mac -> aggregator path halfway: ${PATH_MID:-unknown (tailscale CLI not found)}"
  sleep $(( SECONDS_CUT - SECONDS_CUT / 2 )); sleep 2
  # shellcheck disable=SC2029
  if ssh "ubuntu@$SITE" "tc qdisc show dev $DEV root | grep -q 'qdisc prio'"; then
    echo "!! the impairment is still there; removing it now"
    ssh "ubuntu@$SITE" "sudo tc qdisc del dev $DEV root"
  else
    echo "   healed: back to the default qdisc"
  fi
elif [ "$MODE" = stop-site ]; then
  # shellcheck disable=SC2029
  ssh "ubuntu@$SITE" "systemctl cat fedfleet-site >/dev/null 2>&1" || { echo "!! fedfleet-site is not installed on $SITE (deploy/site/)"; exit 1; }
  echo "== stopping site 2 (fedfleet-site on $SITE) for $SECONDS_CUT s (restart is scheduled on the VM first)"
  # shellcheck disable=SC2029  # all values were checked above
  T_CUT=$(ssh "ubuntu@$SITE" "sudo systemd-run --quiet --unit fedfleet-chaos-heal-$STAMP --on-active=${SECONDS_CUT}s /usr/bin/systemctl start fedfleet-site \
          && sudo systemctl stop fedfleet-site && date +%s.%N") || { echo "!! could not stop the site"; exit 1; }
  echo "   stopped at $T_CUT (Azure VM clock), restarts at about +$SECONDS_CUT s"
  sleep "$SECONDS_CUT"; sleep 3
  if ssh "ubuntu@$SITE" "systemctl is-active --quiet fedfleet-site"; then
    echo "   healed: fedfleet-site is running again"
  else
    echo "!! fedfleet-site is not running; starting it now"
    ssh "ubuntu@$SITE" "sudo systemctl start fedfleet-site"
  fi
  echo "== waiting for the aggregator to list 3 sites online again (up to 5 min)"
  T_BACK=$(wait_online count 3 300) || { echo "!! fewer than 3 sites online 5 min after the restart"; T_BACK=""; }
  [ -n "$T_BACK" ] && echo "   3 sites online again at $T_BACK"
elif [ "$MODE" = cpu ]; then
  echo "== loading the CPUs of $SITE for $SECONDS_CUT s ($([ "$WORKERS" -gt 0 ] && echo "$WORKERS busy loops" || echo "one busy loop per CPU"); systemd stops them at the limit)"
  # shellcheck disable=SC2029,SC2016  # values were checked above; \$ expands on the VM
  T_CUT=$(ssh "ubuntu@$SITE" "sudo systemd-run --quiet --unit fedfleet-chaos-cpu-$STAMP --property=RuntimeMaxSec=${SECONDS_CUT} \
          /bin/sh -c 'n=$WORKERS; [ \$n -gt 0 ] || n=\$(nproc); i=0; while [ \$i -lt \$n ]; do yes > /dev/null & i=\$((i+1)); done; wait' \
          && date +%s.%N") || { echo "!! could not start the load"; exit 1; }
  echo "   loaded at $T_CUT (Azure VM clock), ends at about +$SECONDS_CUT s"
  sleep $(( SECONDS_CUT / 2 ))
  LOAD_MID=$(ssh "ubuntu@$SITE" "echo \"load \$(cut -d' ' -f1 /proc/loadavg) on \$(nproc) CPUs\"")
  echo "   halfway: $LOAD_MID"
  sleep $(( SECONDS_CUT - SECONDS_CUT / 2 )); sleep 3
  # shellcheck disable=SC2029
  if ssh "ubuntu@$SITE" "systemctl is-active --quiet fedfleet-chaos-cpu-$STAMP"; then
    echo "!! the load is still running; stopping it now"
    # shellcheck disable=SC2029
    ssh "ubuntu@$SITE" "sudo systemctl stop fedfleet-chaos-cpu-$STAMP"
  else
    echo "   healed: the load has ended"
  fi
  left=$(ssh "ubuntu@$SITE" "pgrep -c -x yes || true")
  [ "${left:-0}" = 0 ] || echo "!! $left busy loops still running on $SITE; stop them with: ssh ubuntu@$SITE 'pkill -x yes'"
elif [ "$MODE" = partition ]; then
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
    if r.get("aggregated") is False:
        note = ", NOT aggregated (%s)" % r.get("skipped", "no aggregate") + note
    sa = r.get("secagg")
    if sa is not None:
        note = (", SecAgg+" + ("" if sa.get("aggregated") else " not aggregated")) + note
    print("   round %s: %s/%s replied, sites %s, %.1f s, accuracy %.4f%s%s" % (
        r["round"], r.get("nodes_replied"), r.get("nodes_asked"), site_ids(r),
        r["train_round_seconds"], r["global_acc"], eps, note))
if os.environ.get("MODE") == "secagg-drop":
    trained = [r for r in rows if "train_round_seconds" in r]
    d = int(os.environ.get("DROP_ROUND", "0"))
    r = next((x for x in trained if x["round"] == d), None)
    if r is None:
        print("   round %d not logged" % d)
    else:
        sa = r.get("secagg") or {}
        print("   drop round %d: %s/%s replied, sites %s, aggregated %s; SecAgg+ %s" % (
            d, r.get("nodes_replied"), r.get("nodes_asked"), site_ids(r),
            sa.get("aggregated", r.get("aggregated")),
            "recovered from the dropout" if sa.get("aggregated", r.get("aggregated")) and 2 not in site_ids(r)
            else "did NOT recover as expected"))
        later = [x for x in trained if x["round"] > d]
        print("   later rounds with site 2 back: %s" % ([x["round"] for x in later if 2 in site_ids(x)] or "none"))
if kill:
    print("   aggregator back %.1f s after the kill; resume %s at +%.1f s" % (
        up - kill, "started by the watchdog" if os.environ.get("AUTO") == "1" else "first requested", resume - kill))
    if sites_up:
        print("   all 3 sites online %.1f s after the kill" % (sites_up - kill))
if cut and os.environ.get("MODE") in ("delay", "cpu"):
    trained = [r for r in rows if "train_round_seconds" in r]
    phases = {"before": [], "during": [], "after": []}
    for r in trained:
        end = r.get("timestamp", 0); start = end - r["train_round_seconds"]
        p = "before" if end <= cut else ("after" if start >= heal else "during")
        phases[p].append(r)
    for p, rs in phases.items():
        if not rs:
            print("   %s: no rounds" % p); continue
        secs = [r["train_round_seconds"] for r in rs]
        with2 = sum(1 for r in rs if 2 in site_ids(r))
        print("   %s: rounds %s, mean %.1f s, max %.1f s, site 2 in %d of %d" % (
            p, [r["round"] for r in rs], sum(secs) / len(secs), max(secs), with2, len(rs)))
    if os.environ.get("MODE") == "delay":
        print("   impairment: %s" % os.environ.get("NETEM", ""))
        print("   netem counters halfway: %s" % (os.environ.get("NETEM_MID") or "none"))
        print("   Mac -> aggregator path halfway: %s" % (os.environ.get("PATH_MID") or "unknown"))
    else:
        print("   CPU load halfway: %s" % (os.environ.get("LOAD_MID") or "not read"))
    skipped = [r["round"] for r in trained if r.get("aggregated") is False]
    print("   rounds not aggregated (too few replies): %s" % (skipped or "none"))
elif cut:
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
  T_BACK="${T_BACK:-}" CUT="$SECONDS_CUT" MODE="$MODE" AUTO="$AUTO" NETEM="${NETEM:-}" NETEM_MID="${NETEM_MID:-}" PATH_MID="${PATH_MID:-}" LOAD_MID="${LOAD_MID:-}" DROP_ROUND="$DROP_ROUND" python3 "$REPORT"
