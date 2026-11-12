"""Source understanding: shots, speech, silence, repetition, quality, highlights.

PySceneDetect and OpenCV are optional: without them, scene detection uses FFmpeg's scene score and visual quality is measured
on FFmpeg-sampled grey frames with NumPy. The richer, searchable layer (shots, topics, moments, ...) is media/understand.py.
"""
from __future__ import annotations

import difflib
import math
import re
import subprocess
from collections import Counter
from pathlib import Path

from .. import costguard
from ..speech import asr
from . import ff

STOP = set("the a an and or but of to in on for with is are was were be been it this that as at by from we you they i he she "
           "have has had not so if then than there here our your their its can will just about into over more most very".split())


def detect_scenes_ffmpeg(path: str | Path, threshold: float = 0.3, min_len: float = 0.5) -> list[tuple[float, float]]:
    """Shot boundaries with FFmpeg only (the `scene` score of the select filter on a downscaled copy).
    Works without PySceneDetect/OpenCV. `threshold` is FFmpeg's 0..1 scene-change score."""
    dur = ff.duration(path)
    err = ff.run(["-i", path, "-an", "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                 op="detect-scenes")
    cuts: list[float] = []
    for t in sorted(float(x) for x in re.findall(r"pts_time:([\d.]+)", err)):
        if t - (cuts[-1] if cuts else 0.0) >= min_len and dur - t >= min_len:
            cuts.append(round(t, 3))
    bounds = [0.0, *cuts, round(dur, 3)]
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def detect_scenes(path: str | Path, threshold: float = 27.0) -> list[tuple[float, float]]:
    """PySceneDetect when it is installed, otherwise the FFmpeg-only detector (same output shape)."""
    try:
        import scenedetect  # noqa: F401  (optional dependency)
    except ImportError:
        return detect_scenes_ffmpeg(path)
    costguard.authorize("pyscenedetect", "detect")
    from scenedetect import ContentDetector, detect
    scenes = detect(str(path), ContentDetector(threshold=threshold))
    if not scenes:
        return [(0.0, ff.duration(path))]
    return [(round(s.seconds, 3), round(e.seconds, 3)) for s, e in scenes]


# ---------------------------------------------------------------- FFmpeg + NumPy frame measurements (no OpenCV needed)
QUALITY_SIZE = (256, 144)
SOFT_SHARPNESS = 60.0   # Laplacian variance at QUALITY_SIZE below which a picture reads as soft (calibrated on lavfi sources)


def sample_frames(path: str | Path, max_frames: int = 480, size: tuple[int, int] = QUALITY_SIZE, max_fps: float = 4.0,
                  duration: float | None = None):
    """Grey frames sampled evenly with one FFmpeg call. Returns (times, uint8 array [n, h, w])."""
    import numpy as np
    dur = duration if duration is not None else ff.duration(path)
    rate = max(0.05, min(max_fps, max_frames / max(dur, 0.1)))
    W, H = size
    costguard.authorize("ffmpeg", "sample-frames")
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-an", "-vf",
                        f"fps={rate:.5f},scale={W}:{H},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True, timeout=3600)
    if p.returncode != 0:
        raise ff.FFError(f"frame sampling failed: {p.stderr[-500:]!r}")
    n = len(p.stdout) // (W * H)
    frames = np.frombuffer(p.stdout[:n * W * H], dtype=np.uint8).reshape(n, H, W)
    return [round(k / rate, 3) for k in range(n)], frames


def laplacian_var(g) -> float:
    """Variance of the 4-neighbour Laplacian (higher = sharper); the same measure as cv2.Laplacian(...).var()."""
    import numpy as np
    f = g.astype(np.float32)
    lap = -4 * f[1:-1, 1:-1] + f[:-2, 1:-1] + f[2:, 1:-1] + f[1:-1, :-2] + f[1:-1, 2:]
    return float(lap.var())


def frame_shift(a, b) -> tuple[float, float]:
    """Global translation between two grey frames by phase correlation (pixels at the sampled size)."""
    import numpy as np
    fa = np.fft.fft2(a.astype(np.float32) - float(a.mean()))
    fb = np.fft.fft2(b.astype(np.float32) - float(b.mean()))
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    c = np.abs(np.fft.ifft2(r))
    y, x = np.unravel_index(int(np.argmax(c)), c.shape)
    h, w = a.shape
    return float(x - w if x > w // 2 else x), float(y - h if y > h // 2 else y)


def frame_metrics(frames) -> dict:
    """Per sampled frame: sharpness, brightness (0-255), change from the previous frame (0..1) and global shift (px)."""
    import numpy as np
    n = len(frames)
    return {"sharpness": [laplacian_var(f) for f in frames],
            "brightness": [float(f.mean()) for f in frames],
            "diff": [0.0] + [float(np.abs(frames[k].astype(np.int16) - frames[k - 1]).mean() / 255.0) for k in range(1, n)],
            "shift": [(0.0, 0.0)] + [frame_shift(frames[k - 1], frames[k]) for k in range(1, n)]}


def visual_quality_ffmpeg(path: str | Path, samples: int = 8) -> dict:
    """visual_quality() without OpenCV: FFmpeg-sampled frames measured with NumPy. No face detection."""
    _, frames = sample_frames(path, max_frames=samples)
    sharp = [laplacian_var(f) for f in frames]
    bright = [float(f.mean()) for f in frames]
    issues = []
    if sharp and sum(sharp) / len(sharp) < SOFT_SHARPNESS:
        issues.append("soft/blurred picture")
    if bright and sum(bright) / len(bright) < 45:
        issues.append("underexposed")
    if bright and sum(bright) / len(bright) > 215:
        issues.append("overexposed")
    return {"sharpness": round(sum(sharp) / max(len(sharp), 1), 1), "brightness": round(sum(bright) / max(len(bright), 1), 1),
            "face_focus_x": None, "faces_seen": 0, "face_detection": False, "issues": issues,
            "method": f"ffmpeg+numpy Laplacian variance at {QUALITY_SIZE[0]}x{QUALITY_SIZE[1]} grey (OpenCV not installed)"}


def visual_quality(path: str | Path, samples: int = 8) -> dict:
    """Sharpness (Laplacian variance), brightness and a face-based framing focus from sampled frames.
    Falls back to visual_quality_ffmpeg() when OpenCV is not installed."""
    try:
        import cv2
    except ImportError:
        return visual_quality_ffmpeg(path, samples)
    costguard.authorize("pyscenedetect", "opencv-quality")
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    # Haar cascades were dropped from newer OpenCV builds; face-based framing is then simply unavailable.
    face = (cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if hasattr(cv2, "CascadeClassifier") else None)
    sharp, bright, fx = [], [], []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (i + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp.append(float(cv2.Laplacian(g, cv2.CV_64F).var()))
        bright.append(float(g.mean()))
        for (x, y, w, h) in (face.detectMultiScale(g, 1.2, 5, minSize=(g.shape[1] // 12, g.shape[1] // 12)) if face else []):
            fx.append((x + w / 2) / g.shape[1])
    cap.release()
    issues = []
    if sharp and sum(sharp) / len(sharp) < 40:
        issues.append("soft/blurred picture")
    if bright and sum(bright) / len(bright) < 45:
        issues.append("underexposed")
    if bright and sum(bright) / len(bright) > 215:
        issues.append("overexposed")
    return {"sharpness": round(sum(sharp) / max(len(sharp), 1), 1), "brightness": round(sum(bright) / max(len(bright), 1), 1),
            "face_focus_x": round(sorted(fx)[len(fx) // 2], 3) if fx else None, "faces_seen": len(fx), "face_detection": face is not None, "issues": issues}


def find_repetitions(segments: list[dict], threshold: float = 0.82) -> list[dict]:
    """Retakes: a later sentence that says nearly the same thing as an earlier one."""
    out = []
    norm = [re.sub(r"[^\w\s]", "", s["text"].lower()) for s in segments]
    for i in range(len(segments)):
        for j in range(i + 1, min(i + 6, len(segments))):
            if len(norm[i]) > 15 and difflib.SequenceMatcher(None, norm[i], norm[j]).ratio() >= threshold:
                out.append({"earlier": i, "later": j, "text": segments[i]["text"]})
    return out


def score_segments(segments: list[dict]) -> list[float]:
    """Heuristic importance: distinctive vocabulary, numbers, questions, confident decoding."""
    docs = [[w for w in re.findall(r"\w+", s["text"].lower()) if w not in STOP and len(w) > 2] for s in segments]
    df = Counter(w for d in docs for w in set(d))
    tf_all = Counter(w for d in docs for w in d)
    scores = []
    for s, d in zip(segments, docs):
        if not d:
            scores.append(0.0)
            continue
        # words that recur across the video are its topic; reward segments that carry them
        topical = sum(math.log1p(tf_all[w]) for w in d) / len(d)
        distinct = sum(1.0 / df[w] for w in set(d)) / len(set(d))
        bonus = 0.25 * bool(re.search(r"\d", s["text"])) + 0.2 * s["text"].strip().endswith("?")
        conf = max(0.0, 1.0 + s.get("avg_logprob", -0.3))
        scores.append(round((topical + distinct + bonus) * conf, 3))
    return scores


def analyze(path: str | Path, language: str | None = None, transcribe: bool = True, understand: bool = True) -> dict:
    info = ff.probe(path)
    out: dict = {"probe": info, "path": str(path), "duration": info["duration"], "scenes": [], "silences": [],
                 "transcript": None, "loudness": None, "black": [], "freeze": [], "quality": None, "repetitions": [], "issues": [],
                 "not_available": ["speaker diarisation (no diarisation model deployed yet)",
                                   "object/action recognition (no vision model deployed yet)"]}
    if info["video"]:
        out["video"] = {"w": info["video"]["width"], "h": info["video"]["height"], "fps": info["video"]["fps"]}
        out["scenes"] = detect_scenes(path)
        out["black"] = ff.detect_black(path)
        out["freeze"] = ff.detect_freeze(path)
        out["quality"] = visual_quality(path)
        out["issues"] += out["quality"]["issues"]
    if info["audio"]:
        out["silences"] = ff.detect_silence(path)
        out["loudness"] = ff.loudness(path)
        if out["loudness"]["lufs"] < -24:
            out["issues"].append(f"quiet audio ({out['loudness']['lufs']} LUFS)")
        if out["loudness"]["true_peak"] > -0.3:
            out["issues"].append("audio peaks near clipping")
        if transcribe:
            t = asr.transcribe(path, language)
            scores = score_segments(t["segments"])
            for s, sc in zip(t["segments"], scores):
                s["score"] = sc
            out["transcript"] = t
            out["repetitions"] = find_repetitions(t["segments"])
        else:
            out["not_available"].append("transcript (transcription was not requested)")
    else:
        out["issues"].append("no audio track")
    if out["black"]:
        out["issues"].append(f"{len(out['black'])} black section(s)")
    if out["freeze"]:
        out["issues"].append(f"{len(out['freeze'])} frozen section(s)")
    if understand:
        # Additive layer (shots, topics, speakers, moments, motion, ...). It never fails the base analysis.
        try:
            from . import understand as und
            und.enrich(out, path)
        except Exception as e:  # noqa: BLE001 - best effort; the base analysis stands
            out["not_available"].append(f"media understanding layer failed: {type(e).__name__}: {e}"[:200])
    return out
