#!/usr/bin/env bash
# SecAgg+ spike on the live federation (WBS 7.1, 14.4), run from the Mac after cloud_up.sh.
#   scripts/secagg_spike.sh [rounds]        default 3 rounds per mode
# Copies experiments/secagg_spike to the aggregator, runs the toy app there once with plain
# FedAvg and once under Flower's SecAgg+ workflow, both with all three live sites, and prints
# per-round time, the aggregate against the exact expected mean, and Flower's own network-traffic
# count for each run. It does not touch FedFleet's app, run folders or services; the results go
# to ~/secagg-spike-plain.json and ~/secagg-spike-secagg.json on the aggregator.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
AGG="${AGG:-fedfleet-agg}"
ROUNDS="${1:-3}"
[[ "$ROUNDS" =~ ^[0-9]+$ ]] && [ "$ROUNDS" -ge 1 ] && [ "$ROUNDS" -le 10 ] || { echo "rounds: 1-10"; exit 1; }

echo "== checking that no other run is in progress"
active=$(ssh "ubuntu@$AGG" "cd ~ && ~/fedfleet/.venv/bin/flwr ls fedfleet-vm --format json 2>/dev/null" | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("runs", [])
except Exception:
    print("?"); sys.exit()
print(" ".join(r["run-id"] for r in runs if str(r.get("status", "")).split(":")[0] in ("pending", "starting", "running")))')
if [ "$active" = "?" ]; then echo "!! could not read the run list; not starting"; exit 1; fi
if [ -n "$active" ] && [ "${FORCE:-0}" != 1 ]; then echo "!! runs still marked active: $active (FORCE=1 to go ahead)"; exit 1; fi
echo "   none"

echo "== copying the spike app to the aggregator"
# rsync creates only the last folder of the target path, so make the parents first.
ssh "ubuntu@$AGG" 'mkdir -p ~/fedfleet/experiments/secagg_spike' || exit 1
rsync -a --exclude __pycache__ experiments/secagg_spike/ "ubuntu@$AGG:fedfleet/experiments/secagg_spike/" || exit 1

IDS=()
for mode in plain secagg; do
  flag=false; [ "$mode" = secagg ] && flag=true
  echo "== $mode run ($ROUNDS rounds)"
  t0=$(date +%s)
  # shellcheck disable=SC2029  # values are fixed or checked above
  out=$(ssh "ubuntu@$AGG" "cd ~ && rm -f ~/secagg-spike-$mode.json && FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1 timeout 900 \
        ~/fedfleet/.venv/bin/flwr run ~/fedfleet/experiments/secagg_spike fedfleet-vm \
        --run-config 'secagg=$flag num-rounds=$ROUNDS out=\"~/secagg-spike-$mode.json\"' --stream 2>&1")
  rc=$?
  id=$(grep -o "Successfully started run [0-9]*" <<<"$out" | awk '{print $4}')
  echo "   run ${id:-?}, exit $rc, $(( $(date +%s) - t0 )) s on the Mac's clock"
  [ "$rc" = 0 ] && [ -n "$id" ] || { echo "!! the $mode run failed; last lines:"; tail -n 15 <<<"$out"; exit 1; }
  IDS+=("$id")
done

echo "== results"
# shellcheck disable=SC2029
ssh "ubuntu@$AGG" "cd ~ && echo '{\"plain\":' && cat ~/secagg-spike-plain.json && echo ',\"secagg\":' && cat ~/secagg-spike-secagg.json \
    && echo ',\"runs\":' && ~/fedfleet/.venv/bin/flwr ls fedfleet-vm --format json 2>/dev/null && echo '}'" | \
  PLAIN="${IDS[0]}" SECAGG="${IDS[1]}" python3 -c '
import json, os, sys
d = json.load(sys.stdin)
runs = {r["run-id"]: r for r in d["runs"]["runs"]}
expected = lambda rnd: rnd * (100*0.01 + 200*0.02 + 300*0.03) / 600
print("   %-7s %5s %9s %8s %12s %12s" % ("mode", "round", "seconds", "replies", "mean", "max error"))
for mode in ("plain", "secagg"):
    for r in d[mode]:
        err = max(abs(r["min"] - expected(r["round"])), abs(r["max"] - expected(r["round"])))
        print("   %-7s %5d %9.1f %8d %12.6f %12.2e" % (mode, r["round"], r["seconds"], r["replies"], r["mean"], err))
print()
for mode, key in (("plain", "PLAIN"), ("secagg", "SECAGG")):
    r = runs.get(os.environ[key], {})
    nt = r.get("network-traffic", {})
    print("   %-7s run %s: elapsed %.1f s, bytes in %s, out %s, total %s" % (mode, os.environ[key], float(r.get("elapsed", 0)),
          format(nt.get("inbound-bytes", 0), ","), format(nt.get("outbound-bytes", 0), ","), format(nt.get("total-bytes", 0), ",")))
p, s = runs.get(os.environ["PLAIN"], {}), runs.get(os.environ["SECAGG"], {})
if p and s:
    print("   SecAgg+ / plain: time x%.2f, bytes in x%.2f, total bytes x%.2f" % (
        float(s["elapsed"]) / float(p["elapsed"]),
        s["network-traffic"]["inbound-bytes"] / p["network-traffic"]["inbound-bytes"],
        s["network-traffic"]["total-bytes"] / p["network-traffic"]["total-bytes"]))
'
