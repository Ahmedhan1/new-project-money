"""Daily production output archive: final videos live permanently in external storage; local copies are released only
after the external copy was verified byte-for-byte.

Flow for one finished render (`archive_output`):
    1. collect the deliverable: final video (+ captions), a thumbnail, the QA and quality reports, and an archive
       manifest (project -> outputs, SHA-256 of every file, where each copy lives, how to recover it);
    2. upload each file through ACCOUNT 1's storage layer (nexus/cloud/storage/artifacts.py: resumable, crash-safe,
       deduplicated by SHA-256) - Account 2 does not route storage itself;
    3. verify each upload against the provider's own metadata (sha256 / md5 reported by the provider), or by downloading
       the copy and hashing it when the provider reports no checksum;
    4. record the archive reference in the project (project.data["archive"]) and in the ledger (db docs "a2_archive");
    5. only then, and only when the copy is on another disk/provider, delete the local final video (`release`), after
       re-verifying the remote copy and checking that the local bytes are the bytes that were archived.

States: queued -> uploading -> uploaded -> verified -> released;  failed (retry with backoff; the local file is kept).
Crash safety: the record is written as `uploading` with a deterministic transfer id BEFORE any byte is sent; after a
restart `recover()` first asks the destination whether those bytes are already there (by SHA-256), then resumes the
same transfer, so a crash never produces a second archive entry. Nothing in this module deletes an archived copy:
remote retention is not implemented and the setting is disabled (docs/DAILY_OUTPUT_ARCHIVE.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Protocol

from . import config, db

KIND = "a2_archive"
DEFAULTS = {"connection_id": None, "auto_archive": True, "release_local_after_verify": True,
            "max_attempts": 8, "retention": {"enabled": False, "note": "remote retention is not implemented; archived "
                                                                      "daily videos are kept until the owner deletes them"}}
VIDEO_KINDS = ("master", "short")


class ArchiveError(RuntimeError):
    pass


class ArchiveUnavailable(ArchiveError):
    """No external storage layer (Account 1) is installed or configured: outputs stay local, nothing is deleted."""


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


# ------------------------------------------------------------------ settings
def settings() -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    f = config.HOME / "archive" / "settings.json"
    if f.exists():
        try:
            s.update({k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items() if k in DEFAULTS and k != "retention"})
        except ValueError:
            pass
    return s


def save_settings(new: dict) -> dict:
    if "retention" in new and (new["retention"] or {}).get("enabled"):
        raise ArchiveError("remote retention (automatic deletion of archived videos) is not available; it needs a separate "
                           "owner-approved change")
    if "allow_same_disk" in new:
        raise ArchiveError("there is no same-disk override: a local final is released only when its archive copy is on "
                           "another disk or provider")
    bad = [k for k in new if k not in DEFAULTS or k == "retention"]
    if bad:
        raise ArchiveError(f"unknown archive settings: {bad}")
    f = config.HOME / "archive" / "settings.json"
    cur = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    cur.update(new)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    return settings()


# ------------------------------------------------------------------ destinations
class Target(Protocol):
    name: str

    def external(self, local: Path | None = None) -> tuple[bool, str]:
        """True only when the archive copy does not live on the same device as `local` (or NEXUS_HOME)."""
        ...
    def store(self, path: Path, *, kind: str, name: str, project_id: str | None, transfer_id: str, mime: str,
              provenance: dict) -> dict: ...
    def resume(self, transfer_id: str, *, kind: str, name: str, project_id: str | None) -> dict | None: ...
    def find(self, sha256: str) -> dict | None: ...
    def verify(self, ref: dict, sha256: str, local: Path | None = None) -> dict: ...
    def fetch(self, ref: dict, dest: Path) -> Path: ...
    def usage(self) -> dict: ...


class Account1Target:
    """Account 1's storage layer (`nexus.cloud.storage`). Upload, resume, dedupe and fetch are Account 1's; Account 2
    adds a stricter verification (a positive checksum match is required before a local copy may be released)."""

    name = "account1"

    def __init__(self, connection_id: str | None = None):
        try:
            from .cloud.storage import artifacts, transfers  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise ArchiveUnavailable(f"Account 1 storage layer is not installed ({type(e).__name__}: {e})") from e
        self.cid = connection_id

    def _a(self):
        from .cloud.storage import artifacts
        return artifacts

    def _ref(self, m, cid: str | None = None) -> dict:
        los = [lo for lo in m.locations if not cid or lo.connection_id == cid] or list(m.locations)
        lo = max(los, key=lambda x: x.verified_at or 0)
        return {"artifact_id": m.artifact_id, "connection_id": lo.connection_id, "provider": lo.provider, "ref": lo.ref,
                "path": lo.path, "size": m.size, "sha256": m.sha256}

    def _cid(self) -> str:
        return self.cid or (self._a().rules().get("order") or ["local"])[0]

    def external(self, local: Path | None = None) -> tuple[bool, str]:
        from .cloud.storage import connections, get_provider
        cid = self._cid()
        kind = connections.provider_of(cid)
        if kind != "local":
            return True, f"{cid} ({kind})"
        try:  # the provider's REAL root (Account 1 honours NEXUS_LOCAL_STORAGE), not an assumed path
            root = Path(get_provider(cid).root)
        except Exception as e:  # noqa: BLE001 - unknown root: never treat as external
            return False, f"{cid}: LocalProvider root unknown ({type(e).__name__}: {e})"
        return _local_root_external(cid, root, local)

    def store(self, path, *, kind, name, project_id, transfer_id, mime, provenance):
        a = self._a()
        a1kind = kind if kind in ("master", "short", "thumbnail", "subtitle", "project_file") else "other"
        m = a.store_file(path, kind=a1kind, name=name, connection_id=self.cid, mime=mime, project_id=project_id,
                         provenance=provenance, retention="archive", transfer_id=transfer_id)
        return self._ref(m, self.cid)

    def resume(self, transfer_id, *, kind, name, project_id):
        from .cloud.storage import transfers
        t = transfers.get(transfer_id)
        if not t or t.get("status") in ("done", "completed", "verified"):
            return None
        m = self._a().resume_transfer(transfer_id, kind=kind if kind in ("master", "short", "thumbnail", "subtitle", "project_file") else "other",
                                      name=name, project_id=project_id)
        return self._ref(m, t["connection_id"])

    def find(self, sha256):
        a = self._a()
        m = a.find_by_sha(sha256)
        if not m:
            return None
        for lo in m.locations:
            if (not self.cid or lo.connection_id == self.cid) and a._location_valid(lo, sha256):
                return self._ref(m, lo.connection_id)
        return None

    def verify(self, ref, sha256, local=None):
        from .cloud.storage import get_provider
        p = get_provider(ref["connection_id"])
        f = p.stat(ref["ref"])
        if f is None:
            return {"ok": False, "method": "stat", "detail": "object not found at the destination"}
        if f.size is not None and int(f.size) != int(ref.get("size") or f.size):
            return {"ok": False, "method": "size", "detail": f"size {f.size} != {ref.get('size')}"}
        if f.sha256:
            return {"ok": f.sha256.lower() == sha256, "method": "provider_sha256", "detail": f.sha256.lower()}
        ck = p.checksum(ref["ref"]) or {}
        if ck.get("sha256") and ck.get("source") == "provider":
            return {"ok": ck["sha256"].lower() == sha256, "method": "provider_sha256", "detail": ck["sha256"]}
        md5 = f.md5 or ck.get("md5")
        if md5 and local is not None and Path(local).exists():
            return {"ok": md5.lower() == md5_file(local), "method": "provider_md5", "detail": md5}
        with tempfile.TemporaryDirectory(dir=_tmpdir()) as d:
            t = Path(d) / "copy"
            p.download(ref["ref"], t)
            got = sha256_file(t)
        return {"ok": got == sha256, "method": "download_sha256", "detail": got}

    def fetch(self, ref, dest):
        a = self._a()
        m = a.get(ref["artifact_id"])
        if m is None:
            raise ArchiveError(f"artifact {ref['artifact_id']} is not in Account 1's registry")
        return a.fetch_local(m, Path(dest))

    def usage(self):
        from .cloud.storage import get_provider
        cid = self._cid()
        q = get_provider(cid).quota()
        return {"connection_id": cid, "limit": q.limit, "used": q.used, "remaining": q.remaining, "source": q.source}


def _device(path: Path) -> int:
    """Device id of the nearest existing ancestor of `path` (tests replace this to simulate a second volume)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return os.stat(p).st_dev


