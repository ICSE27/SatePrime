#!/bin/sh
set -eu
. "$(dirname "$0")/env.sh"
need docker runc criu
[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 1; }

rm -rf "$BUNDLE"
mkdir -p "$WORK"

$S seed "$IMG" -o "$BUNDLE" --seed-id "$APP-s"

echo "bundle: $BUNDLE"
