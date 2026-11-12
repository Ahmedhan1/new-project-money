"""Account 3 / E demo: private/draft PublishingManifests + packages for all 7 destinations, YouTube adapter run up to
the credential check, CSV analytics import, capability matrix + registry diff. NO real upload happens unless an owner
has connected a channel (credentials present) - the evidence JSON states which.

    NEXUS_HOME=/home/user/new-project-money/.nexus .venv/bin/python scripts/account3/publishing_demo.py [--no-llm]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nexus import config, jobs, platforms  # noqa: E402
from nexus.intelligence.publishing import analytics, capabilities, creds, engine, packaging  # noqa: E402
from nexus.intelligence.publishing.adapters import youtube as yt  # noqa: E402

OUT = ROOT / "docs" / "results" / "account3"
TRANSCRIPT = ("Solar panels lose efficiency when they get hot. Solar panels work best when they stay cool, which is why "
              "installers leave an air gap behind them. Cleaning solar panels twice a year keeps output steady.")
TITLE = "Why solar panels hate heat"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def find_render() -> Path | None:
    vids = sorted(config.PROJECTS.glob("**/*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True) if config.PROJECTS.exists() else []
    return vids[0] if vids else None


def test_clip(d: Path, w: int, h: int) -> Path:
    p = d / f"test_{w}x{h}.mp4"
    if not p.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=30:duration=6",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(p)], check=True)
    return p


def cover(video: Path, out: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)], check=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="skip the one router call (deterministic packaging only)")
    a = ap.parse_args()
    t0 = time.time()
    work = config.HOME / "a3_data" / "publishing_demo"
    work.mkdir(parents=True, exist_ok=True)
    render = find_render()
    if render:
        sources = {"landscape": render, "vertical": render}
        source_note = f"existing render {render}"
    else:
        sources = {"landscape": test_clip(work, 1920, 1080), "vertical": test_clip(work, 1080, 1920),
                   "feed": test_clip(work, 1080, 1350)}
        source_note = "no render under NEXUS_HOME/projects; 6 s ffmpeg testsrc2 clips generated (no speech; demo transcript text below)"
    pick = {"youtube": "landscape", "facebook": "landscape", "instagram_feed": "feed"}

    # Connected-account records: references only. No credentials exist in this environment.
    accts = {p: engine.connect_account(p, "demo") for p in capabilities.PLATFORMS}

    out: dict = {"demo": "account3-publishing", "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source_note,
                 "transcript_used": TRANSCRIPT, "credential_backend": creds.backend(), "destinations": {}}
    for p in capabilities.PLATFORMS:
        video = sources.get(pick.get(p, "vertical"), sources["landscape"])
        use_router = (p == "youtube_shorts") and not a.no_llm
        if use_router:
            with jobs.heavy_lock():
                try:
                    meta = packaging.package(p, TRANSCRIPT, title=TITLE, duration_s=6.0, use_router=True)
                finally:
                    try:
                        from nexus import llm
                        llm.unload()
                    except Exception:
                        pass
        else:
            meta = packaging.package(p, TRANSCRIPT, title=TITLE, duration_s=6.0, use_router=False)
        pkg = work / "packages" / p
        pkg.mkdir(parents=True, exist_ok=True)
        cov = cover(video, pkg / "cover.jpg")
        (pkg / "caption.txt").write_text((meta.get("caption") or f"{meta['title']}\n\n{meta['description']}") + "\n", encoding="utf-8")
        steps = platforms.value(p, "manual_upload") or []
        (pkg / "MANUAL_UPLOAD.md").write_text(f"# {p}\n\nVideo: {video}\n\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
                                              + "\n\nNEXUS did not upload anything.\n", encoding="utf-8")
        (pkg / "metadata.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        md = {"title": meta["title"], "description": meta["description"], "tags": [t.lstrip("#") for t in meta["tags"]],
              "caption": meta.get("caption"), "language": "en", "thumbnail": {"uri": str(cov), "text": meta["thumbnail"]["text"]},
              "package_dir": str(pkg)}
        art = {"uri": str(video), "sha256": sha256(video), "bytes": video.stat().st_size, "media_type": "video/mp4", "role": "master"}
        m = engine.create("demo-publishing", art, p, md, connection_id=accts[p]["id"])
        chk = platforms.check(str(video), p)
        out["destinations"][p] = {"manifest_id": m["id"], "deduplicated": bool(m.get("deduplicated")), "visibility": m["visibility"],
                                  "approval": m["approval"], "status": m["status"], "idempotency_key": m["idempotency_key"][:16] + "…",
                                  "package_dir": str(pkg.relative_to(config.HOME)), "text_source": meta["text_source"],
                                  "title": meta["title"], "tags": meta["tags"], "thumbnail_text": meta["thumbnail"]["text"],
                                  "caption_placement": meta["caption_placement"], "packaging_checks_ok": meta["ok"],
                                  "file_check_ok": chk.get("ok"), "file_check_failed": [c["id"] for c in chk.get("checks", []) if not c.get("ok")],
                                  "adapter": capabilities.api(p)["adapter"], "adapter_status": capabilities.api(p)["adapter_status"]}

    # Queue everything once (private/draft, no approval needed) and run the adapters with the REAL transport.
    for p, d in out["destinations"].items():
        cur = engine.get(d["manifest_id"])
        if cur["status"] == "failed":
            engine.retry_failed(cur["id"])
        elif cur["status"] in ("draft", "approved"):
            engine.enqueue(cur["id"])
    results = {m["platform"]: m for m in engine.process(limit=20)}
    uploaded_any = False
    for p, m in results.items():
        tr = m.get("tracking") or {}
        out["destinations"][p].update(status_after_run=m["status"], blocked=tr.get("blocked"), remote=tr.get("remote"))
        uploaded_any |= m["status"] == "uploaded"
    out["youtube_quota_ledger"] = yt.quota_status()
    out["real_upload_happened"] = uploaded_any
    out["statement"] = ("A real upload happened (see remote ids)." if uploaded_any else
                        "NO real upload happened: no owner credentials are configured; every adapter stopped before any "
                        "network call and recorded what the owner must do.")

    imp = analytics.import_csv(ROOT / "tests" / "fast" / "fixtures_publishing" / "youtube_studio_table.csv", "youtube_studio",
                               period="2026-10-01..2026-10-31", project_id="demo-publishing")
    out["analytics_import"] = {"file": "tests/fast/fixtures_publishing/youtube_studio_table.csv",
                               "note": "SYNTHETIC fixture shaped like a YouTube Studio 'Table data' export; numbers are test data, not metrics",
                               "imported": imp["imported"], "skipped": imp["skipped"], "unmapped_columns": imp["unmapped_columns"],
                               "map_verified": imp["map_verified"], "records": imp["records"]}
    out["registry_diff"] = capabilities.registry_diff()
    out["seconds"] = round(time.time() - t0, 1)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "publishing_demo.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    (OUT / "publishing_capability_matrix.json").write_text(json.dumps(capabilities.matrix(), indent=1, ensure_ascii=False, default=str),
                                                          encoding="utf-8")
    print(json.dumps({p: [d["status_after_run"], (d.get("blocked") or {}).get("code"), d["text_source"]]
                      for p, d in out["destinations"].items()}, indent=1))
    print(out["statement"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
