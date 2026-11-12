"""On-demand model cache: checks disk space, downloads through the Cost Guard, records provenance."""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from . import config, costguard

HF = "https://huggingface.co"


@functools.lru_cache(maxsize=1)
def catalog() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "models.yaml").read_text())


def _fetch(url: str, dest: Path) -> None:
    costguard.check_host(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    # curl gives resumable downloads (-C -) for multi-GB files.
    r = subprocess.run(["curl", "-sSL", "--fail", "-C", "-", "-A", config.USER_AGENT, "-o", str(tmp), url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"download failed for {url}: {r.stderr.strip()}")
    tmp.rename(dest)


def _revision(repo: str) -> str:
    try:
        from . import net
        return net.get_json(f"{HF}/api/models/{repo}").get("sha", "unknown")
    except Exception:
        return "unknown"


def _record(name: str, info: dict) -> None:
    p = config.MODELS / "installed.json"
    data = json.loads(p.read_text()) if p.exists() else {}
    data[name] = info
    p.write_text(json.dumps(data, indent=2))


def installed() -> dict:
    p = config.MODELS / "installed.json"
    return json.loads(p.read_text()) if p.exists() else {}


def ensure_model(name: str) -> Path:
    m = catalog()["models"][name]
    d = config.MODELS / m["dir"]
    missing = [f for f in m["files"] if not (d / f).exists() or (d / f).stat().st_size == 0]
    if missing:
        config.ensure_dirs()  # fresh clone: NEXUS_HOME does not exist yet
        free_mb = shutil.disk_usage(config.HOME).free / 1e6
        if free_mb < m["size_mb"] * 1.5 + 2000:
            raise RuntimeError(f"not enough disk for {name}: need ~{m['size_mb']} MB, {free_mb:.0f} MB free")
        for f in missing:
            _fetch(f"{HF}/{m['repo']}/resolve/main/{f}", d / f)
        _record(name, {"repo": m["repo"], "revision": _revision(m["repo"]), "license": m["license"],
                       "files": m["files"], "downloaded": time.strftime("%Y-%m-%d"), "path": str(d)})
    return d


def ensure_voice(name: str) -> Path:
    v = catalog()["voices"][name]
    config.ensure_dirs()
    d = config.MODELS / "piper"
    onnx = d / f"{name}.onnx"
    for suffix in (".onnx", ".onnx.json"):
        f = d / f"{name}{suffix}"
        if not f.exists() or f.stat().st_size == 0:
            _fetch(f"{HF}/rhasspy/piper-voices/resolve/main/{v['path']}/{name}{suffix}", f)
            _record(f"voice:{name}", {"repo": "rhasspy/piper-voices", "license": v["license"],
                                      "commercial": v["commercial"], "downloaded": time.strftime("%Y-%m-%d")})
    return onnx


def voice_for(lang: str, gender: str | None = None) -> str:
    """Piper voice for a language. Registry languages use their `voice` entry (null -> KeyError, so the caller falls back
    to eSpeak NG); other codes match the catalogue by base language, preferring commercially cleared voices."""
    entry = language(lang, strict=False)
    if entry is not None:
        if not entry.get("voice"):
            raise KeyError(f"no commercially cleared Piper voice for {entry['code']} - {entry.get('reason', '')}")
        if gender is None or catalog()["voices"][entry["voice"]].get("gender") == gender:
            return entry["voice"]
    base = lang.split("-")[0]
    cands = [(n, v) for n, v in catalog()["voices"].items() if v["lang"] in (lang, base) or v["lang"].split("-")[0] == base]
    if not cands:
        raise KeyError(f"no voice for language {lang}")
    cands.sort(key=lambda nv: (not nv[1]["commercial"], nv[1].get("gender") != gender if gender else 0))
    return cands[0][0]


# ---------------------------------------------------------------- language registry (registry/languages.yaml)
STATUSES = ("available", "subtitles-only", "unavailable")
# Unicode ranges per script; letters outside the target script count as code-switching.
SCRIPT_RANGES = {
    "latin": [(0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F), (0x1E00, 0x1EFF)],
    "cyrillic": [(0x400, 0x52F), (0x1C80, 0x1C8F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F)],
    "arabic": [(0x600, 0x6FF), (0x750, 0x77F), (0x8A0, 0x8FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)],
    "devanagari": [(0x900, 0x97F), (0xA8E0, 0xA8FF)],
    "han": [(0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F), (0x3000, 0x303F)],
}
# Digits of other numeral systems -> ASCII, for number-preservation checks.
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹०१२३४५६७८९０１２３４５６７８９", "0123456789" * 4)


@functools.lru_cache(maxsize=1)
def _lang_file() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "languages.yaml").read_text(encoding="utf-8"))


def languages(status: str | None = None) -> dict:
    """{code: entry} in priority order; `status` filters (e.g. "available")."""
    out = {}
    for code, e in sorted(_lang_file()["languages"].items(), key=lambda kv: kv[1].get("priority", 999)):
        if status is None or e["status"] == status:
            out[code] = {"code": code, **e}
    return out


def resolve_lang(code: str) -> str | None:
    """Registry code for `code` (exact, alias, case-insensitive), else None."""
    langs, aliases = _lang_file()["languages"], _lang_file().get("aliases", {})
    if code in langs:
        return code
    if code in aliases:
        return aliases[code]
    low = {k.lower(): k for k in langs} | {k.lower(): v for k, v in aliases.items()}
    return low.get(str(code).lower())


def language(code: str, strict: bool = True) -> dict | None:
    """Registry entry (with `code`) for a language code or alias. strict=False returns None for unknown codes."""
    c = resolve_lang(code)
    if c is None:
        if strict:
            raise KeyError(f"unknown language {code!r}; known: {', '.join(_lang_file()['languages'])}")
        return None
    return {"code": c, **_lang_file()["languages"][c]}


def script_info(script: str) -> dict:
    return _lang_file()["scripts"][script]


def dialect_markers(name: str) -> dict:
    return _lang_file()["dialect_markers"][name]


def in_script(ch: str, script: str) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in SCRIPT_RANGES[script])


def char_script(ch: str) -> str | None:
    for s in SCRIPT_RANGES:
        if in_script(ch, s):
            return s
    return None


def script_of(text: str) -> str:
    """Dominant script of a text's letters ("latin" when there are none)."""
    counts: dict = {}
    for ch in text:
        if ch.isalpha():
            s = char_script(ch)
            if s:
                counts[s] = counts.get(s, 0) + 1
    return max(counts, key=counts.get) if counts else "latin"


def script_for(lang: str) -> str:
    e = language(lang, strict=False)
    return e["script"] if e else ("arabic" if lang.lower().startswith("ar") else "latin")


def fetch_all() -> None:
    for name in catalog()["models"]:
        print("model", name, "->", ensure_model(name))
    for name in catalog()["voices"]:
        print("voice", name, "->", ensure_voice(name))
