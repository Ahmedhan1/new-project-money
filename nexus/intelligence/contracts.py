"""Account 3 shared documents (v1).

Same conventions as Account 2's contract (`docs/ACCOUNT2_CONTRACTS.md` on `work/account-2-cinematic-engine`):
every document carries `"schema": "nexus.<Name>/<major>"`; readers ignore unknown keys and refuse unknown majors;
media is never inlined (files travel as ArtifactRef dicts `{uri, sha256?, bytes?, media_type?, role?}`).
This module is self-contained (no import of Account 1's `nexus/contracts.py` or Account 2's `nexus/contracts/`,
which currently collide on the same import name - see docs/ACCOUNT3_HANDOFF_TO_INTEGRATION.md).

Evidence rule for every document that states a fact or a score: it carries `evidence` (list of Evidence dicts with a
source URL/ref) and a `confidence` in [0, 1]. Nothing here may contain an invented metric.
"""
from __future__ import annotations

import time
import uuid

VERSION = 1


class ContractError(ValueError):
    pass


S, N, I, B, L, D = "str", "num", "int", "bool", "list", "dict"
OPT = "?"

EVIDENCE = {"source": S, "url": S + OPT, "retrieved": S + OPT, "quote": S + OPT, "kind": S + OPT}

SCHEMAS: dict[str, dict] = {
    "Evidence": EVIDENCE,
    # A model/provider as Account 3 declares it. Field names line up with Account 1's AIProviderCapability/v1
    # (provider, connection_id, tasks, models, local, commercial_use, quotas) plus routing facts.
    "ProviderCapability": {"provider": S, "connection_id": S, "tasks": L, "models": L, "local": B, "commercial_use": S,
                           "hosting": S, "privacy": D, "quotas": L, "health": S + OPT, "measured": D + OPT, "verified": S},
    # Research for one topic: claims with sources, conflicts, open questions.
    "ResearchBrief": {"id": S, "topic": S, "language": S, "sources": L, "claims": L, "conflicts": L + OPT,
                      "open_questions": L + OPT, "angles": L + OPT, "created": N, "confidence": N},
    # A scored idea; every factor has its own evidence + confidence. No view predictions.
    "ContentOpportunity": {"id": S, "topic": S, "niche": S, "language": S, "score": N, "factors": D, "evidence": L,
                           "confidence": N, "brief_id": S + OPT, "status": S},
    # Structural analysis of one lawfully accessed reference (never footage or script text beyond short quotes).
    "CreatorReferenceProfile": {"id": S, "source": D, "creator": S, "category": S, "format": S, "duration_s": N + OPT,
                                "hook": D, "story": D, "editing": D, "visual": D, "audio": D, "engagement": L,
                                "packaging": D + OPT, "strengths": L, "weaknesses": L, "techniques": L, "confidence": N,
                                "evidence": L, "measured": D + OPT},
    # An abstract style built from one or more reference profiles; this is what production may use.
    "CreativeStyleProfile": {"id": S, "name": S, "from_references": L, "pacing": D, "hook_patterns": L, "structure": L,
                             "visual_rules": L, "audio_rules": L, "do_not_copy": L},
    # Original script handed to Account 2.
    "ContentScript": {"id": S, "title": S, "language": S, "scenes": L, "brief_id": S + OPT, "style_id": S + OPT,
                      "sources": L, "originality": D},
    # What Account 2's director/production engine receives (maps onto their EditorialPlan.brief / create_video).
    "StoryboardBrief": {"id": S, "script_id": S, "format": S, "aspect": S, "target_s": N, "shots": L, "style": D + OPT,
                        "deliverables": L + OPT},
    "LocalizationManifest": {"id": S, "project_id": S, "source_lang": S, "targets": L, "glossary": D + OPT, "lines": L + OPT,
                             "qa": D + OPT, "voices": L + OPT, "approval": S},
    "PlatformMetadata": {"platform": S, "title": S, "description": S, "tags": L, "language": S, "aspect": S,
                         "duration_s": N + OPT, "thumbnail": D + OPT, "checks": L + OPT, "spec_version": S},
    "PublishingManifest": {"id": S, "project_id": S, "artifact": D, "platform": S, "connection_id": S + OPT,
                           "metadata": D, "visibility": S, "approval": S, "schedule": S + OPT, "status": S,
                           "idempotency_key": S, "history": L + OPT},
    # One first-party metric observation; `source` says where it came from (platform API / owner CSV export).
    "AnalyticsRecord": {"platform": S, "video_id": S, "date": S, "metrics": D, "source": S, "imported": N,
                        "project_id": S + OPT, "variant": S + OPT},
    "ExperimentManifest": {"id": S, "hypothesis": S, "variable": S, "variants": L, "platform": S, "metric": S,
                           "min_samples": I, "status": S, "results": D + OPT, "confidence": N + OPT, "limitations": L + OPT},
    "GrowthRecommendation": {"id": S, "kind": S, "text": S, "evidence": L, "confidence": N, "target": S,
                             "status": S, "experiment_id": S + OPT},
}