def _local_root_external(cid: str, root: Path, local: Path | None) -> tuple[bool, str]:
    ref = Path(local) if local is not None else config.HOME
    rr, hr = root.resolve(), config.HOME.resolve()
    if rr == hr or hr in rr.parents:
        return False, f"{cid}: LocalProvider root {rr} is inside NEXUS_HOME"
    try:
        same = _device(rr) == _device(ref) or _device(rr) == _device(config.HOME)
    except OSError as e:
        return False, f"{cid}: cannot compare devices ({e})"
    if same:
        return False, f"{cid}: LocalProvider root {rr} is on the same disk as {ref}"
    return True, f"{cid}: LocalProvider root {rr} is on another disk"


def _tmpdir() -> str:
    d = config.HOME / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


_TEST_TARGET: Target | None = None


def use_target(t: Target | None) -> None:
    """Tests (and the mock adapter in docs/ACCOUNT2_HANDOFF_TO_ACCOUNT1.md) inject a destination here."""
    global _TEST_TARGET
    _TEST_TARGET = t


def target() -> Target:
    if _TEST_TARGET is not None:
        return _TEST_TARGET
    return Account1Target(settings()["connection_id"])


# ------------------------------------------------------------------ ledger
def _rid(project_id: str | None, sha: str, kind: str) -> str:
    return f"arc_{(project_id or 'shared')[-12:]}_{kind}_{sha[:16]}"


