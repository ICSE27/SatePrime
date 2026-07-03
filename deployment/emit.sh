#!/bin/sh
set -eu
. "$(dirname "$0")/env.sh"
[ -f "$META" ] || { echo "no meta at $META (run build.sh first)" >&2; exit 1; }

$S emit -o "$OUT" --meta "$META" --image-path "$IMAGE" --cid "$CID"

echo "artifacts: $OUT"
