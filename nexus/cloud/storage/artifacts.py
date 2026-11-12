"""Artifacts across storage connections: store (resumable, verified), dedupe by SHA-256, manifests, copy/move between
connections, retention + safe cleanup, destination choice by owner rules and real free space.

Owner rules live in NEXUS_HOME/storage_rules.json (never committed):
    {"authorized_remote": ["drive_a"],          # remote connections the owner allows project data to be uploaded to
     "order": ["drive_a", "drive_b", "local"],  # preference order for automatic placement
     "kinds": {"source": ["local"]},            # optional per-kind order
     "reserve_bytes": 536870912,                # keep this much free on every destination
     "allow_unknown_quota": false}              # a provider that does not report a limit is skipped unless true
Nothing is uploaded to a remote connection that is not in `authorized_remote` (AGENTS.md: no upload of user media or
project data without explicit authorisation). Placement never spreads data over extra accounts to get around a
provider limit: it picks the first destination in the OWNER's order that really has room, or fails.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from ... import config, db
from ...contracts import ArtifactLocation, ArtifactManifest
from ..events import emit
from . import connections, transfers
from .base import DEFAULT_CHUNK, ChecksumMismatch, QuotaExceeded, StorageError, safe_rel, sha256_file

NEXUS_ROOT = "NEXUS"
FOLDERS = ["Projects", "OriginalMedia", "Analysis", "Masters", "Shorts", "Localized", "Thumbnails", "Audio",
           "ProjectFiles", "Archives"]
KIND_FOLDER = {"source": "OriginalMedia", "proxy": "OriginalMedia", "analysis": "Analysis", "master": "Masters",
               "short": "Shorts", "localized": "Localized", "thumbnail": "Thumbnails", "audio": "Audio",
               "subtitle": "Localized", "project_file": "ProjectFiles", "archive": "Archives", "log": "Projects",
               "checkpoint": "Projects", "other": "Projects"}
MANIFEST_SUFFIX = ".manifest.json"


class StorageBlocked(StorageError):
    """Refused by the owner's rules (not a technical failure)."""


# ---------------------------------------------------------------- owner rules
def rules() -> dict:
    f = config.HOME / "storage_rules.json"
    r = {"authorized_remote": [], "order": ["local"], "kinds": {}, "reserve_bytes": 512 * 2 ** 20, "allow_unknown_quota": False}
    if f.exists():
        r.update(json.loads(f.read_text(encoding="utf-8")))
    return r


def save_rules(r: dict) -> dict:
    for c in r.get("authorized_remote", []) + r.get("order", []):
        connections.provider_of(c)
    f = config.HOME / "storage_rules.json"
    f.write_text(json.dumps({**rules(), **r}, indent=1), encoding="utf-8")
    return rules()


def check_authorized(cid: str) -> None:
    if connections.provider_of(cid) == "local":
        return
    if cid not in rules().get("authorized_remote", []):
        raise StorageBlocked(f"{cid} is not in authorized_remote of storage_rules.json; the owner must authorise uploads to it")
    if connections.get(cid)["status"] != "connected":
        raise StorageBlocked(f"{cid} is not connected (status {connections.get(cid)['status']})")


def provider(cid: str):
    from . import get_provider
    return get_provider(cid)


def choose_destination(size: int, kind: str | None = None) -> tuple[str, list[str]]:
    r = rules()
    order = (r.get("kinds") or {}).get(kind or "", None) or r.get("order") or ["local"]
    notes = []
    for cid in order:
        try:
            check_authorized(cid)
            q = provider(cid).quota()
        except Exception as e:  # not authorised / not connected / unreachable: try the next one, say why
            notes.append(f"{cid}: {e}")
            continue
        if q.limit is None and not r.get("allow_unknown_quota"):
            notes.append(f"{cid}: provider reports no storage limit; not assuming unlimited")
            continue
        free = q.remaining if q.remaining is not None else float("inf")
        if free - r.get("reserve_bytes", 0) >= size:
            notes.append(f"{cid}: chosen ({free / 2 ** 30:.2f} GiB free, source {q.source})")
            return cid, notes
        notes.append(f"{cid}: not enough free space ({free / 2 ** 30:.2f} GiB free, need {size / 2 ** 30:.2f} GiB + reserve)")
    raise QuotaExceeded("no destination in the owner's order has room: " + "; ".join(notes))


def folder_for(kind: str, project_id: str | None) -> str:
    return f"{NEXUS_ROOT}/{KIND_FOLDER.get(kind, 'Projects')}/{project_id or '_shared'}"


def create_structure(cid: str) -> dict:
    check_authorized(cid)
    p = provider(cid)
    return {f"{NEXUS_ROOT}/{f}": p.mkdirs(f"{NEXUS_ROOT}/{f}") for f in FOLDERS}


