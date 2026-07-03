import os
from dataclasses import dataclass


@dataclass
class Config:
    sync_token_path: str = "/tmp/checkpoint_ready"
    sync_mode_env:   str = "CHECKPOINT_ENABLED"
    base_cmd_env:    str = "ORIGINAL_ENTRYPOINT"
    base_args_env:   str = "ORIGINAL_CMD"
    ready_file_env:  str = "CHECKPOINT_READY_FILE"
    shim_path:       str = "/opt/satcontainer/checkpoint_wrapper.py"
    patched_label:   str = "satecode.patched"
    build_label:     str = "satecode.build"
    stage_timeout:   int = 300

    resume_signal:   str = "SIGUSR1"

    runc_bin:        str = "runc"
    docker_bin:      str = "docker"
    mkfs_bin:        str = "mkfs.erofs"
    criu_work:       str = "/tmp/criu-work"

    snapshot_dirname: str = "snapshot"
    rootfs_dirname:   str = "rootfs"
    spec_filename:    str = "config.json"

    erofs_sort:      str = "none"
    meta_suffix:     str = ".meta.json"
    boundary_headroom: int = 8 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            sync_token_path=os.environ.get("SATCONTAINER_READY_FILE", cls.sync_token_path),
            stage_timeout=int(os.environ.get("SATCONTAINER_IMPORT_TIMEOUT", cls.stage_timeout)),
            runc_bin=os.environ.get("SATCONTAINER_RUNC", cls.runc_bin),
            docker_bin=os.environ.get("SATCONTAINER_DOCKER", cls.docker_bin),
            mkfs_bin=os.environ.get("SATCONTAINER_MKFS_EROFS", cls.mkfs_bin),
        )


default_config = Config()
