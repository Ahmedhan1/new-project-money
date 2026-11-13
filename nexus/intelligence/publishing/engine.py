"""Publishing engine: connected accounts, PublishingManifest lifecycle, approval gate, idempotency, queue, schedule,
retries with backoff, history. Adapters do the platform I/O (adapters/).

Safety rules enforced here (and again in each adapter):
  * visibility public/unlisted only with approval == 'approved_by_owner'; default is the platform's private/draft mode
  * same artifact + platform + connection + metadata = same idempotency key = never a second upload
  * owner-action failures (missing credentials, unapproved app, quota) are 'failed' with `blocked` and never auto-retried
  * release gate (release_gate.py): no upload without a passing Account 2 QualityReport for this exact output, a valid
    checksum, a decidable AI-content disclosure and an acceptable language state; public/unlisted additionally needs
    human QA approval, completed native review where required and the owner's disclosure decision where the platform
    asks for one. Every evaluation is stored in `release_check` and the history.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time

from .. import contracts as C
from . import capabilities, creds, release_gate, store
from .adapters.base import AdapterBlocked, PermanentError, TransientError

OWNER_APPROVAL = "approved_by_owner"
GATED = {"public", "unlisted"}
BACKOFF_BASE_S, BACKOFF_CAP_S, MAX_ATTEMPTS = 30.0, 3600.0, 5
TOKENISH = re.compile(r"(ya29\.|1//0|GOCSPX-|EAA[A-Za-z0-9]{20,}|act\.|[A-Za-z0-9_\-]{40,})")


class PublishError(ValueError):
    pass


# ------------------------------------------------------------------ connected accounts (no secrets, references only)
def connect_account(platform: str, label: str, external_id: str = "", secret_ref: str | None = None,
                    scopes: list[str] | None = None, app_approved: bool = False, note: str = "") -> dict:
    capabilities.api(platform)  # validates the platform name
    ref = secret_ref or creds.secret_name(platform, label, "token")
    if not creds.NAME_RE.match(ref) or TOKENISH.search(ref):
        raise PublishError("secret_ref must be a vault reference name (pub.<platform>.<label>.<kind>), never a token")
    store.ensure()
    aid = f"acct_{hashlib.sha1(f'{platform}:{label}'.encode()).hexdigest()[:10]}"
    data = {"id": aid, "platform": platform, "label": label, "external_id": external_id, "secret_ref": ref,
            "scopes": scopes or [], "app_approved": bool(app_approved), "note": note, "credential_backend": creds.backend()}
    from ... import db
    db.execute("INSERT OR REPLACE INTO pub_accounts(id,platform,label,external_id,secret_ref,created,data) VALUES(?,?,?,?,?,?,?)",
               (aid, platform, label, external_id, ref, time.time(), json.dumps(data)))
    return data


def get_account(aid: str | None) -> dict | None:
    if not aid:
        return None
    from ... import db
    store.ensure()
    r = db.query("SELECT data FROM pub_accounts WHERE id=?", (aid,))
    return json.loads(r[0]["data"]) if r else None


# ------------------------------------------------------------------ manifests
def _canon(metadata: dict) -> str:
    return json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def idempotency_key(artifact: dict, platform: str, metadata: dict, connection_id: str | None = None) -> str:
    ident = artifact.get("sha256") or f"{artifact.get('uri')}|{artifact.get('bytes')}"
    return hashlib.sha256(f"{ident}\n{platform}\n{connection_id or ''}\n{_canon(metadata)}".encode()).hexdigest()


def _event(m: dict, event: str, **detail) -> None:
    m.setdefault("history", []).append({"ts": round(time.time(), 3), "event": event, **detail})


RELEASE_KEYS = ("quality_report", "provenance", "disclosure_decision", "language")


def _check_release(m: dict, event: str = "release_checked") -> dict:
    rc = release_gate.evaluate(m)
    m["release_check"] = rc
    _event(m, event, upload_ok=rc["upload_ok"], public_ok=rc["public_ok"], blockers=rc["blockers"][:6],
           public_blockers=rc["public_blockers"][:6], qa=(rc["quality"]["report"] or {}).get("status"),
           human_approval=(rc["quality"]["report"] or {}).get("human_approval"), disclosure=rc["disclosure"]["categories"],
           language=rc["language"]["state"], review=rc["language"]["reviewer_approval"])
    return rc


def create(project_id: str, artifact: dict, platform: str, metadata: dict, connection_id: str | None = None,
           visibility: str | None = None, schedule: str | None = None, release: dict | None = None) -> dict:
    """New PublishingManifest (or the existing one with the same idempotency key, flagged `deduplicated`)."""
    cap = capabilities.api(platform)
    if not artifact.get("uri"):
        raise PublishError("artifact needs a 'uri' (ArtifactRef)")
    key = idempotency_key(artifact, platform, metadata, connection_id)
    if (old := store.by_key(key)) is not None:
        return {**old, "deduplicated": True}
    safe = cap.get("default_visibility", "private")
    wanted = visibility or safe
    if wanted not in C.VISIBILITY:
        raise PublishError(f"visibility must be one of {sorted(C.VISIBILITY)}")
    m = C.make("PublishingManifest", id=C.new_id("pub"), project_id=project_id, artifact=artifact, platform=platform,
               connection_id=connection_id, metadata=metadata, visibility=safe if wanted in GATED else wanted,
               approval="pending", schedule=schedule, status="awaiting_approval" if wanted in GATED else "draft",
               idempotency_key=key, history=[])
    m["requested_visibility"] = wanted
    m["tracking"] = {"attempts": 0, "due": None, "remote": None, "last_error": None, "blocked": None, "upload_session": None}
    m["release"] = {k: (release or {}).get(k) for k in RELEASE_KEYS}
    _event(m, "created", visibility=m["visibility"], requested=wanted)
    _check_release(m)
    return store.save_manifest(C.check(m))


def attach_release(mid: str, **parts) -> dict:
    """Attach or replace release evidence (a newer QualityReport after human review, the owner's disclosure decision,
    the language record after native review). Earlier evaluations stay in the history."""
    m = get(mid)
    bad = set(parts) - set(RELEASE_KEYS)
    if bad:
        raise PublishError(f"unknown release keys {sorted(bad)}; allowed {RELEASE_KEYS}")
    if m["status"] in ("uploaded", "uploading"):
        raise PublishError(f"manifest is {m['status']}: release evidence can no longer change")
    m.setdefault("release", {}).update(parts)
    _event(m, "release_evidence_attached", keys=sorted(parts))
    _check_release(m)
    return store.save_manifest(m)


def release_check(mid: str) -> dict:
    m = get(mid)
    rc = _check_release(m)
    store.save_manifest(m)
    return rc


def get(mid: str) -> dict:
    m = store.get_manifest(mid)
    if m is None:
        raise PublishError(f"no manifest {mid}")
    return m


def approve(mid: str, approval: str = OWNER_APPROVAL, by: str = "owner") -> dict:
    """Record the owner's approval. Only 'approved_by_owner' unlocks the requested public/unlisted visibility."""
    m = get(mid)
    if approval != OWNER_APPROVAL:
        raise PublishError(f"approval must be '{OWNER_APPROVAL}' (the owner's explicit decision)")
    m["approval"] = approval
    if m.get("requested_visibility") in GATED:
        m["visibility"] = m["requested_visibility"]
    if m["status"] in ("draft", "awaiting_approval"):
        m["status"] = "approved"
    _event(m, "approved", by=by, visibility=m["visibility"])
    return store.save_manifest(C.check(m))