# ---------------------------------------------------------------- registry (table `artifacts`)
def _save(m: ArtifactManifest) -> None:
    db.execute("INSERT OR REPLACE INTO artifacts(id,kind,name,sha256,size,project_id,job_id,manifest,created) VALUES(?,?,?,?,?,?,?,?,?)",
               (m.artifact_id, m.kind, m.name, m.sha256, m.size, m.project_id, m.produced_by_job, m.model_dump_json(), m.created_at))


def get(artifact_id: str) -> ArtifactManifest | None:
    rows = db.query("SELECT manifest FROM artifacts WHERE id=?", (artifact_id,))
    return ArtifactManifest.model_validate_json(rows[0]["manifest"]) if rows else None


def find_by_sha(sha: str) -> ArtifactManifest | None:
    rows = db.query("SELECT manifest FROM artifacts WHERE sha256=? ORDER BY created LIMIT 1", (sha.lower(),))
    return ArtifactManifest.model_validate_json(rows[0]["manifest"]) if rows else None


def list_artifacts(project_id: str | None = None, kind: str | None = None, limit: int = 500) -> list[ArtifactManifest]:
    q, a = "SELECT manifest FROM artifacts WHERE 1=1", []
    if project_id:
        q += " AND project_id=?"
        a.append(project_id)
    if kind:
        q += " AND kind=?"
        a.append(kind)
    rows = db.query(q + " ORDER BY created DESC LIMIT ?", (*a, limit))
    return [ArtifactManifest.model_validate_json(r["manifest"]) for r in rows]


def register(m: ArtifactManifest) -> ArtifactManifest:
    """Dedupe by SHA-256: a second registration of the same bytes merges its locations into the first manifest."""
    old = find_by_sha(m.sha256)
    if old is None:
        _save(m)
        return m
    have = {(lo.connection_id, lo.ref) for lo in old.locations}
    old.locations += [lo for lo in m.locations if (lo.connection_id, lo.ref) not in have]
    _save(old)
    return old


# ---------------------------------------------------------------- store / copy / move
def _location_valid(lo: ArtifactLocation, sha: str) -> bool:
    try:
        f = provider(lo.connection_id).stat(lo.ref)
        if f is None:
            return False
        if f.sha256:
            return f.sha256.lower() == sha
        return provider(lo.connection_id).checksum(lo.ref).get("sha256") in (None, sha)
    except Exception:
        return False


def _put_manifest(p, m: ArtifactManifest, path: str) -> None:
    tmp = config.CACHE / "manifests" / f"{m.artifact_id}{MANIFEST_SUFFIX}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(m.model_dump_json(indent=1), encoding="utf-8")
    try:
        p.upload(tmp, path + MANIFEST_SUFFIX, mime="application/json", props={"nexus_manifest_for": m.artifact_id})
    except Exception as e:  # the manifest copy is a convenience; the DB row is authoritative
        emit("storage.failed", f"manifest upload for {m.artifact_id} failed: {e}", artifact_id=m.artifact_id)
    finally:
        tmp.unlink(missing_ok=True)


def store_file(src: str | Path, *, kind: str, name: str | None = None, connection_id: str | None = None,
               mime: str = "application/octet-stream", project_id: str | None = None, job_id: str | None = None,
               provenance: dict | None = None, retention: str = "keep", expires_at: float | None = None,
               dest_path: str | None = None, chunk_size: int = DEFAULT_CHUNK, transfer_id: str | None = None,
               on_chunk=None, write_manifest: bool = True) -> ArtifactManifest:
    src = Path(src)
    size, sha = src.stat().st_size, sha256_file(src)
    name = name or src.name
    cid = connection_id or choose_destination(size, kind)[0]
    check_authorized(cid)
    existing = find_by_sha(sha)
    if existing:
        for lo in existing.locations:
            if lo.connection_id == cid and _location_valid(lo, sha):
                emit("storage.uploaded", f"{name}: already stored on {cid} (deduplicated by sha256)",
                     artifact_id=existing.artifact_id, connection_id=cid, deduplicated=True)
                return existing
    path = safe_rel(dest_path or f"{folder_for(kind, project_id)}/{name}")
    # failover: when the upload to the chosen destination fails technically, try the next destination in the owner's
    # order that is authorised and has room. A quota refusal is not "retried on another account": it is reported, and the
    # next destination is only used because the owner listed it as an allowed destination for this kind.
    candidates = [cid] if connection_id else [cid] + [c for c in _order_for(kind) if c != cid]
    errors = []
    for i, c in enumerate(candidates):
        if i:
            try:
                check_authorized(c)
                q = provider(c).quota()
                if q.remaining is not None and q.remaining - rules().get("reserve_bytes", 0) < size:
                    errors.append(f"{c}: not enough free space")
                    continue
            except Exception as e:  # noqa: BLE001
                errors.append(f"{c}: {e}")
                continue
        p = provider(c)
        try:
            f = p.upload(src, path, mime=mime, chunk_size=chunk_size, transfer_id=transfer_id if i == 0 else None,
                         on_chunk=on_chunk, props={"nexus_kind": kind, "nexus_retention": retention})
        except Exception as e:  # noqa: BLE001
            emit("storage.failed", f"upload of {name} to {c} failed: {e}", connection_id=c)
            errors.append(f"{c}: {e}")
            if connection_id:
                raise
            continue
        if i:
            emit("storage.uploaded", f"{name}: failed over to {c} after: {'; '.join(errors)}"[:500], connection_id=c, failover=True)
        m = _finish_store(p, c, f, path, sha, size, existing, name=name, kind=kind, mime=mime, project_id=project_id,
                          job_id=job_id, provenance=provenance, retention=retention, expires_at=expires_at,
                          write_manifest=write_manifest)
        return replicate(m)
    raise StorageError(f"upload of {name} failed on every allowed destination: {'; '.join(errors)}")


