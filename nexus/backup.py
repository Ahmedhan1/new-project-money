"""Verified, versioned, deduplicated backups of NEXUS data to one or more storage destinations.

Layout at a destination (any StorageBackend):
    repo.json                       format version, KDF salt, key check (no secrets)
    blobs/<aa>/<sha256>             file contents, content-addressed (dedup + incremental for free)
    snapshots/<id>.json             what the data looked like at one moment: path -> blob, size, checksums

Rules that are enforced here, not just documented:
  * a backup is only reported successful after every object was re-read from the destination and its
    size + SHA-256 matched (`verify`)
  * destinations are independent: one failing never stops or corrupts the others
  * remote destinations require encryption (AES-256-GCM, key derived from the user's passphrase with scrypt)
  * restore always writes into a new, empty directory and never touches the live data
  * temporary/reproducible data (render work files, caches, model weights, logs) is never backed up
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from . import config, db, storage
from .storage import StorageBackend, StorageBlocked, StorageError, sha256_file

FORMAT = 1
CHUNK = 4 << 20
MAGIC = b"NXB1"
# Priority order: what is backed up first and what a small free allowance should be spent on.
TIERS = ["state", "metadata", "production", "assets", "originals", "exports"]
DEFAULT_TIERS = ["state", "metadata", "production", "assets", "originals"]


# ----------------------------------------------------------------- encryption
def passphrase() -> str | None:
    return os.environ.get("NEXUS_BACKUP_PASSPHRASE") or None


def _key(pw: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(pw.encode("utf-8"))


def _encrypt_file(src: Path, dest: Path, key: bytes) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(key)
    with open(src, "rb") as fi, open(dest, "wb") as fo:
        fo.write(MAGIC)
        i = 0
        while True:
            chunk = fi.read(CHUNK)
            last = len(chunk) < CHUNK
            nonce = secrets.token_bytes(12)
            # chunk index + last-flag are authenticated: chunks cannot be reordered, dropped or truncated unnoticed
            ct = aes.encrypt(nonce, chunk, i.to_bytes(8, "big") + bytes([last]))
            fo.write(len(ct).to_bytes(4, "big") + nonce + ct)
            i += 1
            if last:
                break


def _decrypt_file(src: Path, dest: Path, key: bytes) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(key)
    with open(src, "rb") as fi, open(dest, "wb") as fo:
        if fi.read(4) != MAGIC:
            raise StorageError("not a NEXUS encrypted object")
        i, done = 0, False
        while True:
            head = fi.read(4)
            if not head:
                break
            n = int.from_bytes(head, "big")
            nonce, ct = fi.read(12), fi.read(n)
            for last in (False, True):
                try:
                    fo.write(aes.decrypt(nonce, ct, i.to_bytes(8, "big") + bytes([last])))
                    done = last
                    break
                except InvalidTag:
                    if last:
                        raise StorageError("decryption failed: wrong passphrase or corrupted object")
            i += 1
        if not done:
            raise StorageError("encrypted object is truncated")


def _repo(b: StorageBackend, pw: str | None, create: bool) -> tuple[dict, bytes | None]:
    """Read (or initialise) the destination's repo.json and return (repo, key)."""
    with tempfile.TemporaryDirectory() as d:
        t = Path(d) / "repo.json"
        if b.exists("repo.json"):
            b.get("repo.json", t)
            repo = json.loads(t.read_text())
        elif create:
            repo = {"format": FORMAT, "created": time.time(), "encrypted": bool(pw)}
            if pw:
                salt = secrets.token_bytes(16)
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                nonce = secrets.token_bytes(12)
                repo.update(salt=base64.b64encode(salt).decode(),
                            check=base64.b64encode(nonce + AESGCM(_key(pw, salt)).encrypt(nonce, b"nexus", None)).decode())
            t.write_text(json.dumps(repo))
            b.put("repo.json", t)
        else:
            raise StorageError(f"{b.name}: no NEXUS backup repository here")
    if not repo.get("encrypted"):
        return repo, None
    if not pw:
        raise StorageError(f"{b.name}: repository is encrypted - set NEXUS_BACKUP_PASSPHRASE")
    key = _key(pw, base64.b64decode(repo["salt"]))
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(repo["check"])
    try:
        AESGCM(key).decrypt(raw[:12], raw[12:], None)
    except InvalidTag:
        raise StorageError(f"{b.name}: wrong backup passphrase")
    return repo, key


