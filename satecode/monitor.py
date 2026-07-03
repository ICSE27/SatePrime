import ctypes
import ctypes.util
import errno
import hashlib
import os
import re
import select
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_INOTIFY_HDR = struct.Struct("iIII")
_IN_MASK = 0x00000108


def _inotify_wait(directory: str, filename: str, timeout: float) -> bool:
    lib = ctypes.util.find_library("c")
    if not lib:
        return _poll_wait(directory, filename, timeout)
    libc = ctypes.CDLL(lib, use_errno=True)
    ifd = libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if ifd < 0:
        return _poll_wait(directory, filename, timeout)
    wd = libc.inotify_add_watch(ifd, directory.encode(), ctypes.c_uint32(_IN_MASK))
    if wd < 0:
        os.close(ifd)
        return _poll_wait(directory, filename, timeout)
    try:
        target   = filename.encode()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            r, _, _ = select.select([ifd], [], [], min(left, 1.0))
            if not r:
                continue
            try:
                buf = os.read(ifd, 4096)
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    continue
                raise
            off = 0
            while off + _INOTIFY_HDR.size <= len(buf):
                _, _, _, nlen = _INOTIFY_HDR.unpack_from(buf, off)
                off += _INOTIFY_HDR.size
                name = b""
                if nlen:
                    name = buf[off:off + nlen].rstrip(b"\x00")
                    off += nlen
                if name == target:
                    return True
    finally:
        os.close(ifd)


def _poll_wait(directory: str, filename: str, timeout: float) -> bool:
    target   = os.path.join(directory, filename)
    deadline = time.monotonic() + timeout
    interval = 0.05
    while time.monotonic() < deadline:
        if os.path.exists(target):
            return True
        time.sleep(min(interval, deadline - time.monotonic()))
        interval = min(interval * 1.5, 2.0)
    return os.path.exists(target)


def _wait_token(token_path: str, timeout: float) -> bool:
    if os.path.exists(token_path):
        return True
    d = str(Path(token_path).parent)
    n = os.path.basename(token_path)
    try:
        return _inotify_wait(d, n, timeout)
    except Exception:
        return _poll_wait(d, n, timeout)