def records(project_id: str | None = None) -> list[dict]:
    rs = db.list_docs(KIND)
    return [r for r in rs if not project_id or r.get("project_id") == project_id]


def get(rid: str) -> dict | None:
    return db.get_doc(KIND, rid)


def _save(r: dict) -> dict:
    r["updated"] = time.time()
    db.put_doc(KIND, r, r["id"])
    return r


def _event(r: dict, msg: str) -> None:
    r.setdefault("events", []).append([round(time.time(), 3), msg])
    r["events"] = r["events"][-40:]


def enqueue(path: str | Path, *, kind: str, project_id: str | None, name: str | None = None, mime: str | None = None,
            source: str | None = None, deliverable: str | None = None, provenance: dict | None = None) -> dict:
    """Register a local file for archival (idempotent: the same bytes for the same project are one record)."""
    p = Path(path).resolve()
    sha, size = sha256_file(p), p.stat().st_size
    rid = _rid(project_id, sha, kind)
    r = get(rid)
    if r:
        if r["state"] == "released" and not Path(r["local_path"]).exists():
            return r
        r["local_path"] = str(p)
        return _save(r)
    r = {"id": rid, "project_id": project_id, "kind": kind, "name": name or p.name, "local_path": str(p),
         "source_path": source or str(p), "deliverable": deliverable, "size": size, "sha256": sha,
         "mime": mime or _mime(p), "state": "queued", "attempts": 0, "transfer_id": f"a2arc_{sha[:24]}",
         "remote": None, "verification": None, "created": time.time(), "provenance": provenance or {}}
    _event(r, "queued")
    return _save(r)


def _mime(p: Path) -> str:
    return {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".jpg": "image/jpeg",
            ".png": "image/png", ".json": "application/json", ".srt": "application/x-subrip", ".vtt": "text/vtt"}.get(
        p.suffix.lower(), "application/octet-stream")


def _backoff(n: int) -> float:
    return min(6 * 3600, 60 * 2 ** max(0, n - 1))


