"""Central paths and settings. Everything NEXUS writes lives under NEXUS_HOME."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.2.0"
HOME = Path(os.environ.get("NEXUS_HOME", ROOT / ".nexus"))
MODELS = HOME / "models"
PROJECTS = HOME / "projects"
# Working storage (render segments, pieces, proxies, temp files) can live on a faster/bigger local disk.
CACHE = Path(os.environ["NEXUS_WORK_DIR"]) if os.environ.get("NEXUS_WORK_DIR") else HOME / "cache"
ASSETS = HOME / "assets"
DB_PATH = HOME / "nexus.db"
LOGS = HOME / "logs"
RUN = HOME / "run"
BACKUPS = HOME / "backups"
DEFAULT_PORT = 8765


def set_home(path: str | Path) -> None:
    """Point every data path at another NEXUS_HOME (tests, restore drills, relocated installs)."""
    global HOME, MODELS, PROJECTS, CACHE, ASSETS, DB_PATH, LOGS, RUN, BACKUPS
    HOME = Path(path)
    MODELS, PROJECTS, ASSETS = HOME / "models", HOME / "projects", HOME / "assets"
    CACHE = Path(os.environ["NEXUS_WORK_DIR"]) if os.environ.get("NEXUS_WORK_DIR") else HOME / "cache"
    DB_PATH, LOGS, RUN, BACKUPS = HOME / "nexus.db", HOME / "logs", HOME / "run", HOME / "backups"


def settings() -> dict:
    """User settings (HOME/settings.json, never committed). Environment variables win."""
    import json
    f = HOME / "settings.json"
    data = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    if os.environ.get("NEXUS_PORT"):
        data["port"] = int(os.environ["NEXUS_PORT"])
    data.setdefault("port", DEFAULT_PORT)
    data.setdefault("host", "127.0.0.1")
    data.setdefault("min_free_disk_gb", 2.0)
    data.setdefault("backup_every_hours", 24)
    return data
REGISTRY_DIR = Path(__file__).resolve().parent / "registry"

USER_AGENT = "nexus-video-ai/0.1 (zero-spend local video production tool)"

# Uniform intermediate format: every rendered segment uses these so segments
# can be stream-copied together and cached individually.
FPS = 30
AUDIO_RATE = 48000
VCODEC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
ACODEC = ["-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2"]

_WINFONTS = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
FONT_LATIN_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    str(_WINFONTS / "segoeuib.ttf"), str(_WINFONTS / "arialbd.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
FONT_ARABIC_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    str(_WINFONTS / "tahomabd.ttf"), str(_WINFONTS / "segoeuib.ttf"), str(_WINFONTS / "arialbd.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _first_existing(cands: list[str]) -> str:
    return next((f for f in cands if Path(f).exists()), cands[0])


FONT_LATIN = _first_existing(FONT_LATIN_CANDIDATES)


def font_for(lang: str) -> str:
    return _first_existing(FONT_ARABIC_CANDIDATES) if lang.startswith("ar") else FONT_LATIN


def ensure_dirs() -> None:
    for d in (HOME, MODELS, PROJECTS, CACHE, ASSETS, LOGS, RUN):
        d.mkdir(parents=True, exist_ok=True)
