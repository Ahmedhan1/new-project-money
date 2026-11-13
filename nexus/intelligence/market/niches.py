"""Niche database: seed topics, Wikipedia categories, editorial monetisation/platform priors. Custom niches allowed."""
from __future__ import annotations

import copy
import functools
from pathlib import Path

import yaml

from ... import config

DB = Path(__file__).with_name("niches.yaml")
REQUIRED = ("label", "seeds", "categories", "monetisation", "platform_fit", "preset")


@functools.lru_cache(maxsize=1)
def _base() -> dict:
    return yaml.safe_load(DB.read_text(encoding="utf-8"))


def validate_niche(nid: str, n: dict) -> list[str]:
    errs = [f"{nid}: missing '{k}'" for k in REQUIRED if k not in n]
    if not errs:
        if n["monetisation"].get("tier") not in ("high", "medium", "low", "restricted"):
            errs.append(f"{nid}: monetisation.tier must be high|medium|low|restricted")
        if any(not 0 <= float(v) <= 1 for v in n["platform_fit"].values()):
            errs.append(f"{nid}: platform_fit values must be in [0, 1]")
    return errs


def load(settings: dict | None = None) -> dict:
    """Built-in niches + custom ones (settings.json market.niches_file / market.custom_niches). Custom ids override."""
    settings = settings if settings is not None else (config.settings().get("market") or {})
    db = copy.deepcopy(_base())
    extra = {}
    if settings.get("niches_file") and Path(settings["niches_file"]).exists():
        extra.update((yaml.safe_load(Path(settings["niches_file"]).read_text(encoding="utf-8")) or {}).get("niches", {}))
    extra.update(settings.get("custom_niches") or {})
    for nid, n in extra.items():
        n = {"categories": [], "monetisation": {"tier": "medium", "notes": "custom niche"},
             "platform_fit": {"youtube_long": 0.5, "youtube_shorts": 0.5, "tiktok": 0.5}, "preset": "educational", **n}
        n.setdefault("label", nid)
        n.setdefault("seeds", [])
        n["custom"] = True
        db["niches"][nid] = n
    for nid, n in db["niches"].items():
        errs = validate_niche(nid, n)
        if errs:
            raise ValueError("; ".join(errs))
    return db


def get(nid: str, settings: dict | None = None) -> dict:
    db = load(settings)
    if nid not in db["niches"]:
        raise KeyError(f"unknown niche '{nid}' (known: {', '.join(db['niches'])})")
    return {"id": nid, "sensitive": db.get("default_sensitive", []), **db["niches"][nid]}


def watchlist(niche_ids: list[str] | None = None, settings: dict | None = None) -> list[dict]:
    db = load(settings)
    out = []
    for nid, n in db["niches"].items():
        if niche_ids and nid not in niche_ids:
            continue
        out += [{"topic": s, "niche": nid, "lang": n.get("language", "en")} for s in n["seeds"]]
    return out


def check_categories(ctx, nid: str, settings: dict | None = None) -> dict:
    """Which configured categories exist on Wikipedia (network; used by the demo, not by fast tests)."""
    n = get(nid, settings)
    lang = n.get("category_lang", "en")
    try:
        d = ctx.get(f"https://{lang}.wikipedia.org/w/api.php", {"action": "query", "prop": "categoryinfo", "format": "json",
                                                                 "titles": "|".join(n["categories"])})
    except Exception as e:  # noqa: BLE001 - report, do not crash the demo
        return {"niche": nid, "status": "error", "reason": f"{type(e).__name__}: {e}"}
    found = {p["title"]: ("missing" not in p) for p in d.get("query", {}).get("pages", {}).values()}
    return {"niche": nid, "status": "ok", "exists": found}
