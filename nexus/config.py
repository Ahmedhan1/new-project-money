"""Central paths and settings. Everything NEXUS writes lives under NEXUS_HOME."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("NEXUS_HOME", ROOT / ".nexus"))
MODELS = HOME / "models"
PROJECTS = HOME / "projects"
CACHE = HOME / "cache"
ASSETS = HOME / "assets"
DB_PATH = HOME / "nexus.db"
REGISTRY_DIR = Path(__file__).resolve().parent / "registry"

USER_AGENT = "nexus-video-ai/0.1 (zero-spend local video production tool)"

# Uniform intermediate format: every rendered segment uses these so segments
# can be stream-copied together and cached individually.
FPS = 30
AUDIO_RATE = 48000
VCODEC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
ACODEC = ["-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2"]

FONT_LATIN = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_ARABIC_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def font_for(lang: str) -> str:
    if lang.startswith("ar"):
        for f in FONT_ARABIC_CANDIDATES:
            if Path(f).exists():
                return f
    return FONT_LATIN


def ensure_dirs() -> None:
    for d in (HOME, MODELS, PROJECTS, CACHE, ASSETS):
        d.mkdir(parents=True, exist_ok=True)