def _parse_when(schedule: str | None) -> float | None:
    if not schedule:
        return None
    t = dt.datetime.fromisoformat(schedule.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def enqueue(mid: str, schedule: str | None = None) -> dict:
    m = get(mid)
    if m["status"] in ("uploaded", "uploading", "queued"):
        return m
    if m["visibility"] in GATED and m["approval"] != OWNER_APPROVAL:
        raise PublishError("public/unlisted needs approval == 'approved_by_owner' first")
    if m["status"] == "awaiting_approval":
        raise PublishError("the owner asked for a public/unlisted post: approve() it, or recreate it as private")
    rc = _check_release(m)
    if not rc["upload_ok"]:
        store.save_manifest(m)
        raise PublishError("release gate: " + " | ".join(rc["blockers"]))
    if m["visibility"] in GATED and not rc["public_ok"]:
        store.save_manifest(m)
        raise PublishError("release gate (public/unlisted): " + " | ".join(rc["public_blockers"]))
    if schedule:
        m["schedule"] = schedule
    m["status"] = "queued"
    m["tracking"].update(due=_parse_when(m.get("schedule")), blocked=None, last_error=None)
    _event(m, "queued", schedule=m.get("schedule"))
    return store.save_manifest(C.check(m))


def cancel(mid: str) -> dict:
    m = get(mid)
    if m["status"] in ("uploaded", "uploading"):
        raise PublishError(f"cannot cancel a manifest that is {m['status']}")
    m["status"] = "cancelled"
    _event(m, "cancelled")
    return store.save_manifest(m)


def backoff(attempt: int, retry_after: float | None = None) -> float:
    return max(retry_after or 0.0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempt - 1))))


