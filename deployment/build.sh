#!/bin/sh
set -eu
. "$(dirname "$0")/env.sh"
need "$MKFS"
[ -d "$BUNDLE" ] || { echo "no bundle at $BUNDLE (run seed.sh first)" >&2; exit 1; }
[ -f "$SEQ" ]    || { echo "no sequence at $SEQ (run record.sh first)" >&2; exit 1; }

mkdir -p "$LIB"

$S build -i "$BUNDLE" -o "$IMAGE" -s "$SEQ" --mkfs-erofs "$MKFS"

echo "image: $IMAGE"
echo "meta:  $META"
