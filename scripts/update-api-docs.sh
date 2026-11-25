#!/usr/bin/env bash
# Vendored assets of the API docs (/api/docs, /api/redoc): served by
# the hub itself (admin only) instead of a CDN. Fetches the npm tarballs of the
# pinned versions, copies the few files the pages need and records SHA-256.
#
#   scripts/update-api-docs.sh            refresh (after changing a version)
#   scripts/update-api-docs.sh --check    verify the vendored files (CI)
set -euo pipefail
cd "$(dirname "$0")/.."

SWAGGER_UI_VERSION=5.34.1
REDOC_VERSION=2.5.4
DEST=halumar/rootfs/opt/hub/docs_assets

if [ "${1:-}" = "--check" ]; then
  (cd "$DEST" && shasum -a 256 -c SHA256SUMS --quiet)
  grep -qx "swagger-ui-dist $SWAGGER_UI_VERSION" "$DEST/VERSIONS"
  grep -qx "redoc $REDOC_VERSION" "$DEST/VERSIONS"
  echo "api docs assets: ok"
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
(cd "$tmp" && npm pack --silent "swagger-ui-dist@$SWAGGER_UI_VERSION" "redoc@$REDOC_VERSION" >/dev/null)
mkdir -p "$tmp/s" "$tmp/r" "$DEST"
tar -xzf "$tmp/swagger-ui-dist-$SWAGGER_UI_VERSION.tgz" -C "$tmp/s"
tar -xzf "$tmp/redoc-$REDOC_VERSION.tgz" -C "$tmp/r"
cp "$tmp/s/package/swagger-ui-bundle.js" "$tmp/s/package/swagger-ui.css" "$DEST/"
cp "$tmp/r/package/bundles/redoc.standalone.js" "$DEST/"
# License texts travel with the bundles (the bundles point to *.LICENSE.txt).
cp "$tmp/s/package/swagger-ui-bundle.js.LICENSE.txt" "$tmp/r/package/bundles/redoc.standalone.js.LICENSE.txt" "$DEST/"
cp "$tmp/s/package/LICENSE" "$DEST/swagger-ui-dist.LICENSE"
cp "$tmp/s/package/NOTICE" "$DEST/swagger-ui-dist.NOTICE"
cp "$tmp/r/package/LICENSE" "$DEST/redoc.LICENSE"
# Source maps are not shipped; drop the reference so browsers do not ask for them.
sed -i.bak -e '/^\/\/# sourceMappingURL=/d' "$DEST/swagger-ui-bundle.js" "$DEST/redoc.standalone.js"
sed -i.bak -e 's#/\*\# sourceMappingURL=[^*]*\*/##' "$DEST/swagger-ui.css"
rm -f "$DEST"/*.bak
printf 'swagger-ui-dist %s\nredoc %s\n' "$SWAGGER_UI_VERSION" "$REDOC_VERSION" > "$DEST/VERSIONS"
(cd "$DEST" && shasum -a 256 swagger-ui-bundle.js swagger-ui.css redoc.standalone.js \
  swagger-ui-bundle.js.LICENSE.txt redoc.standalone.js.LICENSE.txt \
  swagger-ui-dist.LICENSE swagger-ui-dist.NOTICE redoc.LICENSE > SHA256SUMS)
echo "updated $DEST"
