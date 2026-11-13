"""Reproducible evidence runs for Account 1 storage/ingest (writes docs/results/account1/*.json).

    python -m nexus.cloud.storage.evidence ingest  [URL]   live public-domain media ingestion -> LocalProvider
    python -m nexus.cloud.storage.evidence ssrf             SSRF / platform refusal suite with the REAL resolver
    python -m nexus.cloud.storage.evidence resume  [FILE]   resumable upload killed with SIGKILL, resumed by a new
                                                            process, against the local fake Drive (separate process)
Each run uses a throw-away NEXUS_HOME (set NEXUS_HOME before running to choose it). The fake-Drive run is protocol
evidence only; real Google Drive evidence needs the owner (tests/live/test_drive_live.py, Demo B).
"""
from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs" / "results" / "account1"
NASA = ("http://images-assets.nasa.gov/video/jsc2022m000270_Orion_First_Imagery_Timelapse_and_Original_221117/"
        "jsc2022m000270_Orion_First_Imagery_Timelapse_and_Original_221117~mobile.mp4")
CLIENT = {"client_id": "evidence.apps.googleusercontent.com", "client_secret": "evidence-secret-EXAMPLE"}


def _write(name: str, data: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    data = {"date": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "platform": platform.platform(),
            "python": platform.python_version(), **data}
    p = OUT / name
    p.write_text(json.dumps(data, indent=1, default=str) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- live ingestion
def run_ingest(url: str = NASA) -> dict:
    from ...contracts import MediaIngestionJob, MediaSource
    from .. import ingest
    from .local import LocalProvider
    t0 = time.time()
    job = MediaIngestionJob(source=MediaSource(kind="http_url", ref=url, rights="public_domain",
                                               licence="NASA media usage guidelines (US Government work, no copyright)",
                                               attribution="NASA/JSC - Orion first imagery timelapse (Artemis I)"),
                            project_id="evidence", max_bytes=64 * 2 ** 20)
    j = ingest.run(job)
    dt = time.time() - t0
    out = {"url": url, "status": j.status, "reason": j.reason, "seconds": round(dt, 2)}
    if j.artifact:
        m = j.artifact
        lp = LocalProvider()
        lo = m.locations[0]
        recomputed = lp.checksum(lo.ref)
        out.update(artifact_id=m.artifact_id, mime=m.mime, size=m.size, sha256=m.sha256, location=lo.model_dump(),
                   recomputed_sha256_matches=recomputed["sha256"] == m.sha256, provenance=m.provenance,
                   mbit_per_s=round(m.size * 8 / dt / 1e6, 2))
        again = ingest.run(MediaIngestionJob(source=job.source, project_id="evidence", max_bytes=job.max_bytes))
        out["second_ingest_of_same_url"] = {"status": again.status, "reason": again.reason,
                                            "same_artifact": again.artifact and again.artifact.artifact_id == m.artifact_id}
    out["unknown_rights_check"] = ingest.run(MediaIngestionJob(source=MediaSource(kind="http_url", ref=url))).reason
    _write("ingest_live.json", out)
    return out


# ---------------------------------------------------------------- SSRF with the real resolver
SSRF_CASES = [
    "http://localhost/a.mp4", "http://127.0.0.1/a.mp4", "http://2130706433/a.mp4", "http://0x7f000001/a.mp4",
    "http://0177.0.0.1/a.mp4", "http://127.1/a.mp4", "http://[::1]/a.mp4", "http://[::ffff:127.0.0.1]/a.mp4",
    "http://[::ffff:a9fe:a9fe]/latest", "http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/",
    "http://100.100.100.200/latest/meta-data/", "http://[fd00:ec2::254]/latest/meta-data/",
    "http://127.0.0.1.nip.io/a.mp4", "http://169.254.169.254.nip.io/latest", "http://10.0.0.1.sslip.io/a.mp4",
    "http://192-168-1-1.sslip.io/a.mp4", "http://localtest.me/a.mp4", "http://lvh.me/a.mp4", "http://vcap.me/a.mp4",
    "http://images-assets.nasa.gov:8080/a.mp4", "http://user:pass@images-assets.nasa.gov/a.mp4",
    "file:///etc/passwd", "ftp://ftp.example.org/a.mp4", "gopher://127.0.0.1:70/",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ", "https://www.tiktok.com/@nasa/video/1",
    "https://www.instagram.com/reel/abc/", "https://www.facebook.com/watch/?v=1", "https://x.com/NASA/status/1",
    "https://twitter.com/NASA/status/1", "https://vimeo.com/76979871",
    "https://example.com/video.mp4",  # public, but not a declared zero-spend host
]


def run_ssrf() -> dict:
    from .. import ingest
    rows = []
    for url in SSRF_CASES:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        try:
            resolved = ingest.system_resolver(host, 80) if host else []
        except OSError as e:
            resolved = [f"unresolvable: {e.__class__.__name__}"]
        try:
            ingest.validate_url(url, ingest.Policy())
            rows.append({"url": url, "resolved": resolved, "verdict": "ACCEPTED"})
        except ingest.Rejected as e:
            rows.append({"url": url, "resolved": resolved, "verdict": "rejected", "reason": e.reason})
    ok = ingest.validate_url(NASA, ingest.Policy())
    out = {"cases": len(rows), "rejected": sum(r["verdict"] == "rejected" for r in rows), "results": rows,
           "control_accepted": {"url": NASA, "resolved": ok.ips}}
    _write("ingest_ssrf_live.json", out)
    return out


# ---------------------------------------------------------------- resumable upload: kill -9 + resume
def _child(args: list[str]) -> None:
    """Runs in a separate process: point at the fake, then upload or resume."""
    from . import oauth
    from .gdrive import GoogleDriveProvider
    cmd, base, tid = args[0], args[1], args[2]
    oauth.set_endpoints(oauth.fake_endpoints(base))
    p = GoogleDriveProvider("drive_a")
    if cmd == "upload":
        p.upload(Path(args[3]), "NEXUS/Masters/evidence/" + Path(args[3]).name, mime="video/mp4",
                 chunk_size=4 * 256 * 1024, transfer_id=tid)
    else:
        first = []
        t0 = time.time()
        f = p.resume(tid, on_chunk=lambda s, n: first.append(s) if not first else None)
        print(json.dumps({"resumed_from": first[0] if first else None, "seconds": round(time.time() - t0, 2),
                          "ref": f.ref, "sha256": f.sha256, "md5": f.md5, "size": f.size}), flush=True)


def run_resume(src: str | None = None) -> dict:
    import httpx

    from ... import vault
    from . import oauth, transfers
    from .base import sha256_file
    from .gdrive import GoogleDriveProvider
    if not src:
        src = str(Path(os.environ["NEXUS_HOME"]) / "evidence_master.mp4")
        if not Path(src).exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=30",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=30", "-c:v", "libx264", "-b:v", "6M",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", src], check=True)
    size = Path(src).stat().st_size
    sha = sha256_file(src)
    srv = subprocess.Popen([sys.executable, "-m", "nexus.cloud.storage.fake_drive", "--port", "0"], stdout=subprocess.PIPE,
                           text=True)
    base = srv.stdout.readline().strip()
    env = {**os.environ}
    try:
        httpx.post(base + "/__fake/config", json={"client": CLIENT})
        oauth.set_endpoints(oauth.fake_endpoints(base))
        vault.put(oauth.CLIENT_SECRET_NAME, json.dumps(CLIENT))
        s = oauth.device_start("drive_a")
        httpx.post(base + "/__fake/approve", json={"user_code": s["user_code"], "email": "owner-a@example.com"})
        d = json.loads(vault.get("gdevice_drive_a"))
        d["next_poll"] = 0
        vault.put("gdevice_drive_a", json.dumps(d))
        assert oauth.device_poll("drive_a")["status"] == "connected"
        tid = "xfer_evidence_resume"
        child = subprocess.Popen([sys.executable, "-m", "nexus.cloud.storage.evidence", "_child", "upload", base, tid, src],
                                 env=env)
        t0 = time.time()
        while True:  # kill -9 once ~45 % has been acknowledged by the server
            t = transfers.get(tid)
            if t and t["sent"] >= size * 0.45:
                break
            if child.poll() is not None or time.time() - t0 > 120:
                raise RuntimeError("upload child ended before it could be interrupted")
            time.sleep(0.01)
        child.send_signal(signal.SIGKILL)
        child.wait()
        killed = transfers.get(tid)
        status = GoogleDriveProvider("drive_a").query_upload(killed["session_uri"], size)
        httpx.post(base + "/__fake/config", json={"drop_next_put_after": 300000})  # and cut the first resumed chunk
        r = subprocess.run([sys.executable, "-m", "nexus.cloud.storage.evidence", "_child", "resume", base, tid],
                           env=env, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-2000:])
        res = json.loads(r.stdout.strip().splitlines()[-1])
        back = Path(os.environ["NEXUS_HOME"]) / "roundtrip.mp4"
        GoogleDriveProvider("drive_a").download(res["ref"], back)
        out = {"server": "local fake Google Drive (protocol evidence, not real Drive)", "file": Path(src).name,
               "size": size, "sha256": sha, "chunk_bytes": 4 * 256 * 1024,
               "killed_with": "SIGKILL", "db_acknowledged_at_kill": killed["sent"],
               "db_status_after_kill": killed["status"],
               "server_offset_on_status_query": status if isinstance(status, int) else "complete",
               "resumed_from_byte": res["resumed_from"], "resume_seconds": res["seconds"],
               "injected_mid_chunk_connection_cut_on_resume": True,
               "final_status": transfers.get(tid)["status"], "drive_sha256Checksum": res["sha256"],
               "sha256_match": res["sha256"] == sha, "roundtrip_download_sha256_match": sha256_file(back) == sha,
               "bytes_not_resent": res["resumed_from"]}
        _write("storage_resume.json", out)
        return out
    finally:
        srv.terminate()
        oauth.set_endpoints(None)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "_child":
        _child(argv[1:])
        return 0
    os.environ.setdefault("NEXUS_HOME", str(Path("/tmp") / f"nexus_evidence_{os.getpid()}"))
    from ... import config
    config.set_home(os.environ["NEXUS_HOME"])
    config.ensure_dirs()
    fn = {"ingest": run_ingest, "ssrf": run_ssrf, "resume": run_resume}[argv[0]]
    print(json.dumps(fn(*argv[1:]), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
