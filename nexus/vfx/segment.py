"""Background removal / replacement with licence-clean ONNX matting models on CPU (onnxruntime).

Models (registry ids, Apache-2.0, fetched only with consent via generate.downloads):
    mediapipe-selfie  0.46 MB, 256x256 input, people only           (fast)
    modnet            25.9 MB, short side 512 (multiple of 32), portrait matting with soft hair edges
    u2netp            4.6 MB, 320x320 salient object (products/objects)

    matte(frame_rgb, model) -> float32 alpha HxW in [0, 1]
    replace_background(src, out, bg="#00FF00"|"blur"|path, model=..., feather=..., smooth=...) -> result dict
    cutout(src, out, model=...) -> RGBA MOV (PNG codec) for compositing in the renderer as an overlay
Temporal smoothing (exponential, `smooth` 0..0.9) damps mask flicker between frames.
"""
from __future__ import annotations

import functools
import time
from pathlib import Path

import numpy as np

from .. import contracts
from ..enhance import framepipe
from ..generate import cache as gcache
from ..generate import classify, downloads

MODELS = {"mediapipe-selfie": {"file": "onnx/model.onnx"}, "modnet": {"file": "onnx/model.onnx"},
          "u2netp": {"file": "onnx/model.onnx"}}


def _need(model_id: str) -> Path:
    if not downloads.installed(model_id):
        p = downloads.plan(model_id)
        raise downloads.ConsentRequired(f"{p['name']} ({p['total_gb']} GB, {p['licence']['weights_licence']}) is not downloaded; "
                                        "call generate.downloads.fetch() with the user's consent", p)
    return downloads.path(model_id, MODELS[model_id]["file"])


@functools.lru_cache(maxsize=4)
def _session(path: str, threads: int = 4):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])


