#!/bin/sh
# Build the deployment zip: the function files plus the cost code and rate card the report uses.
#   deploy/azure_function/build_zip.sh [OUT.zip]      (default /tmp/fedfleet-report.zip)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
out=${1:-/tmp/fedfleet-report.zip}
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cp "$here/function_app.py" "$here/fedfleet_report.py" "$here/host.json" "$here/requirements.txt" "$here/.funcignore" "$stage/"
cp "$root/fedfleet/cost.py" "$stage/fedfleet_cost.py"
cp "$root/deploy/rates.toml" "$stage/rates.toml"
rm -f "$out"
(cd "$stage" && zip -qr "$out" .)
unzip -l "$out"
