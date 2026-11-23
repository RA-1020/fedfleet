#!/usr/bin/env bash
# Restore drill (disaster recovery): rebuild a run folder on the aggregator from the Azure Blob
# backup, under a NEW name, so it can be resumed with POST /runs {"name": <new>, "resume": true}.
#   scripts/restore_run.sh fedfleet-runs-<source> <new-name>
# Runs on the Mac. The VM's own SAS token can only create and write blobs, so a compromised VM
# cannot read the backups; the download happens here instead, with the Azure CLI's login
# (--auth-mode key: the CLI fetches the storage account key itself and never prints it).
# Nothing existing is touched: the source folder stays as it is and a new folder is refused if it
# already exists. When the source folder still exists on the VM, the restored files are compared
# with it byte for byte (sha256), which tests the backup itself. Prints how long each step took.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
SRC="${1:?usage: scripts/restore_run.sh fedfleet-runs-<source> <new-name>}"
NEW="${2:?usage: scripts/restore_run.sh fedfleet-runs-<source> <new-name>}"
AGG="${AGG:-fedfleet-agg}"
ACCOUNT="${ACCOUNT:-fedfleetruns429}"
CONTAINER="${CONTAINER:-fedfleet-runs}"
# Both names go into commands run on the VM, so only safe characters are accepted. The new name
# follows the control API's rule, because the restored run is resumed through it.
if ! [[ "$SRC" =~ ^fedfleet-runs[a-z0-9-]*$ ]]; then
  echo "source must look like fedfleet-runs-<name> (lower-case letters, digits, -)"; exit 1
fi
if ! [[ "$NEW" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; then
  echo "new name: 1-40 lower-case letters, digits or hyphens (the folder becomes fedfleet-runs-<new>)"; exit 1
fi
DEST="fedfleet-runs-$NEW"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
t0=$(date +%s)

# shellcheck disable=SC2029  # $DEST is meant to expand here; it was checked above
if ssh "ubuntu@$AGG" "test -e ~/$DEST"; then
  echo "!! ~/$DEST already exists on $AGG; pick another name"; exit 1
fi

echo "== downloading $SRC/* from Blob ($ACCOUNT/$CONTAINER)"
az storage blob download-batch --account-name "$ACCOUNT" --source "$CONTAINER" \
  --destination "$WORK" --pattern "$SRC/*" --auth-mode key --only-show-errors --output none \
  || { echo "!! download failed (signed in? az login)"; exit 1; }
t1=$(date +%s)
if [ ! -f "$WORK/$SRC/latest_checkpoint.pt" ] || [ ! -f "$WORK/$SRC/rounds.jsonl" ]; then
  echo "!! the backup of $SRC has no latest_checkpoint.pt or rounds.jsonl; nothing to resume from"
  ls -l "$WORK/$SRC" 2>/dev/null; exit 1
fi
(cd "$WORK/$SRC" && ls -l && shasum -a 256 -- *) | sed 's/^/   /'
last=$(python3 -c 'import json,sys
r=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
t=[x for x in r if isinstance(x.get("round"),int) and x["round"]>0]
print("%d rounds, last global_acc %s" % (t[-1]["round"], t[-1].get("global_acc")) if t else "no rounds")' "$WORK/$SRC/rounds.jsonl")
echo "   backup holds: $last"

echo "== comparing with the original on $AGG (if it is still there)"
# shellcheck disable=SC2029
orig=$(ssh "ubuntu@$AGG" "cd ~/$SRC 2>/dev/null && sha256sum latest_checkpoint.pt rounds.jsonl")
if [ -z "$orig" ]; then
  echo "   original not on the VM; skipped"
else
  local_sums=$(cd "$WORK/$SRC" && shasum -a 256 latest_checkpoint.pt rounds.jsonl)
  if [ "$(echo "$orig" | awk '{print $1}')" = "$(echo "$local_sums" | awk '{print $1}')" ]; then
    echo "   identical: latest_checkpoint.pt and rounds.jsonl match the VM byte for byte"
  else
    echo "!! differs from the VM copy (the backup may be one cycle behind a run in progress):"
    echo "$orig" | sed 's/^/   VM:   /'; echo "$local_sums" | sed 's/^/   Blob: /'
  fi
fi

echo "== copying to $AGG:~/$DEST"
rsync -a "$WORK/$SRC/" "ubuntu@$AGG:$DEST/" || { echo "!! copy failed"; exit 1; }
t2=$(date +%s)
# shellcheck disable=SC2029
ssh "ubuntu@$AGG" "ls -l ~/$DEST" | sed 's/^/   /'

echo "== done: download $((t1 - t0)) s, copy $((t2 - t1)) s, total $((t2 - t0)) s"
echo "   resume it (sites up first, rounds = the total to reach):"
echo "   POST /runs {\"name\":\"$NEW\",\"rounds\":<total>,\"min_nodes\":3,\"resume\":true}"
