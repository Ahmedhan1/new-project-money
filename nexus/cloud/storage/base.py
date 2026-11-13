"""StorageProvider behaviour contract (contracts v1 `StorageProviderInfo` describes it statically).

Every provider implements the same operations. Resumable upload is implemented ONCE here as an engine over three
provider primitives (start_upload / query_upload / put_chunk) so that every provider gets the same crash-safe
behaviour: the session id and the last byte the provider acknowledged are written to the `transfers` table after
every chunk, and `resume(transfer_id)` asks the provider how much it really has before sending more (the provider is
the source of truth - never our own counter).
"""
from __future__ import annotations

import hashlib
import os
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ...contracts import ProviderHealth, QuotaStatus, StorageProviderInfo
from . import transfers as tx

CHUNK_UNIT = 256 * 1024  # Drive: every chunk except the last must be a multiple of 256 KiB
DEFAULT_CHUNK = 32 * CHUNK_UNIT  # 8 MiB


class StorageError(RuntimeError):
    pass


class NotConfigured(StorageError):
    """No OAuth client / no tokens for this connection: the owner must connect it first."""


class ReauthRequired(StorageError):
    """The refresh token was revoked or expired: the owner must authorise the connection again."""


class QuotaExceeded(StorageError):
    pass


class ChecksumMismatch(StorageError):
    pass


class TransientError(StorageError):
    """Network error / 5xx: safe to query the upload status and continue."""


class UploadSessionExpired(StorageError):
    pass


