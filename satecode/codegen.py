import subprocess
import tempfile
from pathlib import Path
from string import Template


_C_SRC = r"""
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define _BUF_SZ (1024 * 1024)

static unsigned char _buf[_BUF_SZ];

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <image> [bytes]\n", argv[0]);
        return 1;
    }
    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    uint64_t want = 0;
    if (argc >= 3) want = (uint64_t)strtoull(argv[2], NULL, 10);

#ifdef POSIX_FADV_WILLNEED
    posix_fadvise(fd, 0, (off_t)want, POSIX_FADV_WILLNEED);
#endif

    uint64_t done = 0;
    for (;;) {
        size_t chunk = _BUF_SZ;
        if (want && (want - done) < chunk) chunk = (size_t)(want - done);
        if (want && chunk == 0) break;
        ssize_t n = read(fd, _buf, chunk);
        if (n <= 0) break;
        done += (uint64_t)n;
    }
    close(fd);
    return 0;
}
""".lstrip()


_PRIMER_UNIT = """\
[Unit]
Description=stage warm
DefaultDependencies=no
After=local-fs.target
Before=sat-app.service {extra_before}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={exec_path} {image_path} {boundary}

[Install]
WantedBy=sysinit.target
"""


_APP_UNIT = """\
[Unit]
Description=stage app
After=sat-primer.service local-fs.target
Requires=sat-primer.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={launch_path}

[Install]
WantedBy=multi-user.target
"""


_LAUNCH_TMPL = Template(r"""#!/bin/sh
set -u

RUNC="$${RUNC:-$runc}"
EROFS="$${EROFS:-$erofs}"
MNT="$${MNT:-$mnt}"
UPPER="$${UPPER:-$upper}"
OVL_WORK="$${OVL_WORK:-$ovl_work}"
BUNDLE="$${BUNDLE:-$bundle}"
SNAP="$${SNAP:-$snap}"
CID="$${CID:-$cid}"
CRIU_WORK="$${CRIU_WORK:-$criu_work}"
BOUNDARY="$${BOUNDARY:-$boundary}"
SIG="$${SIG:-$sig}"
PRIMER="$${PRIMER:-$primer}"
EROFSFUSE="$${EROFSFUSE:-erofsfuse}"
DIAG="$${DIAG:-$$BUNDLE/diag}"

_log() { echo "[launcher] $$*" >&2; }

_pre() {
    [ -f "$$EROFS" ] || { _log "image missing: $$EROFS"; return 1; }
    command -v "$$RUNC" >/dev/null 2>&1 || { _log "runc missing: $$RUNC"; return 1; }
    return 0
}

_mount_image() {
    mkdir -p "$$MNT"
    if mountpoint -q "$$MNT" 2>/dev/null; then
        :
    elif mount -t erofs -o loop "$$EROFS" "$$MNT" 2>/dev/null; then
        :
    elif command -v "$$EROFSFUSE" >/dev/null 2>&1 && "$$EROFSFUSE" "$$EROFS" "$$MNT"; then
        :
    else
        _log "cannot mount image (no kernel erofs and erofsfuse unavailable)"
        return 1
    fi
    [ -d "$$MNT/rootfs" ] || { _log "no rootfs in image"; return 1; }
    return 0
}

_prefetch() {
    if [ -x "$$PRIMER" ]; then
        "$$PRIMER" "$$EROFS" "$$BOUNDARY" || true
    elif [ "$$BOUNDARY" -gt 0 ]; then
        _kb=$$(( ($$BOUNDARY + 1023) / 1024 ))
        dd if="$$EROFS" of=/dev/null bs=1024 count="$$_kb" 2>/dev/null || true
    else
        dd if="$$EROFS" of=/dev/null bs=1M 2>/dev/null || true
    fi
}

_mount_overlay() {
    mkdir -p "$$UPPER" "$$OVL_WORK" "$$BUNDLE/rootfs"
    if ! mountpoint -q "$$BUNDLE/rootfs" 2>/dev/null; then
        mount -t overlay overlay \
            -o "lowerdir=$$MNT/rootfs,upperdir=$$UPPER,workdir=$$OVL_WORK" \
            "$$BUNDLE/rootfs" || return 1
    fi
    return 0
}

_stage() {
    cp -f "$$MNT/config.json" "$$BUNDLE/config.json" || return 1
    ln -sfn "$$MNT/$$SNAP" "$$BUNDLE/$$SNAP"
    return 0
}

_restore() {
    mkdir -p "$$CRIU_WORK"
    "$$RUNC" delete --force "$$CID" >/dev/null 2>&1 || true
    "$$RUNC" restore --detach \
        --image-path "$$BUNDLE/$$SNAP" \
        --work-path "$$CRIU_WORK" \
        --bundle "$$BUNDLE" \
        "$$CID"
}

_resume() {
    "$$RUNC" kill "$$CID" "$$SIG"
}

_observe() {
    _st=$$("$$RUNC" state "$$CID" 2>/dev/null | tr -d ' \n')
    case "$$_st" in
        *\"status\":\"running\"*|*\"status\":\"created\"*|*\"status\":\"stopped\"*)
            _log "post-check ok: $$CID" ;;
        *) _log "post-check: state unavailable for $$CID" ;;
    esac
}

_diag() {
    mkdir -p "$$DIAG"
    _f="$$DIAG/restore-fail.$$$$.txt"
    {
        echo "cid=$$CID stage=$${1:-?}"
        "$$RUNC" state "$$CID" 2>&1
        echo "--- criu restore.log (tail) ---"
        tail -n 120 "$$CRIU_WORK/restore.log" 2>/dev/null
        echo "--- dmesg (tail) ---"
        dmesg 2>/dev/null | tail -n 40
    } > "$$_f" 2>&1
    _log "diagnostics -> $$_f"
}

_cold() {
    _log "reverting to cold start: $$CID"
    "$$RUNC" delete --force "$$CID" >/dev/null 2>&1 || true
    if [ -f "$$BUNDLE/config.json" ]; then
        sed -i 's/"CHECKPOINT_ENABLED=1"/"CHECKPOINT_ENABLED=0"/' "$$BUNDLE/config.json" 2>/dev/null || true
    fi
    "$$RUNC" run --bundle "$$BUNDLE" "$$CID"
}

_pre            || { _cold; exit $$?; }
_mount_image    || { _diag mount;   _cold; exit $$?; }
_prefetch
_mount_overlay  || { _diag overlay; _cold; exit $$?; }
_stage          || { _diag stage;   _cold; exit $$?; }

_restore; _restore_rc=$$?
if [ "$$_restore_rc" -ne 0 ]; then
    _log "restore failed (rc=$$_restore_rc)"
    _diag "restore rc=$$_restore_rc"
    _cold
    exit $$?
fi

_resume; _resume_rc=$$?
if [ "$$_resume_rc" -ne 0 ]; then
    _log "resume failed (rc=$$_resume_rc)"
    _diag "resume rc=$$_resume_rc"
    _cold
    exit $$?
fi

_observe
_log "restored and resumed: $$CID"
exit 0
""")


