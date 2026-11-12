"""VFX processors (media-to-media, CPU): background removal/replacement, subject/mask tracking, procedural particles,
light leaks and lens flares, depth-of-field and 2.5D parallax from a still (an ANIMATED STILL, never generated video).

    segment.replace_background / segment.cutout / segment.matte       ONNX matting (MediaPipe selfie, MODNet, U2-Netp)
    track.track_box / mask_keyframes / follow_keyframes / render_tracked_mask
    procedural.particles / light_leak / lens_flare / overlay
    depth.depth / depth_of_field / parallax
    effect_manifest() -> EffectManifest of what is available here (models installed or not, cost class)
    apply(clip, op, **params) -> clip whose `src` is the processed segment (same contract as nexus.enhance.apply)
Models are fetched only with the user's consent (nexus.generate.downloads); without them the ops raise ConsentRequired.
"""
from __future__ import annotations

from .. import contracts
from ..generate import downloads

EFFECTS = [
    {"type": "vfx.replace_background", "params": {"bg": "#RRGGBB|blur|image/video path", "model": "mediapipe-selfie|modnet|u2netp",
                                                   "feather": "px", "smooth": "0..0.9"}, "cost": "heavy (per-frame model)",
     "models": ["mediapipe-selfie", "modnet", "u2netp"]},
    {"type": "vfx.cutout", "params": {"model": "mediapipe-selfie|modnet"}, "cost": "heavy", "output": "RGBA MOV",
     "models": ["mediapipe-selfie", "modnet"]},
    {"type": "vfx.track", "params": {"box": "[x,y,w,h] 0..1", "start": "s", "duration": "s"}, "cost": "medium",
     "output": "mask keyframes / follow keyframes"},
    {"type": "vfx.tracked_mask", "params": {"effect": "blur|pixelate|black|dim", "region": "inside|outside"}, "cost": "medium"},
    {"type": "vfx.particles", "params": {"kind": "dust|snow|embers|bokeh", "density": "0..1"}, "cost": "medium",
     "output": "RGBA MOV", "label": "procedural"},
    {"type": "vfx.light_leak", "params": {"palette": "warm|cool", "intensity": "0..1"}, "cost": "light", "output": "RGBA MOV"},
    {"type": "vfx.lens_flare", "params": {"path": "[[x,y],[x,y]]", "intensity": "0..1"}, "cost": "medium", "output": "RGBA MOV"},
    {"type": "vfx.depth_of_field", "params": {"focus": "0..1 depth", "max_blur": "px"}, "cost": "medium",
     "models": ["depth-anything-v2-small"]},
    {"type": "vfx.parallax", "params": {"amount": "0..0.1", "seconds": "s"}, "cost": "medium", "label": "animated-still",
     "models": ["depth-anything-v2-small"]},
]


def effect_manifest() -> dict:
    out = []
    for e in EFFECTS:
        ms = e.get("models") or []
        inst = {m: downloads.installed(m) for m in ms}
        out.append({**e, "kind": "media-to-media", "available": (not ms) or any(inst.values()), "models_installed": inst,
                    "needs_download_gb": {m: downloads.plan(m)["total_gb"] for m, ok in inst.items() if not ok}})
    return contracts.make("EffectManifest", effects=out)


def apply(clip: dict, op: str, **params) -> dict:
    """Process the clip's [in, out] segment and return a new clip pointing at the result (undo info in `enhanced_from`)."""
    from . import segment, track
    if clip.get("type") != "video":
        raise ValueError("vfx.apply() processes video clips")
    t_in, t_out = float(clip.get("in", 0)), float(clip["out"])
    if op == "replace_background":
        r = segment.replace_background(clip["src"], start=t_in, duration=t_out - t_in, **params)
    elif op == "tracked_mask":
        from pathlib import Path

        from ..generate.cache import MediaCache, key
        tr = track.track_box(clip["src"], tuple(params.pop("box")), start=t_in, duration=t_out - t_in)
        k = key("vfx.tracked_mask", {**params, "in": t_in, "out": t_out, "box": tr["samples"][0]}, [clip["src"]])
        mc = MediaCache()
        dest = mc.get(k, ".mp4") or mc.path_for(k, ".mp4")
        if not Path(dest).exists() or Path(dest).stat().st_size == 0:
            track.render_tracked_mask(clip["src"], dest, tr, **params)
            mc.put(k, dest, {"op": "tracked_mask"})
        r = {"path": str(dest), "track": tr}
    else:
        raise ValueError(f"unknown vfx op {op!r}")
    new = dict(clip)
    hist = list(clip.get("enhanced_from") or [])
    hist.append({"src": clip["src"], "in": t_in, "out": t_out, "op": f"vfx.{op}", "params": {k: str(v) for k, v in params.items()}})
    new.update({"src": r["path"], "in": 0.0, "out": round(t_out - t_in, 3), "enhanced_from": hist})
    new["provenance"] = {**(clip.get("provenance") or {"kind": "footage"}), "processing": [h["op"] for h in hist]}
    return new
