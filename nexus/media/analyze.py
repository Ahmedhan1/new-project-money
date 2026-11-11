"""Source understanding: shots, speech, silence, repetition, quality, highlights."""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from pathlib import Path

from .. import costguard
from ..speech import asr
from . import ff

STOP = set("the a an and or but of to in on for with is are was were be been it this that as at by from we you they i he she "
           "have has had not so if then than there here our your their its can will just about into over more most very".split())


def detect_scenes(path: str | Path, threshold: float = 27.0) -> list[tuple[float, float]]:
    costguard.authorize("pyscenedetect", "detect")
    from scenedetect import ContentDetector, detect
    scenes = detect(str(path), ContentDetector(threshold=threshold))
    if not scenes:
        return [(0.0, ff.duration(path))]
    return [(round(s.seconds, 3), round(e.seconds, 3)) for s, e in scenes]


def visual_quality(path: str | Path, samples: int = 8) -> dict:
    """Sharpness (Laplacian variance), brightness and a face-based framing focus from sampled frames."""
    costguard.authorize("pyscenedetect", "opencv-quality")
    import cv2
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


def analyze(path: str | Path, language: str | None = None, transcribe: bool = True) -> dict:
    info = ff.probe(path)
    out: dict = {"probe": info, "scenes": [], "silences": [], "transcript": None, "loudness": None,
                 "black": [], "freeze": [], "quality": None, "repetitions": [], "issues": [],
                 "not_available": ["speaker diarisation (no diarisation model deployed yet)",
                                   "object/action recognition (no vision model deployed yet)"]}
    if info["video"]:
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
        out["issues"].append("no audio track")
    if out["black"]:
        out["issues"].append(f"{len(out['black'])} black section(s)")
    if out["freeze"]:
        out["issues"].append(f"{len(out['freeze'])} frozen section(s)")
    return out
