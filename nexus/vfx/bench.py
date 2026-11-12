"""VFX benchmark on the licensed benchmark media with the REAL models (installed via generate.downloads with consent).

    flock /tmp/nexus-heavy.lock python -m nexus.vfx.bench --out docs/results/account2_vfx

interview (1280x720 talking head): background replacement with MediaPipe selfie and MODNet (s/frame + frames to look at),
broll (1920x1012): box tracking on a moving shot, depth map + depth-of-field + 2.5D parallax from one frame,
procedural overlays at 1280x720 (generation speed) and a screen-blend composite.
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


def _src(i: str) -> Path:
    return Path(os.environ.get("NEXUS_BENCH_MEDIA", config.ROOT / ".nexus" / "benchmarks")) / i / "source.mp4"


def _save(arr: np.ndarray, out: Path, width: int = 640) -> None:
    from PIL import Image
    im = Image.fromarray(arr)
    im.thumbnail((width, width))
    im.save(out, quality=85)


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.ROOT / "docs" / "results" / "account2_vfx"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = config.CACHE / "vfx_bench"
    work.mkdir(parents=True, exist_ok=True)
    rep: dict = {"date": time.strftime("%Y-%m-%d"), "machine": {"platform": platform.platform(), "cpus": os.cpu_count(),
                                                                "note": "shared 4-vCPU container, no GPU"},
                 "models_installed": {m: downloads.installed(m) for m in ("mediapipe-selfie", "modnet", "depth-anything-v2-small")}}
    # ---- background replacement on the talking head
    iv = _src("interview")
    seg = work / "iv_seg.mp4"
    ff.run(["-ss", 60, "-t", 3, "-i", iv, "-c:v", "libx264", "-crf", "14", "-c:a", "aac", seg], op="bench-cut")
    src_frames = metrics.read_frames(seg)
    rep["background"] = {}
    shots = [src_frames[len(src_frames) // 2]]
    for m in ("mediapipe-selfie", "modnet"):
        if not downloads.installed(m):
            rep["background"][m] = "not installed"
            continue
        r = segment.replace_background(seg, work / f"bg_{m}.mp4", bg="#00B140", model_id=m, use_cache=False)
        fr = metrics.read_frames(r["path"])
        shots.append(fr[len(fr) // 2])
        green = (np.abs(fr.astype(int) - np.array([0, 177, 64])).sum(-1) < 40).mean()
        rep["background"][m] = {"frames": r["frames"], "seconds": r["seconds"], "s_per_frame": r["s_per_frame"],
                                "fps": round(r["frames"] / r["seconds"], 2), "share_replaced": round(float(green), 3)}
    _strip(shots, out / "interview_background_replacement.jpg")
    # ---- tracking on B-roll
    br = _src("broll")
    bseg = work / "br_seg.mp4"
    ff.run(["-ss", 40, "-t", 4, "-i", br, "-an", "-c:v", "libx264", "-crf", "14", bseg], op="bench-cut")
    t0 = time.time()
    tr = track.track_box(bseg, (0.4, 0.4, 0.2, 0.2))
    secs = time.time() - t0
    rep["tracking"] = {"frames": tr["frames"], "seconds": round(secs, 2), "s_per_frame": round(secs / tr["frames"], 4),
                       "redetections": tr["redetections"], "lost": tr["lost"],
                       "start_box": tr["samples"][0], "end_box": tr["samples"][-1]}
    bf = metrics.read_frames(bseg)
    import cv2
    boxed = []
    for i in (0, len(bf) // 2, len(bf) - 1):
        s = tr["samples"][min(i, len(tr["samples"]) - 1)]
        f = bf[i].copy()
        H, W = f.shape[:2]
        cv2.rectangle(f, (int(s["x"] * W), int(s["y"] * H)), (int((s["x"] + s["w"]) * W), int((s["y"] + s["h"]) * H)), (255, 40, 40), 4)
        boxed.append(f)
    _strip(boxed, out / "broll_tracking.jpg")
    # ---- depth, DoF, parallax from one B-roll frame and one ad frame
    if downloads.installed("depth-anything-v2-small"):
        rep["depth"] = {}
        for name, path, at in (("broll", br, 45.0), ("ad", _src("ad"), 62.0), ("interview", iv, 61.0)):
            still = work / f"{name}_still.png"
            ff.run(["-ss", at, "-i", path, "-frames:v", 1, still], op="bench-still")
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
            rep["depth"][name] = {"input": f"{img.shape[1]}x{img.shape[0]}", "depth_seconds": round(dt, 2),
                                  "dof_seconds": dof["seconds"], "parallax_seconds": par["seconds"], "parallax_frames": par["frames"],
                                  "parallax_classification": par["manifest"]["classification"],
                                  "still_motion": metrics.still_motion(pf)}
    # ---- procedural overlays at 1280x720
    rep["procedural"] = {}
    for kind in ("dust", "snow", "embers", "bokeh"):
        t0 = time.time()
        r = procedural.particles(work / f"p_{kind}.mov", kind=kind, size=(1280, 720), seconds=2.0, fps=30)
        rep["procedural"][kind] = {"seconds": round(time.time() - t0, 2), "frames": r["frames"]}
    for name, fn in (("light_leak", procedural.light_leak), ("lens_flare", procedural.lens_flare)):
        t0 = time.time()
        r = fn(work / f"{name}.mov", size=(1280, 720), seconds=2.0, fps=30)
        rep["procedural"][name] = {"seconds": round(time.time() - t0, 2), "frames": r["frames"]}
    comp = []
    for fx, mode in (("p_snow.mov", "over"), ("p_embers.mov", "screen"), ("light_leak.mov", "screen"), ("lens_flare.mov", "screen")):
        o = procedural.overlay(seg, work / fx, work / f"ov_{fx}.mp4", mode=mode)
        f = metrics.read_frames(o["path"])
        comp.append(f[len(f) // 2])
    _strip(comp, out / "interview_overlays.jpg", h=200)
    (out / "bench.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(json.dumps(rep, default=str)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