def upload(rid: str, t: Target | None = None) -> dict:
    """Upload + verify one record. Never deletes anything. Failure -> state failed with the reason and a retry time."""
    r = get(rid)
    if r is None:
        raise ArchiveError(f"unknown archive record {rid}")
    if r["state"] in ("verified", "released"):
        return r
    t = t or target()
    local = Path(r["local_path"])
    try:
        if r["state"] in ("queued", "failed"):
            if not local.exists():
                raise ArchiveError(f"local file is missing: {local}")
            if sha256_file(local) != r["sha256"]:
                raise ArchiveError("the local file changed after it was queued; queue it again")
            r["state"], r["attempts"] = "uploading", r["attempts"] + 1
            _event(r, f"uploading (attempt {r['attempts']}, transfer {r['transfer_id']})")
            _save(r)  # persisted BEFORE the first byte: a crash from here on is recovered by recover()
        ref = t.find(r["sha256"])
        if ref:
            _event(r, f"destination already has these bytes (artifact {ref['artifact_id']}): no second upload")
        else:
            ref = t.resume(r["transfer_id"], kind=r["kind"], name=r["name"], project_id=r["project_id"])
            if ref:
                _event(r, "resumed the interrupted transfer")
            else:
                if not local.exists():
                    raise ArchiveError(f"local file is missing: {local}")
                ref = t.store(local, kind=r["kind"], name=r["name"], project_id=r["project_id"], transfer_id=r["transfer_id"],
                              mime=r["mime"], provenance={"archive_record": r["id"], **r.get("provenance", {})})
        r["remote"], r["state"] = ref, "uploaded"
        _event(r, f"uploaded to {ref.get('connection_id')} {ref.get('path') or ref.get('ref')}")
        _save(r)
        v = t.verify(ref, r["sha256"], local if local.exists() else None)
        r["verification"] = {**v, "at": time.time()}
        if not v["ok"]:
            raise ArchiveError(f"verification failed ({v['method']}): {v['detail']}")
        r["state"], r["last_error"] = "verified", None
        _event(r, f"verified by {v['method']}")
        return _save(r)
    except Exception as e:  # noqa: BLE001 - every failure keeps the local file and is retried
        r = {**get(rid), **{k: r[k] for k in ("remote", "verification", "events", "attempts") if k in r}}
        r["state"], r["last_error"] = "failed", f"{type(e).__name__}: {e}"[:500]
        r["next_retry"] = time.time() + _backoff(r["attempts"])
        _event(r, f"failed: {r['last_error']} (local copy kept)")
        _save(r)
        if isinstance(e, ArchiveUnavailable):
            raise
        return r


def recover(t: Target | None = None, now: float | None = None) -> dict:
    """After a restart: finish uploads that were in flight, retry failed ones whose backoff elapsed."""
    now = now or time.time()
    done = []
    for r in records():
        if r["state"] in ("uploading", "uploaded") or (r["state"] == "failed" and r.get("next_retry", 0) <= now
                                                       and r["attempts"] < settings()["max_attempts"]):
            if r["state"] == "uploaded":
                r["state"] = "uploading"
                _save(r)
            done.append(upload(r["id"], t)["state"])
        elif r["state"] == "queued":
            done.append(upload(r["id"], t)["state"])
    return {"processed": len(done), "states": {s: done.count(s) for s in set(done)}}


def release(rid: str, t: Target | None = None) -> dict:
    """Delete the local copy of an archived final video. Refuses unless: state verified, the destination is external
    (always required: there is no override), the remote copy re-verifies NOW, and the local bytes equal the archived bytes."""
    r = get(rid)
    if r is None:
        raise ArchiveError(f"unknown archive record {rid}")
    if r["state"] == "released":
        return r
    if r["state"] != "verified":
        raise ArchiveError(f"{rid} is {r['state']}: only a verified external copy allows deleting the local file")
    if r["kind"] not in VIDEO_KINDS:
        raise ArchiveError("only final videos are released; thumbnails, reports and manifests stay local (small)")
    t = t or target()
    local = Path(r["local_path"])
    ext, why = t.external(local)
    if not ext:
        raise ArchiveError(f"the archive copy is not external ({why}); deleting the local copy would not free space and would "
                           "leave a single disk holding the video")
    v = t.verify(r["remote"], r["sha256"], local if local.exists() else None)
    if not v["ok"]:
        r["state"] = "failed"
        r["last_error"] = f"re-verification before release failed ({v['method']}): {v['detail']}"
        _event(r, r["last_error"] + " - local copy kept")
        _save(r)
        raise ArchiveError(r["last_error"])
    if local.exists():
        if sha256_file(local) != r["sha256"]:
            raise ArchiveError("the local file differs from the archived bytes; it is kept (archive it again)")
        _check_release_path(local, r)
        local.unlink()
    r["state"], r["released_at"] = "released", time.time()
    r["release_verification"] = {**v, "at": time.time()}
    _event(r, f"local copy deleted after re-verification ({v['method']}); the archive copy is the permanent one")
    _save(r)
    _update_project(r)
    try:  # intermediates of this render are no longer needed either (shared ones are kept)
        from . import diskman
        p = diskman.plan(["intermediate_after_archive"])
        if p["items"]:
            diskman.execute(p["plan_id"], ["intermediate_after_archive"])
    except Exception:  # noqa: BLE001
        pass
    return r


