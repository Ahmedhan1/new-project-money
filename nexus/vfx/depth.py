"""Depth effects with Depth Anything V2 **Small** (Apache-2.0; Base/Large are CC BY-NC and never used) via onnxruntime.

    depth(frame_rgb) -> float32 HxW relative inverse depth normalised to 0 (far) .. 1 (near)
    depth_of_field(image, out, focus=None, max_blur=8) -> image with depth-dependent blur (focus = depth value in focus;
                                                          None = the nearest large region, i.e. the subject)
    parallax(image, out, seconds=5, amount=0.04, size=(1280,720)) -> 2.5D camera move from ONE still.
        The result is an ANIMATED STILL (classification `animated-still`), never generated video.
"""
from __future__ import annotations

import functools
import subprocess
import time
from pathlib import Path

import numpy as np

from .. import contracts, costguard
from ..generate import classify, downloads
from ..media import ff

MODEL_ID = "depth-anything-v2-small"
MEAN, STD = np.array([0.485, 0.456, 0.406], np.float32), np.array([0.229, 0.224, 0.225], np.float32)


@functools.lru_cache(maxsize=1)
def _session(path: str, threads: int = 4):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])


def _model() -> str:
    if not downloads.installed(MODEL_ID):
        p = downloads.plan(MODEL_ID)
        raise downloads.ConsentRequired(f"Depth Anything V2 Small ({p['total_gb']} GB, Apache-2.0) is not downloaded; call "
                                        "generate.downloads.fetch() with the user's consent", p)
    return str(downloads.path(MODEL_ID, "onnx/model.onnx"))


def depth(frame: np.ndarray, size: int = 518) -> np.ndarray:
    import cv2
    s = _session(_model())
    h, w = frame.shape[:2]
    sc = size / min(h, w)
    nh, nw = max(14, int(round(h * sc / 14)) * 14), max(14, int(round(w * sc / 14)) * 14)
    x = (cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255.0 - MEAN) / STD
    d = s.run(None, {s.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0][0]
    d = cv2.resize(d.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
    lo, hi = np.percentile(d, 1), np.percentile(d, 99)
    return np.clip((d - lo) / max(1e-6, hi - lo), 0, 1)


def _load(image) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(image).convert("RGB")) if not isinstance(image, np.ndarray) else image


def depth_of_field(image, out: str | Path | None = None, focus: float | None = None, max_blur: float = 8.0, band: float = 0.08,
                   depth_map: np.ndarray | None = None) -> dict:
    """Layered blur: 6 blur levels mixed per pixel by |depth - focus| (a cheap, artefact-tolerant lens blur)."""
    import cv2
    img = _load(image)
    t0 = time.time()
    d = depth(img) if depth_map is None else depth_map
    if focus is None:
        focus = float(np.percentile(d, 90))  # the near subject
    coc = np.clip((np.abs(d - focus) - band) / 0.45, 0, 1)  # depth within +-band of focus stays sharp
    levels = [img.astype(np.float32)] + [cv2.GaussianBlur(img, (0, 0), max(0.5, max_blur * k / 5)).astype(np.float32)
                                         for k in range(1, 6)]
    pos = coc * 5
    lo = np.floor(pos).astype(int)
    fr = (pos - lo)[..., None]
    stack = np.stack(levels)
    hi = np.minimum(lo + 1, 5)
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    res = stack[lo, yy, xx] * (1 - fr) + stack[hi, yy, xx] * fr
    res = np.clip(res + 0.5, 0, 255).astype(np.uint8)
    if out:
        from PIL import Image
        Image.fromarray(res).save(out)
    return {"image": res, "depth": d, "focus": focus, "seconds": round(time.time() - t0, 2), "path": str(out) if out else None}


def parallax(image, out: str | Path, seconds: float = 5.0, amount: float = 0.04, size=(1280, 720), fps: int = 30,
             zoom: float = 1.06, pivot: float = 0.15, source_provenance: dict | None = None) -> dict:
    """Depth-layered camera drift: near pixels move more than far ones (backward warp from the depth map; the slight
    zoom hides the edges). Disocclusions are filled by the warp's edge stretching - visible on strong moves."""
    import cv2
    img = _load(image)
    W, H = size
    sc = max(W / img.shape[1], H / img.shape[0]) * zoom
    big = cv2.resize(img, (int(np.ceil(img.shape[1] * sc)), int(np.ceil(img.shape[0] * sc))), interpolation=cv2.INTER_CUBIC)
    y0, x0 = (big.shape[0] - H) // 2, (big.shape[1] - W) // 2
    t0 = time.time()
    d = depth(img)
    dbig = cv2.resize(d, (big.shape[1], big.shape[0]), interpolation=cv2.INTER_LINEAR)
    dbig = cv2.GaussianBlur(dbig, (0, 0), 3)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    costguard.authorize("ffmpeg", "vfx-parallax")
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                          f"{W}x{H}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt",
                          "yuv420p", str(out)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    n = int(round(seconds * fps))
    dcrop = dbig[y0:y0 + H, x0:x0 + W]
    for i in range(n):
        f = i / max(1, n - 1)
        cam = (f - 0.5) * 2  # -1 .. 1
        shift = amount * W * cam * (dcrop - pivot)  # depth `pivot` stays put; nearer layers slide further
        mapx = (xx + x0 - shift).astype(np.float32)
        mapy = (yy + y0 - 0.3 * shift * H / W).astype(np.float32)
        fr = cv2.remap(big, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        p.stdin.write(np.ascontiguousarray(fr).tobytes())
    p.stdin.close()
    err = p.stderr.read().decode(errors="replace")
    if p.wait() != 0:
        raise ff.FFError(f"parallax encode failed: {ff.tail(err)}", op="vfx-parallax")
    secs = time.time() - t0
    prov = {"kind": "animated_still", "method": "depth parallax (2.5D) from one still - Depth Anything V2 Small",
            "model": MODEL_ID, "still": source_provenance or {"kind": "unknown"}, "amount": amount}
    man = classify.manifest("parallax", prov, Path(out), inputs=[], compute={"device": "cpu", "seconds": round(secs, 2)},
                            seconds=secs, path_check=False)
    assert man["classification"] == "animated-still"
    return {"path": str(out), "provenance": prov, "manifest": man, "seconds": round(secs, 2), "frames": n,
            "effect": contracts.make("EffectManifest", effects=[{"type": "vfx.parallax", "label": "animated-still"}])}
