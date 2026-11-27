#!/usr/bin/env bash
# Integration test (task 15.2): a real federation on this machine, plain and with SecAgg+,
# checked by scripts/ci_check_runs.py. Used by CI on every pull request; runs anywhere.
#
#   scripts/ci_federation.sh [rounds] [sites]        (default 2 rounds, 3 sites)
#
# One insecure SuperLink and <sites> SuperNodes as processes on 127.0.0.1, synthetic data
# (data-path empty), everything in a temporary folder that is removed afterwards. Nothing here
# touches ~/.flwr, ~/fedfleet-runs or the live system. Exit code 0 only if both runs complete
# and the checks pass.
set -euo pipefail
cd "$(dirname "$0")/.."
ROUNDS="${1:-2}"; SITES="${2:-3}"
[[ "$ROUNDS" =~ ^[0-9]+$ && "$SITES" =~ ^[0-9]+$ ]] || { echo "usage: $0 [rounds] [sites]"; exit 2; }
RUN_TIMEOUT="${RUN_TIMEOUT:-900}"   # seconds per run

WORK="$(mktemp -d)"
export FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
export FLWR_HOME="$WORK/flwr"
mkdir -p "$FLWR_HOME" "$WORK/logs" "$WORK/runs"
printf '[superlink.ci]\naddress = "127.0.0.1:8000"\ninsecure = true\n' > "$FLWR_HOME/config.toml"

pids=()
finish() {
  status=$?
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  if [ "$status" -ne 0 ]; then
    echo "== logs (last lines)"
    for f in "$WORK"/logs/*.log; do echo "--- $f"; tail -25 "$f"; done
  fi
  if [ -n "${KEEP_WORK:-}" ]; then echo "kept $WORK"; else rm -rf "$WORK"; fi
  exit "$status"
}
trap finish EXIT

wait_port() {   # $1 = port, $2 = what
  for _ in $(seq 1 60); do
    python3 -c "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 1)" "$1" 2>/dev/null && return 0
    sleep 1
  done
  echo "!! $2 did not open port $1"; return 1
}

echo "== aggregator"
(cd "$WORK" && exec flower-superlink --insecure) > "$WORK/logs/superlink.log" 2>&1 &
pids+=($!)
wait_port 9092 "SuperLink (fleet)"
wait_port 8000 "SuperLink (control)"

echo "== $SITES sites"
for ((i = 0; i < SITES; i++)); do
  FLWR_HOME="$WORK/site-$i" flower-supernode --insecure --superlink 127.0.0.1:9092 \
    --node-config "partition-id=$i num-partitions=$SITES" --port $((9094 + i)) \
    > "$WORK/logs/site-$i.log" 2>&1 < /dev/null &
  pids+=($!)
done

online() {
  flwr supernode ls ci --format json 2>/dev/null \
    | python3 -c 'import json,sys; print(sum(1 for n in json.load(sys.stdin).get("nodes", []) if str(n.get("status","")).lower()=="online"))' \
    2>/dev/null || echo 0
}
for _ in $(seq 1 60); do [ "$(online)" -ge "$SITES" ] && break; sleep 2; done
[ "$(online)" -ge "$SITES" ] || { echo "!! only $(online) of $SITES sites registered"; exit 1; }
echo "   $SITES sites online"

run() {   # $1 = name, $2 = extra run config
  local name="$1" cfg out run_id t0 status
  cfg="num-server-rounds=$ROUNDS min-nodes=$SITES round-timeout=300.0 metrics-dir='$WORK/runs/$name' $2"
  echo "== run $name ($cfg)"
  out=$(flwr run . ci --run-config "$cfg" 2>&1) || { echo "$out"; return 1; }
  run_id=$(sed -n 's/.*Successfully started run \([0-9][0-9]*\).*/\1/p' <<<"$out" | head -1)
  [ -n "$run_id" ] || { echo "$out"; echo "!! no run id"; return 1; }
  t0=$(date +%s)
  while :; do
    status=$(flwr ls ci --run-id "$run_id" --format json 2>/dev/null \
      | python3 -c 'import json,sys; r=json.load(sys.stdin)["runs"][0]; print(r["status"], "|", r.get("status-details",""))' \
      2>/dev/null || echo "unknown |")
    case "$status" in
      finished:completed*) echo "   run $run_id: $status ($(( $(date +%s) - t0 )) s)"; return 0 ;;
      finished:*) echo "!! run $run_id: $status"; return 1 ;;
    esac
    [ $(( $(date +%s) - t0 )) -lt "$RUN_TIMEOUT" ] || { echo "!! run $run_id still $status after $RUN_TIMEOUT s"; return 1; }
    sleep 3
  done
}

run plain ""
run secagg "secagg=true"

echo "== checks"
python3 scripts/ci_check_runs.py --rounds "$ROUNDS" --sites "$SITES" "$WORK/runs/plain" "$WORK/runs/secagg"
