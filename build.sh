#!/usr/bin/env bash
# Build a packaged extension, injecting deployment-specific values.
#
# The committed manifest carries neutral placeholders so the repo is shareable.
# Your own add-on id and self-hosted update URL live in build.env, which is
# gitignored. Without build.env this still produces a working extension — it
# just won't self-update, and you'll want your own id before signing.
set -euo pipefail
cd "$(dirname "$0")"

[ -f build.env ] && . ./build.env

EXT_ID="${EXT_ID:-model-shelf@example.com}"
UPDATE_URL="${UPDATE_URL:-}"

VERSION=$(python3 -c "import json;print(json.load(open('extension/manifest.json'))['version'])")
OUT="dist/model-shelf-${VERSION}.xpi"
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT

cp -r extension/. "$BUILD/"
EXT_ID="$EXT_ID" UPDATE_URL="$UPDATE_URL" python3 - "$BUILD/manifest.json" <<'PY'
import json, os, sys
p = sys.argv[1]
m = json.load(open(p))
gecko = m["browser_specific_settings"]["gecko"]
gecko["id"] = os.environ["EXT_ID"]
if os.environ.get("UPDATE_URL"):
    gecko["update_url"] = os.environ["UPDATE_URL"]
json.dump(m, open(p, "w"), indent=2)
PY

mkdir -p dist && rm -f "$OUT"
( cd "$BUILD" && zip -q -r -FS "$OLDPWD/$OUT" . )
echo "built $OUT ($(stat -c%s "$OUT") bytes)  id=$EXT_ID  update_url=${UPDATE_URL:-none}"