def _order_for(kind: str | None) -> list[str]:
    r = rules()
    return list((r.get("kinds") or {}).get(kind or "", None) or r.get("order") or ["local"])


def replicate(m: ArtifactManifest) -> ArtifactManifest:
    """Opt-in backup copies: storage_rules.json "replicate": {"master": ["drive_a"], ...}. Each copy is verified by SHA-256
    (copy_to). A failed replica is reported and leaves the primary copy untouched."""
    for c in (rules().get("replicate") or {}).get(m.kind, []):
        if any(lo.connection_id == c for lo in m.locations):
            continue
        try:
            m = copy_to(m.artifact_id, c)
        except Exception as e:  # noqa: BLE001
            emit("storage.failed", f"replica of {m.name} to {c} failed: {e}"[:500], artifact_id=m.artifact_id, connection_id=c)
    return m


def _finish_store(p, cid, f, path, sha, size, existing, *, name, kind, mime, project_id, job_id, provenance, retention,
                  expires_at, write_manifest) -> ArtifactManifest:
    lo = ArtifactLocation(provider=p.INFO.provider, connection_id=cid, ref=f.ref, path=path, verified_at=time.time())
    if existing:
        existing.locations = [x for x in existing.locations if x.connection_id != cid] + [lo]
        m = existing
    else:
        m = ArtifactManifest(kind=kind, name=name, mime=mime, size=size, sha256=sha, project_id=project_id,
                             produced_by_job=job_id, locations=[lo], provenance=provenance or {}, retention=retention,
                             expires_at=expires_at)
    _save(m)
    if write_manifest:
        _put_manifest(p, m, path)
    emit("storage.uploaded", f"{name} stored on {cid} ({size} bytes, sha256 verified)", artifact_id=m.artifact_id,
         connection_id=cid, size=size)
    return m


def resume_transfer(transfer_id: str, *, kind: str = "other", name: str | None = None, project_id: str | None = None,
                    provenance: dict | None = None) -> ArtifactManifest:
    """Finish an interrupted upload (after a crash or restart) and register the artifact."""
    t = transfers.get(transfer_id)
    if t is None:
        raise StorageError(f"unknown transfer {transfer_id}")
    p = provider(t["connection_id"])
    f = p.resume(transfer_id)
    existing = find_by_sha(t["sha256"])
    meta = t["meta"]
    return _finish_store(p, t["connection_id"], f, t["remote_ref"], t["sha256"], int(t["size"]), existing,
                         name=name or Path(t["remote_ref"]).name, kind=(meta.get("props") or {}).get("nexus_kind", kind),
                         mime=meta.get("mime") or "application/octet-stream", project_id=project_id, job_id=None,
                         provenance=provenance, retention=(meta.get("props") or {}).get("nexus_retention", "keep"),
                         expires_at=None, write_manifest=True)


def fetch_local(m: ArtifactManifest, dest: Path) -> Path:
    """Download a verified copy of the artifact from the first location that works."""
    errors = []
    for lo in m.locations:
        try:
            provider(lo.connection_id).download(lo.ref, dest)
            if sha256_file(dest) != m.sha256:
                dest.unlink(missing_ok=True)
                raise ChecksumMismatch("sha256 differs from manifest")
            return dest
        except Exception as e:
            errors.append(f"{lo.connection_id}: {e}")
    raise StorageError(f"no readable copy of {m.artifact_id}: {errors}")


