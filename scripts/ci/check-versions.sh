#!/usr/bin/env bash
# Version consistency: config.yaml (what the Supervisor reads), version.py,
# frontend/package.json + package-lock.json and the top CHANGELOG section must name
# the same version.
set -euo pipefail
cd "$(dirname "$0")/../.."

config="$(sed -nE 's/^version:[[:space:]]*"?([^"[:space:]]+)"?.*/\1/p' halumar/config.yaml | head -1)"
pyver="$(sed -nE 's/^VERSION[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' halumar/rootfs/opt/hub/version.py | head -1)"
pkg="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' frontend/package.json)"
lock="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["version"], d["packages"][""]["version"])' frontend/package-lock.json)"
changelog="$(sed -nE 's/^##[[:space:]]+\[?v?([0-9][^] [:space:]]*)\]?[[:space:]]*$/\1/p' halumar/CHANGELOG.md | head -1)"

printf '%-28s %s\n' "halumar/config.yaml" "$config" \
  "version.py" "$pyver" \
  "frontend/package.json" "$pkg" \
  "frontend/package-lock.json" "$lock" \
  "CHANGELOG.md (top section)" "$changelog"

status=0
for v in "$pyver" "$pkg" $lock "$changelog"; do
  if [ "$v" != "$config" ]; then
    status=1
  fi
done
if [ "$status" -ne 0 ]; then
  echo "::error file=halumar/config.yaml::Version mismatch, expected ${config} everywhere (see table above)."
fi
exit "$status"
