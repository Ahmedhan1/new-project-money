"""/api/v1/system: the control centre's service panel (owner only): status, stop, restart, update check/install.

Stop and restart run in a detached `python -m nexus stop|restart` process started after the response is sent, so the
request is answered before the API process goes away. Updates come only from this repository's GitHub releases: the
installer asset is downloaded, its SHA-256 is checked against the release's `<asset>.sha256` file, and on Windows the
verified installer is started (it stops the app, upgrades the program folder and keeps the user data). Nothing is
installed when the checksum is missing or differs.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from .. import config
from . import audit, security

router = APIRouter(prefix="/api/v1/system", tags=["system"], dependencies=[Depends(security.require_owner)])
RELEASES = "https://api.github.com/repos/Ahmedhan1/new-project-money/releases/latest"
ASSET = "NEXUS_VIDEO_AI_Setup.exe"


def _detached(args: list[str]) -> None:
    kw: dict = {"creationflags": 0x00000008 | 0x00000200 | 0x08000000} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, "-m", "nexus", *args], cwd=str(config.ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "NEXUS_HOME": str(config.HOME)}, **kw)


@router.get("/status")
def status():
    from .. import launcher
    import shutil
    return {"version": config.VERSION, "installed": config.installed(), "home": str(config.HOME), "services": launcher.service_status(),
            "python": sys.version.split()[0], "ffmpeg": bool(shutil.which("ffmpeg")), "platform": sys.platform}


@router.post("/stop")
def stop(bg: BackgroundTasks):
    audit.record("system.stop")
    bg.add_task(_detached, ["stop"])
    return {"ok": True, "detail": "stopping; start NEXUS again from the desktop shortcut"}


@router.post("/restart")
def restart(bg: BackgroundTasks):
    audit.record("system.restart")
    bg.add_task(_detached, ["restart"])
    return {"ok": True, "detail": "restarting; this page reconnects in a few seconds"}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "nexus-video-ai-updater"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _vtuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.lstrip("v").split("."))


def check_update(fetch=_get_json) -> dict:
    try:
        rel = fetch(RELEASES)
    except Exception as e:  # noqa: BLE001
        return {"current": config.VERSION, "available": False, "error": f"{type(e).__name__}: {e}"[:200]}
    tag = rel.get("tag_name") or ""
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets") or []}
    return {"current": config.VERSION, "latest": tag.lstrip("v"), "available": bool(tag) and _vtuple(tag) > _vtuple(config.VERSION),
            "installer": assets.get(ASSET), "sha256_file": assets.get(ASSET + ".sha256"), "notes": (rel.get("body") or "")[:2000]}


def download_verified(info: dict, dest_dir: Path, opener=urllib.request.urlopen) -> Path:
    if not info.get("installer") or not info.get("sha256_file"):
        raise HTTPException(409, "the release has no installer or no .sha256 file; nothing downloaded")
    with opener(info["sha256_file"], timeout=30) as r:
        want = r.read().decode().split()[0].strip().lower()
    if len(want) != 64:
        raise HTTPException(409, "the release's .sha256 file is malformed")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out, h = dest_dir / ASSET, hashlib.sha256()
    part = out.with_suffix(".part")
    with opener(info["installer"], timeout=60) as r, open(part, "wb") as f:
        for b in iter(lambda: r.read(1 << 20), b""):
            h.update(b)
            f.write(b)
    if h.hexdigest() != want:
        part.unlink(missing_ok=True)
        raise HTTPException(409, f"installer SHA-256 mismatch ({h.hexdigest()} != {want}); deleted, nothing installed")
    os.replace(part, out)
    return out


@router.get("/update")
def update_check():
    return check_update()


@router.post("/update")
def update_install():
    info = check_update()
    if not info.get("available"):
        return {"ok": False, "detail": "no newer release", **info}
    exe = download_verified(info, config.HOME / "updates")
    audit.record("system.update", version=info.get("latest"))
    if os.name != "nt" or not config.installed():
        return {"ok": True, "verified": True, "path": str(exe), "detail": "verified; run the installer on Windows to upgrade"}
    subprocess.Popen([str(exe), "/SILENT", "/NORESTART"], creationflags=0x00000008 | 0x00000200)
    return {"ok": True, "verified": True, "path": str(exe), "detail": "installer started; NEXUS restarts when it finishes"}
