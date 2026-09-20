#!/usr/bin/env bash
# Publish a signed XPI to the shelfd /ext/ route and regenerate updates.json,
# so installed copies pick the new version up on their own.
set -euo pipefail
cd "$(dirname "$0")"
[ -f build.env ] && . ./build.env
: "${EXT_ID:?set EXT_ID in build.env}"
: "${UPDATE_URL:?set UPDATE_URL in build.env}"
: "${EXT_HOST:?set EXT_HOST in build.env (user@host running shelfd)}"
EXT_DIR="${EXT_DIR:-/var/lib/shelfd/ext}"

XPI="${1:?usage: publish-update.sh dist/model-shelf-X.Y.Z-signed.xpi}"
VERSION=$(python3 -c "
import json,sys,zipfile
print(json.loads(zipfile.ZipFile(sys.argv[1]).read('manifest.json'))['version'])" "$XPI")
NAME="model-shelf-${VERSION}.xpi"
BASE="${UPDATE_URL%/updates.json}"

python3 - "$EXT_ID" "$VERSION" "$BASE/$NAME" > /tmp/updates.json <<'PY'
import json, sys
ext_id, version, link = sys.argv[1:4]
json.dump({"addons": {ext_id: {"updates": [
    {"version": version, "update_link": link}]}}}, sys.stdout, indent=2)
PY

scp -q "$XPI" "$EXT_HOST:$EXT_DIR/$NAME"
scp -q /tmp/updates.json "$EXT_HOST:$EXT_DIR/updates.json"
ssh "$EXT_HOST" "chown shelfd:shelfd $EXT_DIR/$NAME $EXT_DIR/updates.json && chmod 644 $EXT_DIR/$NAME $EXT_DIR/updates.json"
rm -f /tmp/updates.json
echo "published $NAME -> $BASE/$NAME"
