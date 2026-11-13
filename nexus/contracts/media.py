"""Account 2 integration contract: the JSON documents exchanged with Account 1 (workers, storage, scheduler) and
Account 3 (research, routing, localization, publishing, growth). Docs: docs/ACCOUNT2_CONTRACTS.md.

Every document carries `schema` ("nexus.<Name>/<major>") so a consumer can refuse a version it does not understand.
Schemas are deliberately small (required keys + types); everything else is optional and must be ignored by readers.
`validate(doc)` returns a list of problems (empty = valid); `check(doc)` raises ContractError.
No dependency on jsonschema: the validator below understands the subset used here.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

VERSION = 1


class ContractError(ValueError):
    pass


S, N, I, B, L, D = "str", "num", "int", "bool", "list", "dict"
OPT = "?"  # suffix: optional key

ARTIFACT_REF = {"uri": S, "sha256": S + OPT, "bytes": I + OPT, "media_type": S + OPT, "role": S + OPT}

SCHEMAS: dict[str, dict] = {
    # A file somewhere (local path, storage URI from Account 1, or http(s) for public sources). Never inline media.
    "ArtifactRef": ARTIFACT_REF,
    "ArtifactManifest": {"artifacts": L, "producer": S, "job_id": S + OPT},
    # A source or produced media item with rights and technical facts.
    "MediaAsset": {"id": S, "ref": D, "kind": S, "rights": D, "duration": N + OPT, "width": I + OPT, "height": I + OPT,
                   "fps": N + OPT, "has_audio": B + OPT, "provenance": D + OPT},
    # Output of nexus.media.analyze/understand for one asset (time ranges in source seconds).
    "MediaAnalysis": {"asset_id": S, "duration": N, "scenes": L, "transcript": D + OPT, "speakers": L + OPT, "faces": L + OPT,
                      "silences": L + OPT, "loudness": D + OPT, "quality": D + OPT, "visual": D + OPT, "models": D + OPT},
    # Renderer input (docs/TIMELINE.md) + an EDL view. `timeline` is the full renderer dict.
    "TimelineEDL": {"timeline": D, "edl": L + OPT, "tracks": L + OPT, "version_id": S + OPT},
    # Explainable director output: what was kept/cut/inserted and why. Editable before rendering.
    "EditorialPlan": {"brief": S, "profile": S, "decisions": L, "chapters": L + OPT, "target_s": N + OPT, "deliverables": L + OPT},
    # A render job description independent of where it runs.
    "RenderJobManifest": {"job_id": S, "kind": S, "inputs": L, "timeline": D + OPT, "settings": D, "outputs": L,
                          "requirements": D, "checkpoint": D + OPT},
    # What Account 1's scheduler sends to a worker, and what it gets back.
    "RenderExecutionRequest": {"request_id": S, "manifest": D, "environment": D, "deadline_s": N + OPT, "attempt": I + OPT},
    "RenderExecutionResult": {"request_id": S, "status": S, "outputs": L, "metrics": D, "error": D + OPT, "checkpoint": D + OPT},
    "EffectManifest": {"effects": L},
    "AudioMasterManifest": {"input": D, "output": D, "chain": L, "before": D, "after": D, "target_lufs": N},
    "GeneratedMediaManifest": {"model": D, "task": S, "prompt": S + OPT, "inputs": L, "output": D, "seconds": N + OPT,
                               "classification": S, "licence": D, "compute": D},
    "QualityReport": {"output": S, "status": S, "stages": L, "score": N + OPT, "human_approval": S},
    "ReferenceStyleProfile": {"name": S, "source": D, "metrics": D, "structure": L + OPT, "rights": D},
}

STATUSES = {"queued", "running", "done", "failed", "cancelled", "deferred"}
# GeneratedMediaManifest.classification: never "generated-video" for an animated still.
CLASSIFICATIONS = {"generated-video", "generated-image", "animated-still", "procedural", "edited-source"}
RIGHTS_BASES = {"owned", "licensed", "public-domain", "commentary", "generated"}

TYPES = {S: str, N: (int, float), I: int, B: bool, L: list, D: dict}


def _check_type(v, t: str) -> bool:
    py = TYPES[t]
    if t == N and isinstance(v, bool):
        return False
    return isinstance(v, py)


def validate(doc: dict, name: str | None = None) -> list[str]:
    if not isinstance(doc, dict):
        return ["document is not an object"]
    tag = doc.get("schema", "")
    name = name or tag.split("/")[0].removeprefix("nexus.")
    if name not in SCHEMAS:
        return [f"unknown schema {tag or name!r}"]
    out = []
    if tag:
        try:
            major = int(tag.split("/")[1])
        except (IndexError, ValueError):
            major = -1
        if major != VERSION:
            out.append(f"{name}: version {tag!r} not supported (this build reads /{VERSION})")
    for key, t in SCHEMAS[name].items():
        opt = t.endswith(OPT)
        t = t.rstrip(OPT)
        if key not in doc or doc[key] is None:
            if not opt:
                out.append(f"{name}.{key} is required")
            continue
        if not _check_type(doc[key], t):
            out.append(f"{name}.{key} must be {t}, got {type(doc[key]).__name__}")
    # semantic rules
    if name == "RenderExecutionResult" and doc.get("status") not in STATUSES:
        out.append(f"RenderExecutionResult.status must be one of {sorted(STATUSES)}")
    if name == "GeneratedMediaManifest" and doc.get("classification") not in CLASSIFICATIONS:
        out.append(f"GeneratedMediaManifest.classification must be one of {sorted(CLASSIFICATIONS)}")
    if name == "MediaAsset" and isinstance(doc.get("rights"), dict) and doc["rights"].get("basis") not in RIGHTS_BASES:
        out.append(f"MediaAsset.rights.basis must be one of {sorted(RIGHTS_BASES)}")
    for k in ("outputs", "inputs", "artifacts"):
        for i, a in enumerate(doc.get(k) or []):
            if isinstance(a, dict) and "uri" in a:
                out += [f"{name}.{k}[{i}].{p}" for p in (validate(a, "ArtifactRef"))]
    return out


def check(doc: dict, name: str | None = None) -> dict:
    errs = validate(doc, name)
    if errs:
        raise ContractError("; ".join(errs))
    return doc


def make(name: str, **fields) -> dict:
    """Build a document with its schema tag and validate it."""
    return check({"schema": f"nexus.{name}/{VERSION}", **fields}, name)


def artifact(path: str | Path, role: str = "", media_type: str = "", hash_it: bool = True) -> dict:
    p = Path(path)
    ref = {"uri": p.resolve().as_uri() if p.exists() else str(path), "role": role or None, "media_type": media_type or None}
    if p.exists():
        ref["bytes"] = p.stat().st_size
        if hash_it:
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            ref["sha256"] = h.hexdigest()
    return {k: v for k, v in ref.items() if v is not None}


def local_path(uri: str) -> Path | None:
    """Local file of an ArtifactRef uri: file:///C:/x/a.mp4 -> C:\\x\\a.mp4 on Windows, file:///x/a.mp4 -> /x/a.mp4 on
    POSIX; None for a non-file URI (nexus://, https://). A bare path is returned as-is."""
    from urllib.parse import urlparse
    from urllib.request import url2pathname
    if not uri:
        return None
    if uri.startswith("file:"):
        return Path(url2pathname(urlparse(uri).path))
    if "://" in uri:
        return None
    return Path(uri)


def new_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:8]}"


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False)
