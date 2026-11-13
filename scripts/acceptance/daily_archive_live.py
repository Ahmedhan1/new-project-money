"""Daily Archive against the owner's REAL Google Drive, with disposable media only.

    python scripts/acceptance/daily_archive_live.py --source-home ~/.nexus-or-repo/.nexus --connection drive_a \
        --out docs/results/a2_storage/daily_archive_live.json

Runs in a throw-away copy of NEXUS_HOME (database + the Drive vault entries + vault key; never the Kaggle token), so the
owner's archive settings, records and files are never touched; the copy is deleted at the end. Uses the existing vault
connections (no new OAuth). Creates one disposable project with a generated test video (~20 MB, lavfi test pattern),
archives it through Account 1's storage layer with an injected network drop mid-upload, resumes, verifies, re-verifies
before releasing the local copy, browses and retrieves. Nothing on Drive is deleted; the disposable files stay in the
app's NEXUS folder (listed in the evidence) for the owner to remove or keep.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-home", required=True)
    ap.add_argument("--connection", default="drive_a")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    src_home = Path(a.source_home).resolve()
    home = Path(tempfile.mkdtemp(prefix="nexus-live-archive-"))
    os.chmod(home, 0o700)
    (home / "secrets").mkdir(mode=0o700)
    shutil.copy2(src_home / "nexus.db", home / "nexus.db")
    for f in ["vault.key", "google_oauth_client.secret"] + [p.name for p in (src_home / "secrets").glob("gdrive_*.secret")]:
        shutil.copy2(src_home / "secrets" / f, home / "secrets" / f)
    os.environ.update(NEXUS_HOME=str(home), NEXUS_SCHEDULER_THREAD="0", NEXUS_EMBEDDED_WORKER="0")
    ev: dict = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": "LIVE (owner's Google Drive, disposable media)",
                "connection": a.connection, "steps": [], "health": {}}

    def step(name, ok, **d):
        ev["steps"].append({"step": name, "ok": bool(ok), **d})
        print("PASS" if ok else "FAIL", name, json.dumps(d, default=str)[:240], flush=True)
        return ok

    try:
        from nexus import config
        config.set_home(home)
        config.ensure_dirs()
        from nexus import archive, project as prj
        from nexus.cloud.storage import artifacts, get_provider
        from nexus.cloud.storage import gdrive
        for cid in ("drive_a", "drive_b", "drive_c"):
            try:
                h = get_provider(cid).health()
                q = (h.quotas or [None])[0]
                ev["health"][cid] = {"status": h.status, "detail": h.detail, "quota_source": q.source if q else None,
                                     "used_gb": round(q.used / 2 ** 30, 2) if q and q.used is not None else None,
                                     "limit_gb": round(q.limit / 2 ** 30, 2) if q and q.limit else None}
            except Exception as e:  # noqa: BLE001
                ev["health"][cid] = {"status": "error", "detail": f"{type(e).__name__}: {e}"[:300]}
        step("Drive connections healthy (provider-reported quota)", ev["health"].get(a.connection, {}).get("status") == "ok", health=ev["health"])
        artifacts.save_rules({"authorized_remote": [a.connection], "order": [a.connection, "local"]})
        archive.save_settings({"connection_id": a.connection, "auto_archive": False})

        p = prj.Project.create(f"NEXUS acceptance (disposable) {time.strftime('%Y%m%d-%H%M%S')}")
        (p.dir / "renders").mkdir(parents=True, exist_ok=True)
        final = p.dir / "renders" / "disposable_archive_test.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=12", "-f", "lavfi", "-i",
                        "sine=f=440:d=12", "-shortest", "-c:v", "libx264", "-b:v", "20M", "-maxrate", "20M", "-bufsize", "4M",
                        "-c:a", "aac", "-metadata", "comment=NEXUS disposable acceptance media", str(final)], check=True)
        fsha, fsize = sha(final), final.stat().st_size
        step("disposable media created (generated test pattern, not owner data; > 2 upload chunks)", fsize > 2 * 8 * 2 ** 20, bytes=fsize, sha256=fsha)

        # 1) interrupted transfer: the network drops after the first 8 MiB chunk of the master
        real_put, calls = gdrive.GoogleDriveProvider.put_chunk, {"n": 0, "after_resume": 0, "drop": True, "offsets": []}

        def flaky(self, session, offset, data, size):
            calls["n"] += 1
            if calls["drop"] and calls["n"] == 2 and size == fsize:
                raise ConnectionError("simulated network drop during the second chunk")
            if not calls["drop"] and size == fsize:
                calls["after_resume"] += 1
                calls["offsets"].append(offset)
            return real_put(self, session, offset, data, size)
        gdrive.GoogleDriveProvider.put_chunk = flaky
        out1 = archive.archive_output(p.id, "renders/disposable_archive_test.mp4", release_local=True)
        rec = next(r for r in archive.records(p.id) if r["kind"] == "master")
        step("upload interrupted mid-transfer: record failed, local copy kept, nothing released",
             rec["state"] == "failed" and final.exists() and not out1.get("released"), state=rec["state"], error=rec.get("last_error"))
        from nexus.cloud.storage import transfers as tx
        t = tx.get(rec["transfer_id"]) or {}
        step("transfer kept its resumable session and offset", bool(t.get("session_uri")) and int(t.get("sent") or 0) > 0,
             sent=t.get("sent"), size=t.get("size"))

        # 2) resume after "restart": recover() retries the failed record and continues the same session
        calls["drop"] = False
        rr = archive.recover(now=time.time() + 10 ** 7)
        rec = archive.get(rec["id"])
        resumed = any("resumed the interrupted transfer" in e[1] for e in rec["events"])
        total_chunks = -(-fsize // (8 * 2 ** 20))
        step("resumed the interrupted transfer (same session, did not restart from byte 0)",
             rec["state"] == "verified" and resumed and calls["after_resume"] < total_chunks and min(calls["offsets"] or [0]) > 0,
             first_offset_after_resume=min(calls["offsets"] or [0]),
             recover=rr, chunks_after_resume=calls["after_resume"], total_chunks=total_chunks,
             verification=rec.get("verification", {}).get("method"))
        gdrive.GoogleDriveProvider.put_chunk = real_put

        # 3) independent check: full download from Drive, byte-identical
        prov = get_provider(a.connection)
        dl = home / "tmp" / "independent_download.mp4"
        dl.parent.mkdir(parents=True, exist_ok=True)
        prov.download(rec["remote"]["ref"], dl)
        st = prov.stat(rec["remote"]["ref"])
        step("independent full download is byte-identical (SHA-256) and Drive's md5 matches",
             sha(dl) == fsha and dl.stat().st_size == fsize and (st.md5 or "").lower() == hashlib.md5(final.read_bytes()).hexdigest(),
             remote_path=rec["remote"].get("path"), drive_md5=st.md5, size=st.size)
        dl.unlink()

        # 4) other files of the deliverable (thumbnail, manifest) and the archive manifest
        for r in archive.records(p.id):
            if r["state"] not in ("verified", "released"):
                archive.upload(r["id"])
        recs = archive.records(p.id)
        step("every file of the deliverable verified (master, thumbnail, manifest)", all(r["state"] in ("verified", "released") for r in recs),
             files=[(r["kind"], r["name"], r["state"], (r.get("verification") or {}).get("method")) for r in recs])
        mf = json.loads((config.HOME / "archive" / p.id / "disposable_archive_test.archive.json").read_text())
        step("archive manifest lists the master with SHA-256 and recovery instructions",
             any(f["sha256"] == fsha for f in mf["files"]) and "retrieve" in mf["recovery"]["how"], contract=mf["contract"])
        m = artifacts.get(rec["remote"]["artifact_id"])
        step("Account 1 registry: kind master, retention archive (never auto-cleaned)", m.kind == "master" and m.retention == "archive",
             retention=m.retention)

        # 5) release only after a fresh remote verification
        rel = archive.release(rec["id"])
        step("local final deleted only after a fresh re-verification", rel["state"] == "released" and not final.exists()
             and rel["release_verification"]["ok"], method=rel["release_verification"]["method"],
             reverified_at=rel["release_verification"]["at"], verified_at=rec["verification"]["at"])

        # 6) browse + retrieve
        b = archive.browse(p.id)
        step("archive browser lists the released video", any(x.get("state") == "released" for x in b), entries=len(b))
        got = archive.retrieve(rec["id"])
        step("retrieve: downloaded back, SHA-256 identical", sha(Path(got)) == fsha, bytes=Path(got).stat().st_size)
        ev["remote_files_left_on_drive"] = [{"kind": r["kind"], "path": (r.get("remote") or {}).get("path"), "bytes": r["size"]}
                                            for r in archive.records(p.id)]
        ev["final"] = {"bytes": fsize, "sha256": fsha}
    except Exception as e:  # noqa: BLE001
        step("unexpected error", False, error=f"{type(e).__name__}: {e}"[:500])
    finally:
        shutil.rmtree(home, ignore_errors=True)   # the copied vault entries and database go with it
        ev["temp_home_removed"] = not home.exists()
    ev["ok"] = all(s["ok"] for s in ev["steps"])
    from nexus import vault
    import re
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", json.dumps(ev, indent=1, default=str))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(vault.redact(text), encoding="utf-8")
    print("OK" if ev["ok"] else "FAILED")
    return 0 if ev["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
