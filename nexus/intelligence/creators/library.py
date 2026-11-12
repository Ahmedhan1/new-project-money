"""Curated pattern library (patterns.yaml): high-level format archetypes with a source for every claim."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

PATH = Path(__file__).with_name("patterns.yaml")


@lru_cache(maxsize=1)
def load() -> dict:
    return yaml.safe_load(PATH.read_text(encoding="utf-8"))


def archetypes() -> list[dict]:
    return load()["archetypes"]


def get(archetype_id: str | None) -> dict | None:
    return next((a for a in archetypes() if a["id"] == archetype_id), None)


def check() -> list[str]:
    """Every claim names a declared source or 'hypothesis'; no numeric pacing without a source."""
    lib, errs = load(), []
    srcs = set(lib["sources"]) | {"hypothesis"}
    for a in lib["archetypes"]:
        for sec in ("hook_patterns", "structure", "editing", "visual", "audio", "metrics_named_by_source"):
            for c in a.get(sec) or []:
                if c.get("src") not in srcs:
                    errs.append(f"{a['id']}.{sec}: unknown source {c.get('src')}")
        for k, v in (a.get("pacing") or {}).items():
            if isinstance(v, (int, float)) and not a["pacing"].get("src"):
                errs.append(f"{a['id']}.pacing.{k}: number without source")
    return errs