def run_one(mid: str, adapters: dict | None = None, now: float | None = None) -> dict:
    """Attempt one upload. Returns the updated manifest; never raises for platform failures (they are recorded)."""
    from .adapters import registry
    m = get(mid)
    now = now or time.time()
    if m["status"] != "queued":
        return m
    if m["visibility"] in GATED and m["approval"] != OWNER_APPROVAL:  # defence in depth
        raise PublishError("refusing: public/unlisted without owner approval")
    tr = m["tracking"]
    rc = _check_release(m)  # defence in depth: evidence may have changed since enqueue (file edited, report replaced)
    if not rc["upload_ok"] or (m["visibility"] in GATED and not rc["public_ok"]):
        why = rc["blockers"] or rc["public_blockers"]
        tr.update(blocked={"code": "release_gate", "message": " | ".join(why)[:600], "owner_action": why}, last_error="release gate")
        m["status"] = "failed"
        _event(m, "blocked", code="release_gate")
        return store.save_manifest(m)
    adapter = (adapters or {}).get(m["platform"]) or registry.adapter_for(m["platform"])
    acct = get_account(m.get("connection_id"))
    tr["attempts"] = int(tr.get("attempts", 0)) + 1
    m["status"] = "uploading"
    _event(m, "upload_started", attempt=tr["attempts"], adapter=type(adapter).__name__)
    store.save_manifest(m)

    def save(state: dict) -> None:  # resumable state survives a crash (the job ledger re-runs us)
        tr["upload_session"] = state
        store.save_manifest(m)

    try:
        adapter.preflight(m, acct)
        res = adapter.upload(m, acct, {"save": save, "now": now})
        tr.update(remote=res, last_error=None, upload_session=None)
        m["status"] = "uploaded"
        _event(m, "uploaded", remote_id=res.get("remote_id"), remote_status=res.get("remote_status"))
    except AdapterBlocked as e:
        tr.update(blocked=e.as_dict(), last_error=str(e))
        m["status"] = "failed"
        _event(m, "blocked", **e.as_dict())
    except TransientError as e:
        tr["last_error"] = str(e)
        if tr["attempts"] >= MAX_ATTEMPTS:
            m["status"] = "failed"
            _event(m, "failed", error=str(e), reason="max attempts")
        else:
            tr["due"] = now + backoff(tr["attempts"], e.retry_after)
            m["status"] = "queued"
            _event(m, "retry_scheduled", error=str(e), due=tr["due"])
    except PermanentError as e:
        tr["last_error"] = str(e)
        m["status"] = "failed"
        _event(m, "failed", error=str(e))
    return store.save_manifest(m)


def process(now: float | None = None, adapters: dict | None = None, limit: int = 10) -> list[dict]:
    """Run every due queued manifest once (scheduler / worker tick)."""
    now = now or time.time()
    return [run_one(m["id"], adapters, now) for m in store.due(now)[:limit]]


def retry_failed(mid: str) -> dict:
    """Owner fixed the cause (e.g. connected the account): put a failed manifest back in the queue."""
    m = get(mid)
    if m["status"] != "failed":
        return m
    m["status"] = "queued"
    m["tracking"].update(due=None, blocked=None, attempts=0)
    _event(m, "requeued_by_owner")
    return store.save_manifest(m)


def history(mid: str) -> list[dict]:
    return get(mid).get("history", [])


def public_view(m: dict) -> dict:
    """Manifest without internal tracking - what an API/UI shows."""
    return {k: v for k, v in m.items() if k != "tracking"} | {"blocked": (m.get("tracking") or {}).get("blocked"),
                                                              "remote": (m.get("tracking") or {}).get("remote")}