def copy_to(artifact_id: str, dst_cid: str, *, delete_source: str | None = None) -> ArtifactManifest:
    """Copy (download -> upload -> verify) to another connection. With delete_source=<connection id>, that source copy
    is deleted ONLY after the destination copy has been re-checked against the manifest SHA-256."""
    m = get(artifact_id)
    if m is None:
        raise StorageError(f"unknown artifact {artifact_id}")
    check_authorized(dst_cid)
    work = config.CACHE / "transfers" / artifact_id
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / m.name
    try:
        if not any(lo.connection_id == dst_cid and _location_valid(lo, m.sha256) for lo in m.locations):
            srcs = [lo for lo in m.locations if lo.connection_id != dst_cid] or m.locations
            fetch_local(m.model_copy(update={"locations": srcs}), tmp)
            p = provider(dst_cid)
            path = next((lo.path for lo in m.locations if lo.path), f"{folder_for(m.kind, m.project_id)}/{m.name}")
            f = p.upload(tmp, path, mime=m.mime, props={"nexus_kind": m.kind, "nexus_retention": m.retention})
            m = _finish_store(p, dst_cid, f, path, m.sha256, m.size, m, name=m.name, kind=m.kind, mime=m.mime,
                              project_id=m.project_id, job_id=m.produced_by_job, provenance=m.provenance,
                              retention=m.retention, expires_at=m.expires_at, write_manifest=True)
        if delete_source and delete_source != dst_cid:
            dst = next(lo for lo in m.locations if lo.connection_id == dst_cid)
            if not _location_valid(dst, m.sha256):
                raise ChecksumMismatch(f"destination copy on {dst_cid} failed re-verification; source kept")
            for lo in [x for x in m.locations if x.connection_id == delete_source]:
                provider(lo.connection_id).delete(lo.ref, only_app_created=True)
                m.locations.remove(lo)
            _save(m)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return m


def move(artifact_id: str, src_cid: str, dst_cid: str) -> ArtifactManifest:
    return copy_to(artifact_id, dst_cid, delete_source=src_cid)


# ---------------------------------------------------------------- retention + safe cleanup
def _cleanup_items(now: float) -> list[dict]:
    items = []
    for m in list_artifacts(limit=100000):
        if m.retention != "temporary" or m.expires_at is None or m.expires_at > now:
            continue
        for lo in m.locations:
            try:
                f = provider(lo.connection_id).stat(lo.ref)
            except Exception as e:
                items.append({"artifact_id": m.artifact_id, "connection_id": lo.connection_id, "ref": lo.ref,
                              "action": "skip", "reason": f"unreachable: {e}"})
                continue
            if f is None:
                action, reason = "forget", "already gone"
            elif not f.app_created:
                action, reason = "skip", "not created by NEXUS"
            else:
                action, reason = "delete", f"temporary, expired {time.strftime('%Y-%m-%d', time.gmtime(m.expires_at))}"
            items.append({"artifact_id": m.artifact_id, "connection_id": lo.connection_id, "ref": lo.ref, "path": lo.path,
                          "size": m.size, "action": action, "reason": reason})
    return items


def cleanup(dry_run: bool = True, plan_id: str | None = None, now: float | None = None) -> dict:
    """Dry run first: returns a plan id. Executing requires that plan id, and refuses if the plan changed meanwhile.
    Only files NEXUS created are ever deleted; `archive` and `keep` retention are never touched."""
    now = now or time.time()
    items = _cleanup_items(now)
    key = sorted((i["artifact_id"], i["connection_id"], i["ref"], i["action"]) for i in items)
    if dry_run:
        pid = db.put_doc("cleanup_plan", {"key": key, "created": time.time()})
        return {"plan_id": pid, "dry_run": True, "items": items,
                "bytes": sum(i.get("size", 0) for i in items if i["action"] == "delete")}
    plan = db.get_doc("cleanup_plan", plan_id or "")
    if not plan:
        raise StorageBlocked("run a dry-run first and pass its plan_id")
    if time.time() - plan["created"] > 3600 or [list(k) for k in key] != plan["key"]:
        raise StorageBlocked("the cleanup plan is stale or no longer matches; run the dry-run again")
    done = []
    for i in items:
        if i["action"] not in ("delete", "forget"):
            continue
        if i["action"] == "delete":
            provider(i["connection_id"]).delete(i["ref"], only_app_created=True)
        m = get(i["artifact_id"])
        m.locations = [lo for lo in m.locations if not (lo.connection_id == i["connection_id"] and lo.ref == i["ref"])]
        if m.locations:
            _save(m)
        else:
            db.execute("DELETE FROM artifacts WHERE id=?", (m.artifact_id,))
        done.append(i)
    db.execute("DELETE FROM docs WHERE kind='cleanup_plan' AND id=?", (plan_id,))
    return {"plan_id": plan_id, "dry_run": False, "deleted": done}
