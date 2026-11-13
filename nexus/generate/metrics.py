"""Image/video quality measures used by the enhancement benchmarks, the generated-video benchmark and the
classification guard. NumPy + OpenCV only (no torch).

    psnr(a, b), ssim(a, b)                     uint8 HxW(xC) arrays, SSIM = Wang et al. 2004 (Gaussian 11x11, sigma 1.5)
    read_frames(path, n=None, width=None)      decoded RGB frames via FFmpeg
    temporal(frames)                           motion, flicker, frame-to-frame SSIM, optical-flow warp error
    still_motion(frames)                       how well ONE still + a global similarity transform explains every frame
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np


def _gray(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a)
    if a.ndim == 3:
        a = a[..., :3].astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    return a.astype(np.float64)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((np.asarray(a, np.float64) - np.asarray(b, np.float64)) ** 2))
    return 99.0 if mse == 0 else round(10 * np.log10(255.0**2 / mse), 3)


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    import cv2
    x, y = _gray(a), _gray(b)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2

    def blur(z):
        return cv2.GaussianBlur(z, (11, 11), 1.5)
    mx, my = blur(x), blur(y)
    sxx, syy, sxy = blur(x * x) - mx * mx, blur(y * y) - my * my, blur(x * y) - mx * my
    m = ((2 * mx * my + c1) * (2 * sxy + c2)) / ((mx * mx + my * my + c1) * (sxx + syy + c2))
    return round(float(m[5:-5, 5:-5].mean() if min(m.shape) > 20 else m.mean()), 5)


def read_frames(path: str | Path, n: int | None = None, width: int | None = None, start: float = 0.0,
                fps: float | None = None) -> np.ndarray:
    from ..media import ff
    v = ff.probe(path)["video"]
    w, h = v["width"], v["height"]
    if width:
        h = int(round(h * width / w / 2)) * 2
        w = width
    vf = ([f"fps={fps}"] if fps else []) + [f"scale={w}:{h}:flags=area"]
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", str(start), "-i", str(path), "-an",
           "-vf", ",".join(vf)] + (["-frames:v", str(n)] if n else []) + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=3600)
    k = len(p.stdout) // (w * h * 3)
    return np.frombuffer(p.stdout[: k * w * h * 3], dtype=np.uint8).reshape(k, h, w, 3)


def flow_warp_error(a: np.ndarray, b: np.ndarray) -> float:
    """Mean |b - warp(a, flow a->b)| / 255 after Farneback flow: low = the next frame is explained by motion of the
    previous one (temporally consistent); high = pixels appear/change without motion (flicker, boiling texture)."""
    import cv2
    ga, gb = _gray(a).astype(np.uint8), _gray(b).astype(np.uint8)
    flow = cv2.calcOpticalFlowFarneback(gb, ga, None, 0.5, 3, 15, 3, 5, 1.2, 0)
    h, w = ga.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    warped = cv2.remap(ga, gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    return round(float(np.abs(warped.astype(np.float64) - gb).mean() / 255.0), 5)


def temporal(frames: np.ndarray, max_pairs: int = 48) -> dict:
    f = np.asarray(frames)
    if len(f) < 2:
        return {"frames": len(f), "motion": 0.0, "flicker": 0.0, "ssim_f2f_mean": 1.0, "ssim_f2f_min": 1.0, "warp_error": 0.0}
    idx = np.linspace(0, len(f) - 2, min(max_pairs, len(f) - 1)).astype(int)
    g = np.stack([_gray(x) for x in f])
    diff = np.abs(np.diff(g, axis=0)).mean(axis=(1, 2))
    br = g.mean(axis=(1, 2))
    ss = [ssim(f[i], f[i + 1]) for i in idx]
    we = [flow_warp_error(f[i], f[i + 1]) for i in idx]
    return {"frames": int(len(f)), "motion": round(float(diff.mean()), 3), "motion_min": round(float(diff.min()), 3),
            "flicker": round(float(np.abs(np.diff(br, 2)).mean()) if len(br) > 2 else 0.0, 3),
            "ssim_f2f_mean": round(float(np.mean(ss)), 4), "ssim_f2f_min": round(float(np.min(ss)), 4),
            "warp_error": round(float(np.mean(we)), 5), "warp_error_max": round(float(np.max(we)), 5)}


def still_motion(frames: np.ndarray, samples: int = 12, width: int = 192) -> dict:
    """Fit every sampled frame as a similarity transform (zoom/pan/rotate) of the FIRST frame and report the residual.
    A Ken Burns / pan of one still has residual ~0 with non-zero motion; real video (moving subjects, parallax, new
    content) does not. Used by classify.py as evidence, never as the only criterion."""
    import cv2
    f = np.asarray(frames)
    if len(f) < 3:
        return {"frames": len(f), "verdict": "too-short", "residual": None, "motion": 0.0}
    h0, w0 = f.shape[1:3]
    hh = int(round(h0 * width / w0))
    small = [cv2.resize(_gray(x).astype(np.float32), (width, hh), interpolation=cv2.INTER_AREA) for x in f]
    ref = small[0]
    res, mot = [], []
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6)
    warp = np.eye(2, 3, dtype=np.float32)
    for i in np.linspace(1, len(f) - 1, min(samples, len(f) - 1)).astype(int):
        cur = small[i]
        mot.append(float(np.abs(cur - ref).mean()))
        try:  # initialised with the previous sample's warp: a slow zoom/pan converges step by step
            _, warp = cv2.findTransformECC(ref, cur, warp.copy(), cv2.MOTION_AFFINE, crit, None, 5)
        except cv2.error:
            warp = np.eye(2, 3, dtype=np.float32)
            res.append(float(np.abs(cur - ref).mean()))
            continue
        # warp maps ref coordinates into cur: sampling cur through it reproduces ref if cur is a transform of ref
        aligned = cv2.warpAffine(cur, warp, (width, hh), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
        valid = cv2.warpAffine(np.ones_like(cur), warp, (width, hh), flags=cv2.INTER_NEAREST + cv2.WARP_INVERSE_MAP) > 0.5
        valid = cv2.erode(valid.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        res.append(float(np.abs(aligned - ref)[valid].mean()) if valid.sum() > 100 else float(np.abs(cur - ref).mean()))
    residual, motion = float(np.median(res)), float(np.mean(mot))
    if motion < 0.5:
        verdict = "static"
    elif residual < 3.0 and residual < 0.25 * motion:
        verdict = "global-transform-of-one-still"
    else:
        verdict = "independent-motion"
    return {"frames": int(len(f)), "residual": round(residual, 3), "motion": round(motion, 3), "verdict": verdict}
