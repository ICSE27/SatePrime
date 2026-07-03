from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from satecode.config import Config, default_config

_TOKEN_DIR_IN_CTR  = "/run/satecode"
_TOKEN_NAME        = "checkpoint_ready"
_DEFAULT_PATH      = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _default_spec(rootfs: str = "rootfs") -> dict:
    return {
        "ociVersion": "1.0.2-dev",
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": ["sh"],
            "env": [
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "TERM=xterm",
            ],
            "cwd": "/",
            "capabilities": {
                k: [
                    "CAP_AUDIT_WRITE", "CAP_KILL", "CAP_NET_BIND_SERVICE",
                    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID",
                    "CAP_SETGID", "CAP_SETUID", "CAP_SETPCAP", "CAP_NET_RAW",
                    "CAP_SYS_CHROOT", "CAP_MKNOD", "CAP_SETFCAP",
                ]
                for k in ("bounding", "effective", "permitted", "ambient")
            },
            "rlimits": [{"type": "RLIMIT_NOFILE", "hard": 1024, "soft": 1024}],
            "noNewPrivileges": True,
        },
        "root": {"path": rootfs, "readonly": False},
        "hostname": "satecode",
        "mounts": [
            {"destination": "/proc", "type": "proc", "source": "proc"},
            {"destination": "/dev", "type": "tmpfs", "source": "tmpfs",
             "options": ["nosuid", "strictatime", "mode=755", "size=65536k"]},
            {"destination": "/dev/pts", "type": "devpts", "source": "devpts",
             "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620", "gid=5"]},
            {"destination": "/dev/shm", "type": "tmpfs", "source": "shm",
             "options": ["nosuid", "noexec", "nodev", "mode=1777", "size=65536k"]},
            {"destination": "/dev/mqueue", "type": "mqueue", "source": "mqueue",
             "options": ["nosuid", "noexec", "nodev"]},
            {"destination": "/sys", "type": "sysfs", "source": "sysfs",
             "options": ["nosuid", "noexec", "nodev", "ro"]},
            {"destination": "/sys/fs/cgroup", "type": "cgroup", "source": "cgroup",
             "options": ["nosuid", "noexec", "nodev", "relatime", "ro"]},
        ],
        "linux": {
            "resources": {"devices": [{"allow": False, "access": "rwm"}]},
            "namespaces": [
                {"type": "pid"}, {"type": "network"}, {"type": "ipc"},
                {"type": "uts"}, {"type": "mount"},
            ],
            "maskedPaths": [
                "/proc/acpi", "/proc/asound", "/proc/kcore", "/proc/keys",
                "/proc/latency_stats", "/proc/timer_list", "/proc/timer_stats",
                "/proc/sched_debug", "/sys/firmware", "/proc/scsi",
            ],
            "readonlyPaths": [
                "/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger",
            ],
        },
    }


