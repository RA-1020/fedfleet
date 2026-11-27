#!/usr/bin/env bash
# Secret scan (task 0.4), run by CI on every pull request and push to main.
#   scripts/ci_secrets.sh
# Rescans the working tree against .secrets.baseline (detect-secrets) and fails on any finding
# that has not been reviewed. Reviewed false positives keep their status when lines move,
# because findings are matched by a hash of the secret, not by line number.
# A new false positive: run the scan below, check the finding really is harmless, set
# "is_secret": false on it in .secrets.baseline, and commit the baseline.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
cp .secrets.baseline "$tmp"
detect-secrets scan --exclude-files '(^|/)(\.git|node_modules|\.venv)/' \
  --exclude-files '\.secrets\.baseline$' --baseline "$tmp" > /dev/null
python3 - "$tmp" <<'PY'
import json, sys
results = json.load(open(sys.argv[1]))["results"]
bad = [(f, s["line_number"], s["type"]) for f, items in results.items() for s in items
       if s.get("is_secret") is not False]
for f, line, kind in bad:
    print(f"!! possible secret: {f}:{line} ({kind})")
if bad:
    print("Remove it (and rotate it if it was ever real), or mark a proven false positive in .secrets.baseline.")
    sys.exit(1)
print("no unreviewed secrets")
PY
