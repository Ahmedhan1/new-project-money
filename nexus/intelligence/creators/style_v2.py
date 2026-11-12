"""Creator reference intelligence V2: 14 measurable style dimensions per lawfully held reference, and a weighted blend
of several references into one abstract target style.

Every dimension is a number with a unit and the method that produced it; nothing is inferred from a creator's
reputation. Frame-based dimensions (on-screen text, colour, motion) are measured from the file with ffmpeg + numpy +
Tesseract; when a tool or the file is missing the dimension is `null` with the reason - never guessed.

  1 hook_speech_onset_s     seconds until the first spoken word          (transcript)
  2 hook_cuts_first_5s      cuts in the first 5 s                        (scene detection)
  3 cuts_per_min            edit rate                                    (scene detection)
  4 shot_rhythm_cv          shot-length coefficient of variation         (scene detection) - 0 = metronomic
  5 escalation              slope of cuts/min over the 10 pacing slices, per slice (positive = speeds up)
  6 wpm_speaking            words per minute while speaking              (transcript)
  7 speech_share            share of runtime with speech                 (transcript)
  8 question_rate_per_min   questions asked per minute                   (transcript)
  9 fact_density_per_100w   numbers per 100 spoken words                 (transcript)
 10 stakes_rate_per_min     stakes/emotion words per minute              (transcript, fixed word list)
 11 loudness_range_lu       EBU R128 loudness range                      (ffmpeg ebur128)
 12 non_speech_audio_share  upper bound of music/effects share           (audio - speech - silence)
 13 on_screen_text_share    share of sampled frames with readable text   (Tesseract on 1 frame / 2 s)
 14 colour                  mean saturation and brightness 0-1           (frames)
 (+) motion_energy          mean absolute frame difference 0-1           (frames) - reported, folded into colour/visual

`combine(profiles, weights)` blends references per dimension (weighted mean), reports the spread and an agreement
score, and marks a dimension `conflict` when references disagree strongly - a blend of a 2-cut/min documentary and a
40-cut/min challenge video is not a style, and the output says so instead of averaging it away."""
from __future__ import annotations

import math
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path

DIMENSIONS = {
    "hook_speech_onset_s": ("s", "measured.speech.time_to_first_speech_s"),
    "hook_cuts_first_5s": ("cuts", "measured.opening.first_5s.cuts"),
    "cuts_per_min": ("1/min", "measured.cuts_per_min"),
    "shot_rhythm_cv": ("ratio", None),
    "escalation": ("cuts/min per slice", None),
    "wpm_speaking": ("words/min", "measured.speech.wpm_speaking"),
    "speech_share": ("ratio", "measured.speech.speech_share"),
    "question_rate_per_min": ("1/min", None),
    "fact_density_per_100w": ("per 100 words", None),
    "stakes_rate_per_min": ("1/min", None),
    "loudness_range_lu": ("LU", "measured.audio.lra"),
    "non_speech_audio_share": ("ratio", "measured.audio.non_speech_audio_share_upper_bound"),
    "on_screen_text_share": ("ratio", None),
    "colour": ("saturation,brightness 0-1", None),
}
# scale used to normalise a dimension when comparing / judging agreement (typical spread across formats)
SCALE = {"hook_speech_onset_s": 5, "hook_cuts_first_5s": 3, "cuts_per_min": 20, "shot_rhythm_cv": 1, "escalation": 2,
         "wpm_speaking": 80, "speech_share": 0.5, "question_rate_per_min": 2, "fact_density_per_100w": 3, "stakes_rate_per_min": 2,
         "loudness_range_lu": 10, "non_speech_audio_share": 0.5, "on_screen_text_share": 0.5, "colour": 0.4}
CONFLICT_SPREAD = 1.0  # normalised range above which references are not one style


def _get(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _r(x, n=3):
    return None if x is None else round(float(x), n)


def _frames(path: str | Path, every_s: float = 2.0, width: int = 320, limit: int = 90) -> list:
    """Sampled RGB frames as numpy arrays (empty list when ffmpeg/numpy are missing)."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return []
    if not shutil.which("ffmpeg") or not Path(path).exists():
        return []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps=1/{every_s},scale={width}:-2", "-frames:v", str(limit),
                        f"{td}/f%04d.png"], check=False, timeout=600)
        return [np.asarray(Image.open(p).convert("RGB"), dtype="float32") / 255.0 for p in sorted(Path(td).glob("f*.png"))]


def _ocr_words(frame) -> int:
    """Readable words in a frame (Tesseract confidence >= 60, length >= 3)."""
    from PIL import Image
    import numpy as np
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "f.png"
        img = Image.fromarray((frame * 255).astype("uint8"))
        img = img.resize((img.width * 2, img.height * 2))
        img.save(f)
        out = subprocess.run(["tesseract", str(f), "-", "--psm", "11", "tsv"], capture_output=True, text=True, timeout=60).stdout
    n = 0
    for line in out.splitlines()[1:]:
        c = line.split("\t")
        if len(c) >= 12 and c[11].strip() and len(c[11].strip()) >= 3 and float(c[10] or -1) >= 60 and any(ch.isalpha() for ch in c[11]):
            n += 1
    return n


def frame_dimensions(path: str | Path) -> dict:
    frames = _frames(path)
    if not frames:
        return {"on_screen_text_share": None, "colour": None, "motion_energy": None,
                "frame_note": "file or ffmpeg/numpy unavailable - frame dimensions not measured"}
    import numpy as np
    sat, bri = [], []
    for f in frames:
        mx, mn = f.max(axis=2), f.min(axis=2)
        sat.append(float(np.mean(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0))))
        bri.append(float(np.mean(mx)))
    motion = [float(np.mean(np.abs(a - b))) for a, b in zip(frames, frames[1:]) if a.shape == b.shape]
    text = None
    if shutil.which("tesseract"):
        text = sum(1 for f in frames if _ocr_words(f) >= 2) / len(frames)
    return {"on_screen_text_share": _r(text), "colour": {"saturation": _r(statistics.fmean(sat)), "brightness": _r(statistics.fmean(bri))},
            "motion_energy": _r(statistics.fmean(motion)) if motion else None, "frames_sampled": len(frames),
            "frame_note": "1 frame / 2 s at 320 px" + ("" if text is not None else "; Tesseract missing - text share not measured")}


def dimensions(profile: dict, media_path: str | Path | None = None) -> dict:
    """The 14 dimensions for one CreatorReferenceProfile (V1 profile + optional frame analysis of the same file)."""
    m = profile.get("measured") or {}
    out: dict[str, dict] = {}
    for k, (unit, path) in DIMENSIONS.items():
        if path:
            out[k] = {"value": _r(_get(profile, path)), "unit": unit, "method": path}
    curve = [s.get("cuts_per_min", 0) for s in (_get(profile, "story.pacing_curve") or m.get("pacing_curve") or [])]
    shots = m.get("shot_lengths") or []
    if not shots and m.get("mean_shot_s") and m.get("shots"):
        # V1 profiles keep summary stats; CV from (longest - shortest) / mean is a documented rough proxy
        cv = (m["longest_shot_s"] - m["shortest_shot_s"]) / (4 * m["mean_shot_s"]) if m.get("mean_shot_s") else None
        out["shot_rhythm_cv"] = {"value": _r(cv), "unit": "ratio", "method": "range/4/mean (proxy: per-shot lengths not stored)"}
    else:
        cv = statistics.pstdev(shots) / statistics.fmean(shots) if len(shots) > 1 else None
        out["shot_rhythm_cv"] = {"value": _r(cv), "unit": "ratio", "method": "pstdev/mean of shot lengths"}
    if len(curve) >= 3:
        xs = range(len(curve))
        mx, my = statistics.fmean(xs), statistics.fmean(curve)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, curve)) / sum((x - mx) ** 2 for x in xs)
        out["escalation"] = {"value": _r(slope), "unit": "cuts/min per slice", "method": "least-squares slope of pacing curve"}
    else:
        out["escalation"] = {"value": None, "unit": "cuts/min per slice", "method": "pacing curve missing"}
    sp = m.get("speech") or {}
    mins = (profile.get("duration_s") or m.get("duration_s") or 0) / 60.0
    words = sp.get("words") or 0
    has_speech = bool(sp.get("available")) and words > 0
    out["question_rate_per_min"] = {"value": _r(sp.get("questions", 0) / mins) if has_speech and mins else None, "unit": "1/min",
                                    "method": "'?' and question words in the transcript"}
    out["fact_density_per_100w"] = {"value": _r(100 * sp.get("numbers", 0) / words) if has_speech else None, "unit": "per 100 words",
                                    "method": "number tokens in the transcript"}
    out["stakes_rate_per_min"] = {"value": _r(sp.get("stakes_words", 0) / mins) if has_speech and mins else None, "unit": "1/min",
                                  "method": "fixed stakes word list (measure.STAKES_WORDS)"}
    fd = frame_dimensions(media_path) if media_path else {"on_screen_text_share": None, "colour": None, "motion_energy": None,
                                                          "frame_note": "no media file given"}
    out["on_screen_text_share"] = {"value": fd["on_screen_text_share"], "unit": "ratio", "method": "Tesseract on sampled frames"}
    out["colour"] = {"value": fd["colour"], "unit": "saturation,brightness 0-1", "method": "HSV-style mean over sampled frames"}
    out["motion_energy"] = {"value": fd["motion_energy"], "unit": "0-1", "method": "mean abs difference of consecutive sampled frames"}
    measured = sum(1 for k in DIMENSIONS if out[k]["value"] is not None)
    return {"reference": profile.get("id"), "label": f"{profile.get('creator')} / {profile.get('format')}", "dimensions": out,
            "coverage": f"{measured}/{len(DIMENSIONS)}", "frame_note": fd.get("frame_note")}


def _scalar(v):
    if isinstance(v, dict):  # colour -> one number for agreement (saturation dominates the look)
        return v.get("saturation")
    return v


def distance(a: dict, b: dict) -> dict:
    """Normalised per-dimension distance between two dimension sets (0 = same, 1 = one typical spread apart)."""
    per = {}
    for k in DIMENSIONS:
        x, y = _scalar(a["dimensions"][k]["value"]), _scalar(b["dimensions"][k]["value"])
        if x is not None and y is not None:
            per[k] = round(abs(x - y) / SCALE[k], 3)
    return {"per_dimension": per, "mean": round(statistics.fmean(per.values()), 3) if per else None, "compared": len(per)}


def combine(dimsets: list[dict], weights: list[float] | None = None, name: str = "blended reference style") -> dict:
    """Weighted blend of several references. Per dimension: weighted mean, min/max, agreement (1 - normalised range),
    and `conflict` when the references are more than one typical spread apart."""
    if not dimsets:
        raise ValueError("no references to combine")
    w = weights or [1.0] * len(dimsets)
    if len(w) != len(dimsets) or any(x < 0 for x in w) or not sum(w):
        raise ValueError("weights must be non-negative, one per reference, not all zero")
    out, conflicts = {}, []
    for k in DIMENSIONS:
        vals = [(_scalar(d["dimensions"][k]["value"]), wi, d["dimensions"][k]["value"]) for d, wi in zip(dimsets, w)]
        vals = [(v, wi, raw) for v, wi, raw in vals if v is not None and wi > 0]
        if not vals:
            out[k] = {"value": None, "n": 0, "note": "not measured in any weighted reference"}
            continue
        tw = sum(wi for _, wi, _ in vals)
        mean = sum(v * wi for v, wi, _ in vals) / tw
        rng = (max(v for v, _, _ in vals) - min(v for v, _, _ in vals)) / SCALE[k]
        entry = {"value": _r(mean), "min": _r(min(v for v, _, _ in vals)), "max": _r(max(v for v, _, _ in vals)), "n": len(vals),
                 "agreement": _r(max(0.0, 1 - rng)), "unit": DIMENSIONS[k][0]}
        if k == "colour":
            entry["brightness"] = _r(sum(raw["brightness"] * wi for _, wi, raw in vals) / tw)
        if len(vals) > 1 and rng > CONFLICT_SPREAD:
            entry["conflict"] = True
            conflicts.append(k)
        out[k] = entry
    measured = [e for e in out.values() if e.get("n")]
    agree = statistics.fmean(e["agreement"] for e in measured) if measured else 0.0
    coverage = len(measured) / len(DIMENSIONS)
    confidence = round(agree * coverage * (1 - math.exp(-len(dimsets) / 2)), 3)  # more references -> more confidence
    return {"schema": "nexus.BlendedStyle/1", "name": name, "references": [d["reference"] for d in dimsets],
            "weights": [round(x / sum(w), 3) for x in w], "dimensions": out, "conflicts": conflicts, "confidence": confidence,
            "verdict": ("coherent" if not conflicts else f"incoherent on {conflicts}: choose one reference's value or re-weight"),
            "production_targets": targets(out), "note": "numbers only; no footage, wording, branding or persona is carried over"}


def targets(dims: dict) -> dict:
    """What production may use: pacing/speech targets for Account 2 (create preset + script length)."""
    v = {k: (dims.get(k) or {}).get("value") for k in DIMENSIONS}
    cpm = v["cuts_per_min"]
    return {"mean_shot_s": _r(60 / cpm, 2) if cpm else None, "speech_onset_max_s": v["hook_speech_onset_s"],
            "cut_in_first_5s": bool(v["hook_cuts_first_5s"] and v["hook_cuts_first_5s"] >= 0.5), "wpm": v["wpm_speaking"],
            "facts_per_100_words": v["fact_density_per_100w"], "questions_per_min": v["question_rate_per_min"],
            "on_screen_text_share": v["on_screen_text_share"], "escalate_pacing": bool(v["escalation"] and v["escalation"] > 0.2)}