def matte(frame: np.ndarray, model_id: str = "mediapipe-selfie") -> np.ndarray:
    import cv2
    s = _session(str(_need(model_id)))
    h, w = frame.shape[:2]
    inp = s.get_inputs()[0].name
    if model_id == "mediapipe-selfie":
        x = cv2.resize(frame, (256, 256), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        a = s.run(None, {inp: x.transpose(2, 0, 1)[None]})[0][0, 0]
    elif model_id == "modnet":
        sc = 512 / min(h, w)
        nh, nw = max(32, int(round(h * sc / 32)) * 32), max(32, int(round(w * sc / 32)) * 32)
        x = (cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0 - 0.5) / 0.5
        a = s.run(None, {inp: x.transpose(2, 0, 1)[None]})[0][0, 0]
    else:  # u2netp: 320x320, ImageNet normalisation, first output, min-max normalised
        x = cv2.resize(frame, (320, 320), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
        a = s.run(None, {inp: x.transpose(2, 0, 1)[None]})[0][0, 0]
        a = (a - a.min()) / max(1e-6, float(a.max() - a.min()))
    return np.clip(cv2.resize(a.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR), 0, 1)


def _background(bg, w: int, h: int):
    """Returns fn(frame, index) -> background frame."""
    import cv2
    if bg in (None, "blur"):
        return lambda f, i: cv2.GaussianBlur(f, (0, 0), max(4, w / 60))
    if isinstance(bg, str) and bg.startswith("#") and len(bg) == 7:
        col = np.array([int(bg[k:k + 2], 16) for k in (1, 3, 5)], np.uint8)
        plate = np.broadcast_to(col, (h, w, 3)).copy()
        return lambda f, i: plate
    p = Path(bg)
    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        from PIL import Image
        im = np.asarray(Image.open(p).convert("RGB"))
        sc = max(w / im.shape[1], h / im.shape[0])
        im = cv2.resize(im, (int(np.ceil(im.shape[1] * sc)), int(np.ceil(im.shape[0] * sc))), interpolation=cv2.INTER_AREA)
        y0, x0 = (im.shape[0] - h) // 2, (im.shape[1] - w) // 2
        plate = np.ascontiguousarray(im[y0:y0 + h, x0:x0 + w])
        return lambda f, i: plate
    from ..generate import metrics
    frames = metrics.read_frames(p)
    frames = np.stack([cv2.resize(x, (w, h), interpolation=cv2.INTER_AREA) for x in frames])
    return lambda f, i: frames[i % len(frames)]


def replace_background(src: str | Path, out: str | Path | None = None, *, bg="#00B140", model_id: str = "mediapipe-selfie",
                       feather: float = 1.5, smooth: float = 0.5, threshold: float | None = None, start: float = 0.0,
                       duration: float | None = None, use_cache: bool = True) -> dict:
    import cv2
    _need(model_id)
    params = {"bg": str(bg), "model": model_id, "feather": feather, "smooth": smooth, "threshold": threshold}
    inputs = [src] + ([bg] if isinstance(bg, (str, Path)) and Path(str(bg)).exists() else [])
    k = gcache.key("vfx.replace_background", {**params, "start": start, "duration": duration}, inputs, model_id)
    mc = gcache.MediaCache()
    if use_cache and out is None and (hit := mc.get(k, ".mp4")):
        return {"path": str(hit), "cached": True, "manifest": (mc.meta(k) or {}).get("manifest"), "params": params}
    dest = Path(out) if out else mc.path_for(k, ".mp4")
    info = framepipe.video_info(src)["video"]
    w, h = info["width"], info["height"]
    bgf = _background(bg, w, h)
    state = {"prev": None}

    def fn(frame, i):
        a = matte(frame, model_id)
        if threshold is not None:
            a = np.clip((a - threshold) * 8 + 0.5, 0, 1)
        if state["prev"] is not None and smooth > 0:
            a = smooth * state["prev"] + (1 - smooth) * a
        state["prev"] = a
        if feather > 0:
            a = cv2.GaussianBlur(a, (0, 0), feather)
        a3 = a[..., None]
        return (frame.astype(np.float32) * a3 + bgf(frame, i).astype(np.float32) * (1 - a3) + 0.5).astype(np.uint8)
    t0 = time.time()
    r = framepipe.process_video(src, dest, fn, start=start, duration=duration, job_key=k)
    secs = time.time() - t0
    man = classify.manifest("segment", {"kind": "vfx", "method": f"background replacement ({model_id})", "model": model_id,
                                        "params": params}, dest, inputs=[contracts.artifact(src, role="source", hash_it=False)],
                            compute={"device": "cpu", "seconds": round(secs, 2), "frames": r["frames"]}, seconds=secs,
                            path_check=False)
    if out is None:
        mc.put(k, dest, {"op": "replace_background", "manifest": man})
    return {"path": str(dest), "cached": False, "manifest": man, "params": params, "seconds": round(secs, 2),
            "frames": r["frames"], "s_per_frame": round(secs / max(1, r["frames"]), 4)}


def cutout(src: str | Path, out: str | Path, *, model_id: str = "mediapipe-selfie", feather: float = 1.5, smooth: float = 0.5,
           start: float = 0.0, duration: float | None = None) -> dict:
    """Subject with alpha (RGBA, PNG-in-MOV) - composited by the renderer as an overlay over any background."""
    import cv2
    state = {"prev": None}

    def fn(frame, i):
        a = matte(frame, model_id)
        if state["prev"] is not None and smooth > 0:
            a = smooth * state["prev"] + (1 - smooth) * a
        state["prev"] = a
        a = cv2.GaussianBlur(a, (0, 0), feather) if feather > 0 else a
        return np.dstack([frame, (a * 255 + 0.5).astype(np.uint8)])
    info = framepipe.video_info(src)["video"]
    return framepipe.process_video(src, out, fn, out_size=(info["width"], info["height"]), alpha=True, start=start,
                                   duration=duration, job_key=f"cutout:{model_id}:{feather}:{smooth}")
