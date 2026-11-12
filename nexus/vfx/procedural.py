"""Procedural overlays (NumPy, seeded, no model, labelled `procedural`): particles, light leaks, lens flares.

    particles(out, kind="dust"|"snow"|"embers"|"bokeh", size=(1280,720), seconds=4, fps=30, density=0.5, seed=1)
    light_leak(out, size, seconds, fps, intensity=0.6, palette="warm"|"cool", seed=1)
    lens_flare(out, size, seconds, fps, path=((0.2,0.25),(0.6,0.3)), intensity=0.8)
        -> RGBA MOV (PNG codec, straight alpha) to composite with `overlay()` or as a renderer overlay track
    overlay(base, fx, out, mode="over"|"screen", opacity=1.0) -> composited MP4 (base audio kept)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from .. import config, contracts, costguard
from ..generate import classify
from ..media import ff

KINDS = {
    # count per megapixel, radius px range, speed px/s (x, y), colour, blur, twinkle
    "dust": {"n": 90, "r": (0.6, 2.2), "v": ((-6, 6), (-4, 4)), "col": (255, 244, 220), "alpha": (0.25, 0.8)},
    "snow": {"n": 160, "r": (1.0, 3.5), "v": ((-15, 15), (40, 110)), "col": (255, 255, 255), "alpha": (0.5, 0.95)},
    "embers": {"n": 70, "r": (0.8, 2.6), "v": ((-20, 20), (-90, -30)), "col": (255, 140, 40), "alpha": (0.5, 1.0)},
    "bokeh": {"n": 14, "r": (14, 46), "v": ((-8, 8), (-5, 5)), "col": (255, 214, 160), "alpha": (0.10, 0.30)},
}


def _writer(out: Path, w: int, h: int, fps: float):
    costguard.authorize("ffmpeg", "vfx-procedural")
    return subprocess.Popen(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
                             "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "png", "-pix_fmt", "rgba", str(out)],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def _finish(p, out: Path, kind: str, params: dict, frames: int) -> dict:
    p.stdin.close()
    err = p.stderr.read().decode(errors="replace")
    if p.wait() != 0:
        raise ff.FFError(f"procedural encode failed: {ff.tail(err)}", op="vfx-procedural")
    man = classify.manifest(kind, {"kind": "procedural", "method": f"nexus.vfx.procedural.{kind}", "params": params}, out,
                            compute={"device": "cpu"}, path_check=False)
    return {"path": str(out), "frames": frames, "alpha": True, "manifest": man}


def _splat(canvas_a: np.ndarray, canvas_rgb: np.ndarray, x: float, y: float, r: float, col, a: float, soft: bool) -> None:
    h, w = canvas_a.shape
    rr = int(np.ceil(r * (2.2 if soft else 1.3))) + 1
    x0, x1, y0, y1 = max(0, int(x) - rr), min(w, int(x) + rr + 1), max(0, int(y) - rr), min(h, int(y) + rr + 1)
    if x0 >= x1 or y0 >= y1:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = np.sqrt((xx - x) ** 2 + (yy - y) ** 2) / max(r, 0.3)
    k = np.exp(-d * d * 1.2) if soft else np.clip(1.2 - d, 0, 1) ** 1.5
    k = (k * a).astype(np.float32)
    old = canvas_a[y0:y1, x0:x1]
    new = old + k * (1 - old)
    wgt = np.where(new > 1e-6, k / np.maximum(new, 1e-6), 0)[..., None]
    canvas_rgb[y0:y1, x0:x1] = canvas_rgb[y0:y1, x0:x1] * (1 - wgt) + np.array(col, np.float32) * wgt
    canvas_a[y0:y1, x0:x1] = new


def particles(out: str | Path, kind: str = "dust", size=(1280, 720), seconds: float = 4.0, fps: float = 30,
              density: float = 0.5, seed: int = 1) -> dict:
    if kind not in KINDS:
        raise ValueError(f"particle kind must be one of {sorted(KINDS)}")
    spec = KINDS[kind]
    w, h = size
    rng = np.random.default_rng(seed)
    n = max(6, int(spec["n"] * (w * h / 1e6) * (0.2 + 1.6 * density)))
    pos = rng.uniform([0, 0], [w, h], (n, 2))
    vel = np.stack([rng.uniform(*spec["v"][0], n), rng.uniform(*spec["v"][1], n)], -1)
    k = h / 720.0  # sizes and speeds are specified for 720p
    rad = np.maximum(0.6, rng.uniform(*spec["r"], n) * k)
    vel *= k
    alp = rng.uniform(*spec["alpha"], n)
    ph = rng.uniform(0, 2 * np.pi, n)
    out = Path(out)
    p = _writer(out, w, h, fps)
    frames = int(round(seconds * fps))
    for i in range(frames):
        t = i / fps
        a = np.zeros((h, w), np.float32)
        rgb = np.zeros((h, w, 3), np.float32)
        wob = np.stack([np.sin(t * 1.3 + ph) * 3, np.cos(t * 0.9 + ph) * 2], -1) if kind in ("dust", "snow") else 0
        cur = pos + vel * t + wob
        cur[:, 0] %= w
        cur[:, 1] %= h
        tw = 0.65 + 0.35 * np.sin(t * (6 if kind == "embers" else 2) + ph)
        for j in range(n):
            _splat(a, rgb, cur[j, 0], cur[j, 1], rad[j], spec["col"], float(alp[j] * tw[j]), soft=kind != "snow")
        p.stdin.write(np.dstack([np.clip(rgb, 0, 255), np.clip(a * 255, 0, 255)]).astype(np.uint8).tobytes())
    return _finish(p, out, f"particles.{kind}", {"kind": kind, "density": density, "seed": seed, "size": list(size)}, frames)


PALETTES = {"warm": [(255, 120, 40), (255, 190, 90), (255, 70, 60)], "cool": [(80, 150, 255), (150, 110, 255), (90, 220, 255)]}


def light_leak(out: str | Path, size=(1280, 720), seconds: float = 4.0, fps: float = 30, intensity: float = 0.6,
               palette: str = "warm", seed: int = 1) -> dict:
    """Soft drifting colour blooms from the frame edges (screen-blend overlay)."""
    w, h = size
    rng = np.random.default_rng(seed)
    cols = PALETTES.get(palette, PALETTES["warm"])
    blobs = [{"c": np.array(cols[k % len(cols)], np.float32), "x0": rng.choice([-0.15, 1.15]) if k % 2 == 0 else rng.uniform(0, 1),
              "y0": rng.uniform(-0.1, 1.1), "r": rng.uniform(0.35, 0.7), "ph": rng.uniform(0, 6.28), "sp": rng.uniform(0.15, 0.4)}
             for k in range(4)]
    sw, sh = max(32, w // 8), max(18, h // 8)  # render small, upscale: leaks are very soft
    yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
    xx, yy = xx / sw, yy / sh
    import cv2
    out = Path(out)
    p = _writer(out, w, h, fps)
    frames = int(round(seconds * fps))
    for i in range(frames):
        t = i / fps
        a = np.zeros((sh, sw), np.float32)
        rgb = np.zeros((sh, sw, 3), np.float32)
        for b in blobs:
            cx = b["x0"] + 0.25 * np.sin(t * b["sp"] + b["ph"])
            cy = b["y0"] + 0.2 * np.cos(t * b["sp"] * 0.8 + b["ph"])
            k = np.exp(-(((xx - cx) ** 2 + ((yy - cy) * h / w) ** 2) / (b["r"] ** 2)) * 3)
            k *= intensity * (0.75 + 0.25 * np.sin(t * 0.7 + b["ph"]))
            rgb += b["c"] * k[..., None]
            a = a + k * (1 - a)
        rgb = rgb / np.maximum(a[..., None] + 1e-6, 1e-6) * np.minimum(a[..., None], 1)
        big = cv2.resize(np.dstack([np.clip(rgb, 0, 255), np.clip(a, 0, 1) * 255]), (w, h), interpolation=cv2.INTER_CUBIC)
        p.stdin.write(np.clip(big, 0, 255).astype(np.uint8).tobytes())
    return _finish(p, out, "light_leak", {"palette": palette, "intensity": intensity, "seed": seed}, frames)


def lens_flare(out: str | Path, size=(1280, 720), seconds: float = 4.0, fps: float = 30,
               path=((0.2, 0.25), (0.6, 0.3)), intensity: float = 0.8) -> dict:
    """Light source moving along `path` (fractions) with ghosts mirrored through the frame centre + a horizontal streak."""
    w, h = size
    out = Path(out)
    p = _writer(out, w, h, fps)
    frames = int(round(seconds * fps))
    ghosts = [(0.35, 0.05, (120, 200, 255)), (0.7, 0.03, (255, 170, 90)), (1.25, 0.07, (140, 255, 170)), (1.6, 0.02, (255, 120, 200))]
    for i in range(frames):
        f = i / max(1, frames - 1)
        sx = (path[0][0] + (path[1][0] - path[0][0]) * f) * w
        sy = (path[0][1] + (path[1][1] - path[0][1]) * f) * h
        a = np.zeros((h, w), np.float32)
        rgb = np.zeros((h, w, 3), np.float32)
        _splat(a, rgb, sx, sy, 0.05 * h, (255, 245, 225), 0.95 * intensity, soft=True)
        _splat(a, rgb, sx, sy, 0.012 * h, (255, 255, 255), 1.0 * intensity, soft=True)
        cx, cy = w / 2, h / 2
        for k, r, col in ghosts:
            gx, gy = sx + (cx - sx) * 2 * k / 1.6, sy + (cy - sy) * 2 * k / 1.6
            _splat(a, rgb, gx, gy, r * h, col, 0.22 * intensity, soft=False)
        y0, y1 = max(0, int(sy) - 2), min(h, int(sy) + 3)
        xs = np.exp(-np.abs(np.arange(w) - sx) / (0.25 * w)) * 0.55 * intensity
        a[y0:y1] = np.maximum(a[y0:y1], xs[None, :])
        rgb[y0:y1] = np.where(a[y0:y1, :, None] > 0, np.maximum(rgb[y0:y1], np.array([200, 225, 255], np.float32)), rgb[y0:y1])
        p.stdin.write(np.dstack([np.clip(rgb, 0, 255), np.clip(a * 255, 0, 255)]).astype(np.uint8).tobytes())
    return _finish(p, out, "lens_flare", {"path": [list(x) for x in path], "intensity": intensity}, frames)


def overlay(base: str | Path, fx: str | Path, out: str | Path, mode: str = "over", opacity: float = 1.0) -> dict:
    """Composite an RGBA overlay (looped/cut to the base length) over a video; `screen` for light effects."""
    info = ff.probe(base)
    v = info["video"]
    if mode == "screen":
        g = (f"[1:v]scale={v['width']}:{v['height']},format=rgba,colorchannelmixer=aa={opacity:.3f},premultiply=inplace=1,"
             "format=gbrp[f];[0:v]format=gbrp[b];[b][f]blend=all_mode=screen,format=yuv420p[o]")
    else:
        g = (f"[1:v]scale={v['width']}:{v['height']},format=rgba,colorchannelmixer=aa={opacity:.3f}[f];"
             "[0:v][f]overlay=format=auto:shortest=0:eof_action=pass,format=yuv420p[o]")
    ff.run(["-i", base, "-stream_loop", "-1", "-i", fx, "-filter_complex", g, "-map", "[o]", "-map", "0:a?", "-t",
            f"{info['duration']:.3f}", *config.VCODEC, "-c:a", "aac", out], op="vfx-overlay")
    return {"path": str(out), "manifest": contracts.make("EffectManifest", effects=[{"type": f"vfx.overlay.{mode}",
                                                                                      "fx": str(fx), "opacity": opacity}])}
