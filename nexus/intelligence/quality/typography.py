"""Typography gate: every on-screen string (titles, lower thirds, captions, credits) must be drawable with a real glyph
for each character AND survive a frame-level read-back.

check_text(text, lang)  -> coverage per character (fontTools cmaps of the fallback chain), fonts chosen per run
render_check(text, lang) -> renders the string exactly as the renderer does, then reads it back with Tesseract OCR
                            (eng/ara/chi_sim/hin/rus/deu) and reports character recall. OCR is evidence, not proof:
                            low recall flags a frame for a human look; missing glyphs are a hard failure."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from ...timeline import fonts

OCR_LANG = {"en": "eng", "de": "deu", "fr": "eng", "ar": "ara", "ar-EG": "ara", "ar-Gulf": "ara", "zh": "chi_sim", "ru": "rus", "hi": "hin",
            "es": "eng", "pt": "eng", "it": "eng", "tr": "eng", "ja": "jpn", "ko": "kor"}  # Latin-script langs read with eng


def check_text(text: str, lang: str = "en") -> dict:
    miss = fonts.missing(text)
    runs = [{"text": t, "font": Path(f).name, "script": s} for t, f, s in fonts.visual_runs(text)]
    return {"ok": not miss, "missing_glyphs": miss, "runs": runs,
            "finding": None if not miss else f"no installed font can draw {miss} - would render as empty boxes"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return "".join(ch for ch in s if ch.isalnum())


def ocr_available(lang: str = "en") -> bool:
    if not shutil.which("tesseract"):
        return False
    have = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()
    code = OCR_LANG.get(lang)
    return bool(code) and all(c in have for c in code.split("+"))  # unknown language: no read-back, never English OCR


def render_check(text: str, lang: str = "en", width: int = 1600, size_rel: float = 0.05) -> dict:
    """Render like the product (same fonts/runs/shaping), without shadow, on white; OCR it back."""
    from PIL import Image
    from ...timeline import text as ttext
    res = check_text(text, lang)
    img = Image.new("RGBA", (width, 220), (255, 255, 255, 255))
    ttext.STYLES.setdefault("_qa", (size_rel, 0.5, (255, 255, 255, 255), (0, 0, 0, 255), False))  # boxed style = no shadow
    ttext.draw_text_block(img, text, "_qa", lang)
    out = {**res, "ocr": None, "recall": None}
    if not ocr_available(lang):
        out["ocr_note"] = f"tesseract with '{OCR_LANG.get(lang, 'eng')}' not installed - frame read-back skipped"
        return out
    langs = OCR_LANG.get(lang, "eng")
    if any(fonts.script_of(c) == "latin" for c in text) and "eng" not in langs:
        langs += "+eng"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.png"
        img.convert("RGB").save(p)
        txt = subprocess.run(["tesseract", str(p), "-", "-l", langs, "--psm", "7"], capture_output=True, text=True).stdout.strip()
    a, b = _norm(text), _norm(txt)
    recall = SequenceMatcher(None, a, b).ratio() if a else 1.0
    out.update(ocr=txt, recall=round(recall, 3), ocr_ok=recall >= 0.6)
    return out
