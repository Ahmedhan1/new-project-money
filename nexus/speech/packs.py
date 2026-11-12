"""Language packs: loader, schema validation, script detection and typography.

The packs live in `nexus/registry/languages.yaml` (one entry per BCP-47-style tag). This module has no heavy
dependencies so that `config`, `timeline.subs`, `timeline.text` and `localize` can all use it.
Public entry points are re-exported by `nexus.localize` (`packs()`, `pack(tag)`).
"""
from __future__ import annotations

import functools
import re
import unicodedata

import yaml

from .. import config

LICENCE_STATES = ("yes", "no", "unverified")
DIRECTIONS = ("ltr", "rtl")
SCRIPTS = ("Latn", "Cyrl", "Arab", "Hans", "Hant", "Jpan", "Deva")
LINE_BREAKS = ("space", "char")

# ------------------------------------------------------------------ loading


@functools.lru_cache(maxsize=1)
def registry() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "languages.yaml").read_text(encoding="utf-8"))


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@functools.lru_cache(maxsize=None)
def _resolved(tag: str) -> dict:
    reg = registry()
    raw = reg["packs"][tag]
    p = dict(raw)
    p["subtitles"] = _merge(reg.get("subtitle_defaults", {}), raw.get("subtitles", {}))
    p["tag"] = tag
    return p


def tags() -> list[str]:
    return list(registry()["packs"])


def canonical(tag: str) -> str:
    """Map loose spellings (`ar_eg`, `AR-EG`, `ar-gulf`, `zh-CN`) to a pack tag; unknown tags raise KeyError."""
    t = str(tag or "").strip()
    known = {k.lower(): k for k in tags()}
    low = t.replace("_", "-").lower()
    if low in known:
        return known[low]
    alias = {k.lower(): v for k, v in registry().get("aliases", {}).items()}
    if low in alias:
        return alias[low]
    base = low.split("-")[0]
    if base in known:
        return known[base]
    raise KeyError(f"no language pack for '{tag}' (known: {', '.join(tags())})")


def pack(tag: str) -> dict:
    """The full pack for a tag (subtitle defaults merged in). Raises KeyError for an unknown language."""
    return _resolved(canonical(tag))


def get(tag: str) -> dict | None:
    try:
        return pack(tag)
    except KeyError:
        return None


def packs() -> dict[str, dict]:
    return {t: _resolved(t) for t in tags()}


def subtitle_rules(tag: str | None) -> dict:
    p = get(tag or "en")
    return p["subtitles"] if p else dict(registry().get("subtitle_defaults", {}))


def font_sets() -> dict:
    return registry().get("font_sets", {})


# ------------------------------------------------------------------ schema

def _req(d: dict, keys: list[str], where: str, errs: list[str]) -> None:
    for k in keys:
        if k not in d or d[k] in (None, ""):
            errs.append(f"{where}: missing '{k}'")


def _licence(d: dict, where: str, errs: list[str]) -> None:
    cu = d.get("commercial_use")
    if cu not in LICENCE_STATES:
        errs.append(f"{where}: commercial_use must be one of {LICENCE_STATES} (quoted), got {cu!r}")
    if not d.get("licence"):
        errs.append(f"{where}: missing 'licence'")


def validate(tag: str, p: dict | None = None) -> list[str]:
    """Schema check of one pack. Returns a list of problems (empty = valid)."""
    p = p if p is not None else pack(tag)
    errs: list[str] = []
    _req(p, ["name", "native_name", "script", "direction", "fonts", "asr", "mt", "tts", "subtitles", "typography", "status"], tag, errs)
    if p.get("script") not in SCRIPTS:
        errs.append(f"{tag}: script {p.get('script')!r} not in {SCRIPTS}")
    if p.get("direction") not in DIRECTIONS:
        errs.append(f"{tag}: direction must be ltr|rtl")
    for s in p.get("fonts", []) or []:
        if s not in font_sets():
            errs.append(f"{tag}: unknown font set {s!r}")
    asr = p.get("asr") or {}
    _req(asr, ["model", "code"], f"{tag}.asr", errs)
    _licence(asr, f"{tag}.asr", errs)
    mt = p.get("mt") or {}
    _req(mt, ["model", "target", "length_ratio"], f"{tag}.mt", errs)
    _licence(mt, f"{tag}.mt", errs)
    supported = registry().get("mt_supported_targets", [])
    if mt.get("target") and supported and mt["target"] not in supported:
        errs.append(f"{tag}.mt: target {mt['target']!r} is not a verified MADLAD-400 tag")
    tts = p.get("tts") or {}
    if not isinstance(tts.get("voices"), list):
        errs.append(f"{tag}.tts: 'voices' must be a list (may be empty)")
    for i, v in enumerate(tts.get("voices") or []):
        w = f"{tag}.tts.voices[{i}]"
        _req(v, ["id", "engine", "source_url", "licence", "dataset_licence", "card_read_on"], w, errs)
        _licence(v, w, errs)
        if v.get("engine") == "piper" and not v.get("path"):
            errs.append(f"{w}: piper voice needs 'path'")
    fb = tts.get("fallback") or {}
    _req(fb, ["engine", "voice"], f"{tag}.tts.fallback", errs)
    _licence(fb, f"{tag}.tts.fallback", errs)
    st = p.get("subtitles") or {}
    for k in ("max_chars_per_line", "max_lines", "max_cps", "line_break", "punctuation", "numerals", "karaoke"):
        if k not in st:
            errs.append(f"{tag}.subtitles: missing '{k}'")
    if st.get("line_break") not in LINE_BREAKS:
        errs.append(f"{tag}.subtitles.line_break must be one of {LINE_BREAKS}")
    if not isinstance(st.get("max_chars_per_line", 0), int) or not isinstance(st.get("max_lines", 0), int):
        errs.append(f"{tag}.subtitles: max_chars_per_line / max_lines must be integers")
    d = p.get("dialect")
    if d is not None:
        _req(d, ["base", "adaptation", "lexicon", "llm_style", "quality_statement", "pronunciation"], f"{tag}.dialect", errs)
        if not p.get("needs_native_review"):
            errs.append(f"{tag}: a dialect profile must set needs_native_review: true")
        for i, e in enumerate(d.get("lexicon") or []):
            if not e.get("msa") or not e.get("dialect"):
                errs.append(f"{tag}.dialect.lexicon[{i}]: needs 'msa' and 'dialect'")
    return errs


