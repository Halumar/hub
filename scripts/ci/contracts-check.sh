#!/usr/bin/env bash
# Contract gate: the hub fulfils the pinned contract.
#
#   1. OpenAPI: the FastAPI app produces exactly the snapshot in the contract
#      (needs the hub requirements installed in the active Python).
#   2. Codegen: generated models checked into this repo match the pinned contract
#      (Python hub_contracts/ in the app dir, TypeScript in frontend/src/lib/contracts).
#      A target that does not exist yet is reported, not failed: it becomes
#      mandatory with the commit that adds the generated files.
#
# CONTRACTS_DIR points to a contract checkout (CI: .contracts).
set -euo pipefail
cd "$(dirname "$0")/../.."
C="${CONTRACTS_DIR:-.contracts}"
PY="${PYTHON:-python3}"
APP=halumar/rootfs/opt/hub

echo "contract $(tr -d '[:space:]' < "$C/VERSION") from $C"

"$PY" "$C/tools/snapshot_openapi.py" --app-dir "$APP" --check

check_codegen() { # lang out marker-path [package]
  local lang="$1" out="$2" marker="$3" pkg="${4:-}"
  if [ ! -e "$marker" ]; then
    echo "::notice::codegen $lang: $marker not present yet, check skipped"
    return 0
  fi
  "$PY" "$C/tools/codegen/generate.py" --lang "$lang" --out "$out" ${pkg:+--package "$pkg"} --check
}
# Contract data: the hub serves byte-identical copies of the contract data files.
for f in tile-capabilities tile-icons quick-access control-allowlist room-climate; do
  if ! cmp -s "$C/data/$f.json" "$APP/data/${f//-/_}.json"; then
    echo "::error file=$APP/data/${f//-/_}.json::differs from contract data/$f.json (copy it from the pinned contract)"
    exit 1
  fi
done
for f in profile-rules profile-suggestions family events access-requests; do
  if ! cmp -s "$C/data/$f.json" "$APP/data/${f//-/_}.json"; then
    echo "::error file=$APP/data/${f//-/_}.json::differs from contract data/$f.json (copy it from the pinned contract)"
    exit 1
  fi
done
# Value schemas of the profile rules (services/profiles/schema.py validates with them).
for f in profile-rule-values kids-rules defs; do
  if ! cmp -s "$C/schemas/common/$f.json" "$APP/data/schemas/$f.json"; then
    echo "::error file=$APP/data/schemas/$f.json::differs from contract schemas/common/$f.json"
    exit 1
  fi
done
# Shared resolver and expiry vectors (tests/test_profile_resolver.py).
if ! diff -r "$C/fixtures/profiles" halumar/tests/fixtures/profiles >/dev/null; then
  echo "::error::halumar/tests/fixtures/profiles differs from contract fixtures/profiles"
  exit 1
fi
# Shared room climate vectors (tests/test_room_climate.py).
if ! diff -r "$C/fixtures/climate" halumar/tests/fixtures/climate >/dev/null; then
  echo "::error::halumar/tests/fixtures/climate differs from contract fixtures/climate"
  exit 1
fi
echo "contract data: tile-capabilities, tile-icons, quick-access, control allowlist, room climate, profile rules, suggestions, family, value schemas, resolver, family-check and room climate vectors up to date"

check_codegen python "$APP" "$APP/hub_contracts" hub_contracts
check_codegen ts frontend/src/lib/contracts frontend/src/lib/contracts
echo "contracts-check: ok"