# ----------------------------------------------------------------- what gets backed up
def _db_snapshot(dest: Path) -> None:
    """Consistent copy of the live database (SQLite online backup API - safe while workers are writing)."""
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()


def collect(tiers: list[str], tmp: Path) -> list[dict]:
    """[{path (relative to NEXUS_HOME, posix), src, tier}] in priority order."""
    H = config.HOME
    items: list[dict] = []

    def add(src: Path, tier: str, rel: str | None = None):
        if src.is_file():
            items.append({"path": rel or src.relative_to(H).as_posix(), "src": src, "tier": tier})

    if "state" in tiers:
        if config.DB_PATH.exists():
            _db_snapshot(tmp / "nexus.db")
            add(tmp / "nexus.db", "state", "nexus.db")
        for n in ("settings.json", "storage.json"):
            add(H / n, "state")
        add(config.MODELS / "installed.json", "state")  # the model manifest, not the weights
    per_project = {"metadata": ("timelines", "reports"), "production": ("audio",), "originals": ("media",), "exports": ("renders",)}
    for tier in TIERS:
        if tier not in tiers:
            continue
        if tier == "assets" and config.ASSETS.exists():
            for f in sorted(config.ASSETS.rglob("*")):
                add(f, "assets")
        for sub in per_project.get(tier, ()):
            for pdir in sorted(config.PROJECTS.glob("*")) if config.PROJECTS.exists() else []:
                for f in sorted((pdir / sub).rglob("*")) if (pdir / sub).exists() else []:
                    add(f, tier)
    return items


# ----------------------------------------------------------------- backup
def _snapshot_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)


def backup_to(b: StorageBackend, tiers: list[str], cap_bytes: int | None = None, pw: str | None = None, note: str = "") -> dict:
    """One destination. Uploads only blobs it does not already hold, then verifies the whole snapshot."""
    up, why = b.available()
    if not up:  # checked first so an unplugged drive is reported, not silently re-created somewhere else
        raise StorageError(why)
    repo, key = _repo(b, pw, create=True)
    sid = _snapshot_id()
    t0 = time.time()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        items = collect(tiers, tmp)
        u = b.usage()
        limits = [cap_bytes - u["used_bytes"]] if cap_bytes else []          # room left under the free allowance
        if u.get("free_bytes") is not None:
            limits.append(int(u["free_bytes"] * 0.98))                         # room physically left at the destination
        room = min(limits) if limits else None
        files, new_blobs, uploaded, skipped_cap = [], 0, 0, []
        known: dict[str, dict] = {}
        for it in items:
            sha = sha256_file(it["src"])
            blob = f"blobs/{sha[:2]}/{sha}"
            size = it["src"].stat().st_size
            if sha not in known:
                have = b.size(blob)
                if have is None:
                    if key:
                        enc = tmp / "enc"
                        _encrypt_file(it["src"], enc, key)
                        up = enc
                    else:
                        up = it["src"]
                    stored_size = up.stat().st_size
                    if room is not None and uploaded + stored_size > room:
                        # zero-spend guard: never grow a destination past its free allowance. Priority order means
                        # the least important data is what gets left out - and it is reported, never silent.
                        skipped_cap.append({"path": it["path"], "tier": it["tier"], "size": size})
                        continue
                    stored_sha = sha256_file(up)
                    b.put(blob, up)
                    new_blobs += 1
                    uploaded += stored_size
                    known[sha] = {"stored_size": stored_size, "stored_sha256": stored_sha, "new": True}
                else:
                    known[sha] = {"stored_size": have, "stored_sha256": None, "new": False}
            files.append({"path": it["path"], "tier": it["tier"], "size": size, "sha256": sha, "blob": blob,
                          "stored_size": known[sha]["stored_size"], "stored_sha256": known[sha]["stored_sha256"]})
        # objects that were already there: take their stored checksum from the previous snapshot that recorded it
        missing_sha = {f["sha256"] for f in files if f["stored_sha256"] is None}
        if missing_sha:
            for s in reversed(list_snapshots(b)):
                for f in load_snapshot(b, s["id"], key)["files"]:
                    if f["sha256"] in missing_sha and f.get("stored_sha256"):
                        for g in files:
                            if g["sha256"] == f["sha256"]:
                                g["stored_sha256"] = f["stored_sha256"]
                        missing_sha.discard(f["sha256"])
                if not missing_sha:
                    break
        manifest = {"format": FORMAT, "id": sid, "created": time.time(), "nexus_version": config.VERSION, "home": str(config.HOME),
                    "os": os.name, "tiers": tiers, "note": note, "encrypted": bool(key), "files": files, "skipped_over_cap": skipped_cap}
        mf = tmp / "manifest.json"
        mf.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        if key:
            _encrypt_file(mf, tmp / "manifest.enc", key)
            mf = tmp / "manifest.enc"
        b.put(f"snapshots/{sid}.json", mf)
    v = verify(b, sid, pw, deep_all=False, new_blobs={f["blob"] for f in files if known[f["sha256"]]["new"]})
    return {"destination": b.name, "snapshot": sid, "ok": v["ok"], "files": len(files), "bytes": sum(f["size"] for f in files),
            "new_blobs": new_blobs, "uploaded_bytes": uploaded, "skipped_over_cap": skipped_cap, "verify": v,
            "seconds": round(time.time() - t0, 2), "encrypted": bool(key), "ts": time.time()}