def export_rootfs(image_or_cid: str, rootfs: Path, docker_bin: str = "docker",
                  is_container: bool = False) -> Path:
    rootfs.mkdir(parents=True, exist_ok=True)
    created: Optional[str] = None
    cid = image_or_cid
    try:
        if not is_container:
            r = subprocess.run([docker_bin, "create", image_or_cid],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError("docker create: {}".format(r.stderr.strip()))
            cid = created = r.stdout.strip()

        proc = subprocess.Popen([docker_bin, "export", cid], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|*") as tf:
                _safe_extract(tf, rootfs)
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            raise RuntimeError("docker export exited with {}".format(rc))
    finally:
        if created:
            subprocess.run([docker_bin, "rm", created], capture_output=True)
    return rootfs


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    for m in tf:
        target = (dest / m.name).resolve()
        if not str(target).startswith(str(dest)):
            continue
        try:
            tf.extract(m, dest, set_attrs=True)
        except (OSError, PermissionError):
            continue


def inspect_image(image: str, docker_bin: str = "docker") -> Tuple[List[str], List[str], List[str], str]:
    r = subprocess.run([docker_bin, "inspect", image], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("docker inspect: {}".format(r.stderr.strip()))
    data = json.loads(r.stdout)
    cfg = (data[0].get("Config") or {}) if data else {}
    return (cfg.get("Entrypoint") or [],
            cfg.get("Cmd") or [],
            cfg.get("Env") or [],
            cfg.get("WorkingDir") or "/")


def install_wrapper(rootfs: Path, shim_path: str) -> Path:
    src = Path(__file__).parent / "agent.py"
    dest = rootfs / shim_path.lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    dest.chmod(0o755)
    return dest


def build_spec(entrypoint: List[str], cmd: List[str], env: List[str], workdir: str,
               config: Optional[Config] = None,
               extra_mounts: Optional[List[dict]] = None,
               base_spec: Optional[dict] = None) -> dict:
    cfg  = config or default_config
    spec = json.loads(json.dumps(base_spec)) if base_spec else _default_spec(cfg.rootfs_dirname)

    p = spec.setdefault("process", {})
    p["terminal"] = False
    p["args"] = ["python3", cfg.shim_path]
    p["cwd"]  = workdir or "/"

    token = "{}/{}".format(_TOKEN_DIR_IN_CTR, _TOKEN_NAME)
    merged = _merge_env(env, {
        cfg.base_cmd_env:  json.dumps(entrypoint),
        cfg.base_args_env: json.dumps(cmd),
        cfg.sync_mode_env: "1",
        cfg.ready_file_env: token,
        "PYTHONUNBUFFERED": "1",
    })
    if not any(e.startswith("PATH=") for e in merged):
        merged.insert(0, "PATH=" + _DEFAULT_PATH)
    p["env"] = merged

    if extra_mounts:
        spec.setdefault("mounts", []).extend(extra_mounts)
    return spec


def _merge_env(base: List[str], overrides: Dict[str, str]) -> List[str]:
    out: List[str] = []
    seen = set(overrides)
    for e in base:
        k = e.split("=", 1)[0]
        if k in seen:
            continue
        out.append(e)
    for k, v in overrides.items():
        out.append("{}={}".format(k, v))
    return out


class SeedBuilder:

    def __init__(self, image: str, output_dir, seed_id: str = "sat-seed",
                 is_container: bool = False,
                 entrypoint: Optional[List[str]] = None,
                 cmd: Optional[List[str]] = None,
                 config: Optional[Config] = None,
                 ready_timeout: float = 600.0,
                 checkpoint_args: Optional[List[str]] = None,
                 leave_running: bool = False):
        self.image         = image
        self.bundle        = Path(output_dir)
        self.seed_id       = seed_id
        self.is_container  = is_container
        self.entrypoint    = entrypoint
        self.cmd           = cmd
        self.cfg           = config or default_config
        self.ready_timeout = ready_timeout
        self.checkpoint_args = checkpoint_args or []
        self.leave_running = leave_running

    def build(self) -> dict:
        cfg     = self.cfg
        rootfs  = self.bundle / cfg.rootfs_dirname
        snap    = self.bundle / cfg.snapshot_dirname
        spec_p  = self.bundle / cfg.spec_filename
        self.bundle.mkdir(parents=True, exist_ok=True)

        export_rootfs(self.image, rootfs, cfg.docker_bin, is_container=self.is_container)
        install_wrapper(rootfs, cfg.shim_path)

        token_dir = rootfs / _TOKEN_DIR_IN_CTR.lstrip("/")
        token_dir.mkdir(parents=True, exist_ok=True)

        ep, cm, env, wd = ([], [], [], "/")
        try:
            ep, cm, env, wd = inspect_image(self.image, cfg.docker_bin)
        except Exception:
            pass
        if self.entrypoint is not None:
            ep = self.entrypoint
        if self.cmd is not None:
            cm = self.cmd

        spec = build_spec(ep, cm, env, wd, config=cfg)
        spec_p.write_text(json.dumps(spec, indent=2))

        token = token_dir / _TOKEN_NAME
        if token.exists():
            token.unlink()
        self._run_detached()

        if not self._wait_ready(token):
            self._cleanup()
            raise TimeoutError("seed not ready after {}s".format(self.ready_timeout))
        self._checkpoint(snap)

        result = {
            "bundle":     self.bundle,
            "rootfs":     rootfs,
            "snapshot":   snap,
            "config":     spec_p,
            "seed_id":    self.seed_id,
            "entrypoint": ep,
            "cmd":        cm,
        }
        return result

    def _run_detached(self) -> None:
        log = self.bundle / "seed.log"
        cmd = [self.cfg.runc_bin, "run", "--detach",
               "--bundle", str(self.bundle),
               "--pid-file", str(self.bundle / "seed.pid"),
               self.seed_id]
        with log.open("wb") as fh:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            raise RuntimeError("runc run failed (rc={}); see {}".format(r.returncode, log))

    def _wait_ready(self, token: Path) -> bool:
        from satecode.monitor import _wait_token
        return _wait_token(str(token), self.ready_timeout)

    def _checkpoint(self, snap: Path) -> None:
        snap.mkdir(parents=True, exist_ok=True)
        cmd = [self.cfg.runc_bin, "checkpoint",
               "--image-path", str(snap),
               "--work-path", self.cfg.criu_work]
        if self.leave_running:
            cmd.append("--leave-running")
        cmd += self.checkpoint_args + [self.seed_id]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("runc checkpoint: {}".format(r.stderr.strip()))

    def _cleanup(self) -> None:
        subprocess.run([self.cfg.runc_bin, "delete", "--force", self.seed_id],
                       capture_output=True)
