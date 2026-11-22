#!/usr/bin/env bash
# Scripted teardown after a federation (WBS 13.7): export the run's model to ONNX with a model
# card on the aggregator, wait until the Blob backup has copied both files off the VM, then
# stop the test federation (scripts/cloud_down.sh). Prints how long each step took.
#   scripts/teardown.sh fedfleet-runs-<name>
# The Oracle aggregator itself stays up (Always Free); its runs remain on disk and in Blob storage.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
RUN="${1:?usage: scripts/teardown.sh fedfleet-runs-<name>}"
AGG="${AGG:-fedfleet-agg}"
# The name goes into commands run on the VM, so only safe characters are accepted.
if ! [[ "$RUN" =~ ^fedfleet-runs[A-Za-z0-9._/-]*$ ]] || [[ "$RUN" == *..* ]]; then
  echo "run folder must look like fedfleet-runs-<name> (letters, digits, . _ - /)"; exit 1
fi
t0=$(date +%s)

echo "== exporting $RUN to ONNX on $AGG"
# shellcheck disable=SC2029  # $RUN is meant to expand here; it was checked above
ssh "ubuntu@$AGG" "cd ~/fedfleet && .venv/bin/python scripts/export_model.py ~/$RUN" || { echo "!! export failed"; exit 1; }
t1=$(date +%s)

echo "== waiting for the Blob backup to copy model.onnx and model_card.md (up to 2 min)"
ok=0
for _ in $(seq 1 24); do
  # shellcheck disable=SC2029
  n=$(ssh "ubuntu@$AGG" "journalctl -u fedfleet-backup --since '-10 min' --no-pager | grep -c -E 'uploaded $RUN/(model.onnx|model_card.md)'")
  if [ "${n:-0}" -ge 2 ]; then ok=1; break; fi
  sleep 5
done
[ $ok = 1 ] && echo "   both files are in Blob storage" || echo "!! not seen in the backup log yet; check: journalctl -u fedfleet-backup"
t2=$(date +%s)

echo "== stopping the test federation"
scripts/cloud_down.sh
t3=$(date +%s)
echo "== teardown times: export $((t1 - t0)) s, copy to Blob $((t2 - t1)) s, shutdown $((t3 - t2)) s, total $((t3 - t0)) s"
