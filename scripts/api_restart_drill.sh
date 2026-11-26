#!/usr/bin/env bash
# Control-API upgrade drill (task 13.6): restart the control API in the middle of a run and show
# that training is not disturbed. The API only starts runs and reads their logs; it is not in
# the path between the sites and the aggregator, so rounds should carry on as if nothing happened.
#
#   scripts/api_restart_drill.sh <name> [--rounds 6] [--after 2]
#
# Run on the Mac after scripts/cloud_up.sh (3 sites). Starts a plain run through the gateway,
# waits for round <after>, then on the aggregator restarts fedfleet-control-api (the same thing
# an upgrade does: new code, same port) and times, on the aggregator itself, how long
# http://127.0.0.1:8088/healthz is unavailable (polled every 0.1 s; the gateway's rate limit
# of 20 requests a minute is too coarse for this). Then it checks the gateway answers again,
# waits for the last round, and prints the rounds around the restart.
set -euo pipefail
cd "$(dirname "$0")/.."
AGG="${AGG:-fedfleet-agg}"
GATEWAY="${GATEWAY:-https://fedfleet-gateway.fedfleet-ra.workers.dev}"
TOKEN_FILE="${TOKEN_FILE:-$HOME/.fedfleet-api-token}"

NAME="${1:-}"; shift || true
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || { echo "usage: $0 <name> [--rounds 6] [--after 2]"; exit 1; }
ROUNDS=6; AFTER=2
while [ $# -gt 0 ]; do
  case "$1" in
    --rounds) ROUNDS="$2"; shift 2 ;;
    --after) AFTER="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done
for v in "$ROUNDS" "$AFTER"; do [[ "$v" =~ ^[0-9]+$ ]] || { echo "numbers only: $v"; exit 1; }; done
[ "$AFTER" -lt "$ROUNDS" ] || { echo "--after must be below --rounds"; exit 1; }
[ -r "$TOKEN_FILE" ] || { echo "no API token at $TOKEN_FILE"; exit 1; }

gw() {   # $1 = method, $2 = path, [$3 = body]; prints "HTTP-code body"
  curl -sS -o /tmp/fedfleet-drill-body -w '%{http_code}' -X "$1" "$GATEWAY$2" \
    -H "Authorization: Bearer $(cat "$TOKEN_FILE")" -H 'content-type: application/json' \
    -H 'User-Agent: fedfleet-drill/1.0' ${3:+-d "$3"} || echo 000
  echo " $(head -c 300 /tmp/fedfleet-drill-body 2>/dev/null)"
}
rounds_logged() {
  # shellcheck disable=SC2029  # NAME is checked above
  ssh "ubuntu@$AGG" "grep -c train_round_seconds ~/fedfleet-runs-$NAME/rounds.jsonl 2>/dev/null" || true
}
wait_rounds() {   # $1 = rounds, $2 = timeout s
  local n t0; t0=$(date +%s)
  while :; do
    n=$(rounds_logged); n=${n:-0}
    [ "$n" -ge "$1" ] && return 0
    [ $(( $(date +%s) - t0 )) -ge "$2" ] && { echo "!! only $n of $1 rounds after $2 s"; exit 1; }
    sleep 5
  done
}

echo "== starting fedfleet-runs-$NAME ($ROUNDS rounds, 3 sites)"
out=$(gw POST /runs "{\"name\":\"$NAME\",\"rounds\":$ROUNDS,\"min_nodes\":3,\"auto_resume\":false}")
echo "   $out"
[[ "$out" == 202* ]] || { echo "!! the run did not start"; exit 1; }

echo "== waiting for round $AFTER"
wait_rounds "$AFTER" 600

echo "== restarting fedfleet-control-api on the aggregator (as an upgrade would)"
# shellcheck disable=SC2016  # expanded on the aggregator
times=$(ssh "ubuntu@$AGG" 'until curl -sf -o /dev/null http://127.0.0.1:8088/healthz; do sleep 0.1; done
  t0=$(date +%s.%N); sudo systemctl restart fedfleet-control-api; t1=$(date +%s.%N)
  down=""
  for _ in $(seq 1 600); do
    if curl -sf -o /dev/null --max-time 1 http://127.0.0.1:8088/healthz; then break; fi
    [ -z "$down" ] && down=$(date +%s.%N)
    sleep 0.1
  done
  t2=$(date +%s.%N); echo "$t0 $t1 $t2"')
read -r T0 T1 T2 <<<"$times"
python3 -c "t0,t1,t2=$T0,$T1,$T2; print(f'   systemctl restart returned after {t1-t0:.2f} s; /healthz answered again {t2-t0:.2f} s after the restart began')"

echo "== the gateway after the restart"
echo "   GET /healthz -> $(gw GET /healthz)"
st=$(gw GET /status)
echo "   GET /status  -> ${st:0:3} (services: $(python3 -c 'import json; d=json.load(open("/tmp/fedfleet-drill-body")); print(", ".join(f"{k}={v}" for k,v in d.get("services",{}).items()))' 2>/dev/null || echo '?'))"

echo "== waiting for round $ROUNDS"
wait_rounds "$ROUNDS" 900

echo "== rounds (aggregator clock; R marks the restart)"
# shellcheck disable=SC2029  # NAME is checked above
ssh "ubuntu@$AGG" "cat ~/fedfleet-runs-$NAME/rounds.jsonl" | T0="$T0" T2="$T2" python3 -c '
import json, os, sys
t0, t2 = float(os.environ["T0"]), float(os.environ["T2"])
rows = [json.loads(l) for l in sys.stdin if l.strip()]
rows = [r for r in rows if "train_round_seconds" in r]
for r in rows:
    end = r.get("timestamp", 0); start = end - r["train_round_seconds"]
    mark = "  <- R: the API restarted during this round" if start <= t0 <= end else ""
    sites = r.get("sites") or []
    print("   round %d: %s/%s replied, %.1f s, accuracy %.4f%s" % (
        r["round"], r.get("nodes_replied", len(sites)), r.get("nodes_asked", "?"),
        r["train_round_seconds"], r.get("global_acc", float("nan")), mark))
before = [r["train_round_seconds"] for r in rows if r.get("timestamp", 0) < t0]
after = [r["train_round_seconds"] for r in rows if r.get("timestamp", 0) >= t0]
if before and after:
    print("   mean round before the restart %.1f s, from it on %.1f s; all replies: %s" % (
        sum(before) / len(before), sum(after) / len(after),
        all(r.get("nodes_replied") == r.get("nodes_asked") for r in rows)))
'