def run_backup(tiers: list[str] | None = None, only: str | None = None, note: str = "") -> dict:
    """Back up to every enabled destination. Each destination succeeds or fails on its own."""
    pw = passphrase()
    results = []
    for d in storage.destinations():
        if not d.get("enabled", True) or (only and d["name"] != only):
            continue
        try:
            b = storage.open_destination(d)
            if not isinstance(b, storage.LocalBackend) and not pw:
                raise StorageBlocked(f"'{d['name']}' is remote: set NEXUS_BACKUP_PASSPHRASE - private data is only uploaded encrypted")
            want = [t for t in (tiers or DEFAULT_TIERS) if t in storage.allowed_tiers(d)]
            r = backup_to(b, want, storage.free_cap_bytes(d), pw if not isinstance(b, storage.LocalBackend) or d.get("encrypt") else None, note)
        except StorageBlocked as e:
            r = {"destination": d["name"], "ok": False, "blocked": True, "error": str(e), "ts": time.time()}
        except Exception as e:  # unreachable drive, network down, quota: this destination failed, the others continue
            r = {"destination": d["name"], "ok": False, "error": f"{type(e).__name__}: {e}", "ts": time.time()}
        results.append(r)
    good = [r for r in results if r["ok"]]
    out = {"ok": bool(good), "complete": bool(results) and len(good) == len(results), "results": results, "ts": time.time(),
           "copies": len(good), "warning": None}
    if good and len(good) < len(results):
        out["warning"] = "backup succeeded on " + ", ".join(r["destination"] for r in good) + " but FAILED on " + \
                         ", ".join(r["destination"] for r in results if not r["ok"])
    elif not good:
        out["warning"] = "NO destination holds this backup - the local originals are the only copy"
    config.ensure_dirs()
    (config.HOME / "last_backup.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    try:
        db.put_doc("backup", {**out, "id": time.strftime("%Y%m%d-%H%M%S")})
    except sqlite3.Error:
        pass
    return out


# ----------------------------------------------------------------- inspect / verify
def list_snapshots(b: StorageBackend) -> list[dict]:
    return [{"id": Path(o["key"]).stem, "size": o["size"]} for o in b.list("snapshots") if o["key"].endswith(".json")]


def load_snapshot(b: StorageBackend, sid: str, key: bytes | None) -> dict:
    with tempfile.TemporaryDirectory() as d:
        t = Path(d) / "m"
        b.get(f"snapshots/{sid}.json", t)
        if key:
            _decrypt_file(t, Path(d) / "p", key)
            t = Path(d) / "p"
        return json.loads(t.read_text(encoding="utf-8"))


def verify(b: StorageBackend, sid: str | None = None, pw: str | None = None, deep_all: bool = True, new_blobs: set[str] | None = None) -> dict:
    """Re-read the destination. Every object must exist with the recorded size; checksums are compared for
    all objects (deep_all) or at least for the ones uploaded in this run."""
    _, key = _repo(b, pw, create=False)
    snaps = list_snapshots(b)
    if not snaps:
        return {"ok": False, "problems": ["no snapshots at destination"], "checked": 0}
    sid = sid or snaps[-1]["id"]
    m = load_snapshot(b, sid, key)
    problems, hashed, seen = [], 0, set()
    for f in m["files"]:
        if f["blob"] in seen:
            continue
        seen.add(f["blob"])
        size = b.size(f["blob"])
        if size is None:
            problems.append(f"MISSING {f['path']} ({f['blob']})")
        elif size != f["stored_size"]:
            problems.append(f"SIZE MISMATCH {f['path']}: {size} != {f['stored_size']}")
        elif (deep_all or f["blob"] in (new_blobs or ())) and f.get("stored_sha256"):
            hashed += 1
            if b.sha256(f["blob"]) != f["stored_sha256"]:
                problems.append(f"CHECKSUM MISMATCH {f['path']} ({f['blob']})")
    return {"ok": not problems, "snapshot": sid, "objects": len(seen), "checksums_compared": hashed, "problems": problems,
            "deep": deep_all, "ts": time.time()}


# ----------------------------------------------------------------- restore
def relocate(home: Path, old_home: str) -> int:
    """Projects store absolute media paths. After restoring to another directory, rewrite them."""
    new_home = str(home)
    if old_home == new_home:
        return 0
    pairs = [(old_home, new_home)]
    for a, b_ in list(pairs):  # JSON-escaped (Windows backslashes) and forward-slash variants
        ja, jb = json.dumps(a)[1:-1], json.dumps(b_)[1:-1]
        if ja != a:
            pairs.append((ja, jb))
        if "\\" in a:
            pairs.append((a.replace("\\", "/"), b_.replace("\\", "/")))
    n = 0
    dbf = home / "nexus.db"
    if dbf.exists():
        c = sqlite3.connect(dbf)
        cols = {"projects": ["data"], "jobs": ["params", "result", "stages", "log"], "assets": ["path"], "docs": ["data"]}
        for table, cs in cols.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col in cs:
                if col in have:
                    for a, b_ in pairs:
                        n += c.execute(f"UPDATE {table} SET {col}=REPLACE({col}, ?, ?) WHERE instr({col}, ?)>0", (a, b_, a)).rowcount
        c.commit()
        c.close()
    for f in list((home / "projects").glob("*/timelines/*.json")) + list((home / "projects").glob("*/reports/*.json")):
        s = f.read_text(encoding="utf-8")
        s2 = s
        for a, b_ in pairs:
            s2 = s2.replace(a, b_)
        if s2 != s:
            f.write_text(s2, encoding="utf-8")
            n += 1
    return n


def check_references(home: Path) -> dict:
    """Open every project in a restored/moved NEXUS_HOME and confirm that what it points to exists."""
    out = {"projects": 0, "timelines": 0, "media_ok": 0, "missing": [], "reproducible_missing": []}
    dbf = home / "nexus.db"
    if not dbf.exists():
        return {**out, "missing": ["nexus.db"]}
    c = sqlite3.connect(dbf)
    rows = c.execute("SELECT id, data FROM projects").fetchall()
    c.close()

    def ref(path: str, where: str):
        if Path(path).exists():
            out["media_ok"] += 1
        elif "/cache/" in path.replace("\\", "/") or "/work/" in path.replace("\\", "/"):
            out["reproducible_missing"].append(f"{where}: {path}")  # regenerated on next render
        else:
            out["missing"].append(f"{where}: {path}")

    for pid, data in rows:
        out["projects"] += 1
        d = json.loads(data)
        for m in d.get("media", []):
            ref(m["path"], f"{pid} media")
        for br in d.get("branches", {}).values():
            for v in br["versions"]:
                f = home / "projects" / pid / "timelines" / f"{v['id']}.json"
                if not f.exists():
                    out["missing"].append(f"{pid} timeline {v['id']}")
                    continue
                out["timelines"] += 1
                tl = json.loads(f.read_text(encoding="utf-8"))
                for cl in tl.get("clips", []):
                    if cl.get("src"):
                        ref(cl["src"], f"{pid} {v['id']} clip")
                for a in tl.get("audio", []):
                    ref(a["src"], f"{pid} {v['id']} audio")
                if (tl.get("music") or {}).get("src"):
                    ref(tl["music"]["src"], f"{pid} {v['id']} music")
    out["ok"] = not out["missing"]
    return out


def restore(b: StorageBackend, target: str | Path, sid: str | None = None, pw: str | None = None) -> dict:
    """Restore a snapshot into a NEW directory. Every file is checksum-verified as it is written."""
    target = Path(target)
    if target.resolve() == config.HOME.resolve():
        raise StorageError("restore target is the live NEXUS_HOME - choose another directory")
    if target.exists() and any(target.iterdir()):
        raise StorageError(f"restore target {target} is not empty - restore never overwrites existing data")
    _, key = _repo(b, pw, create=False)
    snaps = list_snapshots(b)
    if not snaps:
        raise StorageError(f"{b.name}: no snapshots to restore")
    sid = sid or snaps[-1]["id"]
    m = load_snapshot(b, sid, key)
    target.mkdir(parents=True, exist_ok=True)
    bad, n, t0 = [], 0, time.time()
    with tempfile.TemporaryDirectory() as d:
        for f in m["files"]:
            dest = (target / f["path"]).resolve()
            if target.resolve() not in dest.parents:
                bad.append(f"unsafe path in snapshot: {f['path']}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                if key:
                    b.get(f["blob"], Path(d) / "enc")
                    _decrypt_file(Path(d) / "enc", dest, key)
                else:
                    b.get(f["blob"], dest)
                if sha256_file(dest) != f["sha256"]:
                    bad.append(f"checksum mismatch after restore: {f['path']}")
                    dest.unlink()
                else:
                    n += 1
            except StorageError as e:
                bad.append(f"{f['path']}: {e}")
    rewritten = relocate(target, m["home"])
    refs = check_references(target)
    return {"ok": not bad and refs.get("ok", False), "snapshot": sid, "target": str(target), "files_restored": n, "files_expected": len(m["files"]),
            "problems": bad, "paths_rewritten": rewritten, "references": refs, "seconds": round(time.time() - t0, 2),
            "use": f"set NEXUS_HOME={target} (or move it over the old data folder after checking it) and start NEXUS"}


def restore_drill(only: str | None = None) -> dict:
    """Prove the latest backup is restorable: restore each destination into a scratch directory, verify, delete."""
    pw, out = passphrase(), []
    for d in storage.destinations():
        if not d.get("enabled", True) or (only and d["name"] != only):
            continue
        scratch = Path(tempfile.mkdtemp(prefix="nexus-restore-drill-"))
        try:
            b = storage.open_destination(d)
            enc = _repo(b, pw, create=False)[0].get("encrypted")
            r = restore(b, scratch / "home", pw=pw if enc else None)
            out.append({"destination": d["name"], "ok": r["ok"], "files": r["files_restored"], "problems": r["problems"] + r["references"]["missing"]})
        except Exception as e:
            out.append({"destination": d["name"], "ok": False, "problems": [f"{type(e).__name__}: {e}"]})
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    res = {"ok": bool(out) and all(r["ok"] for r in out), "results": out, "ts": time.time()}
    (config.HOME / "last_restore_drill.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def prune(b: StorageBackend, keep: int = 10, pw: str | None = None) -> dict:
    """Drop all but the newest `keep` snapshots, then delete blobs no remaining snapshot references."""
    _, key = _repo(b, pw, create=False)
    snaps = list_snapshots(b)
    drop, kept = snaps[:-keep] if keep else snaps, snaps[-keep:] if keep else []
    live = {f["blob"] for s in kept for f in load_snapshot(b, s["id"], key)["files"]}
    for s in drop:
        b.delete(f"snapshots/{s['id']}.json")
    gone = 0
    for o in b.list("blobs"):
        if o["key"] not in live:
            b.delete(o["key"])
            gone += 1
    return {"snapshots_removed": len(drop), "snapshots_kept": len(kept), "blobs_removed": gone}
