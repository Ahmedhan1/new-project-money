"""LocalProvider: a filesystem root under NEXUS_HOME (default destination, and the provider used by the fast tests).

Layout:  <root>/<path>                    the files (refs are relative POSIX paths)
         <root>/.nexus/uploads/<id>.part  in-progress resumable uploads (+ .json with the destination)
         <root>/.nexus/meta/<path>.json   app properties (NEXUS-created marker, sha256 cache)
         <root>/.nexus/versions/<path>@<ts>  previous versions when a file is overwritten
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from ... import config
from ...contracts import ProviderHealth, QuotaStatus, StorageProviderInfo
from . import connections
from .base import RemoteFile, StorageError, StorageProvider, safe_rel, sha256_file

INFO = StorageProviderInfo(provider="local", display="Local disk (NEXUS_HOME/storage/local)",
                           capabilities=["upload", "resumable_upload", "download", "list", "delete", "move", "checksum",
                                         "versions", "quota", "folders"],
                           max_connections=1, billing_required=False,
                           notes="Always available; quota = the real free disk space of the volume.")


def default_root() -> Path:
    return Path(os.environ.get("NEXUS_LOCAL_STORAGE") or (config.HOME / "storage" / "local"))


class LocalProvider(StorageProvider):
    INFO = INFO

    def __init__(self, root: str | Path | None = None, connection_id: str = connections.LOCAL_ID):
        self.connection_id = connection_id
        self.root = Path(root or default_root())
        self.root.mkdir(parents=True, exist_ok=True)
        self._sys = self.root / ".nexus"
        for d in ("uploads", "meta", "versions"):
            (self._sys / d).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ helpers
    def _abs(self, ref: str) -> Path:
        rel = safe_rel(ref)
        if rel.split("/", 1)[0] == ".nexus":
            raise ValueError("reserved path")
        p = (self.root / rel).resolve()
        if self.root.resolve() not in (p, *p.parents):
            raise ValueError(f"path escapes the storage root: {ref!r}")
        return p

    def _meta_path(self, ref: str) -> Path:
        return self._sys / "meta" / (safe_rel(ref) + ".json")

    def _meta(self, ref: str) -> dict:
        mp = self._meta_path(ref)
        return json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}

    def _set_meta(self, ref: str, m: dict) -> None:
        mp = self._meta_path(ref)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(m), encoding="utf-8")

    def _file(self, ref: str) -> RemoteFile:
        p = self._abs(ref)
        m = self._meta(ref)
        st = p.stat()
        sha = m.get("sha256") if m.get("mtime") == st.st_mtime and m.get("size") == st.st_size else None
        return RemoteFile(ref=safe_rel(ref), name=p.name, path=safe_rel(ref), size=None if p.is_dir() else st.st_size,
                          mime=m.get("mime", ""), sha256=sha, modified=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
                          is_folder=p.is_dir(), app_created=bool(m.get("props", {}).get("nexus") == "1"),
                          props=m.get("props", {}))

    # ------------------------------------------------------------ health / quota
    def connect(self) -> ProviderHealth:
        connections.ensure(self.connection_id)
        return self.health()

    def health(self) -> ProviderHealth:
        q = self.quota()
        ok = os.access(self.root, os.W_OK)
        connections.success(self.connection_id) if ok else connections.failure(self.connection_id, "root not writable")
        return ProviderHealth(provider="local", connection_id=self.connection_id,
                              status="ok" if ok and not q.exhausted else ("quota_exhausted" if q.exhausted else "unavailable"),
                              detail=f"root={self.root}", identity="local", quotas=[q],
                              last_success=time.time() if ok else None)

    def quota(self) -> QuotaStatus:
        du = shutil.disk_usage(self.root)
        return QuotaStatus(provider="local", connection_id=self.connection_id, resource="storage_bytes", limit=du.total,
                           used=du.total - du.free, remaining=du.free, source="measured", exhausted=du.free < 64 * 2 ** 20)

    # ------------------------------------------------------------ namespace
    def list(self, path: str = "") -> list[RemoteFile]:
        base = self._abs(path) if safe_rel(path) else self.root
        if not base.is_dir():
            return []
        out = []
        for p in sorted(base.iterdir()):
            if p.name == ".nexus" and base == self.root:
                continue
            out.append(self._file(p.relative_to(self.root).as_posix()))
        return out

    def mkdirs(self, path: str) -> str:
        p = self._abs(path)
        p.mkdir(parents=True, exist_ok=True)
        return safe_rel(path)

    def stat(self, ref: str) -> RemoteFile | None:
        try:
            p = self._abs(ref)
        except ValueError:
            return None
        return self._file(ref) if p.exists() else None

    def checksum(self, ref: str) -> dict:
        p = self._abs(ref)
        if not p.is_file():
            raise StorageError(f"no such file {ref}")
        m = self._meta(ref)
        st = p.stat()
        if m.get("sha256") and m.get("mtime") == st.st_mtime and m.get("size") == st.st_size:
            return {"sha256": m["sha256"], "md5": None, "source": "provider"}
        sha = sha256_file(p)
        self._set_meta(ref, {**m, "sha256": sha, "mtime": st.st_mtime, "size": st.st_size})
        return {"sha256": sha, "md5": None, "source": "computed"}

    def delete(self, ref: str, *, only_app_created: bool = True) -> bool:
        p = self._abs(ref)
        if not p.exists():
            return False
        if only_app_created and not self._meta(ref).get("props", {}).get("nexus") == "1":
            raise StorageError(f"refusing to delete {ref}: not created by NEXUS")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        mp = self._meta_path(ref)
        if mp.exists():
            mp.unlink()
        return True

    def move(self, ref: str, dest_path: str) -> RemoteFile:
        src, dst = self._abs(ref), self._abs(dest_path)
        if dst.exists():
            raise StorageError(f"destination exists: {dest_path}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        mp = self._meta_path(ref)
        if mp.exists():
            nm = self._meta_path(dest_path)
            nm.parent.mkdir(parents=True, exist_ok=True)
            os.replace(mp, nm)
        return self._file(dest_path)

    def versions(self, ref: str) -> list[dict]:
        rel = safe_rel(ref)
        vdir = self._sys / "versions" / Path(rel).parent
        out = [{"id": "current", "size": self._abs(ref).stat().st_size, "current": True}] if self._abs(ref).exists() else []
        if vdir.is_dir():
            for v in sorted(vdir.glob(Path(rel).name + "@*")):
                out.append({"id": v.name.rsplit("@", 1)[1], "size": v.stat().st_size, "current": False})
        return out

    def download(self, ref: str, dest: Path, *, resume: bool = True) -> Path:
        src = self._abs(ref)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if resume and part.exists() else 0
        with open(src, "rb") as fi, open(part, "ab" if have else "wb") as fo:
            fi.seek(have)
            shutil.copyfileobj(fi, fo, 1 << 20)
        if sha256_file(part) != self.checksum(ref)["sha256"]:
            part.unlink()
            raise StorageError(f"download of {ref} failed verification")
        os.replace(part, dest)
        return dest

    # ------------------------------------------------------------ resumable upload primitives
    def start_upload(self, dest_path: str, size: int, mime: str, props: dict) -> str:
        self._abs(dest_path)  # validates
        sid = uuid.uuid4().hex
        (self._sys / "uploads" / f"{sid}.json").write_text(json.dumps({"dest": safe_rel(dest_path), "size": size,
                                                                        "mime": mime, "props": props}), encoding="utf-8")
        (self._sys / "uploads" / f"{sid}.part").write_bytes(b"")
        return f"local:{sid}"

    def _session(self, session: str) -> tuple[Path, Path]:
        sid = session.split(":", 1)[1]
        if not sid.isalnum():
            raise ValueError("bad session")
        return self._sys / "uploads" / f"{sid}.json", self._sys / "uploads" / f"{sid}.part"

    def query_upload(self, session: str, size: int) -> int | RemoteFile | None:
        js, part = self._session(session)
        if not js.exists():
            done = json.loads((js.with_suffix(".done")).read_text()) if js.with_suffix(".done").exists() else None
            return self._file(done["dest"]) if done and self.stat(done["dest"]) else None
        return part.stat().st_size if part.exists() else None

    def put_chunk(self, session: str, offset: int, data: bytes, size: int) -> int | RemoteFile:
        js, part = self._session(session)
        if not js.exists():
            raise StorageError("upload session not found")
        have = part.stat().st_size
        if offset != have:
            raise StorageError(f"offset {offset} != acknowledged {have}")
        with open(part, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        have += len(data)
        if have < size:
            return have
        info = json.loads(js.read_text(encoding="utf-8"))
        dst = self._abs(info["dest"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():  # keep the previous version
            vp = self._sys / "versions" / (info["dest"] + "@" + str(int(time.time() * 1000)))
            vp.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dst, vp)
        os.replace(part, dst)
        st = dst.stat()
        self._set_meta(info["dest"], {"mime": info["mime"], "props": info["props"], "sha256": sha256_file(dst),
                                      "mtime": st.st_mtime, "size": st.st_size})
        os.replace(js, js.with_suffix(".done"))
        return self._file(info["dest"])

    def abort_upload(self, session: str) -> None:
        for p in self._session(session):
            if p.exists():
                p.unlink()

    def local_path(self, ref: str) -> Path:
        return self._abs(ref)