@dataclass
class RemoteFile:
    ref: str
    name: str
    path: str = ""
    size: int | None = None
    mime: str = ""
    sha256: str | None = None
    md5: str | None = None
    modified: str | None = None
    is_folder: bool = False
    app_created: bool = False
    props: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: str | Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class StorageProvider(ABC):
    INFO: StorageProviderInfo
    connection_id: str

    # ------------------------------------------------------------ identity / health
    @abstractmethod
    def connect(self) -> ProviderHealth: ...
    @abstractmethod
    def health(self) -> ProviderHealth: ...
    @abstractmethod
    def quota(self) -> QuotaStatus: ...

    # ------------------------------------------------------------ namespace
    @abstractmethod
    def list(self, path: str = "") -> list[RemoteFile]: ...
    @abstractmethod
    def mkdirs(self, path: str) -> str:
        """Create every folder of `path` (e.g. NEXUS/Masters) that is missing; return the provider ref of the last."""
    @abstractmethod
    def stat(self, ref: str) -> RemoteFile | None: ...
    @abstractmethod
    def checksum(self, ref: str) -> dict:
        """{'sha256': hex|None, 'md5': hex|None, 'source': 'provider'|'computed'}"""
    @abstractmethod
    def delete(self, ref: str, *, only_app_created: bool = True) -> bool: ...
    @abstractmethod
    def move(self, ref: str, dest_path: str) -> RemoteFile:
        """Rename / re-parent within this connection."""
    @abstractmethod
    def versions(self, ref: str) -> list[dict]: ...
    @abstractmethod
    def download(self, ref: str, dest: Path, *, resume: bool = True) -> Path: ...

    # ------------------------------------------------------------ resumable upload primitives
    @abstractmethod
    def start_upload(self, dest_path: str, size: int, mime: str, props: dict) -> str:
        """Open an upload session; return its id/URI (persisted in transfers.session_uri)."""
    @abstractmethod
    def query_upload(self, session: str, size: int) -> int | RemoteFile | None:
        """Bytes the provider has acknowledged, the finished file, or None when the session is gone/expired."""
    @abstractmethod
    def put_chunk(self, session: str, offset: int, data: bytes, size: int) -> int | RemoteFile:
        """Send bytes [offset, offset+len). Returns the new acknowledged offset, or the finished file."""
    def abort_upload(self, session: str) -> None:  # optional
        return None

    # ------------------------------------------------------------ engine (shared by every provider)
    def upload(self, local: str | Path, dest_path: str, *, mime: str = "application/octet-stream",
               props: dict | None = None, chunk_size: int = DEFAULT_CHUNK, transfer_id: str | None = None,
               on_chunk: Callable[[int, int], None] | None = None) -> RemoteFile:
        local = Path(local)
        if chunk_size % CHUNK_UNIT:
            raise ValueError("chunk_size must be a multiple of 256 KiB")
        size = local.stat().st_size
        digest = sha256_file(local)
        if transfer_id and tx.get(transfer_id):
            t = tx.get(transfer_id)
        else:
            t = tx.create(self.connection_id, "upload", str(local), dest_path, size, digest, transfer_id=transfer_id,
                          meta={"mime": mime, "props": props or {}, "chunk_size": chunk_size})
        return self._run_upload(t, on_chunk)

    def resume(self, transfer_id: str, on_chunk: Callable[[int, int], None] | None = None) -> RemoteFile:
        t = tx.get(transfer_id)
        if t is None:
            raise StorageError(f"unknown transfer {transfer_id}")
        if t["connection_id"] != self.connection_id:
            raise StorageError(f"transfer {transfer_id} belongs to {t['connection_id']}, not {self.connection_id}")
        if t["status"] == "done":
            f = self.stat(t["result_ref"]) if t.get("result_ref") else None
            if f:
                return f
        return self._run_upload(t, on_chunk)

    def _run_upload(self, t: dict, on_chunk) -> RemoteFile:
        local = Path(t["local_path"])
        size, meta = int(t["size"]), t["meta"]
        if not local.exists() or local.stat().st_size != size or sha256_file(local) != t["sha256"]:
            tx.update(t["id"], status="failed", error="local file changed or missing since the upload started")
            raise StorageError("local file changed or missing since the upload started; start a new upload")
        chunk = int(meta.get("chunk_size") or DEFAULT_CHUNK)
        tx.update(t["id"], status="running", error=None)
        session = t.get("session_uri")
        restarts = transient = 0
        try:
            while True:
                if not session:
                    session = self.start_upload(t["remote_ref"], size, meta.get("mime") or "application/octet-stream",
                                                {**(meta.get("props") or {}), "nexus": "1", "nexus_sha256": t["sha256"]})
                    tx.update(t["id"], session_uri=session, sent=0)
                try:
                    state = self.query_upload(session, size)
                    if state is None:  # expired / unknown session: restart from byte 0 with a new session
                        restarts += 1
                        if restarts > 2:
                            raise UploadSessionExpired("upload session keeps expiring")
                        session = None
                        continue
                    with open(local, "rb") as fh:
                        while not isinstance(state, RemoteFile):
                            offset = int(state)
                            tx.update(t["id"], sent=offset)
                            if on_chunk:
                                on_chunk(offset, size)
                            fh.seek(offset)
                            data = fh.read(chunk)
                            state = self.put_chunk(session, offset, data, size)
                            transient = 0
                    result = state
                    break
                except TransientError:
                    transient += 1
                    if transient > 5:
                        raise
                    time.sleep(min(2 ** transient * 0.2, 5))
            self._verify(result, t)
        except Exception as e:
            tx.update(t["id"], status="interrupted" if not isinstance(e, ChecksumMismatch) else "failed",
                      error=str(e)[:500])
            raise
        tx.update(t["id"], status="done", sent=size, result_ref=result.ref)
        if on_chunk:
            on_chunk(size, size)
        return result

    def _verify(self, f: RemoteFile, t: dict) -> None:
        local_sha = t["sha256"]
        if f.sha256:
            ok = f.sha256.lower() == local_sha
        elif f.md5:
            ok = f.md5.lower() == md5_file(t["local_path"])
        else:
            c = self.checksum(f.ref)
            ok = (c.get("sha256") or "").lower() == local_sha
        if not ok or (f.size is not None and int(f.size) != int(t["size"])):
            try:
                self.delete(f.ref, only_app_created=True)
            finally:
                raise ChecksumMismatch(f"remote copy of {t['local_path']} does not match (sha256 {local_sha[:12]}...)")


def safe_rel(path: str) -> str:
    """Normalise a provider path: forward slashes, no '..', no absolute paths."""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"path escapes the storage root: {path!r}")
    return "/".join(parts)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() in ("1", "true", "yes")
