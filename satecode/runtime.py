import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from satecode.config import default_config, Config


def _is_oci(tar: tarfile.TarFile) -> bool:
    try:
        tar.getmember("oci-layout")
        return True
    except KeyError:
        return False


def _read_oci_index(tar: tarfile.TarFile) -> dict:
    f = tar.extractfile("index.json")
    if f is None:
        raise ValueError("index.json missing")
    return json.load(f)


def _read_oci_manifest(tar: tarfile.TarFile, digest: str) -> dict:
    p = "blobs/sha256/{}".format(digest.split(":")[1])
    f = tar.extractfile(p)
    if f is None:
        raise ValueError("manifest blob missing: {}".format(p))
    return json.load(f)


def _read_docker_manifest(tar: tarfile.TarFile) -> list:
    f = tar.extractfile("manifest.json")
    if f is None:
        raise ValueError("manifest.json missing")
    return json.load(f)


def _read_config(tar: tarfile.TarFile, ref: str) -> dict:
    if ref.startswith("sha256:"):
        path = "blobs/sha256/{}".format(ref.split(":")[1])
    else:
        path = ref
    f = tar.extractfile(path)
    if f is None:
        raise ValueError("config missing: {}".format(path))
    return json.load(f)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_layer(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for fpath, content in files.items():
            for i in range(1, len(Path(fpath).parts)):
                dname = "/".join(Path(fpath).parts[:i])
                try:
                    tf.getmember(dname)
                except KeyError:
                    di = tarfile.TarInfo(name=dname)
                    di.type = tarfile.DIRTYPE
                    di.mode = 0o755
                    tf.addfile(di)
            info = tarfile.TarInfo(name=fpath.lstrip("/"))
            info.size = len(content)
            info.mode = 0o755 if fpath.endswith(".py") else 0o644
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _parse_df_instruction(line: str, kw: str) -> list:
    val = line[len(kw):].strip()
    if val.startswith("["):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            pass
    return ["sh", "-c", val]


class ImagePatcher:

    def __init__(self, input_tar: str, dockerfile: Optional[str] = None,
                 config: Optional[Config] = None):
        self.input_tar  = Path(input_tar)
        self.dockerfile = Path(dockerfile) if dockerfile else None
        self.config     = config or default_config

        if not self.input_tar.exists():
            raise FileNotFoundError("Not found: {}".format(self.input_tar))

        self._oci         = False
        self._manifest    = None
        self._oci_index   = None
        self._oci_mf      = None
        self._img_cfg     = None
        self._cfg_ref     = None

    def _load(self, tar: tarfile.TarFile) -> None:
        self._oci = _is_oci(tar)
        if self._oci:
            self._oci_index = _read_oci_index(tar)
            self._oci_mf    = _read_oci_manifest(tar, self._oci_index["manifests"][0]["digest"])
            self._cfg_ref   = self._oci_mf["config"]["digest"]
        else:
            self._manifest = _read_docker_manifest(tar)
            self._cfg_ref  = self._manifest[0]["Config"]
        self._img_cfg = _read_config(tar, self._cfg_ref)

    def get_original_entrypoint(self) -> Tuple[list, list]:
        with tarfile.open(self.input_tar, "r") as tar:
            self._load(tar)
        c = self._img_cfg.get("config", {})
        return (c.get("Entrypoint") or []), (c.get("Cmd") or [])

    def is_patched(self) -> bool:
        with tarfile.open(self.input_tar, "r") as tar:
            self._load(tar)
        labels = self._img_cfg.get("config", {}).get("Labels") or {}
        return labels.get(self.config.patched_label) == "true"

    def is_injected(self) -> bool:
        return self.is_patched()

    def _shim_bytes(self) -> bytes:
        return (Path(__file__).parent / "agent.py").read_bytes()

    def _df_entrypoint(self) -> Tuple[Optional[list], Optional[list]]:
        if not self.dockerfile or not self.dockerfile.exists():
            return None, None
        ep = cmd = None
        for line in self.dockerfile.read_text().splitlines():
            s = line.strip()
            u = s.upper()
            if u.startswith("ENTRYPOINT"):
                ep = _parse_df_instruction(s, "ENTRYPOINT")
            elif u.startswith("CMD"):
                cmd = _parse_df_instruction(s, "CMD")
        return ep, cmd

    def _update_config(self, ep_json: str, cmd_json: str, diff_id: str) -> None:
        c = self._img_cfg.setdefault("config", {})

        env = c.setdefault("Env", [])
        env = [e for e in env
               if not e.startswith(self.config.base_cmd_env + "=")
               and not e.startswith(self.config.base_args_env + "=")]
        env.append("{}={}".format(self.config.base_cmd_env,  ep_json))
        env.append("{}={}".format(self.config.base_args_env, cmd_json))
        c["Env"] = env

        labels = c.setdefault("Labels", {}) or {}
        c["Labels"] = labels
        labels[self.config.patched_label] = "true"
        labels[self.config.build_label]   = "0.1.0"

        c["Entrypoint"] = ["python3", self.config.shim_path]
        c["Cmd"]        = []

        self._img_cfg.setdefault("rootfs", {"type": "layers", "diff_ids": []})["diff_ids"].append(diff_id)
        self._img_cfg.setdefault("history", []).append({
            "created_by": "satecode patch", "empty_layer": False
        })

    def _apply_oci(self, tmpdir: Path, blobs: Path, cfg_digest: str, layer_digest: str,
                   cfg_size: int, layer_size: int, tag: str) -> None:
        self._oci_mf["config"]["digest"] = cfg_digest
        self._oci_mf["config"]["size"]   = cfg_size
        self._oci_mf["layers"].append({
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": layer_digest, "size": layer_size,
        })
        mf_bytes   = json.dumps(self._oci_mf).encode()
        mf_digest  = "sha256:" + _sha256(mf_bytes)
        (blobs / mf_digest.split(":")[1]).write_bytes(mf_bytes)

        old = blobs / self._oci_index["manifests"][0]["digest"].split(":")[1]
        if old.exists():
            old.unlink()

        self._oci_index["manifests"][0].update({
            "digest": mf_digest, "size": len(mf_bytes)
        })
        ann = self._oci_index["manifests"][0].setdefault("annotations", {})
        ann["io.containerd.image.name"] = "docker.io/library/{}".format(tag)
        if ":" in tag:
            ann["org.opencontainers.image.ref.name"] = tag.split(":")[1]
        (tmpdir / "index.json").write_text(json.dumps(self._oci_index, indent=2))

        mf_path = tmpdir / "manifest.json"
        if mf_path.exists():
            dm = json.loads(mf_path.read_text())
            dm[0]["Config"] = "blobs/sha256/" + cfg_digest.split(":")[1]
            dm[0]["Layers"].append("blobs/sha256/" + layer_digest.split(":")[1])
            dm[0]["RepoTags"] = [tag]
            mf_path.write_text(json.dumps(dm, indent=2))

    def _apply_docker(self, tmpdir: Path, cfg_digest: str, layer_digest: str, tag: str) -> None:
        self._manifest[0]["Config"] = cfg_digest.split(":")[1] + ".json"
        self._manifest[0]["Layers"].append(layer_digest)
        self._manifest[0]["RepoTags"] = [tag]
        (tmpdir / "manifest.json").write_text(json.dumps(self._manifest, indent=2))

    def _orig_tag(self) -> str:
        if self._oci and self._oci_index:
            ann  = self._oci_index["manifests"][0].get("annotations", {})
            name = ann.get("io.containerd.image.name", "")
            if name.startswith("docker.io/library/"):
                name = name[len("docker.io/library/"):]
            if name:
                return name
        if self._manifest:
            tags = self._manifest[0].get("RepoTags", [])
            if tags:
                return tags[0]
        return "patched:latest"

    def patch(self, output_tar: str, force: bool = False, tag_suffix: str = "-wrapped") -> str:
        out = Path(output_tar)
        if out.exists() and not force:
            raise FileExistsError("'{}' exists. Use force=True.".format(output_tar))

        with tempfile.TemporaryDirectory() as _tmp:
            tmp = Path(_tmp)
            with tarfile.open(self.input_tar, "r") as tar:
                tar.extractall(tmp)
                self._load(tar)

            c   = self._img_cfg.get("config", {})
            ep  = c.get("Entrypoint") or []
            cmd = c.get("Cmd") or []
            if not ep and not cmd:
                dep, dcmd = self._df_entrypoint()
                if dep:  ep  = dep
                if dcmd: cmd = dcmd

            ep_json  = json.dumps(ep)
            cmd_json = json.dumps(cmd)

            layer_raw    = _make_layer({self.config.shim_path.lstrip("/"): self._shim_bytes()})
            diff_id      = "sha256:" + _sha256(layer_raw)
            layer_gz     = gzip.compress(layer_raw)
            layer_digest = "sha256:" + _sha256(layer_gz)

            blobs = tmp / "blobs" / "sha256"
            blobs.mkdir(parents=True, exist_ok=True)
            (blobs / layer_digest.split(":")[1]).write_bytes(layer_gz)

            self._update_config(ep_json, cmd_json, diff_id)

            cfg_bytes  = json.dumps(self._img_cfg).encode()
            cfg_digest = "sha256:" + _sha256(cfg_bytes)
            cfg_hash   = cfg_digest.split(":")[1]

            if self._oci:
                (blobs / cfg_hash).write_bytes(cfg_bytes)
            else:
                (tmp / (cfg_hash + ".json")).write_bytes(cfg_bytes)

            old_ref = self._cfg_ref
            if old_ref.startswith("sha256:"):
                old_p = blobs / old_ref.split(":")[1]
            else:
                old_p = tmp / old_ref
            if old_p.exists():
                old_p.unlink()

            orig = self._orig_tag()
            if tag_suffix and ":" in orig:
                name, ver = orig.rsplit(":", 1)
                new_tag = "{}{}:{}".format(name, tag_suffix, ver)
            elif tag_suffix:
                new_tag = orig + tag_suffix
            else:
                new_tag = orig

            if self._oci:
                self._apply_oci(tmp, blobs, cfg_digest, layer_digest,
                                len(cfg_bytes), len(layer_gz), new_tag)
            else:
                self._apply_docker(tmp, cfg_digest, layer_digest, new_tag)

            out.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(out, "w") as tf:
                for item in tmp.iterdir():
                    tf.add(item, arcname=item.name)

        return str(out)

    def inject(self, output_tar: str, force: bool = False, tag_suffix: str = "-wrapped") -> str:
        return self.patch(output_tar, force=force, tag_suffix=tag_suffix)

    def _parse_dockerfile_entrypoint(self):
        return self._df_entrypoint()


def _apply_layer_tar(layer_tar: tarfile.TarFile, dest: Path) -> None:
    for m in layer_tar.getmembers():
        parts = Path(m.name).parts
        if not parts:
            continue
        if parts[-1] == ".wh..wh..opq":
            target = dest / Path(*parts[:-1]) if len(parts) > 1 else dest
            shutil.rmtree(target, ignore_errors=True)
            continue
        if parts[-1].startswith(".wh."):
            real = parts[-1][4:]
            target = dest / Path(*parts[:-1], real) if len(parts) > 1 else dest / real
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            continue
        target = dest / m.name.lstrip("/")
        if m.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif m.issym():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            os.symlink(m.linkname, target)
        elif m.islnk():
            src = dest / m.linkname.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            try:
                os.link(src, target)
            except OSError:
                fh = layer_tar.extractfile(m)
                if fh:
                    target.write_bytes(fh.read())
        elif m.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            fh = layer_tar.extractfile(m)
            if fh:
                target.write_bytes(fh.read())
                target.chmod(m.mode)


def extract_image(image_tar: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(image_tar) as outer:
        names = outer.getnames()
        oci = "oci-layout" in names
        if oci:
            idx  = json.load(outer.extractfile("index.json"))
            mfd  = idx["manifests"][0]["digest"].split(":")[1]
            mf   = json.load(outer.extractfile(f"blobs/sha256/{mfd}"))
            lpaths = ["blobs/sha256/{}".format(d["digest"].split(":")[1])
                      for d in mf["layers"]]
        else:
            mf_list = json.load(outer.extractfile("manifest.json"))
            lpaths  = mf_list[0]["Layers"]

        for lp in lpaths:
            raw = outer.extractfile(lp).read()
            try:
                raw = gzip.decompress(raw)
            except (OSError, Exception):
                pass
            with tarfile.open(fileobj=io.BytesIO(raw)) as lt:
                _apply_layer_tar(lt, dest)


def _emit_symlink(tf: tarfile.TarFile, base: Path, rel: str) -> None:
    info = tarfile.TarInfo(name=rel)
    info.type = tarfile.SYMTYPE
    info.linkname = os.readlink(base / rel)
    tf.addfile(info)


def _emit_dir(tf: tarfile.TarFile, base: Path, rel: str) -> None:
    info = tarfile.TarInfo(name=rel)
    info.type = tarfile.DIRTYPE
    try:
        info.mode = (base / rel).stat().st_mode & 0o7777
    except OSError:
        info.mode = 0o755
    tf.addfile(info)


def _add_ordered(tf: tarfile.TarFile, base: Path, rel: str,
                 seen: set, sym: set) -> int:
    rel = rel.strip("/")
    if not rel or rel in seen:
        return 0
    p = base / rel
    if not (p.exists() or p.is_symlink()):
        return 0

    parts = Path(rel).parts
    for i in range(1, len(parts)):
        dname = "/".join(parts[:i])
        if dname in seen:
            if dname in sym:
                return 0
            continue
        dp = base / dname
        if dp.is_symlink():
            _emit_symlink(tf, base, dname)
            seen.add(dname); sym.add(dname)
            return 0
        seen.add(dname)
        _emit_dir(tf, base, dname)

    if p.is_symlink():
        _emit_symlink(tf, base, rel)
        seen.add(rel); sym.add(rel)
        return 0
    if p.is_dir():
        seen.add(rel)
        _emit_dir(tf, base, rel)
        return 0
    if p.is_file():
        tf.add(p, arcname=rel, recursive=False)
        seen.add(rel)
        try:
            return p.stat().st_size
        except OSError:
            return 0
    return 0


def _build_ordered_tar(base: Path, tier_a: List[str], tier_b: List[str],
                       out_tar: Path, aux_dir: Optional[Path] = None) -> int:
    hot_bytes = 0
    seen: set = set()
    sym:  set = set()
    with tarfile.open(out_tar, "w") as tf:
        for rel in tier_a:
            hot_bytes += _add_ordered(tf, base, rel, seen, sym)
        for rel in tier_b:
            _add_ordered(tf, base, rel, seen, sym)
        if aux_dir and aux_dir.exists():
            for fp in sorted(aux_dir.rglob("*")):
                if fp.is_file():
                    tf.add(fp, arcname=".aux/" + str(fp.relative_to(aux_dir)),
                           recursive=False)
    return hot_bytes


def _round_up(n: int, block: int = 4096) -> int:
    return ((n + block - 1) // block) * block


def build_image(source: Path, output: Path,
                tier_a: List[str], tier_b: List[str],
                aux_dir: Optional[Path] = None,
                mkfs_bin: str = "mkfs.erofs",
                sort: str = "none",
                source_kind: str = "auto",
                config: Optional[Config] = None) -> dict:
    cfg = config or default_config
    if source_kind == "auto":
        source_kind = "bundle" if Path(source).is_dir() else "image"

    with tempfile.TemporaryDirectory(prefix="sec_build_") as _tmp:
        tmp     = Path(_tmp)
        ord_tar = tmp / "ord.tar"

        if source_kind == "image":
            base = tmp / "rootfs"
            extract_image(Path(source), base)
        else:
            base = Path(source)

        hot_bytes = _build_ordered_tar(base, tier_a, tier_b, ord_tar, aux_dir)

        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [mkfs_bin, "--tar=f", "--sort={}".format(sort), str(output), str(ord_tar)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("mkfs.erofs failed: {}\n  cmd: {}".format(
                r.stderr.strip(), " ".join(cmd)))

    erofs_size = output.stat().st_size
    boundary   = min(erofs_size, _round_up(hot_bytes) + cfg.boundary_headroom)
    meta_path  = Path(str(output) + cfg.meta_suffix)
    meta = {
        "format":       "erofs",
        "version":      1,
        "image":        output.name,
        "output":       str(output.resolve()),
        "meta_path":    str(meta_path),
        "boundary":     boundary,
        "erofs_size":   erofs_size,
        "hot_bytes":    hot_bytes,
        "sha256":       _sha256(output.read_bytes()),
        "tier_a_count": len(tier_a),
        "tier_b_count": len(tier_b),
        "sort":         sort,
        "source_kind":  source_kind,
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta
