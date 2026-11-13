"""Terminology memory: approved translations of names and domain terms, per target language, with provenance.

Entries come from three places, each recorded: a project glossary, a native reviewer's correction (strongest), or the
owner. `glossary(lang)` feeds translation QA (`check_line(..., glossary=...)`), so a term a reviewer fixed once is
enforced in every later translation. Stored in NEXUS_HOME/intelligence/terminology.json (local only, never uploaded)."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from ... import config

_LOCK = threading.Lock()
RANK = {"native_review": 3, "owner": 2, "glossary": 1, "machine": 0}


def path() -> Path:
    return config.HOME / "intelligence" / "terminology.json"


def _load() -> dict:
    p = path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"schema": "nexus.TerminologyMemory/1", "terms": {}}


def _save(d: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _key(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


def add(term: str, lang: str, target: str, *, origin: str = "glossary", by: str = "", note: str = "") -> dict:
    """Record an approved rendering. A lower-ranked origin never overwrites a higher one (a machine suggestion cannot
    replace a native reviewer's correction)."""
    if origin not in RANK:
        raise ValueError(f"origin must be one of {sorted(RANK)}")
    with _LOCK:
        d = _load()
        e = d["terms"].setdefault(_key(term), {"term": term.strip(), "targets": {}})
        cur = e["targets"].get(lang)
        if cur and RANK[cur["origin"]] > RANK[origin]:
            return {"kept": cur, "refused": {"target": target, "origin": origin}}
        hist = (cur or {}).get("history", []) + ([{k: cur[k] for k in ("target", "origin", "by", "at")}] if cur else [])
        e["targets"][lang] = {"target": target.strip(), "origin": origin, "by": by, "note": note, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                              "history": hist[-10:]}
        _save(d)
        return {"saved": e["targets"][lang]}


def lookup(term: str, lang: str) -> str | None:
    e = _load()["terms"].get(_key(term))
    return (e or {}).get("targets", {}).get(lang, {}).get("target")


def glossary(lang: str, text: str | None = None) -> dict[str, str]:
    """{source term: approved target} for `lang`; only terms present in `text` when given (for QA of one line)."""
    out = {}
    low = (text or "").lower()
    for k, e in _load()["terms"].items():
        t = e["targets"].get(lang)
        if t and (text is None or k in low):
            out[e["term"]] = t["target"]
    return out


def seed(glossary_by_lang: dict[str, dict[str, str]], by: str = "project glossary") -> int:
    n = 0
    for lang, g in glossary_by_lang.items():
        for term, tgt in g.items():
            n += "saved" in add(term, lang, tgt, origin="glossary", by=by)
    return n


def stats() -> dict:
    out: dict[str, dict] = {}
    for e in _load()["terms"].values():
        for lang, t in e["targets"].items():
            s = out.setdefault(lang, {"terms": 0, "native_review": 0})
            s["terms"] += 1
            s["native_review"] += t["origin"] == "native_review"
    return out
