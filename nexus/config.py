"""Central paths and settings. Everything NEXUS writes lives under NEXUS_HOME."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.2.0"
HOME = Path(os.environ.get("NEXUS_HOME", ROOT / ".nexus"))
MODELS = HOME / "models"
PROJECTS = HOME / "projects"
CACHE = HOME / "cache"
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
    MODELS, PROJECTS, CACHE, ASSETS = HOME / "models", HOME / "projects", HOME / "cache", HOME / "assets"
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


# Fonts per script come from registry/languages.yaml (`fonts`, referenced by each language's `font`).
def _lang_registry() -> dict:
    import functools

    import yaml

    @functools.lru_cache(maxsize=1)
    def load() -> dict:
        return yaml.safe_load((REGISTRY_DIR / "languages.yaml").read_text(encoding="utf-8"))
    global _lang_registry
    _lang_registry = load
    return load()


def _font_key(lang: str) -> str:
    reg = _lang_registry()
    code = lang if lang in reg["languages"] else reg.get("aliases", {}).get(lang)
    if code is None:
        low = {k.lower(): k for k in reg["languages"]} | {k.lower(): v for k, v in reg.get("aliases", {}).items()}
        code = low.get(str(lang).lower())
    if code is not None:
        return reg["languages"][code]["font"]
    if str(lang).lower().startswith("ar"):
        return "arabic"
    lang = {"han": "han_sc"}.get(lang, lang)
    return lang if lang in reg["fonts"] else "latin"   # a script name ("han", "devanagari", ...) is accepted too


def _has_glyphs(path: str, index: int, sample: str) -> bool:
    """True when every sample character renders differently from the font's missing-glyph box (no tofu)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        f = ImageFont.truetype(path, 32, index=index)

        def px(ch: str) -> bytes:
            im = Image.new("L", (64, 64))
            ImageDraw.Draw(im).text((8, 8), ch, font=f, fill=255)
            return im.tobytes()
        tofu = px("\U0010FFFD")
        return all(px(ch) != tofu and any(px(ch)) for ch in sample)
    except Exception:
        return False


_FACE_CACHE: dict = {}


def font_face_for(lang: str) -> tuple[str, int, str]:
    """(font file, collection index, fontconfig family) for a language code or script name. The first candidate that
    exists AND really contains the script's sample glyphs wins; Latin DejaVu is the last resort."""
    key = _font_key(lang)
    if key not in _FACE_CACHE:
        spec = _lang_registry()["fonts"][key]
        found = None
        for c in spec["files"]:
            f = c["file"].replace("%WINDIR%", str(_WINFONTS.parent))
            if Path(f).exists() and _has_glyphs(f, int(c.get("index", 0)), spec.get("sample", "")):
                found = (f, int(c.get("index", 0)), c.get("family", spec["family"]))
                break
        _FACE_CACHE[key] = found or (FONT_LATIN, 0, "DejaVu Sans")
    return _FACE_CACHE[key]


def font_for(lang: str) -> str:
    """Font file that contains the language's script (TrueType collections: see font_face_for for the index)."""
    return font_face_for(lang)[0]


def font_name_for(lang: str) -> str:
    """fontconfig family name for ASS subtitles (libass), matching the file font_for returns."""
    return font_face_for(lang)[2]


def ensure_dirs() -> None:
    for d in (HOME, MODELS, PROJECTS, CACHE, ASSETS, LOGS, RUN):
        d.mkdir(parents=True, exist_ok=True)
