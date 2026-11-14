"""Make a big source video workable: one H.264/AAC MP4, at most 1080p (the long side of a vertical video too), sensible
bitrate, fast-start. Used for large uploads (phones record 4K/HEVC at 50-100 Mbit/s): editing, analysis, rendering and
saving all get faster and smaller, and every later stage reads a format FFmpeg decodes everywhere.

Rules: nothing is done when the file is already small and simple (same quality, no re-encode). Otherwise one encode on
the best working encoder (NVENC on the cloud studio's GPU, else libx264) at constant quality "balanced"; frame rate is
kept (capped at 60), audio becomes AAC 160 kb/s. A result that is not smaller is discarded and the original kept.
"""
from __future__ import annotations

from pathlib import Path

from . import encoders, ff

MAX_SIDE = 1920           # long side; 1080p landscape or 1080x1920 vertical
SMALL_BYTES = 300 * 2 ** 20
MAX_BITRATE = 12_000_000  # above this a 1080p source is wasteful for editing


def plan(info: dict, size: int) -> dict:
    """Decide from a probe(): {"reencode": bool, "why": [...], "scale": filter or None}."""
    v = info.get("video") or {}
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    dur = float(info.get("duration") or 0)
    br = size * 8 / dur if dur else 0
    why = []
    if max(w, h) > MAX_SIDE:
        why.append(f"{w}x{h} is bigger than 1080p")
    if br > MAX_BITRATE:
        why.append(f"{br / 1e6:.0f} Mbit/s is more than editing needs")
    if (v.get("codec") or "").lower() not in ("h264", "") and size > SMALL_BYTES:
        why.append(f"{v.get('codec')} is slow to edit")
    if not v:
        return {"reencode": False, "why": ["no video stream (audio file): kept as is"], "scale": None}
    if size <= SMALL_BYTES and max(w, h) <= MAX_SIDE:
        return {"reencode": False, "why": ["already small"], "scale": None}
    scale = None
    if max(w, h) > MAX_SIDE:
        scale = f"scale='if(gt(iw,ih),{MAX_SIDE},-2)':'if(gt(iw,ih),-2,{MAX_SIDE})'"
    return {"reencode": bool(why), "why": why or ["within limits"], "scale": scale}


def optimize(src: Path, dest: Path, on_progress=None) -> dict:
    """Write the optimised copy to `dest` (when worthwhile). Returns a report; report["path"] is the file to use."""
    src, dest = Path(src), Path(dest)
    size = src.stat().st_size
    info = ff.probe(src)
    p = plan(info, size)
    rep = {"input_bytes": size, "why": p["why"], "path": str(src), "reencoded": False}
    if not p["reencode"]:
        return rep
    fps = float((info.get("video") or {}).get("fps") or 30)
    vf = [p["scale"]] if p["scale"] else []
    if fps > 60:
        vf.append("fps=60")
    dest.parent.mkdir(parents=True, exist_ok=True)
    common = ["-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", *(["-vf", ",".join(vf)] if vf else []),
              *encoders.audio_args("aac", "balanced"), "-movflags", "+faststart", str(dest)]
    try:
        ff.run([*common[:-1], *encoders.video_args("h264", "balanced"), common[-1]], op="optimize",
               timeout=6 * 3600, on_progress=on_progress, total=float(info.get("duration") or 0) or None)
    except Exception:  # noqa: BLE001 - a hardware encoder can still fail at run time: one CPU retry
        ff.run([*common[:-1], *encoders.video_args("h264", "balanced", hw="cpu"), common[-1]], op="optimize-cpu",
               timeout=6 * 3600, on_progress=on_progress, total=float(info.get("duration") or 0) or None)
    out = dest.stat().st_size
    if out >= size:
        dest.unlink(missing_ok=True)
        rep["why"].append("the re-encoded file was not smaller: original kept")
        return rep
    return {**rep, "path": str(dest), "reencoded": True, "output_bytes": out, "saved_percent": round(100 * (1 - out / size), 1)}