CONFIDENCE_KEYS = {"ResearchBrief", "ContentOpportunity", "CreatorReferenceProfile", "GrowthRecommendation"}
STATUSES = {
    "ContentOpportunity": {"proposed", "approved", "rejected", "in_production", "published"},
    "PublishingManifest": {"draft", "awaiting_approval", "approved", "queued", "uploading", "uploaded", "failed", "cancelled"},
    "ExperimentManifest": {"planned", "running", "collecting", "concluded", "inconclusive", "stopped"},
    "GrowthRecommendation": {"proposed", "accepted", "rejected", "sent_to_production", "tested"},
}
VISIBILITY = {"private", "unlisted", "draft", "public"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def make(name: str, **fields) -> dict:
    if name not in SCHEMAS:
        raise ContractError(f"unknown contract {name}")
    doc = {"schema": f"nexus.{name}/{VERSION}", **fields}
    if "created" in SCHEMAS[name] and "created" not in doc:
        doc["created"] = time.time()
    check(doc)
    return doc


def _type_ok(v, t: str) -> bool:
    return {"str": lambda x: isinstance(x, str), "num": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
            "int": lambda x: isinstance(x, int) and not isinstance(x, bool), "bool": lambda x: isinstance(x, bool),
            "list": lambda x: isinstance(x, list), "dict": lambda x: isinstance(x, dict)}[t](v)


def validate(doc: dict) -> list[str]:
    if not isinstance(doc, dict) or not isinstance(doc.get("schema"), str) or "/" not in doc["schema"]:
        return ["missing 'schema'"]
    name, major = doc["schema"].removeprefix("nexus.").rsplit("/", 1)
    if name not in SCHEMAS:
        return [f"unknown contract {name}"]
    if major != str(VERSION):
        return [f"{name}: unsupported major version {major} (this reader understands {VERSION})"]
    errs = []
    for key, t in SCHEMAS[name].items():
        opt = t.endswith(OPT)
        t = t.rstrip(OPT)
        if key not in doc:
            if not opt:
                errs.append(f"{name}: missing '{key}'")
            continue
        if doc[key] is None and opt:
            continue
        if not _type_ok(doc[key], t):
            errs.append(f"{name}.{key}: expected {t}")
    if name in CONFIDENCE_KEYS and _type_ok(doc.get("confidence"), "num") and not 0 <= doc["confidence"] <= 1:
        errs.append(f"{name}.confidence must be in [0, 1]")
    if name in STATUSES and isinstance(doc.get("status"), str) and doc["status"] not in STATUSES[name]:
        errs.append(f"{name}.status '{doc['status']}' not in {sorted(STATUSES[name])}")
    if name == "PublishingManifest":
        if doc.get("visibility") not in VISIBILITY:
            errs.append("PublishingManifest.visibility must be one of private/unlisted/draft/public")
        if doc.get("visibility") == "public" and doc.get("approval") != "approved_by_owner":
            errs.append("PublishingManifest: public visibility requires approval == 'approved_by_owner'")
    for ev in doc.get("evidence") or []:
        if not isinstance(ev, dict) or not ev.get("source"):
            errs.append(f"{name}.evidence items need a 'source'")
            break
    return errs


def check(doc: dict) -> dict:
    errs = validate(doc)
    if errs:
        raise ContractError("; ".join(errs))
    return doc


def evidence(source: str, url: str | None = None, quote: str | None = None, kind: str | None = None) -> dict:
    e = {"source": source, "retrieved": time.strftime("%Y-%m-%d")}
    if url:
        e["url"] = url
    if quote:
        e["quote"] = quote[:300]
    if kind:
        e["kind"] = kind
    return e


def to_account1_ai_capability(cap: dict) -> dict:
    """Project a ProviderCapability onto Account 1's AIProviderCapability/v1 field set."""
    return {"contract": "AIProviderCapability/v1", **{k: cap[k] for k in ("provider", "connection_id", "tasks", "models", "local")},
            "commercial_use": cap["commercial_use"] if cap["commercial_use"] in ("yes", "no") else "unverified", "quotas": cap.get("quotas", [])}