def validate_all() -> dict[str, list[str]]:
    return {t: validate(t) for t in tags()}


# ------------------------------------------------------------------ scripts

def char_script(ch: str) -> str:
    o = ord(ch)
    if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0x08A0 <= o <= 0x08FF or 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
        return "Arab"
    if 0x0400 <= o <= 0x052F:
        return "Cyrl"
    if 0x0900 <= o <= 0x097F or 0xA8E0 <= o <= 0xA8FF:
        return "Deva"
    if 0x3040 <= o <= 0x309F:
        return "Hira"
    if 0x30A0 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
        return "Kana"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FFFF:
        return "Hani"
    if 0xAC00 <= o <= 0xD7AF:
        return "Hang"
    if 0x0590 <= o <= 0x05FF:
        return "Hebr"
    if 0x0370 <= o <= 0x03FF:
        return "Grek"
    if ch.isascii() and ch.isalpha() or 0x00C0 <= o <= 0x024F or 0x1E00 <= o <= 0x1EFF:
        return "Latn"
    return "Zyyy"  # common / unknown


# Which character scripts count as "own script" for each pack script.
OWN = {"Latn": {"Latn"}, "Cyrl": {"Cyrl"}, "Arab": {"Arab"}, "Deva": {"Deva"}, "Hans": {"Hani"}, "Hant": {"Hani"},
       "Jpan": {"Hani", "Hira", "Kana"}}
CJK = {"Hans", "Hant", "Jpan"}


def script_profile(text: str) -> dict[str, int]:
    """Letter counts per script (marks, digits, punctuation and spaces ignored)."""
    out: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        s = char_script(ch)
        out[s] = out.get(s, 0) + 1
    return out


def own_share(text: str, tag: str) -> float:
    prof = script_profile(text)
    total = sum(prof.values())
    if not total:
        return 1.0
    own = OWN.get(pack(tag)["script"], set())
    return sum(n for s, n in prof.items() if s in own) / total


def foreign_runs(text: str, tag: str, allow: list[str] | tuple = ()) -> list[str]:
    """Runs of letters written in a script that is not the pack's own (allow-listed terms excluded)."""
    own = OWN.get(pack(tag)["script"], set())
    t = text
    for a in sorted(allow, key=len, reverse=True):
        if a:
            t = t.replace(a, " ")
    runs, cur = [], ""
    for ch in t:
        if ch.isalpha() and char_script(ch) not in own:
            cur += ch
        elif cur and (unicodedata.category(ch).startswith("M") or ch in "'-’"):
            cur += ch
        else:
            if cur:
                runs.append(cur)
            cur = ""
    if cur:
        runs.append(cur)
    return [r for r in runs if any(c.isalpha() for c in r)]


# ------------------------------------------------------------------ bidi

BIDI_EMBED_OPEN = "\u202a\u202b\u202d\u202e"   # LRE RLE LRO RLO
BIDI_EMBED_CLOSE = "\u202c"                     # PDF
BIDI_ISOLATE_OPEN = "\u2066\u2067\u2068"        # LRI RLI FSI
BIDI_ISOLATE_CLOSE = "\u2069"                   # PDI
BIDI_MARKS = "\u200e\u200f\u061c"               # LRM RLM ALM
BIDI_ALL = BIDI_EMBED_OPEN + BIDI_EMBED_CLOSE + BIDI_ISOLATE_OPEN + BIDI_ISOLATE_CLOSE + BIDI_MARKS


