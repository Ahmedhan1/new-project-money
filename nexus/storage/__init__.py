"""NEXUS Federated Storage: one small interface, interchangeable backends.

The rest of NEXUS talks to `StorageBackend` only. Destinations are configured by the user in
NEXUS_HOME/storage.json (never committed); provider facts and the zero-spend eligibility rules live in
registry/storage.yaml. A remote destination is used only when (a) its provider cannot bill, (b) the upload
fits inside the free allowance, and (c) the user has explicitly authorised private uploads to it.
"""
from __future__ import annotations

import functools
import hashlib
import json
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

from .. import config


class StorageError(RuntimeError):
    pass


class StorageBlocked(StorageError):
    """Refused by the zero-spend / privacy policy (not a technical failure)."""


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class StorageBackend(ABC):
    name = "backend"

    @abstractmethod
    def put(self, key: str, src: Path) -> None: ...
    @abstractmethod
    def get(self, key: str, dest: Path) -> None: ...
    @abstractmethod
    def size(self, key: str) -> int | None:
        """Size in bytes, or None when the object does not exist."""
    @abstractmethod
    def list(self, prefix: str = "") -> list[dict]:
        """[{key, size}] for every object under prefix."""
    @abstractmethod
    def delete(self, key: str) -> None: ...
    @abstractmethod
    def usage(self) -> dict:
        """{used_bytes, free_bytes|None}"""

    def exists(self, key: str) -> bool:
        return self.size(key) is not None

    def sha256(self, key: str) -> str:
        """Checksum of the stored object. Default: download and hash (always correct, any backend)."""
        with tempfile.TemporaryDirectory() as d:
            t = Path(d) / "obj"
            self.get(key, t)
            return sha256_file(t)

    def available(self) -> tuple[bool, str]:
        try:
            self.usage()
            return True, "ok"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


class LocalBackend(StorageBackend):
    """A directory: another disk, an external drive, a NAS mount."""

    def __init__(self, root: str | Path, name: str = "local"):
        self.root, self.name = Path(root), name

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise StorageError(f"key escapes the storage root: {key}")
        return p

    def put(self, key: str, src: Path) -> None:
        dest = self._p(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)  # atomic: a crash never leaves a half-written object under its final name

    def get(self, key: str, dest: Path) -> None:
        src = self._p(key)
        if not src.is_file():
            raise StorageError(f"{self.name}: missing object {key}")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)

    def size(self, key: str) -> int | None:
        p = self._p(key)
        return p.stat().st_size if p.is_file() else None

    def sha256(self, key: str) -> str:
        return sha256_file(self._p(key))

    def list(self, prefix: str = "") -> list[dict]:
        base = self.root / prefix
        if not base.exists():
            return []
        return [{"key": f.relative_to(self.root).as_posix(), "size": f.stat().st_size}
                for f in sorted(base.rglob("*")) if f.is_file() and not f.name.endswith(".part")]

    def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)

    def usage(self) -> dict:
        if not self.root.exists():
            if not self.root.parent.exists():
                raise StorageError(f"{self.name}: {self.root} is not reachable (drive unplugged / share offline?)")
            self.root.mkdir(parents=True, exist_ok=True)
        used = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        return {"used_bytes": used, "free_bytes": shutil.disk_usage(self.root).free}


