APP=${APP:-app3}
IMG=${IMG:-satprime/$APP:latest}
LIB=${LIB:-/var/lib/sate/$APP}
WORK=${WORK:-/tmp/sate-$APP}
MKFS=${MKFS:-mkfs.erofs}
CID=${CID:-$APP}
S="python3 -m satecode"

BUNDLE="$WORK/b"
SEQ="$WORK/seq"
IMAGE="$LIB/$APP.img"
META="$IMAGE.meta.json"
OUT="$LIB/out"

need() {
    m=0
    for b in "$@"; do
        command -v "$b" >/dev/null 2>&1 || { echo "missing: $b" >&2; m=1; }
    done
    [ "$m" = 0 ] || { echo "prerequisites unmet" >&2; exit 1; }
}
