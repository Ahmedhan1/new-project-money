"""Language matrix: loader, validation and queries (which voices are commercially cleared, which MT to use)."""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

PATH = Path(__file__).with_name("matrix.yaml")
LICENCE_STATES = ("yes", "no", "unverified")


@functools.lru_cache(maxsize=1)
def load() -> dict:
    return yaml.safe_load(PATH.read_text(encoding="utf-8"))


def languages() -> dict[str, dict]:
    return load()["languages"]


def language(tag: str) -> dict:
    langs = languages()
    if tag in langs:
        return langs[tag]
    low = {k.lower(): k for k in langs}
    if tag.lower() in low:
        return langs[low[tag.lower()]]
    raise KeyError(f"language '{tag}' is not in the matrix ({', '.join(langs)})")


def engine(name: str) -> dict:
    for group in load()["engines"].values():
        if name in group:
            return group[name]
    raise KeyError(name)


def validate() -> list[str]:
    """Structural + honesty checks: every licence state is a known quoted value, every voice has a source,
    and nothing marked commercial is a known non-commercial licence."""
    errs = []
    d = load()
    for gname, group in d["engines"].items():
        for name, e in group.items():
            cu = e.get("commercial_use")
            if cu is not None and cu not in LICENCE_STATES:
                errs.append(f"engine {name}: commercial_use {cu!r} not in {LICENCE_STATES}")
            if not e.get("source"):
                errs.append(f"engine {name}: no source")
    for tag, lang in d["languages"].items():
        for k in ("name", "asr", "mt", "voices", "subtitles"):
            if k not in lang:
                errs.append(f"{tag}: missing {k}")
        for v in lang.get("voices") or []:
            if v.get("commercial_use") not in LICENCE_STATES:
                errs.append(f"{tag}/{v.get('id')}: commercial_use must be a quoted yes/no/unverified")
            if not v.get("source"):
                errs.append(f"{tag}/{v.get('id')}: no source")
            lic = str(v.get("licence", "")).upper()
            if v.get("commercial_use") == "yes" and ("-NC" in lic or "NON-COMMERCIAL" in lic):
                errs.append(f"{tag}/{v.get('id')}: marked commercial but licence is non-commercial")
        s = lang.get("subtitles") or {}
        if s.get("source") and s["source"] not in d["subtitle_sources"]:
            errs.append(f"{tag}: unknown subtitle source {s['source']}")
        for m in lang.get("mt") or []:
            if m not in d["engines"]["mt"]:
                errs.append(f"{tag}: unknown MT engine {m}")
            elif d["engines"]["mt"][m].get("commercial_use") != "yes":
                errs.append(f"{tag}: MT option {m} is not commercially usable")
    return errs


def commercial_voices(tag: str, installed_only: bool = False) -> list[dict]:
    out = [v for v in language(tag).get("voices") or [] if v.get("commercial_use") == "yes"]
    return [v for v in out if v.get("installed")] if installed_only else out


def subtitle_rules(tag: str) -> dict:
    return dict(language(tag)["subtitles"])


def summary() -> list[dict]:
    """One row per language for the doc / API."""
    rows = []
    for tag, lang in languages().items():
        vs = lang.get("voices") or []
        rows.append({"tag": tag, "name": lang["name"], "asr": lang["asr"], "mt": lang.get("mt") or [],
                     "voices_total": len(vs), "voices_commercial": [v["id"] for v in vs if v["commercial_use"] == "yes"],
                     "voices_installed_commercial": [v["id"] for v in vs if v["commercial_use"] == "yes" and v.get("installed")],
                     "subtitles": {k: lang["subtitles"].get(k) for k in ("max_chars_per_line", "max_cps", "direction", "segmentation")}})
    return rows