class RcloneBackend(StorageBackend):
    """Any of rclone's backends (B2, S3-compatible, Drive, OneDrive, SFTP, ...). Credentials stay in rclone's
    own config, created by the user with `rclone config`; NEXUS never sees or stores them. rclone provides
    multipart upload, resume and retries."""

    def __init__(self, remote: str, name: str = "rclone", binary: str = "rclone"):
        self.remote, self.name, self.bin = remote.rstrip("/"), name, binary

    def _run(self, *args: str, timeout: int = 3600) -> str:
        if not shutil.which(self.bin):
            raise StorageError("rclone is not installed (free, MIT licence: https://rclone.org/install/)")
        r = subprocess.run([self.bin, *args, "--retries", "3", "--low-level-retries", "5"], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise StorageError(f"{self.name}: rclone {args[0]} failed: {r.stderr.strip()[-400:]}")
        return r.stdout

    def _k(self, key: str) -> str:
        return f"{self.remote}/{key}"

    def put(self, key: str, src: Path) -> None:
        self._run("copyto", str(src), self._k(key))

    def get(self, key: str, dest: Path) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        self._run("copyto", self._k(key), str(dest))
        if not Path(dest).is_file():
            raise StorageError(f"{self.name}: missing object {key}")

    def size(self, key: str) -> int | None:
        try:
            rows = json.loads(self._run("lsjson", self._k(key)) or "[]")
        except StorageError as e:
            if "not found" in str(e).lower():
                return None
            raise
        return rows[0]["Size"] if rows and not rows[0].get("IsDir") else None

    def list(self, prefix: str = "") -> list[dict]:
        try:
            rows = json.loads(self._run("lsjson", "-R", "--files-only", self._k(prefix) if prefix else self.remote) or "[]")
        except StorageError as e:
            if "not found" in str(e).lower():
                return []
            raise
        pre = prefix.rstrip("/") + "/" if prefix else ""
        return sorted(({"key": pre + r["Path"], "size": r["Size"]} for r in rows), key=lambda r: r["key"])

    def delete(self, key: str) -> None:
        self._run("deletefile", self._k(key))

    def usage(self) -> dict:
        """used_bytes = what NEXUS stores under this remote; free_bytes = what the account has left (when the provider reports it)."""
        try:
            used = json.loads(self._run("size", "--json", self.remote, timeout=600)).get("bytes", 0)
        except StorageError as e:
            if "not found" not in str(e).lower():
                raise
            used = 0
        free = None
        try:
            root = self.remote.split(":")[0] + ":" if ":" in self.remote and not Path(self.remote).is_absolute() else self.remote
            free = json.loads(self._run("about", "--json", root, timeout=120)).get("free")
        except (StorageError, ValueError):
            pass  # backend without `about` (B2, S3)
        return {"used_bytes": used, "free_bytes": free}


# ----------------------------------------------------------------- destinations + zero-spend policy
@functools.lru_cache(maxsize=1)
def providers() -> dict[str, dict]:
    return yaml.safe_load((config.REGISTRY_DIR / "storage.yaml").read_text(encoding="utf-8"))["providers"]


def eligibility(provider: str) -> tuple[bool, str]:
    p = providers().get(provider)
    if p is None:
        return False, f"unknown provider '{provider}' - add a verified profile to registry/storage.yaml first"
    if p["payment_method_required"]:
        return False, f"{p['name']} needs a payment method (zero-spend rule)"
    if p["billing_risk"] != "none":
        return False, f"{p['name']} can bill automatically ({p['billing_risk']})"
    if p["kind"] == "manual":
        return False, f"{p['name']}: {p['notes'].strip()}"
    return True, "eligible: no card, cannot bill"


def default_destinations() -> list[dict]:
    return [{"name": "local", "provider": "local", "root": str(config.BACKUPS), "enabled": True}]


def destinations() -> list[dict]:
    """User configuration (NEXUS_HOME/storage.json). Without one: a single local destination."""
    f = config.HOME / "storage.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))["destinations"]
    return default_destinations()


def save_destinations(dests: list[dict]) -> None:
    config.ensure_dirs()
    (config.HOME / "storage.json").write_text(json.dumps({"destinations": dests}, indent=1), encoding="utf-8")


def open_destination(d: dict) -> StorageBackend:
    """Build the backend for a destination, enforcing eligibility and explicit authorisation."""
    ok, why = eligibility(d["provider"])
    if not ok:
        raise StorageBlocked(f"destination '{d['name']}' refused: {why}")
    p = providers()[d["provider"]]
    if p["kind"] == "local":
        return LocalBackend(d["root"], d["name"])
    if not d.get("authorized"):
        raise StorageBlocked(f"destination '{d['name']}' is remote: set \"authorized\": true in storage.json to allow "
                             f"uploading private project data to {p['name']}")
    return RcloneBackend(d["remote"], d["name"])


def free_cap_bytes(d: dict) -> int | None:
    """Hard ceiling NEXUS keeps a destination under: 90% of the free allowance, or the user's lower limit."""
    p = providers()[d["provider"]]
    caps = [c for c in (p.get("free_gb") and p["free_gb"] * 0.9, d.get("limit_gb")) if c]
    return int(min(caps) * 1e9) if caps else None


def allowed_tiers(d: dict) -> list[str]:
    p = providers()[d["provider"]]
    return [t for t in d.get("tiers", p["allowed_content"]) if t in p["allowed_content"]]


def report() -> list[dict]:
    """Status of every configured destination + every known provider (for `nexus storage` and the UI)."""
    out = []
    for d in destinations():
        row = {"name": d["name"], "provider": d["provider"], "enabled": d.get("enabled", True), "tiers": None, "available": False,
               "detail": "", "used_gb": None, "cap_gb": None}
        try:
            b = open_destination(d)
            row["tiers"] = allowed_tiers(d)
            cap = free_cap_bytes(d)
            row["cap_gb"] = round(cap / 1e9, 2) if cap else None
            u = b.usage()
            row.update(available=True, detail="ok", used_gb=round(u["used_bytes"] / 1e9, 3))
        except StorageBlocked as e:
            row["detail"] = f"BLOCKED: {e}"
        except Exception as e:
            row["detail"] = f"UNAVAILABLE: {e}"
        out.append(row)
    return out
