"""Face detection + tracking for smart reframing (YuNet via OpenCV, MIT licence; OpenCV Apache-2.0).

    detect(path, fps=2) -> analysis["faces"] = {"available", "method", "licence", "tracks":[{"id","samples":[...]}],
                                                "focus":[{"start","end","x","y"}], "samples": n}
    focus_at(analysis, t0, t1) -> (x, y) | None     normalised subject centre for a source time range

The main subject is the largest confident face per sampled frame (a presenter/guest in an interview); its centre path is
median-filtered and split into spans wherever it moves by more than `move` (normalised), so a crop follows a subject
that changes position without jittering. No identity recognition is done.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from .. import config, costguard

MODEL_URL = "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"
LICENCE = "YuNet face_detection_yunet_2023mar.onnx: MIT (opencv_zoo); OpenCV: Apache-2.0"


def model_path() -> Path:
    return config.MODELS / "faces" / "face_detection_yunet_2023mar.onnx"


def ensure_model() -> Path | None:
    p = model_path()
    if p.exists() and p.stat().st_size > 100_000:
        return p
    try:
        from .. import net
        costguard.authorize("yunet-face", "download")
        net.download(MODEL_URL, p, timeout=60, retries=2)
        return p
    except Exception:  # noqa: BLE001 - offline: faces are reported as unavailable
        return None


def _frames(path: str | Path, fps: float, width: int = 640):
    from . import ff
    info = ff.probe(path)
    v = info["video"]
    h = int(round(v["height"] * width / v["width"] / 2)) * 2
    costguard.authorize("ffmpeg", "face-frames")
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-an", "-vf",
                        f"fps={fps},scale={width}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True, timeout=7200)
    n = len(p.stdout) // (width * h * 3)
    return width, h, np.frombuffer(p.stdout[: n * width * h * 3], dtype=np.uint8).reshape(n, h, width, 3)


def smooth_path(samples: list[dict], k: int = 5) -> list[dict]:
    """Median filter over the subject centre (removes detector jitter and single-frame misses)."""
    if not samples:
        return []
    xs = np.array([s["x"] for s in samples], dtype=float)
    ys = np.array([s["y"] for s in samples], dtype=float)
    half = k // 2
    out = []
    for i, s in enumerate(samples):
        lo, hi = max(0, i - half), min(len(samples), i + half + 1)
        out.append({**s, "x": float(np.median(xs[lo:hi])), "y": float(np.median(ys[lo:hi]))})
    return out


def focus_spans(samples: list[dict], move: float = 0.06, min_len: float = 1.0, end: float | None = None) -> list[dict]:
    """Split the smoothed path into spans with a stable centre; a span ends where the subject moves by > `move`."""
    if not samples:
        return []
    spans, cur = [], [samples[0]]
    for s in samples[1:]:
        cx = float(np.mean([c["x"] for c in cur]))
        cy = float(np.mean([c["y"] for c in cur]))
        if (abs(s["x"] - cx) > move or abs(s["y"] - cy) > move) and cur[-1]["t"] - cur[0]["t"] >= min_len:
            spans.append(cur)
            cur = [s]
        else:
            cur.append(s)
    spans.append(cur)
    out = []
    for i, sp in enumerate(spans):
        st = 0.0 if i == 0 else round((spans[i - 1][-1]["t"] + sp[0]["t"]) / 2, 3)
        en = round((sp[-1]["t"] + spans[i + 1][0]["t"]) / 2, 3) if i + 1 < len(spans) else round(end if end else sp[-1]["t"] + 0.5, 3)
        out.append({"start": st, "end": en, "x": round(float(np.mean([s["x"] for s in sp])), 4),
                    "y": round(float(np.mean([s["y"] for s in sp])), 4)})
    return out


def detect(path: str | Path, fps: float = 2.0, min_score: float = 0.7) -> dict:
    import cv2
    m = ensure_model()
    if m is None:
        return {"available": False, "why": "YuNet face model not available (offline?)"}
    from . import ff
    dur = ff.duration(path)
    W, H, frames = _frames(path, fps)
    det = cv2.FaceDetectorYN.create(str(m), "", (W, H), min_score, 0.3, 5000)
    samples, seen = [], 0
    for i, fr in enumerate(frames):
        _, faces = det.detect(fr)
        if faces is None or not len(faces):
            continue
        seen += 1
        f = max(faces, key=lambda r: r[2] * r[3])  # largest face = main subject
        x, y, w, h, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
        samples.append({"t": round(i / fps, 3), "x": (x + w / 2) / W, "y": (y + h / 2) / H, "w": w / W, "h": h / H,
                        "conf": round(score, 3)})
    sm = smooth_path(samples)
    return {"available": True, "method": f"YuNet (OpenCV {cv2.__version__}) at {fps} fps, largest face per frame, median-smoothed",
            "licence": LICENCE, "samples": len(frames), "frames_with_face": seen,
            "coverage": round(seen / max(1, len(frames)), 3),
            "tracks": [{"id": "subject", "samples": [{k: round(v, 4) if isinstance(v, float) else v for k, v in s.items()} for s in sm]}],
            "focus": focus_spans(sm, end=dur)}


def focus_at(analysis: dict, t0: float, t1: float) -> tuple[float, float] | None:
    """Time-weighted subject centre over [t0, t1] (source seconds), or None without face data."""
    f = (analysis or {}).get("faces") or {}
    spans = f.get("focus") or []
    if not f.get("available") or not spans:
        return None
    acc, wsum = np.zeros(2), 0.0
    for s in spans:
        ov = min(t1, s["end"]) - max(t0, s["start"])
        if ov > 0:
            acc += ov * np.array([s["x"], s["y"]])
            wsum += ov
    return (round(float(acc[0] / wsum), 4), round(float(acc[1] / wsum), 4)) if wsum else None
