"""End-to-end daily output: a timeline built from library assets -> render job (disk preflight, QA, quality) -> the
archive job it starts -> upload through Account 1 storage -> verification -> local final released -> retrieval.

Destination: Account 1's Google Drive adapter talking to Account 1's protocol-faithful fake Drive (real HTTP + resumable
sessions); a live Drive needs the owner's OAuth tokens. Requires a tree with Account 1 merged.

    python scripts/acceptance/daily_archive_e2e.py --out docs/results/a2_storage/daily_archive_e2e.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    home = Path(tempfile.mkdtemp(prefix="nexus-daily-"))
    os.environ.update(NEXUS_HOME=str(home), NEXUS_SCHEDULER_THREAD="0", NEXUS_EMBEDDED_WORKER="0")
    from nexus import config
    config.set_home(home)
    config.ensure_dirs()
    from nexus import archive, assetlib, diskman, jobs, project as prj, vault
    from nexus.cloud.storage import artifacts, oauth
    from nexus.cloud.storage.fake_drive import FakeGoogle
    from nexus.timeline import ops
    import nexus.workflows  # noqa: F401  (job handlers)
    t0 = time.time()
    ev: dict = {"date": time.strftime("%Y-%m-%d"), "destination": "Account 1 GoogleDriveProvider -> Account 1 FakeGoogle (local HTTP)",
                "steps": []}

    def step(name, ok, **d):
        ev["steps"].append({"step": name, "ok": bool(ok), **d})
        print("PASS" if ok else "FAIL", name, json.dumps(d, default=str)[:200])

    client = {"client_id": "daily.apps.googleusercontent.com", "client_secret": "x-EXAMPLE", "type": "limited_input"}
    g = FakeGoogle(access_ttl=3600)
    g.add_client(client["client_id"], client["client_secret"])
    oauth.set_endpoints(oauth.fake_endpoints(g.start()))
    vault.put(oauth.CLIENT_SECRET_NAME, json.dumps(client))
    st = oauth.device_start("drive_a")
    g.approve(st["user_code"], "owner@example.com")
    d = json.loads(vault.get("gdevice_drive_a"))
    d["next_poll"] = 0
    vault.put("gdevice_drive_a", json.dumps(d))
    oauth.device_poll("drive_a")
    artifacts.save_rules({"authorized_remote": ["drive_a"], "order": ["drive_a", "local"]})
    archive.save_settings({"connection_id": "drive_a", "auto_archive": True})
    step("owner-authorised destination (drive_a) configured", True)

    p = prj.Project.create("Daily video 2026-11-13")
    src = home / "incoming" / "source.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    bench = ROOT / ".nexus" / "benchmarks" / "interview" / "source.mp4"
    if bench.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "10", "-t", "14", "-i", str(bench), "-vf", "scale=1280:720", "-c:v", "libx264",
                        "-preset", "veryfast", "-c:a", "aac", str(src)], check=True)
        ev["source"] = "NASA ISS interview (public domain benchmark media), 14 s excerpt"
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=14", "-f", "lavfi", "-i", "sine=d=14",
                        "-shortest", "-c:v", "libx264", "-c:a", "aac", str(src)], check=True)
        ev["source"] = "lavfi test pattern"
    src = Path(p.import_media(src)["path"])
    tl = prj.new_timeline(1280, 720)
    tl["clips"] = [ops.video_clip(str(src), 0, 5), ops.video_clip(str(src), 5, 10), ops.video_clip(str(src), 10, 14)]
    p.save_timeline(tl, "storyline")
    used = ["lut.cinematic", "effect.light_leak", "transition.film_burn", "frame.cinematic_239", "sfx.whoosh", "music.documentary_bed",
            "template.end_card"]
    for aid in used:
        tgt, cid = ("clip", prj.Project(p.id).timeline()["clips"][0]["id"]) if aid == "effect.light_leak" else ("timeline", None)
        assetlib.apply(p.id, aid, tgt, cid, at=4.6 if aid == "sfx.whoosh" else None)
    step("timeline built from library assets", True, assets=used, versions=len(prj.Project(p.id).history()["main"]["versions"]))

    du0 = diskman.usage()
    rj = jobs.submit("render", {"project": p.id, "branch": "main", "name": "daily_2026_11_13"}, p.id)["job"]
    jobs.run_worker(once=True)
    r = jobs.get(rj)
    res = r["result"] or {}
    step("render job (disk preflight + QA + quality)", r["status"] == "done", status=r["status"], output=res.get("output"),
         qa=res.get("status"), preflight=(r["stages"].get("disk-preflight") or {}).get("output", {}).get("estimate"), error=(r.get("error") or "")[:300])
    final = p.dir / res["output"]
    sha = hashlib.sha256(final.read_bytes()).hexdigest()
    size = final.stat().st_size
    aj = (res.get("archive") or {}).get("job")
    step("render job started the archive job (auto_archive with a configured destination)", bool(aj), archive_job=aj)
    jobs.run_worker(once=True)
    arj = jobs.get(aj)
    out = arj["result"] or {}
    step("archive job: every file uploaded and verified", arj["status"] == "done" and out.get("verified"), records=out.get("records"),
         error=(arj.get("error") or "")[:300])
    rec = archive.get(out["records"][0]["id"])
    drive_copy = [f for f in g.files.values() if f["name"] == final.name]
    step("Drive holds exactly one copy with the same SHA-256", len(drive_copy) == 1 and hashlib.sha256(drive_copy[0]["data"]).hexdigest() == sha,
         drive_path=rec["remote"]["path"], verification=rec["verification"]["method"])
    step("local final deleted only after verification (state released)", out.get("released") and not final.exists()
         and rec["state"] == "released" and rec["release_verification"]["ok"],
         events=[e[1] for e in rec["events"]])
    m = artifacts.get(rec["remote"]["artifact_id"])
    step("Account 1 artifact registry: kind master, retention archive (never cleaned automatically)", m.kind == "master" and m.retention == "archive")
    proj = prj.Project(p.id).data["archive"]["daily_2026_11_13"]
    step("project record carries the archive reference", proj["state"] == "released" and proj["remote"]["artifact_id"] == m.artifact_id,
         manifest_record=proj.get("manifest_record"))
    kept = sorted(x.name for x in (home / "archive" / p.id).iterdir())
    step("small local metadata kept (thumbnail, manifest)", any(k.endswith(".thumb.jpg") for k in kept), kept=kept)
    du1 = diskman.usage()
    step("browse shows the archived video", archive.browse(p.id)[0]["state"] == "released")
    got = archive.retrieve(rec["id"])
    step("retrieve: downloaded back, SHA-256 identical", hashlib.sha256(Path(got).read_bytes()).hexdigest() == sha, path=str(got))
    g.stop()
    oauth.set_endpoints(None)
    ev.update({"final": {"name": final.name, "bytes": size, "sha256": sha},
               "disk": {"renders_bytes_after_render_before_release": None, "renders_bytes_before": du0["categories"]["renders"]["bytes"],
                        "renders_bytes_after_release": du1["categories"]["renders"]["bytes"],
                        "render_cache_bytes_after_release": du1["categories"]["render_cache"]["bytes"]},
               "seconds": round(time.time() - t0, 1), "ok": all(s["ok"] for s in ev["steps"])})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(ev, indent=1, default=str), encoding="utf-8")
    return 0 if ev["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
