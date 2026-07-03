#!/bin/sh
set -eu
. "$(dirname "$0")/env.sh"
need runc inotifywait
[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 1; }
[ -d "$BUNDLE" ] || { echo "no bundle at $BUNDLE (run seed.sh first)" >&2; exit 1; }

REPLAY_CID="$APP-p"
runc delete --force "$REPLAY_CID" 2>/dev/null || true

$S record --base "$BUNDLE" -o "$SEQ" \
    --restore-cmd runc restore --detach \
        --image-path "$BUNDLE/snapshot" --work-path "$WORK/w" \
        --bundle "$BUNDLE" "$REPLAY_CID"

runc delete --force "$REPLAY_CID" 2>/dev/null || true

echo "sequence: $SEQ"
