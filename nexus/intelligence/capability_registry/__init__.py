"""Living catalogue of AI providers/models Account 3 can route to (providers.yaml next to this file).

Every entry records licence, commercial use, hosting, privacy terms, quotas and when it was last verified. Facts that
were not checked against first-party documentation or measured here are marked `verified: "unverified"`.
Measured latency/quality come from `nexus.intelligence.model_router.bench` and are stored in the database, not here."""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

FILE = Path(__file__).with_name("providers.yaml")


@functools.lru_cache(maxsize=1)
def catalog() -> dict:
    return yaml.safe_load(FILE.read_text(encoding="utf-8"))


def providers() -> dict[str, dict]:
    return {p["id"]: p for p in catalog()["providers"]}


def models() -> list[dict]:
    """Flattened (provider, model) entries with the provider's routing facts merged in."""
    out = []
    for p in catalog()["providers"]:
        for m in p.get("models", []):
            out.append({**{k: v for k, v in p.items() if k != "models"}, "provider": p["id"], **m})
    return out


def reload() -> None:
    catalog.cache_clear()
