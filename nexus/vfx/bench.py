"""VFX benchmark on the licensed benchmark media with the REAL models (installed via generate.downloads with consent).

    flock /tmp/nexus-heavy.lock python -m nexus.vfx.bench --out docs/results/account2_vfx [--parts background,tracking,...]

interview (1280x720 talking head): background replacement with MediaPipe selfie and MODNet (s/frame + frames to look at);
tracking: a cloud patch in broll (1920x1012) and the speaker's head in walktalk (1280x720); depth map + depth-of-field +
2.5D parallax from one frame of broll / ad / interview; procedural overlays at 1280x720 and composites.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import time
from pathlib import Path

import numpy as np

from .. import config
from ..generate import downloads, metrics
from ..media import ff
from . import depth, procedural, segment, track

PARTS = ("background", "tracking", "depth", "procedural")
TRACK_TARGETS = [  # (source, start s, seconds, initial box x, y, w, h as fractions) - chosen by looking at the frames
    ("broll", 40.0, 4.0, (0.25, 0.04, 0.2, 0.2)),        # cloud patch drifting past the ISS
    ("walktalk", 20.0, 6.0, (0.40, 0.10, 0.14, 0.26)),   # speaker's head
]


def _src(i: str) -> Path:
    return Path(os.environ.get("NEXUS_BENCH_MEDIA", config.ROOT / ".nexus" / "benchmarks")) / i / "source.mp4"


def _strip(frames: list[np.ndarray], out: Path, h: int = 240) -> None:
    from PIL import Image
    ims = [Image.fromarray(f) for f in frames]
    ims = [im.resize((int(im.width * h / im.height), h)) for im in ims]
    s = Image.new("RGB", (sum(i.width for i in ims), h))
    x = 0
    for im in ims:
        s.paste(im, (x, 0))
        x += im.width
    s.save(out, quality=85)


def _cut(src: Path, out: Path, at: float, dur: float, audio: bool = False) -> Path:
    ff.run(["-ss", at, "-t", dur, "-i", src, *(["-c:a", "aac"] if audio else ["-an"]), "-c:v", "libx264", "-crf", "14", out],
           op="bench-cut")
    return out


def bench_background(work: Path, out: Path) -> dict:
    seg = _cut(_src("interview"), work / "iv_seg.mp4", 60, 3, audio=True)
    src_frames = metrics.read_frames(seg)
    res, shots = {}, [src_frames[len(src_frames) // 2]]
    for m in ("mediapipe-selfie", "modnet"):
        if not downloads.installed(m):
            res[m] = "not installed"
            continue
        r = segment.replace_background(seg, work / f"bg_{m}.mp4", bg="#00B140", model_id=m, use_cache=False)
        fr = metrics.read_frames(r["path"])
        shots.append(fr[len(fr) // 2])
        green = (np.abs(fr.astype(int) - np.array([0, 177, 64])).sum(-1) < 40).mean()
        res[m] = {"frames": r["frames"], "seconds": r["seconds"], "s_per_frame": r["s_per_frame"],
                  "fps": round(r["frames"] / r["seconds"], 2), "share_replaced": round(float(green), 3)}
    _strip(shots, out / "interview_background_replacement.jpg")
    return res


def bench_tracking(work: Path, out: Path) -> dict:
    import cv2
    res = {}
    for name, at, dur, box in TRACK_TARGETS:
        seg = _cut(_src(name), work / f"track_{name}.mp4", at, dur)
        t0 = time.time()
        tr = track.track_box(seg, box)
        secs = time.time() - t0
        res[name] = {"frames": tr["frames"], "seconds": round(secs, 2), "s_per_frame": round(secs / tr["frames"], 4),
                     "redetections": tr["redetections"], "lost": tr["lost"], "start_box": tr["samples"][0],
                     "end_box": tr["samples"][-1], "mask_keyframes": {k: len(v) for k, v in track.mask_keyframes(tr).items()}}
        bf = metrics.read_frames(seg)
        boxed = []
        for i in (0, len(bf) // 2, len(bf) - 1):
            s = tr["samples"][min(i, len(tr["samples"]) - 1)]
            f = bf[i].copy()
            H, W = f.shape[:2]
            cv2.rectangle(f, (int(s["x"] * W), int(s["y"] * H)), (int((s["x"] + s["w"]) * W), int((s["y"] + s["h"]) * H)),
                          (255, 40, 40), 4)
            boxed.append(f)
        _strip(boxed, out / f"{name}_tracking.jpg")
    return res


def bench_depth(work: Path, out: Path) -> dict:
    if not downloads.installed("depth-anything-v2-small"):
        return {"error": "depth-anything-v2-small not installed"}
    res = {}
    for name, at in (("broll", 45.0), ("ad", 62.0), ("interview", 61.0)):
        still = work / f"{name}_still.png"
        ff.run(["-ss", at, "-i", _src(name), "-frames:v", 1, still], op="bench-still")
        img = depth._load(still)
        t0 = time.time()
        d = depth.depth(img)
        dt = time.time() - t0
        dof = depth.depth_of_field(img, work / f"{name}_dof.png", depth_map=d, max_blur=10)
        dvis = (np.stack([d] * 3, -1) * 255).astype(np.uint8)
        _strip([img, dvis, dof["image"]], out / f"{name}_depth_dof.jpg")
        par = depth.parallax(still, work / f"{name}_parallax.mp4", seconds=3.0, amount=0.035, size=(1280, 720))
        pf = metrics.read_frames(par["path"])
        _strip([pf[0], pf[len(pf) // 2], pf[-1]], out / f"{name}_parallax_frames.jpg")
        res[name] = {"input": f"{img.shape[1]}x{img.shape[0]}", "depth_seconds": round(dt, 2), "dof_seconds": dof["seconds"],
                     "parallax_seconds": par["seconds"], "parallax_frames": par["frames"],
                     "parallax_classification": par["manifest"]["classification"], "still_motion": metrics.still_motion(pf)}
    return res


def bench_procedural(work: Path, out: Path) -> dict:
    res = {}
    for kind in ("dust", "snow", "embers", "bokeh"):
        t0 = time.time()
        r = procedural.particles(work / f"p_{kind}.mov", kind=kind, size=(1280, 720), seconds=2.0, fps=30)
        res[kind] = {"seconds": round(time.time() - t0, 2), "frames": r["frames"]}
    for name, fn in (("light_leak", procedural.light_leak), ("lens_flare", procedural.lens_flare)):
        t0 = time.time()
        r = fn(work / f"{name}.mov", size=(1280, 720), seconds=2.0, fps=30)
        res[name] = {"seconds": round(time.time() - t0, 2), "frames": r["frames"]}
    seg = work / "iv_seg.mp4"
    if not seg.exists():
        _cut(_src("interview"), seg, 60, 3, audio=True)
    comp = []
    for fx, mode in (("p_snow.mov", "over"), ("p_embers.mov", "screen"), ("light_leak.mov", "screen"), ("lens_flare.mov", "screen")):
        o = procedural.overlay(seg, work / fx, work / f"ov_{fx}.mp4", mode=mode)
        f = metrics.read_frames(o["path"])
        comp.append(f[len(f) // 2])
    _strip(comp, out / "interview_overlays.jpg", h=200)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.ROOT / "docs" / "results" / "account2_vfx"))
    ap.add_argument("--parts", default=",".join(PARTS))
    a = ap.parse_args(argv)
    parts = [p for p in a.parts.split(",") if p in PARTS]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = config.CACHE / "vfx_bench"
    work.mkdir(parents=True, exist_ok=True)
    prev = out / "bench.json"
    rep: dict = json.loads(prev.read_text(encoding="utf-8")) if prev.exists() else {}
    rep.update({"date": time.strftime("%Y-%m-%d"), "machine": {"platform": platform.platform(), "cpus": os.cpu_count(),
                                                               "note": "shared 4-vCPU container, no GPU"},
                "models_installed": {m: downloads.installed(m) for m in ("mediapipe-selfie", "modnet", "depth-anything-v2-small")}})
    fns = {"background": bench_background, "tracking": bench_tracking, "depth": bench_depth, "procedural": bench_procedural}
    for p in parts:
        rep[p] = fns[p](work, out)
        prev.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(json.dumps({p: rep[p] for p in parts}, default=str)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
