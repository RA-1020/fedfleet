#!/usr/bin/env bash
# Third copy of FedFleet's backups (3-2-1: the VM, Azure Blob, and this Mac). Downloads the whole
# Blob backup into a dated snapshot under ~/FedFleetBackup/snapshots/. Unchanged files are
# hard links to the previous snapshot, so each snapshot costs only what changed. Keeps the
# newest $KEEP snapshots.
#   scripts/pull_backup.sh                nightly via deploy/backup/com.fedfleet.pullbackup.plist
# The Mac pulls from Blob with the Azure CLI's login; neither the VM nor its SAS token can reach
# this copy (the tailnet also forbids the aggregator from connecting to the Mac). Each snapshot
# is checked against the Blob listing: every blob present, with the same size.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # launchd starts with a minimal PATH
ACCOUNT="${ACCOUNT:-fedfleetruns429}"
CONTAINER="${CONTAINER:-fedfleet-runs}"
ROOT="${FEDFLEET_BACKUP_DIR:-$HOME/FedFleetBackup}"
KEEP="${KEEP:-14}"
STAGE="$ROOT/.staging"
mkdir -p "$ROOT/snapshots"
t0=$(date +%s)
echo "== $(date '+%Y-%m-%d %H:%M:%S') pulling $ACCOUNT/$CONTAINER"
rm -rf "$STAGE" && mkdir -p "$STAGE"
az storage blob download-batch --account-name "$ACCOUNT" --source "$CONTAINER" --destination "$STAGE" \
  --auth-mode key --only-show-errors --output none \
  || { echo "!! download failed (Azure CLI signed in? az login)"; rm -rf "$STAGE"; exit 1; }
LISTING="$ROOT/.listing.json"
az storage blob list --account-name "$ACCOUNT" --container-name "$CONTAINER" --auth-mode key \
  --num-results "*" --only-show-errors --query "[].{name:name, size:properties.contentLength}" -o json > "$LISTING" \
  || { echo "!! could not list the container"; rm -rf "$STAGE"; exit 1; }
SNAP="$ROOT/snapshots/$(date +%Y-%m-%dT%H%M)"
# The newest snapshot that passed its check (a failed one is renamed *-FAILED and never reused).
PREV=$(find "$ROOT/snapshots" -mindepth 1 -maxdepth 1 -type d ! -name '*-FAILED' | sort | tail -1)
if [ -n "$PREV" ]; then
  rsync -a --checksum --link-dest="$PREV" "$STAGE/" "$SNAP/"
else
  rsync -a "$STAGE/" "$SNAP/"
fi
rm -rf "$STAGE"
python3 - "$LISTING" "$SNAP" <<'PY'
import json, os, sys
blobs = json.load(open(sys.argv[1])); snap = sys.argv[2]
bad = [b["name"] for b in blobs
       if not os.path.isfile(os.path.join(snap, b["name"])) or os.path.getsize(os.path.join(snap, b["name"])) != int(b["size"])]
total = sum(int(b["size"]) for b in blobs)
runs = len({b["name"].split("/")[0] for b in blobs})
if bad:
    print(f"!! {len(bad)} of {len(blobs)} blobs missing or a different size in the snapshot, e.g. {bad[:3]}")
    sys.exit(1)
print(f"   verified: {len(blobs)} files from {runs} folders, {total/1e6:.1f} MB, all present with the same size")
PY
ok=$?
rm -f "$LISTING"
if [ "$ok" -ne 0 ]; then
  mv "$SNAP" "$SNAP-FAILED" 2>/dev/null
  echo "!! snapshot kept as $SNAP-FAILED for inspection; the previous good snapshot is unchanged"
  exit 1
fi
# Keep the newest $KEEP good snapshots; failed ones are removed after a week.
n=$(find "$ROOT/snapshots" -mindepth 1 -maxdepth 1 -type d ! -name '*-FAILED' | wc -l | tr -d ' ')
if [ "$n" -gt "$KEEP" ]; then
  find "$ROOT/snapshots" -mindepth 1 -maxdepth 1 -type d ! -name '*-FAILED' | sort | head -n $((n - KEEP)) \
    | while read -r old; do rm -rf "$old"; done
fi
find "$ROOT/snapshots" -mindepth 1 -maxdepth 1 -type d -name '*-FAILED' -mtime +7 -exec rm -rf {} +
echo "== snapshot $SNAP ($(du -sh "$SNAP" | cut -f1) apparent; $(find "$ROOT/snapshots" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ') kept) in $(( $(date +%s) - t0 )) s"