def _tree_digest(p: Path) -> str:
    h = hashlib.sha256()
    for fp in sorted(p.rglob("*")):
        if fp.is_file():
            h.update(fp.relative_to(p).as_posix().encode())
            with fp.open("rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
    return h.hexdigest()


def _bundle_ok(p: Path) -> bool:
    return p.exists() and (
        (p / "config.json").exists()
        or (p / "images.tar").exists()
        or bool(list(p.glob("*.img")))
        or bool(list(p.glob("**/inventory.img")))
    )


def _deliver_signal(task_id: str, token_path: str, runtime: str, ns: str,
                    sig: str = "SIGUSR1", runc_bin: str = "runc") -> None:
    for cmd in (
        [runc_bin, "kill", task_id, sig],
        ["docker", "kill", "-s", sig, task_id],
        [runtime, "-n", ns, "task", "kill", "--signal", sig, task_id],
    ):
        try:
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    raise RuntimeError("cannot signal '{}'".format(task_id))


class SnapshotCoordinator:

    def __init__(self, task_id: str, output_dir,
                 token_path: str = "/tmp/checkpoint_ready",
                 runtime: str = "runc", namespace: str = "default",
                 ready_timeout: float = 600.0, exec_timeout: float = 120.0,
                 resume_signal: str = "SIGUSR1", runc_bin: str = "runc",
                 work_path: str = "/tmp/criu-work",
                 checkpoint_args: Optional[List[str]] = None):
        self.task_id       = task_id
        self.output_dir    = Path(output_dir)
        self.token_path    = token_path
        self.runtime       = runtime
        self.namespace     = namespace
        self.ready_timeout = ready_timeout
        self.exec_timeout  = exec_timeout
        self.resume_signal = resume_signal
        self.runc_bin      = runc_bin
        self.work_path     = work_path
        self.checkpoint_args = checkpoint_args or []

    def _acquire(self) -> Path:
        if self.runtime == "runc":
            snap = self.output_dir / "snapshot"
            snap.mkdir(parents=True, exist_ok=True)
            cmd = [self.runc_bin, "checkpoint", "--image-path", str(snap),
                   "--work-path", self.work_path] + self.checkpoint_args + [self.task_id]
            out = snap
        else:
            out = self.output_dir / "bundle"
            out.mkdir(parents=True, exist_ok=True)
            cmd = [self.runtime, "-n", self.namespace, "task", "checkpoint",
                   "--checkpoint-path", str(out), self.task_id]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.exec_timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("snapshot timed out after {}s".format(self.exec_timeout))
        if r.returncode != 0:
            raise RuntimeError("snapshot failed: {}".format(r.stderr.strip()))
        return out

    def run(self, resume: bool = True) -> dict:
        if not _wait_token(self.token_path, self.ready_timeout):
            raise TimeoutError("token not seen after {}s".format(self.ready_timeout))

        out = self._acquire()
        if not _bundle_ok(out):
            raise RuntimeError("incomplete snapshot at {}".format(out))

        result = {
            "bundle_path": out,
            "digest":      _tree_digest(out),
            "task_id":     self.task_id,
            "timestamp":   time.time(),
        }
        if resume:
            try:
                _deliver_signal(self.task_id, self.token_path, self.runtime,
                                self.namespace, self.resume_signal, self.runc_bin)
            except RuntimeError:
                pass
        return result


_OPENAT_RE = re.compile(r'^\d+\s+openat\(AT_FDCWD,\s*"([^"]+)",[^)]+\)\s*=\s*(-?\d+)')
_OPEN_RE   = re.compile(r'^\d+\s+open\("([^"]+)",[^)]+\)\s*=\s*(-?\d+)')
_EXEC_RE   = re.compile(r'^\d+\s+execve\("([^"]+)"')
_EVENTS_RE = re.compile(r'^[A-Z_]+(?:,[A-Z_]+)*$')


def _rel_to(path: str, base: Path) -> Optional[str]:
    b = os.path.normpath(str(base))
    q = os.path.normpath(path)
    if q == b:
        return None
    prefix = b.rstrip("/") + "/"
    if q.startswith(prefix):
        return q[len(prefix):]
    if not path.startswith("/"):
        rel = path.lstrip("./")
        return rel or None
    return None


def detect_log_format(log_path: Path) -> str:
    try:
        with log_path.open("r", errors="replace") as fh:
            for _ in range(50):
                line = fh.readline()
                if not line:
                    break
                s = line.strip()
                if not s:
                    continue
                if "openat(" in s or "open(" in s or "execve(" in s:
                    return "strace"
                parts = s.split()
                if len(parts) >= 3 and parts[0].isdigit() and _EVENTS_RE.match(parts[-1]):
                    return "inotify"
    except OSError:
        return "unknown"
    return "unknown"


def parse_inotify_log(log_path: Path, base: Path) -> List[str]:
    seen: Dict[str, int] = {}
    rank = 0
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or not _EVENTS_RE.match(parts[-1]):
            continue
        events = parts[-1]
        if "ISDIR" in events:
            continue
        path = " ".join(parts[1:-1])
        rel = _rel_to(path, base)
        if rel is None:
            continue
        if rel not in seen:
            seen[rel] = rank
            rank += 1
    return sorted(seen, key=lambda k: seen[k])


def parse_strace_log(log_path: Path, base: Path) -> List[str]:
    seen: Dict[str, int] = {}
    rank = 0
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        path = None
        for pat in (_OPENAT_RE, _OPEN_RE):
            m = pat.match(line)
            if m and int(m.group(2)) >= 0:
                path = m.group(1)
                break
        if path is None:
            m = _EXEC_RE.match(line)
            if m:
                path = m.group(1)
        if path is None:
            continue
        rel = _rel_to(path, base)
        if rel is None:
            continue
        if rel not in seen:
            seen[rel] = rank
            rank += 1
    return sorted(seen, key=lambda k: seen[k])


_parse_access_log = parse_strace_log


def _all_files(base: Path) -> List[str]:
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() or p.is_symlink():
            out.append(str(p.relative_to(base)))
    return out


def _tier(tier_a: List[str], base: Path) -> Tuple[List[str], List[str]]:
    s = set(tier_a)
    tier_b = [f for f in _all_files(base) if f not in s]
    return tier_a, tier_b


def record_from_log(log_path: Path, base: Path, mode: str = "auto") -> Tuple[List[str], List[str]]:
    if mode == "auto":
        mode = detect_log_format(log_path)
    if mode == "strace":
        tier_a = parse_strace_log(log_path, base)
    else:
        tier_a = parse_inotify_log(log_path, base)
    return _tier(tier_a, base)


def record_inotify(
    bundle: Path,
    restore_cmd: List[str],
    events: str = "open,access",
    inotify_bin: str = "inotifywait",
    settle: float = 1.0,
    timeout: float = 300.0,
    keep_log: Optional[Path] = None,
    establish_timeout: float = 60.0,
) -> Tuple[List[str], List[str]]:
    log_ctx = tempfile.NamedTemporaryFile(prefix="sec_inotify_", suffix=".log", delete=False)
    log_path = Path(keep_log) if keep_log else Path(log_ctx.name)
    log_ctx.close()
    err_ctx = tempfile.NamedTemporaryFile(prefix="sec_inotify_", suffix=".err", delete=False)
    err_path = Path(err_ctx.name)
    err_ctx.close()

    cmd = [inotify_bin, "-m", "-r", "--timefmt", "%s",
           "--format", "%T %w%f %e", "-e", events, str(bundle)]
    with log_path.open("wb") as out, err_path.open("wb") as errf:
        watcher = subprocess.Popen(cmd, stdout=out, stderr=errf)
        try:
            _await_watches(err_path, establish_timeout, settle)
            try:
                subprocess.run(restore_cmd, timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
            except FileNotFoundError:
                raise RuntimeError("restore command not found: {}".format(restore_cmd[0]))
            time.sleep(settle)
        finally:
            watcher.terminate()
            try:
                watcher.wait(timeout=10)
            except subprocess.TimeoutExpired:
                watcher.kill()

    tier = record_from_log(log_path, Path(bundle), mode="inotify")
    if not keep_log:
        try:
            log_path.unlink()
        except OSError:
            pass
    try:
        err_path.unlink()
    except OSError:
        pass
    return tier


def _await_watches(err_path: Path, establish_timeout: float, settle: float) -> None:
    deadline = time.monotonic() + max(establish_timeout, settle)
    while time.monotonic() < deadline:
        try:
            data = err_path.read_bytes()
        except OSError:
            data = b""
        if b"established" in data.lower():
            time.sleep(min(settle, 0.5))
            return
        time.sleep(0.2)
    print("[satecode] warning: inotify watches not confirmed established within "
          "{:.0f}s; access order may be incomplete".format(establish_timeout),
          file=sys.stderr)


def record_access_sequence(
    restore_cmd: List[str],
    base: Path,
    strace_bin: str = "strace",
    timeout: float = 300.0,
) -> Tuple[List[str], List[str]]:
    with tempfile.TemporaryDirectory(prefix="sec_trace_") as td:
        log = os.path.join(td, "acc.log")
        cmd = [strace_bin, "-f", "-e", "trace=open,openat,execve", "-o", log, "--"] + list(restore_cmd)
        try:
            subprocess.run(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            raise RuntimeError("strace not found: '{}'".format(strace_bin))
        tier_a = parse_strace_log(Path(log), base)
    return _tier(tier_a, base)
