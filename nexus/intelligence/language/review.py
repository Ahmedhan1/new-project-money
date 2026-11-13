"""Native-speaker review workflow for translated / dialect lines.

submit() queues lines with their machine QA findings; a reviewer records a verdict per line:
  approve   the line is correct and natural for the variety (counts toward readiness "human_reviewed")
  correct   the reviewer's text replaces the line; corrected terms can be saved to terminology memory
  reject    unusable; the line must be re-translated
Reviewer identity is a free label (no account system); nothing is sent anywhere. Machine QA never marks a line
human-reviewed. Stored in NEXUS_HOME/intelligence/reviews.json."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from ... import config
from . import terminology

VERDICTS = ("approve", "correct", "reject")
_LOCK = threading.Lock()


def path() -> Path:
    return config.HOME / "intelligence" / "reviews.json"


def _load() -> dict:
    p = path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"schema": "nexus.NativeReview/1", "items": {}}


def _save(d: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def submit(lang: str, lines: list[dict], project: str = "", variety_note: str = "") -> list[str]:
    """lines: [{"source", "target", "findings"?}] -> item ids (status pending)."""
    ids = []
    with _LOCK:
        d = _load()
        for i, ln in enumerate(lines):
            iid = f"rev_{uuid.uuid4().hex[:10]}"
            d["items"][iid] = {"id": iid, "lang": lang, "project": project, "index": i, "source": ln["source"], "target": ln["target"],
                               "findings": ln.get("findings", []), "variety_note": variety_note, "status": "pending",
                               "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
            ids.append(iid)
        _save(d)
    return ids


def record(item_id: str, verdict: str, reviewer: str, *, correction: str = "", notes: str = "",
           terms: dict[str, str] | None = None) -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    if verdict == "correct" and not correction.strip():
        raise ValueError("a correction needs the corrected text")
    if not reviewer.strip():
        raise ValueError("reviewer label is required")
    with _LOCK:
        d = _load()
        it = d["items"][item_id]
        it.update(status={"approve": "approved", "correct": "corrected", "reject": "rejected"}[verdict], reviewer=reviewer,
                  correction=correction.strip() or None, notes=notes, reviewed=time.strftime("%Y-%m-%dT%H:%M:%S"))
        _save(d)
    for src, tgt in (terms or {}).items():  # reviewer-confirmed terminology outranks glossary/machine entries
        terminology.add(src, it["lang"], tgt, origin="native_review", by=reviewer, note=f"from review {item_id}")
    return it


def final_text(item_id: str) -> str | None:
    it = _load()["items"][item_id]
    return {"approved": it["target"], "corrected": it.get("correction")}.get(it["status"])


def queue(lang: str | None = None, status: str = "pending") -> list[dict]:
    return [i for i in _load()["items"].values() if (lang is None or i["lang"] == lang) and (status is None or i["status"] == status)]


def stats() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for it in _load()["items"].values():
        s = out.setdefault(it["lang"], {"pending": 0, "approved": 0, "corrected": 0, "rejected": 0, "reviewers": set()})
        s[it["status"]] += 1
        if it.get("reviewer"):
            s["reviewers"].add(it["reviewer"])
    for s in out.values():
        s["reviewers"] = len(s["reviewers"])
        done = s["approved"] + s["corrected"] + s["rejected"]
        s["reviewed"] = done
        s["acceptance"] = round((s["approved"] + s["corrected"]) / done, 3) if done else None
    return out