_CC_MAP = {
    "aarch64": "aarch64-linux-gnu-gcc",
    "arm":     "arm-linux-gnueabihf-gcc",
    "x86_64":  "gcc",
    "native":  "gcc",
}


def source() -> str:
    return _C_SRC


def primer_unit(image_path: str, boundary: int,
                exec_path: str = "/usr/local/bin/sat_primer",
                extra_before: str = "") -> str:
    return _PRIMER_UNIT.format(
        image_path=image_path, boundary=boundary,
        exec_path=exec_path, extra_before=extra_before,
    )


def app_unit(launch_path: str) -> str:
    return _APP_UNIT.format(launch_path=launch_path)


def unit(image_path: str, boundary: int,
         exec_path: str = "/usr/local/bin/sat_primer",
         extra_before: str = "") -> str:
    return primer_unit(image_path, boundary, exec_path, extra_before)


def launcher(runc: str = "/usr/bin/runc",
             erofs: str = "/var/lib/satecode/app.erofs",
             mnt: str = "/mnt/satecode/app",
             upper: str = "/run/satecode/app/upper",
             ovl_work: str = "/run/satecode/app/work",
             bundle: str = "/run/satecode/app/bundle",
             snap: str = "snapshot",
             cid: str = "app",
             criu_work: str = "/tmp/criu-work",
             boundary: int = 0,
             sig: str = "SIGUSR1",
             primer: str = "/usr/local/bin/sat_primer") -> str:
    return _LAUNCH_TMPL.substitute(
        runc=runc, erofs=erofs, mnt=mnt, upper=upper, ovl_work=ovl_work,
        bundle=bundle, snap=snap, cid=cid, criu_work=criu_work,
        boundary=boundary, sig=sig, primer=primer,
    )


def compile_primer(src: str, out: Path, arch: str = "native",
                   cflags: str = "-O2 -static") -> None:
    cc = _CC_MAP.get(arch)
    if not cc:
        raise ValueError("unknown arch: {}".format(arch))
    with tempfile.TemporaryDirectory() as td:
        s = Path(td) / "primer.c"
        s.write_text(src)
        r = subprocess.run([cc] + cflags.split() + ["-o", str(out), str(s)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("compile failed: {}".format(r.stderr.strip()))
    out.chmod(0o755)


def emit(output_dir: Path, image_path: str, boundary: int,
         arch: str = "native", compile_binary: bool = True,
         exec_path: str = "/usr/local/bin/sat_primer",
         runc: str = "/usr/bin/runc",
         mnt: str = "/mnt/satecode/app",
         upper: str = "/run/satecode/app/upper",
         ovl_work: str = "/run/satecode/app/work",
         bundle: str = "/run/satecode/app/bundle",
         snap: str = "snapshot",
         cid: str = "app",
         criu_work: str = "/tmp/criu-work",
         resume_signal: str = "SIGUSR1") -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    out = {}

    src_path = output_dir / "primer.c"
    src_path.write_text(source())
    out["source"] = src_path

    primer_unit_path = output_dir / "sat-primer.service"
    primer_unit_path.write_text(primer_unit(image_path, boundary, exec_path))
    out["primer_unit"] = primer_unit_path

    launch_path = output_dir / "launch.sh"
    launch_path.write_text(launcher(
        runc=runc, erofs=image_path, mnt=mnt, upper=upper, ovl_work=ovl_work,
        bundle=bundle, snap=snap, cid=cid, criu_work=criu_work,
        boundary=boundary, sig=resume_signal, primer=exec_path,
    ))
    launch_path.chmod(0o755)
    out["launcher"] = launch_path

    app_unit_path = output_dir / "sat-app.service"
    app_unit_path.write_text(app_unit(str(launch_path)))
    out["app_unit"] = app_unit_path

    if compile_binary:
        bin_path = output_dir / "sat_primer"
        compile_primer(source(), bin_path, arch)
        out["binary"] = bin_path

    return out
