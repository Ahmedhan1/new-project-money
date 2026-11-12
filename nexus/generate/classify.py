"""Classification guard for produced media (GeneratedMediaManifest.classification).

`generated-video` is granted only when ALL of these hold:
  1. provenance names a real registry model whose tasks include a video task, and nothing marks it simulated;
  2. the provenance does not say the frames came from a still (Ken Burns, pan, parallax, zoompan, depth warp);
  3. when frames are available: they are NOT explained by one still under a global similarity transform
     (metrics.still_motion verdict) and they are not static.
Anything else is downgraded (never upgraded): animated-still, generated-image, procedural or edited-source.

    classify(provenance, path=None, frames=None) -> {"classification", "reasons", "evidence"}
    guard(doc)                                   raises ClassificationError for a GeneratedMediaManifest that claims
                                                 more than its evidence supports
    manifest(task, provenance, output, inputs=..., compute=..., path=...) -> validated GeneratedMediaManifest
"""
from __future__ import annotations

from pathlib import Path

from .. import contracts
from . import catalog, metrics

STILL_WORDS = ("ken burns", "kenburns", "zoompan", "pan on a still", "parallax", "animated still", "animated_still",
               "depth warp", "still image")
VIDEO_TASKS = catalog.VIDEO_TASKS


class ClassificationError(ValueError):
    pass


def _still_derived(p: dict) -> bool:
    text = " ".join(str(p.get(k, "")) for k in ("kind", "method", "source", "note")).lower()
    return p.get("kind") == "animated_still" or bool(p.get("still")) or any(w in text for w in STILL_WORDS)


def classify(provenance: dict, path: str | Path | None = None, frames=None) -> dict:
    p = provenance or {}
    reasons, evidence = [], {}
    kind = p.get("kind")
    if kind in ("procedural", "motion_graphics"):
        return {"classification": "procedural", "reasons": ["procedural generator"], "evidence": evidence}
    if kind in ("edited-source", "enhanced", "footage", "vfx"):
        return {"classification": "edited-source", "reasons": [f"processed source ({kind})"], "evidence": evidence}
    if _still_derived(p):
        return {"classification": "animated-still", "reasons": ["provenance says the motion was added to a still"],
                "evidence": evidence}
    task = p.get("task") or ""
    mid = p.get("model")
    m = None
    if mid:
        try:
            m = catalog.model(str(mid).split("@")[0])
        except KeyError:
            reasons.append(f"model {mid!r} is not in the generation registry")
    else:
        reasons.append("provenance names no model")
    if p.get("simulated"):
        reasons.append("provenance is marked simulated")
    image_only = bool(m) and "image" in (m.get("tasks") or []) and not set(m.get("tasks") or []) & VIDEO_TASKS
    if task == "image" or image_only:
        if m and not p.get("simulated"):
            return {"classification": "generated-image", "reasons": [f"{m['id']} image output"], "evidence": evidence}
        raise ClassificationError("cannot classify as generated-image: " + "; ".join(reasons or ["no model"]))
    if m and not set(m.get("tasks") or []) & VIDEO_TASKS:
        reasons.append(f"{m['id']} is not a video model (tasks: {m.get('tasks')})")
    if task and task not in VIDEO_TASKS:
        reasons.append(f"task {task!r} is not a video-generation task")
    if frames is None and path is not None and Path(path).exists():
        try:
            frames = metrics.read_frames(path, n=96, width=192)
        except Exception as e:  # noqa: BLE001
            reasons.append(f"frames unreadable: {e}")
    if frames is not None:
        sm = metrics.still_motion(frames)
        evidence["still_motion"] = sm
        if sm["verdict"] in ("global-transform-of-one-still", "static"):
            return {"classification": "animated-still",
                    "reasons": reasons + [f"frames are {sm['verdict']} (residual {sm['residual']} vs motion {sm['motion']})"],
                    "evidence": evidence}
    else:
        reasons.append("no frames to verify motion")
    if reasons:
        # not enough evidence for generated video; it is not a still either as far as we know -> say so explicitly
        raise ClassificationError("cannot classify as generated-video: " + "; ".join(reasons))
    return {"classification": "generated-video", "reasons": [f"{m['id']} output with independent motion"], "evidence": evidence}


def guard(doc: dict, path: str | Path | None = None) -> dict:
    """Validate a GeneratedMediaManifest and refuse an over-claimed classification."""
    contracts.check(doc, "GeneratedMediaManifest")
    if doc["classification"] == "generated-video":
        prov = {"model": (doc.get("model") or {}).get("id"), "task": doc.get("task"), **(doc.get("provenance") or {})}
        local = path
        if local is None:
            uri = (doc.get("output") or {}).get("uri", "")
            local = uri[7:] if uri.startswith("file://") else None
        got = classify(prov, path=local)
        if got["classification"] != "generated-video":
            raise ClassificationError(f"manifest claims generated-video but the evidence says {got['classification']}: "
                                      + "; ".join(got["reasons"]))
    return doc


def manifest(task: str, provenance: dict, output: str | Path, *, inputs: list | None = None, compute: dict | None = None,
             prompt: str | None = None, seconds: float | None = None, path_check: bool = True) -> dict:
    """Build a GeneratedMediaManifest whose classification comes from classify(), never from the caller."""
    out = Path(output)
    c = classify({**provenance, "task": provenance.get("task", task)}, path=out if path_check and out.exists() else None)
    mid = provenance.get("model")
    lic = {}
    if mid:
        try:
            m = catalog.model(str(mid).split("@")[0])
            lic = {"weights_licence": m.get("weights_licence"), "commercial_use": m.get("commercial_use"),
                   "class": catalog.licence_class(m), "conditions": m.get("conditions"), "source_url": m.get("source_url")}
        except KeyError:
            pass
    doc = {"model": {"id": mid or "none", **{k: v for k, v in provenance.items() if k in ("revision", "name", "method")}},
           "task": task, "inputs": inputs or [], "output": contracts.artifact(out, role="output", hash_it=out.exists()),
           "classification": c["classification"], "licence": lic or {"note": provenance.get("licence") or "no model weights"},
           "compute": compute or {}, "provenance": provenance, "classification_reasons": c["reasons"],
           "evidence": c["evidence"]}
    if prompt is not None:
        doc["prompt"] = prompt
    if seconds is not None:
        doc["seconds"] = float(seconds)
    return contracts.make("GeneratedMediaManifest", **doc)