def _check_release_path(p: Path, r: dict) -> None:
    rp = p.resolve()
    roots = [(config.PROJECTS / (r.get("project_id") or "_none") / d).resolve() for d in ("renders", "exports")]
    if not any(x in rp.parents for x in roots):
        raise ArchiveError(f"refusing to delete {rp}: not a render or export of project {r.get('project_id')}")


def retrieve(rid: str, dest: str | Path | None = None, t: Target | None = None) -> Path:
    """Download an archived file back (verified by SHA-256). Default destination: its original place."""
    r = get(rid)
    if r is None or not r.get("remote"):
        raise ArchiveError(f"{rid} has no archive copy")
    dest = Path(dest or r["local_path"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from . import diskman
        diskman.preflight(r["size"], what=f"retrieving {r['name']}", path=dest.parent)
    except ImportError:
        pass
    tmp = dest.with_name(dest.name + ".part")
    (t or target()).fetch(r["remote"], tmp)
    if sha256_file(tmp) != r["sha256"]:
        tmp.unlink(missing_ok=True)
        raise ArchiveError("retrieved copy does not match the archived SHA-256")
    os.replace(tmp, dest)
    _event(r, f"retrieved to {dest}")
    if r["state"] == "released" and dest.resolve() == Path(r["local_path"]).resolve():
        r["state"] = "verified"  # a local copy exists again; it may be released again later
    _save(r)
    return dest


# ------------------------------------------------------------------ whole deliverable
def _thumbnail(video: Path, out: Path) -> Path | None:
    from .media import ff
    try:
        d = ff.duration(video)
        out.parent.mkdir(parents=True, exist_ok=True)
        ff.run(["-ss", f"{max(0.0, d * 0.1):.2f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(out)],
               op="archive-thumbnail")
        return out if out.exists() else None
    except Exception:  # noqa: BLE001
        return None


def archive_output(project_id: str, render: str, *, kind: str = "master", t: Target | None = None,
                   release_local: bool | None = None) -> dict:
    """Archive one finished render with everything needed to find, check and recover it later."""
    from . import project as prj
    p = prj.Project(project_id)
    src = (p.dir / render).resolve()
    if p.dir.resolve() not in src.parents or not src.is_file():
        raise ArchiveError(f"render not found in project {project_id}: {render}")
    t = t or target()
    stem = src.stem
    meta_dir = config.HOME / "archive" / project_id
    files: list[tuple[Path, str]] = [(src, kind)]
    for ext in (".srt", ".vtt"):
        if src.with_suffix(ext).exists():
            files.append((src.with_suffix(ext), "subtitle"))
    th = _thumbnail(src, meta_dir / f"{stem}.thumb.jpg")
    if th:
        files.append((th, "thumbnail"))
    for rep in (p.dir / "reports" / f"{stem}.qa.json", p.dir / "reports" / f"{stem}.quality.json"):
        if rep.exists():
            files.append((rep, "project_file"))
    recs = [enqueue(f, kind=k, project_id=project_id, source=str(src), deliverable=stem) for f, k in files]
    for r in recs:
        upload(r["id"], t)
    recs = [get(r["id"]) for r in recs]
    manifest = {
        "contract": "A2DailyArchive/v1", "project_id": project_id, "project_name": p.name, "deliverable": stem,
        "render": render, "created": time.time(),
        "files": [{"record": r["id"], "kind": r["kind"], "name": r["name"], "size": r["size"], "sha256": r["sha256"],
                   "state": r["state"], "remote": r["remote"], "verification": r.get("verification")} for r in recs],
        "recovery": {"how": "python -m nexus archive retrieve <record>  (or Storage page -> Archive -> Retrieve)",
                     "integrity": "every file is checked against its SHA-256 after download",
                     "timeline_versions": p.history(), "provider_notes": "Account 1 ArtifactManifest/v1 ids are in remote.artifact_id"},
    }
    mf = meta_dir / f"{stem}.archive.json"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    mr = enqueue(mf, kind="project_file", project_id=project_id, source=str(src), deliverable=stem)
    mr = upload(mr["id"], t)
    all_ok = all(r["state"] == "verified" for r in recs) and mr["state"] == "verified"
    out = {"deliverable": stem, "manifest": str(mf), "manifest_record": mr["id"], "verified": all_ok,
           "records": [{k: r[k] for k in ("id", "kind", "name", "size", "state")} | {"error": r.get("last_error")} for r in recs + [mr]]}
    _update_project(get(recs[0]["id"]), manifest_record=mr["id"])
    rel = settings()["release_local_after_verify"] if release_local is None else release_local
    if all_ok and rel:
        try:
            out["released"] = release(recs[0]["id"], t)["state"] == "released"
        except ArchiveError as e:
            out["released"], out["release_refused"] = False, str(e)
    else:
        out["released"] = False
        if not all_ok:
            out["release_refused"] = "not every file of the deliverable is verified; the local copy is kept"
    return out


def _update_project(r: dict, manifest_record: str | None = None) -> None:
    if not r or not r.get("project_id"):
        return
    try:
        from . import project as prj
        p = prj.Project(r["project_id"])
    except Exception:  # noqa: BLE001
        return
    a = p.data.setdefault("archive", {})
    e = a.setdefault(r.get("deliverable") or Path(r["local_path"]).stem, {})
    e.update({"record": r["id"], "state": r["state"], "sha256": r["sha256"], "size": r["size"], "remote": r.get("remote"),
              "local_path": r["local_path"], "updated": time.time()})
    if manifest_record:
        e["manifest_record"] = manifest_record
    p.save()


def summary() -> dict:
    rs = records()
    by = {}
    for r in rs:
        by.setdefault(r["state"], [0, 0])
        by[r["state"]][0] += 1
        by[r["state"]][1] += r.get("size", 0)
    local_bytes = sum(r.get("size", 0) for r in rs if r["state"] != "released" and Path(r.get("local_path", "")).exists())
    remote_bytes = sum(r.get("size", 0) for r in rs if r["state"] in ("verified", "released"))
    out = {"records": len(rs), "by_state": {k: {"count": v[0], "bytes": v[1]} for k, v in by.items()},
           "local_bytes": local_bytes, "archived_bytes": remote_bytes, "settings": settings()}
    try:
        t = target()
        ext, why = t.external()
        out["destination"] = {"name": t.name, "external": ext, "detail": why}
        try:
            out["destination"]["usage"] = t.usage()
        except Exception as e:  # noqa: BLE001
            out["destination"]["usage_error"] = f"{type(e).__name__}: {e}"
    except ArchiveUnavailable as e:
        out["destination"] = {"name": None, "external": False, "detail": str(e)}
    return out


def browse(project_id: str | None = None) -> list[dict]:
    """Deliverables (one row per final video) with their archive state, for the archive browser."""
    rows = []
    for r in records(project_id):
        if r["kind"] not in VIDEO_KINDS:
            continue
        sib = [x for x in records(r["project_id"]) if x.get("deliverable") == r.get("deliverable")]
        th = next((x for x in sib if x["kind"] == "thumbnail"), None)
        rows.append({"id": r["id"], "project_id": r["project_id"], "deliverable": r.get("deliverable"), "name": r["name"],
                     "size": r["size"], "sha256": r["sha256"], "state": r["state"], "remote": r.get("remote"),
                     "verification": r.get("verification"), "local_exists": Path(r["local_path"]).exists(),
                     "thumbnail": th["local_path"] if th and Path(th["local_path"]).exists() else None,
                     "files": len(sib), "created": r.get("created"), "error": r.get("last_error")})
    return sorted(rows, key=lambda x: -(x["created"] or 0))


# ------------------------------------------------------------------ mock destination (Account 2 tests only)
class MockTarget:
    """Directory-backed stand-in for Account 1's storage layer, with the same semantics that matter here: content
    dedupe by SHA-256, resumable chunked transfers keyed by transfer id, provider-reported checksums, and fault
    injection (interrupt after N bytes, corrupt the stored copy, report md5 only / no checksum). NOT a storage provider:
    it exists so Account 2's archive logic is tested without Account 1's branch (docs/ACCOUNT2_HANDOFF_TO_ACCOUNT1.md)."""

    name = "mock"

    def __init__(self, root: str | Path, *, external: bool = True, checksum: str = "sha256", chunk: int = 1 << 20):
        self.root = Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "transfers").mkdir(parents=True, exist_ok=True)
        self._external, self.checksum, self.chunk = external, checksum, chunk
        self.fail_after: int | None = None   # raise after sending this many bytes of the next transfer
        self.fail_store = False              # raise immediately (destination down)
        self.corrupt_next = False            # store wrong bytes (verification must catch it)
        self.stores = 0

    def _index(self) -> dict:
        f = self.root / "index.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def _put_index(self, ix: dict) -> None:
        (self.root / "index.json").write_text(json.dumps(ix, indent=1))

    def external(self, local=None):
        return self._external, f"mock destination at {self.root}"

    def _ref(self, sha: str) -> dict:
        e = self._index()[sha]
        return {"artifact_id": e["artifact_id"], "connection_id": "mock", "provider": "mock", "ref": e["ref"], "path": e["ref"],
                "size": e["size"], "sha256": sha}

    def find(self, sha256):
        ix = self._index()
        obj = self.root / "objects" / ix[sha256]["ref"] if sha256 in ix else None
        if obj is not None and obj.exists() and sha256_file(obj) == sha256:  # like Account 1's _location_valid
            return self._ref(sha256)
        return None

    def _run(self, src: Path, tid: str, sha: str, name: str) -> dict:
        part = self.root / "transfers" / f"{tid}.part"
        have = part.stat().st_size if part.exists() else 0
        sent = 0
        with open(src, "rb") as f, open(part, "ab") as out:
            f.seek(have)
            while True:
                b = f.read(self.chunk)
                if not b:
                    break
                if self.fail_after is not None and sent + len(b) > self.fail_after:
                    out.write(b[: max(0, self.fail_after - sent)])
                    self.fail_after = None
                    raise ConnectionError("simulated network failure during upload")
                out.write(b)
                sent += len(b)
        ref = f"{sha[:2]}/{sha}_{name}"
        obj = self.root / "objects" / ref
        obj.parent.mkdir(parents=True, exist_ok=True)
        if self.corrupt_next:
            self.corrupt_next = False
            with open(part, "r+b") as fh:
                fh.seek(0)
                fh.write(b"\x00CORRUPT")
        os.replace(part, obj)
        ix = self._index()
        ix[sha] = {"artifact_id": f"art_mock_{sha[:12]}", "ref": ref, "size": obj.stat().st_size, "name": name}
        self._put_index(ix)
        return self._ref(sha)

    def store(self, path, *, kind, name, project_id, transfer_id, mime, provenance):
        if self.fail_store:
            raise ConnectionError("simulated: destination unreachable")
        self.stores += 1
        sha = sha256_file(path)
        (self.root / "transfers" / f"{transfer_id}.json").write_text(json.dumps({"src": str(path), "sha": sha, "name": name}))
        return self._run(Path(path), transfer_id, sha, name)

    def resume(self, transfer_id, *, kind, name, project_id):
        meta = self.root / "transfers" / f"{transfer_id}.json"
        if not meta.exists() or not (self.root / "transfers" / f"{transfer_id}.part").exists():
            return None
        m = json.loads(meta.read_text())
        return self._run(Path(m["src"]), transfer_id, m["sha"], m["name"])

    def verify(self, ref, sha256, local=None):
        obj = self.root / "objects" / ref["ref"]
        if not obj.exists():
            return {"ok": False, "method": "stat", "detail": "object not found"}
        if self.checksum == "sha256":
            got = sha256_file(obj)
            return {"ok": got == sha256, "method": "provider_sha256", "detail": got}
        if self.checksum == "md5" and local is not None:
            return {"ok": md5_file(obj) == md5_file(local), "method": "provider_md5", "detail": md5_file(obj)}
        got = sha256_file(obj)
        return {"ok": got == sha256, "method": "download_sha256", "detail": got}

    def fetch(self, ref, dest):
        shutil.copyfile(self.root / "objects" / ref["ref"], dest)
        return Path(dest)

    def usage(self):
        used = sum(f.stat().st_size for f in (self.root / "objects").rglob("*") if f.is_file())
        return {"connection_id": "mock", "limit": None, "used": used, "remaining": None, "source": "measured"}
