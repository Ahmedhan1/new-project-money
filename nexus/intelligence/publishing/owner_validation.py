"""Owner-authorised PRIVATE YouTube validation: the one step that turns "tested against recorded responses" into
"verified on a real account". Run by the owner, on their machine, after the OAuth setup in
docs/ACCOUNT3_YOUTUBE_OWNER_VALIDATION.md. Nothing public: the manifest is created private, is never approved, and the
run aborts if YouTube reports any other privacy status.

    python -m nexus.intelligence.publishing.owner_validation youtube --file final.mp4 --qa reports/final.qa.json \
        [--provenance prov.json] [--thumbnail thumb.jpg] [--label main] --i-authorize-a-private-upload

Without --i-authorize-a-private-upload it is a dry run: every local check runs (credentials present, release gate,
quota), no network request is made, and the report lists what is missing.
The report (docs/results/account3/youtube_private_validation.json by default) never contains a token; `mock` is true
when a test transport was injected, so a mock run can never be mistaken for a real one.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import creds, engine, release_gate, youtube_oauth
from .adapters import youtube as yt
from .adapters.base import AdapterBlocked

FLAG = "--i-authorize-a-private-upload"


def youtube_private(file: str | Path, quality_report: dict, *, provenance: dict | None = None, thumbnail: str | None = None,
                    label: str = "main", authorized: bool = False, transport=None, title: str | None = None) -> dict:
    f = Path(file).resolve()
    rep = {"schema": "nexus.YouTubePrivateValidation/1", "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "mock": transport is not None,
           "authorized_by_owner": bool(authorized), "visibility_requested": "private", "steps": [], "verdict": "not_run"}

    def step(name, ok, **detail):
        rep["steps"].append({"step": name, "ok": bool(ok), **detail})
        return ok

    missing = [n for n in youtube_oauth.names(label).values() if not creds.get(n)]
    step("credentials present (values never read into the report)", not missing, missing=missing, backend=creds.backend())
    art = {"uri": str(f), "media_type": "video/mp4"}
    if f.is_file():
        art.update(sha256=release_gate.sha256_file(f), bytes=f.stat().st_size)
    md = {"title": title or f"NEXUS private validation {time.strftime('%Y-%m-%d %H:%M')}",
          "description": "Private upload made by the channel owner to validate NEXUS publishing. Not public."}
    if thumbnail:
        md["thumbnail"] = {"uri": str(Path(thumbnail).resolve())}
    acct = engine.connect_account("youtube", label)
    m = engine.create("owner-validation", art, "youtube", md, connection_id=acct["id"], visibility="private",
                      release={"quality_report": quality_report, "provenance": provenance})
    rc = m["release_check"]
    step("release gate (QualityReport, checksum, disclosure, language)", rc["upload_ok"], blockers=rc["blockers"],
         disclosure=rc["disclosure"]["categories"], contains_synthetic_media=rc["disclosure"]["flag"])
    q = yt.quota_status()
    step("quota available", q["videos_insert_calls"] < q["videos_insert_limit"], quota=q)
    rep["manifest_id"] = m["id"]
    if not all(s["ok"] for s in rep["steps"]):
        rep["verdict"] = "blocked"
        rep["owner_actions"] = youtube_oauth.owner_instructions(label) if missing else rc["blockers"]
        return rep
    if not authorized:
        rep["verdict"] = "dry_run_ready"
        rep["next"] = f"re-run with {FLAG} to make ONE private upload"
        return rep
    if m["visibility"] != "private" or m.get("approval") == engine.OWNER_APPROVAL:
        raise RuntimeError("validation manifests are private and never approved")
    if m["status"] in ("draft", "failed"):
        engine.enqueue(m["id"]) if m["status"] == "draft" else engine.retry_failed(m["id"])
    ad = yt.YouTubeAdapter(transport)
    r = engine.run_one(m["id"], adapters={"youtube": ad})
    remote = (r.get("tracking") or {}).get("remote") or {}
    step("upload (resumable videos.insert)", r["status"] == "uploaded", status=r["status"],
         blocked=(r.get("tracking") or {}).get("blocked"), error=(r.get("tracking") or {}).get("last_error"))
    if r["status"] != "uploaded":
        rep["verdict"] = "failed"
        return rep
    rep["video_id"] = remote.get("remote_id")
    try:
        p = ad.poll(remote["remote_id"], acct)
    except AdapterBlocked as e:
        p = {"found": False, "error": str(e)}
    step("videos.list confirms the video exists", p.get("found"), upload_status=p.get("upload_status"),
         processing=p.get("processing"), failure=p.get("failure_reason"))
    private = step("YouTube reports privacyStatus == private", p.get("privacy") == "private", privacy=p.get("privacy"))
    if thumbnail:
        th = remote.get("thumbnail") or {}
        rep["thumbnail"] = th            # a refusal (unverified channel) is recorded, not a failure of the upload
    rep["quota_after"] = yt.quota_status()
    rep["verdict"] = "verified_private_upload" if all(s["ok"] for s in rep["steps"]) else (
        "UPLOADED_BUT_NOT_PRIVATE - delete it in YouTube Studio now" if not private else "uploaded_with_problems")
    return rep


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="owner_validation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    y = sub.add_parser("youtube")
    y.add_argument("--file", required=True)
    y.add_argument("--qa", required=True, help="Account 2 QualityReport JSON for this file (reports/<name>.qa.json)")
    y.add_argument("--provenance", help="JSON: {media: [GeneratedMediaManifest...], audio: [{kind}...], no_ai_attested_by?}")
    y.add_argument("--thumbnail")
    y.add_argument("--label", default="main")
    y.add_argument("--out", default="docs/results/account3/youtube_private_validation.json")
    y.add_argument(FLAG, dest="authorized", action="store_true")
    a = ap.parse_args(argv)
    qa = json.loads(Path(a.qa).read_text(encoding="utf-8"))
    prov = json.loads(Path(a.provenance).read_text(encoding="utf-8")) if a.provenance else None
    rep = youtube_private(a.file, qa, provenance=prov, thumbnail=a.thumbnail, label=a.label, authorized=a.authorized)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("verdict", "mock", "authorized_by_owner")}, ensure_ascii=False), "->", a.out)
    return 0 if rep["verdict"] in ("verified_private_upload", "dry_run_ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
