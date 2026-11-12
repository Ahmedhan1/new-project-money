"""Production presets (registry/presets.yaml): named bundles of look, sound and pacing."""
from __future__ import annotations

import copy
import functools

import yaml

from . import config

DEFAULT = "educational"
KENBURNS = {"slow": 1.07, "normal": 1.14, "fast": 1.22}  # zoom range of a still over its scene


@functools.lru_cache(maxsize=1)
def _all() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "presets.yaml").read_text(encoding="utf-8"))["presets"]


def names() -> list[dict]:
    return [{"id": k, "label": v["label"], "size": v.get("size"), "captions": v["captions"]["style"]} for k, v in _all().items()]


def get(name: str | None, **overrides) -> dict:
    """A preset as a style dict. Unknown names fall back to the default; overrides win (None values are ignored)."""
    p = copy.deepcopy(_all().get(name or DEFAULT) or _all()[DEFAULT])
    p["preset"] = name if name in _all() else DEFAULT
    p.update({k: v for k, v in overrides.items() if v is not None})
    return p


def apply(tl: dict, style: dict) -> dict:
    """Stamp programme-wide preset choices onto a timeline (captions, overlays, per-clip look and transitions).
    Per-clip values that were set explicitly are kept."""
    tl = copy.deepcopy(tl)
    if style.get("captions"):
        tl["captions"] = copy.deepcopy(style["captions"])
    if style.get("overlays"):
        tl["overlays"] = copy.deepcopy(style["overlays"])
    for i, c in enumerate(tl["clips"]):
        if c["type"] != "color":
            c.setdefault("grade", style.get("grade"))
            if style.get("effects"):
                c.setdefault("effects", copy.deepcopy(style["effects"]))
        if i and c.get("transition") == "crossfade":
            c.setdefault("xfade", style.get("xfade", "fade"))
            c["transition_dur"] = style.get("transition_dur", c.get("transition_dur", 0.5))
    if tl.get("music") and style.get("music_volume") is not None:
        tl["music"]["volume"] = style["music_volume"]
    tl["preset"] = style.get("preset")
    return tl
