"""Server-side secret vault (Account 1). Tokens never reach Git, notebooks, logs, the browser or any API response.

    put(name, value, meta=None)   store (encrypted with Fernet / AES-128-CBC + HMAC-SHA256)
    get(name) -> str | None        decrypt (server code only)
    meta(name) -> dict | None      non-secret metadata (identity, scopes, created) - safe to show the owner
    delete(name)                   remove (disconnect / revoke)
    names() -> list[str]

Storage: NEXUS_HOME/secrets/ (directory 0700, files 0600). The key comes from NEXUS_VAULT_KEY (a Fernet key, for
hosted deployments where the platform's secret store injects it) or is generated once into secrets/vault.key.
NEXUS_HOME is outside the repository and in .gitignore; `scan_repo()` is the no-secret-in-Git check.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,63}$")


def _dir() -> Path:
    d = config.HOME / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _key() -> bytes:
    env = os.environ.get("NEXUS_VAULT_KEY")
    if env:
        return env.encode()
    kf = _dir() / "vault.key"
    if not kf.exists():
        old = os.umask(0o077)
        try:
            kf.write_bytes(Fernet.generate_key())
        finally:
            os.umask(old)
    return kf.read_bytes().strip()


def _path(name: str) -> Path:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid secret name {name!r}")
    return _dir() / f"{name}.secret"


def put(name: str, value: str, meta: dict | None = None) -> None:
    if not value:
        raise ValueError("empty secret")
    blob = {"v": Fernet(_key()).encrypt(value.encode()).decode(), "meta": {**(meta or {}), "stored_at": time.time()}}
    p = _path(name)
    old = os.umask(0o077)
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob), encoding="utf-8")
        os.replace(tmp, p)
    finally:
        os.umask(old)


def get(name: str) -> str | None:
    p = _path(name)
    if not p.exists():
        return None
    try:
        return Fernet(_key()).decrypt(json.loads(p.read_text(encoding="utf-8"))["v"].encode()).decode()
    except (InvalidToken, KeyError, ValueError):
        return None


def meta(name: str) -> dict | None:
    p = _path(name)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("meta") or {}


def update_meta(name: str, **kv) -> None:
    v = get(name)
    if v is not None:
        put(name, v, {**(meta(name) or {}), **kv})


def delete(name: str) -> bool:
    p = _path(name)
    if p.exists():
        p.unlink()
        return True
    return False


def names() -> list[str]:
    return sorted(p.stem for p in _dir().glob("*.secret"))


def redact(text: str) -> str:
    """Remove every stored secret value (and token-shaped strings) from text before it is logged or returned."""
    out = text or ""
    for n in names():
        v = get(n)
        if v and len(v) >= 8:
            out = out.replace(v, f"[secret:{n}]")
    return TOKEN_RE.sub("[redacted-token]", out)


# ---------------------------------------------------------------- no-secret-in-Git scan
TOKEN_RE = re.compile(r"(KGAT_[0-9a-f]{20,}|ya29\.[0-9A-Za-z_\-]{20,}|1//0[0-9A-Za-z_\-]{30,}|GOCSPX-[0-9A-Za-z_\-]{20,}|"
                      r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{32,}|"
                      r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----|xox[bp]-[A-Za-z0-9-]{20,}|nxw_[A-Za-z0-9_-]{40,})")


def scan_repo(root: str | Path, history: bool = False) -> list[dict]:
    """Token-shaped strings in tracked files (and, with history=True, in every committed blob)."""
    root = Path(root)
    hits = []
    files = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True).stdout.split("\0")
    for f in (x for x in files if x):
        p = root / f
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in TOKEN_RE.finditer(text):
            if "test" in f and "EXAMPLE" in text[max(0, m.start() - 80):m.end() + 80]:
                continue
            hits.append({"file": f, "match": m.group(0)[:8] + "...", "where": "worktree"})
    if history:
        log = subprocess.run(["git", "log", "-p", "--all", "--no-color"], cwd=root, capture_output=True, text=True,
                             errors="ignore").stdout
        for m in TOKEN_RE.finditer(log):
            hits.append({"file": "(history)", "match": m.group(0)[:8] + "...", "where": "history"})
    return hits