def strip_bidi(text: str) -> str:
    return "".join(ch for ch in text if ch not in BIDI_ALL)


def bidi_problems(text: str, direction: str) -> list[str]:
    """Unbalanced embeddings/isolates, or explicit controls in a left-to-right pack."""
    probs = []
    for line in text.split("\n"):
        depth_e = depth_i = 0
        for ch in line:
            if ch in BIDI_EMBED_OPEN:
                depth_e += 1
            elif ch == BIDI_EMBED_CLOSE:
                depth_e -= 1
            elif ch in BIDI_ISOLATE_OPEN:
                depth_i += 1
            elif ch == BIDI_ISOLATE_CLOSE:
                depth_i -= 1
            if depth_e < 0 or depth_i < 0:
                probs.append(f"closing control without an opener in {line!r}")
                break
        if depth_e > 0 or depth_i > 0:
            probs.append(f"unclosed embedding/isolate in {line!r}")
        if direction == "ltr" and any(ch in BIDI_EMBED_OPEN + BIDI_ISOLATE_OPEN + "\u202e\u200f" for ch in line) and not any(
                char_script(c) in ("Arab", "Hebr") for c in line):
            probs.append(f"right-to-left control characters in a left-to-right line {line!r}")
    return probs


def isolate_ltr_runs(text: str) -> str:
    """Wrap Latin/digit runs inside an RTL line in LRI…PDI so punctuation next to them lands on the right side.
    Players without isolate support ignore the controls; qa checks they stay balanced."""
    return re.sub(r"([A-Za-z][A-Za-z0-9 .\-+/%]*[A-Za-z0-9%]|[A-Za-z])", "\u2066\\1\u2069", text)


# ------------------------------------------------------------------ typography

DIGITS = {
    "latn": "0123456789",
    "arab": "٠١٢٣٤٥٦٧٨٩",   # Arabic-Indic (used in Egypt and the Gulf)
    "deva": "०१२३४५६७८९",
}


def convert_numerals(text: str, system: str) -> str:
    target = DIGITS.get(system)
    if not target:
        return text
    table = {}
    for src in DIGITS.values():
        for i, ch in enumerate(src):
            table[ord(ch)] = target[i]
    out = text.translate(table)
    if system == "arab":  # decimal separator / percent in Arabic typography
        out = re.sub(r"(?<=[٠-٩])[.,](?=[٠-٩])", "٫", out).replace("%", "٪")
    return out


_NBSP, _NNBSP = "\u00a0", "\u202f"


def typography(text: str, tag: str) -> str:
    """Apply the pack's typographic conventions. Idempotent."""
    p = pack(tag)
    ty = p.get("typography", {})
    t = unicodedata.normalize("NFC", text)
    q = ty.get("quotes")
    if q and len(q) == 2:
        inner = _NBSP if ty.get("quote_inner_space") else ""
        t = re.sub(r'"\s*([^"]+?)\s*"', lambda m: f"{q[0]}{inner}{m.group(1)}{inner}{q[1]}", t)
        if inner:
            t = re.sub(rf"{re.escape(q[0])}\s*", q[0] + inner, t)
            t = re.sub(rf"\s*{re.escape(q[1])}", inner + q[1], t)
    if ty.get("space_before_high_punct"):  # French: narrow no-break space before ; ! ? and no-break space before :
        t = re.sub(r"[ \u00a0\u202f]*([;!?]+)", lambda m: _NNBSP + m.group(1), t)
        t = re.sub(r"(?<!\d)[ \u00a0\u202f]*:(?!//)", _NBSP + ":", t)
        t = re.sub(r"^[\u00a0\u202f]+", "", t)
    sub = ty.get("punct_map") or {}
    if sub:
        own = OWN.get(p["script"], set())
        chars = list(t)
        for i, ch in enumerate(chars):
            if ch in sub:
                prev = next((c for c in reversed(chars[:i]) if not c.isspace()), "")
                if prev and prev.isalpha() and char_script(prev) in own:
                    chars[i] = sub[ch]
        t = "".join(chars)
        if ty.get("full_width"):
            t = re.sub(r"(?<=[，。！？：；、])\s+", "", t)
    if ty.get("sentence_end"):
        end = ty["sentence_end"]
        own = OWN.get(p["script"], set())
        t = "".join(end if ch == "." and i and char_script(t[i - 1]) in own and (i + 1 == len(t) or t[i + 1].isspace())
                    else ch for i, ch in enumerate(t))
    if p["subtitles"].get("numerals") and p["subtitles"]["numerals"] != "latn":
        t = convert_numerals(t, p["subtitles"]["numerals"])
    return t
