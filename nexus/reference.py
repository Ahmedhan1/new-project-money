"""Reference-style intelligence (REF-1): a lawfully supplied reference video -> ReferenceStyleProfile -> director brief.

    analyze(path, analysis, name=None, rights=...) -> nexus.ReferenceStyleProfile/1
    to_overrides(profile) -> {"profile_overrides", "why"}    editorial parameters for director.plan(brief)
    apply(profile, analysis_by_src, brief) -> plan           a DIFFERENT source edited with the reference's structure
    compare(profile, tl) -> {metric: (reference, edit)}      how close an edit came

Structure only. What is measured: shot lengths and cut rate, the first 3 s / 10 s (cuts, first speech, on-screen text,
motion), where the hook line sits, shot-type variation (face size / zero-shot labels from media/visual.py), visual
density (frame difference + edge density), cutaway frequency (shots without the speaker's face while speech runs),
on-screen text frequency (EDGE-DENSITY HEURISTIC, not OCR: dense fine edges in a frame band), music/SFX density from the
audio (non-speech energy and onset rate), pattern-interrupt spacing, ending structure, and thumbnail-candidate frames
ranked by composition (sharpness, colourfulness, exposure, face size and rule-of-thirds placement).
What is NOT kept: no frame, transcript text, title, music or sound of the reference. Thumbnail candidates are timestamps
into the reference for the user's own review; they are never used in an output.
"""
from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import numpy as np

from .media import understand as und

RIGHTS_OK = {"owned", "licensed", "public-domain", "commentary"}
TEXT_EDGE = 0.15          # share of strong-edge pixels in a band above which the band reads as text
TEXT_RATIO = 2.0          # ... and this many times the frame's median band (calibrated: docs/results/a2_reference)
SR = 16000


class ReferenceRefused(ValueError):
    pass


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    a = np.array(sorted(xs), dtype=float)
    return {"n": int(len(a)), "mean": round(float(a.mean()), 3), "median": round(float(np.median(a)), 3),
            "p10": round(float(np.percentile(a, 10)), 3), "p90": round(float(np.percentile(a, 90)), 3),
            "std": round(float(a.std()), 3)}


def _frames(path, fps: float, size=(256, 144), max_frames: int = 900):
    from .media import ff
    dur = ff.duration(path)
    rate = max(0.05, min(fps, max_frames / max(dur, 0.1)))
    W, H = size
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-an", "-vf",
                        f"fps={rate:.5f},scale={W}:{H},format=rgb24", "-f", "rawvideo", "-"], capture_output=True, timeout=3600)
    n = len(p.stdout) // (W * H * 3)
    fr = np.frombuffer(p.stdout[:n * W * H * 3], dtype=np.uint8).reshape(n, H, W, 3)
    return [round((k + 0.5) / rate, 3) for k in range(n)], fr, dur


def _audio(path) -> np.ndarray:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR),
                        "-f", "s16le", "-"], capture_output=True, timeout=3600)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def edge_density(grey: np.ndarray, thr: float = 40.0) -> np.ndarray:
    """Share of strong-gradient pixels per horizontal band (6 bands, top to bottom)."""
    g = grey.astype(np.float32)
    gx = np.abs(g[:, 1:] - g[:, :-1])[:-1]
    gy = np.abs(g[1:] - g[:-1])[:, :-1]
    e = (np.maximum(gx, gy) > thr).astype(np.float32)
    return np.array([b.mean() for b in np.array_split(e, 6, axis=0)])


def text_bands(grey: np.ndarray) -> bool:
    """On-screen text heuristic: a band with dense fine edges AND many short horizontal runs (letters), clearly above the
    frame's own average texture. Labelled as a heuristic everywhere it is reported."""
    d = edge_density(grey)
    lone = d.max() >= TEXT_EDGE and d.max() >= TEXT_RATIO * max(1e-3, float(np.median(d)))  # a caption / lower third
    card = int((d >= TEXT_EDGE).sum()) >= 2 and float(d.min()) < 0.02                       # lines of text on a plain card
    return bool(lone or card)


def _colourfulness(rgb: np.ndarray) -> float:
    r, g, b = [rgb[..., k].astype(np.float32) for k in range(3)]
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def colour_profile(fr: np.ndarray) -> dict:
    """Colour characteristics of sampled RGB frames: luma level and contrast, HSV saturation, Hasler-Suesstrunk
    colourfulness, warm/cool balance (R-B), share of near-monochrome frames."""
    if not len(fr):
        return {"n": 0}
    f = fr.astype(np.float32) / 255.0
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    mx, mn = f.max(-1), f.min(-1)
    sat = np.where(mx > 1e-3, (mx - mn) / np.maximum(mx, 1e-3), 0.0)
    per_sat = sat.reshape(len(f), -1).mean(1)
    return {"n": int(len(f)), "luma_mean": round(float(luma.mean()), 3), "luma_contrast": round(float(luma.reshape(len(f), -1).std(1).mean()), 3),
            "saturation": round(float(per_sat.mean()), 3), "colourfulness": round(float(np.mean([_colourfulness(x) for x in fr])), 1),
            "warmth": round(float((r - b).mean()), 3), "monochrome_share": round(float(np.mean(per_sat < 0.06)), 3)}


def transition_profile(path, cuts: list[float], max_cuts: int = 40, fps: float = 12.0) -> dict:
    """How shots change: around each detected cut, the frame difference at `fps` over +-0.6 s. A hard cut puts most of
    the change into one frame step; a dissolve/wipe spreads it (share of the largest step < 0.5); a fade passes
    through a dark frame. Returns counts and shares."""
    W, H = 96, 54
    kinds = {"hard_cut": 0, "gradual": 0, "fade_through_black": 0}
    sample = cuts if len(cuts) <= max_cuts else [cuts[int(k * len(cuts) / max_cuts)] for k in range(max_cuts)]
    for c in sample:
        a = max(0.0, c - 0.6)
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{a:.3f}", "-i", str(path), "-t", "1.2", "-an",
                            "-vf", f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=120)
        n = len(p.stdout) // (W * H)
        if n < 4:
            continue
        g = np.frombuffer(p.stdout[: n * W * H], dtype=np.uint8).reshape(n, H, W).astype(np.float32)
        d = np.abs(np.diff(g, axis=0)).mean((1, 2))
        if g.mean((1, 2)).min() < 12 and g.mean((1, 2)).max() > 30:
            kinds["fade_through_black"] += 1
        elif d.sum() > 0 and d.max() / d.sum() >= 0.5:
            kinds["hard_cut"] += 1
        else:
            kinds["gradual"] += 1
    tot = sum(kinds.values())
    return {"measured_cuts": tot, **kinds, **{f"{k}_share": round(v / tot, 3) for k, v in kinds.items() if tot}}


def _lap(grey: np.ndarray) -> float:
    f = grey.astype(np.float32)
    return float((-4 * f[1:-1, 1:-1] + f[:-2, 1:-1] + f[2:, 1:-1] + f[1:-1, :-2] + f[1:-1, 2:]).var())


def _face_at(analysis: dict, t: float) -> dict | None:
    f = (analysis or {}).get("faces") or {}
    if not f.get("available"):
        return None
    pts = (f.get("tracks") or [{}])[0].get("samples") or []
    near = [s for s in pts if abs(s["t"] - t) <= 0.6]
    return min(near, key=lambda s: abs(s["t"] - t)) if near else None


def _speech_spans(analysis: dict) -> list[tuple[float, float]]:
    ws = sorted((w["start"], w["end"]) for s in und.sentences(analysis) for w in s["words"])
    out: list[list[float]] = []
    for a, b in ws:
        if out and a - out[-1][1] <= 0.35:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _inside(t: float, spans) -> bool:
    return any(a <= t <= b for a, b in spans)


# ---------------------------------------------------------------- analysis
def analyze(path, analysis: dict, name: str | None = None, rights: dict | None = None, fps: float = 2.0) -> dict:
    """ReferenceStyleProfile of a reference the user may lawfully analyse (`rights.basis` in owned/licensed/public-domain/
    commentary; anything else is refused). `analysis` = media.analyze output for the same file (scenes, transcript,
    faces, visual)."""
    rights = dict(rights or {})
    if rights.get("basis") not in RIGHTS_OK:
        raise ReferenceRefused(f"reference rights basis {rights.get('basis')!r} is not one of {sorted(RIGHTS_OK)}: "
                               "only a lawfully supplied reference can be analysed")
    times, fr, dur = _frames(path, fps)
    grey = fr.astype(np.float32).mean(-1) if len(fr) else np.zeros((0, 1, 1))
    scenes = [(float(a), float(b)) for a, b in (analysis.get("scenes") or []) if b > a] or [(0.0, dur)]
    shot_lens = [b - a for a, b in scenes]
    cuts = [b for _, b in scenes[:-1]]
    mins = max(dur / 60.0, 1e-6)
    spans = _speech_spans(analysis)
    sents = und.sentences(analysis)
    und.score_sentences(sents)
    # ---- per-frame signals
    diff = [0.0] + [float(np.abs(grey[k] - grey[k - 1]).mean() / 255.0) for k in range(1, len(grey))]
    text = [text_bands(g) for g in grey]
    vis_text = [(r["start"], r["end"]) for r in (analysis.get("visual") or {}).get("shots") or []
                if r.get("scene") and r["scene"][0]["label"] in ("text or a title card", "a chart or diagram")]
    if vis_text:  # the visual model saw a title card / chart in these shots: count their frames as text too
        text = [x or any(a <= t < b for a, b in vis_text) for x, t in zip(text, times)]
    edges = [float(edge_density(g).mean()) for g in grey]
    # ---- shot types / variety from the visual layer (or faces)
    vis = analysis.get("visual") or {}
    vrows = vis.get("shots") or []
    types = [r.get("shot_type") for r in vrows if r.get("shot_type")]
    labels = [r["scene"][0]["label"] for r in vrows if r.get("scene")]
    type_share = {t: round(types.count(t) / len(types), 3) for t in sorted(set(types))} if types else {}
    changes = sum(1 for a, b in zip(types, types[1:]) if a != b)
    # cutaways: shots during speech where the face track sees no face (someone else / something else is on screen)
    faces_on = (analysis.get("faces") or {}).get("available")
    cutaway_shots = 0
    if faces_on:
        for a, b in scenes:
            mid = (a + b) / 2
            if _inside(mid, spans) and _face_at(analysis, mid) is None:
                cutaway_shots += 1
    # ---- audio: non-speech energy (music/SFX bed) and onset rate (accents)
    x = _audio(path) if (analysis.get("probe") or {}).get("audio", True) else np.zeros(0, np.float32)
    hop = SR // 10
    n = len(x) // hop
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(1) + 1e-12) if n else np.zeros(0)
    t_rms = (np.arange(n) + 0.5) * hop / SR
    in_speech = np.array([_inside(t, spans) for t in t_rms]) if n else np.zeros(0, bool)
    db = 20 * np.log10(rms + 1e-9)
    non_speech = db[~in_speech] if n else np.zeros(0)
    bed = float(np.mean(non_speech > -45)) if len(non_speech) else 0.0  # share of non-speech time with audible sound
    flux = np.maximum(0, np.diff(db)) if n > 1 else np.zeros(0)
    onsets = int(np.sum((flux > 9.0) & ~in_speech[1:])) if n > 1 else 0  # sharp rises outside speech ~ SFX/music hits
    # ---- hook placement: first sentence with a strong hook score, and the first speech
    first_speech = spans[0][0] if spans else None
    hook = next((s for s in sents if s.get("hook", 0) >= 0.5 and s["start"] < min(30.0, dur / 3)), None)

    def window(t1):
        idx = [k for k, t in enumerate(times) if t < t1]
        return {"cuts": sum(1 for c in cuts if c < t1), "speech_s": round(sum(max(0.0, min(b, t1) - a) for a, b in spans if a < t1), 2),
                "text_frames_share": round(float(np.mean([text[k] for k in idx])) if idx else 0.0, 3),
                "motion": round(float(np.mean([diff[k] for k in idx])) if idx else 0.0, 4),
                "first_speech_s": round(first_speech, 2) if first_speech is not None and first_speech < t1 else None}
    # ---- pattern interrupts: cuts + motion spikes (a visual change of any kind)
    thr = max(0.1, 4 * float(np.median(diff) if diff else 0.0))
    spikes = [times[k] for k in range(1, len(diff)) if diff[k] > thr and diff[k - 1] <= thr]  # onsets of big changes only
    events: list[float] = []
    for t in sorted([float(c) for c in cuts] + spikes):
        if not events or t - events[-1] >= 1.0:  # a cut and the motion it causes are one interrupt
            events.append(round(t, 2))
    gaps = [b - a for a, b in zip([0.0] + events, events + [dur])]
    # ---- ending
    tail = [k for k, t in enumerate(times) if t >= dur - min(8.0, 0.1 * dur)]
    last_speech = spans[-1][1] if spans else None
    black = [b for b in analysis.get("black") or [] if b[1] >= dur - 1.0]
    ending = {"speech_ends_before_s": round(dur - last_speech, 2) if last_speech else None,
              "text_share": round(float(np.mean([text[k] for k in tail])) if tail else 0.0, 3),
              "motion": round(float(np.mean([diff[k] for k in tail])) if tail else 0.0, 4), "fade_to_black": bool(black)}
    ending["kind"] = ("end_card" if ending["text_share"] >= 0.5 else "fade_out" if ending["fade_to_black"] or
                      (ending["speech_ends_before_s"] or 0) > 3.0 else "resolution")
    # ---- thumbnail candidates
    cands = []
    sharp = [_lap(g) for g in grey]
    smax = max(sharp or [1.0]) or 1.0
    for k, t in enumerate(times):
        rgb = fr[k]
        b = float(grey[k].mean())
        s = 0.35 * min(1.0, sharp[k] / (0.6 * smax)) + 0.25 * min(1.0, _colourfulness(rgb) / 60.0) + 0.15 * (1 - abs(b - 128) / 128)
        f = _face_at(analysis, t)
        why = []
        if f:
            size = f.get("h", 0)
            thirds = min(abs(f["x"] - 1 / 3), abs(f["x"] - 2 / 3), abs(f["x"] - 0.5))
            s += 0.15 * (1.0 if 0.18 <= size <= 0.6 else 0.4) + 0.10 * (1 - min(1.0, thirds / 0.17))
            why.append(f"face {size:.2f} of the height at x={f['x']:.2f}")
        if text[k]:
            s -= 0.1
            why.append("text on screen (-)")
        cands.append((s, t, why))
    thumbs: list[dict] = []
    for s, t, why in sorted(cands, reverse=True):
        if all(abs(t - x["t"]) >= max(5.0, dur / 20) for x in thumbs):
            thumbs.append({"t": round(t, 2), "score": round(s, 3), "why": why or ["sharpness, colour, exposure"]})
        if len(thumbs) >= 3:
            break
    metrics = {
        "duration": round(dur, 3), "shot_length": _stats(shot_lens), "cuts_per_min": round(len(cuts) / mins, 2),
        "first_3s": window(3.0), "first_10s": window(10.0),
        "hook": {"t": round(hook["start"], 2) if hook else None, "score": hook.get("hook") if hook else None,
                 "first_speech_s": round(first_speech, 2) if first_speech is not None else None},
        "shot_types": type_share, "shot_type_changes_per_min": round(changes / mins, 2),
        "scene_variety": round(len(set(labels)) / max(1, len(labels)), 3) if labels else None,
        "visual_density": {"motion": round(float(np.mean(diff)) if diff else 0.0, 4),
                           "edges": round(float(np.mean(edges)) if edges else 0.0, 4)},
        "cutaways_per_min": round(cutaway_shots / mins, 2) if faces_on else None,
        "on_screen_text_share": round(float(np.mean(text)) if text else 0.0, 3),
        "on_screen_text_method": "edge-density heuristic per frame band (not OCR)" + (
            " + SigLIP zero-shot 'text or a title card' / 'a chart or diagram' shots" if vis.get("available") else ""),
        "speech_share": round(sum(b - a for a, b in spans) / max(dur, 1e-6), 3),
        "music_bed_share": round(bed, 3), "audio_accents_per_min": round(onsets / mins, 2),
        "pattern_interrupt_s": _stats(gaps),
        "ending": ending, "thumbnail_candidates": thumbs,
        "wpm": round(sum(len(s["words"]) for s in sents) / mins, 1),
        "colour": colour_profile(fr), "transitions": transition_profile(path, cuts),
    }
    structure = [{"t0": 0.0, "t1": round(min(dur, 3.0), 2), "role": "first_3s"},
                 {"t0": 0.0, "t1": round(min(dur, 10.0), 2), "role": "first_10s"}]
    if hook:
        structure.append({"t0": round(hook["start"], 2), "t1": round(hook["end"], 2), "role": "hook"})
    structure.append({"t0": round(max(0.0, dur - min(8.0, 0.1 * dur)), 2), "t1": round(dur, 2), "role": f"ending:{ending['kind']}"})
    from . import contracts
    return contracts.check({"schema": f"nexus.ReferenceStyleProfile/{contracts.media.VERSION}", "name": name or Path(str(path)).stem, **dict(
                   source={"uri": Path(str(path)).resolve().as_uri() if Path(str(path)).exists() else str(path),
                           "duration": round(dur, 3), "analysed_fps": fps},
                   metrics=metrics, structure=structure,
                   rights={**rights, "kept": "numbers and timestamps only; no frames, text or sound of the reference"},
                   method={"visual": vis.get("model") if vis.get("available") else None, "faces": bool(faces_on),
                           "text": metrics["on_screen_text_method"]})})


# ---------------------------------------------------------------- reference -> director
def to_overrides(ref: dict) -> dict:
    """Director profile overrides that reproduce the reference's STRUCTURE (pacing, interrupts, hook timing, cutaway
    rate, caption density, music presence, ending). Every value comes with the measurement it came from."""
    m = ref["metrics"]
    o: dict = {}
    why: list[str] = []
    sl = m.get("shot_length") or {}
    if sl.get("n", 0) >= 3:
        lo = max(0.6, round(sl["p10"], 2))
        hi = max(lo + 0.5, round(sl["p90"], 2))
        o["shot_len"] = [lo, hi]
        o["shot_len_median"] = round(min(hi, max(lo, sl["median"])), 2)
        why.append(f"shot length p10-p90 {lo}-{hi}s, median {sl['median']}s ({m['cuts_per_min']} cuts/min)")
    f10, tot = (m.get("first_10s") or {}).get("cuts", 0), m.get("cuts_per_min", 0)
    if tot and f10 / (10 / 60) > 1.3 * tot:
        o["pacing"] = "front_loaded"
        why.append(f"first 10 s cut {f10 / (10 / 60):.0f}/min vs {tot:.0f}/min overall: front-loaded pacing")
    else:
        o["pacing"] = "steady"
    pi = (m.get("pattern_interrupt_s") or {}).get("median")
    if pi:
        o["pattern_interrupt_s"] = round(max(1.0, (o.get("shot_len") or [1.0])[0], min(20.0, pi * 1.5)), 2)
        why.append(f"a visual change every {pi:.1f}s (median) -> interrupt at most every {o['pattern_interrupt_s']}s")
    h = m.get("hook") or {}
    first = h.get("t") if h.get("t") is not None else h.get("first_speech_s")
    if first is not None:
        o["hook_window_s"] = round(max(1.5, min(15.0, first + 2.0)), 2)
        o["opening"] = "hook_first" if first <= 5 else "cold_open"
        why.append(f"hook line at {first:.1f}s -> hook window {o['hook_window_s']}s")
    if m.get("cutaways_per_min") is not None:
        o["cutaways_per_min"] = round(min(12.0, m["cutaways_per_min"]), 2)
        o["broll"] = {"policy": "cutaway" if m["cutaways_per_min"] > 0.3 else "none"}
        why.append(f"{m['cutaways_per_min']} cutaways/min (shots without the speaker's face during speech)")
    if m.get("on_screen_text_share", 0) >= 0.25:
        o["captions"] = {"style": "pop", "position": "middle", "words_per_cue": 3, "uppercase": True}
        why.append(f"on-screen text in {m['on_screen_text_share']:.0%} of frames (edge heuristic): bold captions")
    o["music"] = {"enabled": m.get("music_bed_share", 0) >= 0.4}
    why.append(f"sound outside speech {m.get('music_bed_share', 0):.0%} of the time -> music {'on' if o['music']['enabled'] else 'off'}")
    col = m.get("colour") or {}
    if col.get("n"):
        g = ("bw" if col["monochrome_share"] >= 0.6 else "warm" if col["warmth"] >= 0.06 else "cool" if col["warmth"] <= -0.04
             else "vivid" if col["colourfulness"] >= 55 else "documentary")
        o["grade"] = g
        why.append(f"colour: saturation {col['saturation']}, warmth {col['warmth']:+.3f}, colourfulness {col['colourfulness']} -> grade '{g}'")
    tr = m.get("transitions") or {}
    if tr.get("measured_cuts", 0) >= 3:
        gradual = tr.get("gradual_share", 0) + tr.get("fade_through_black_share", 0)
        o["transitions"] = {"crossfade_at_topics": gradual >= 0.2, "xfade": "fadeblack" if tr.get("fade_through_black_share", 0) > tr.get("gradual_share", 0) else "dissolve"}
        why.append(f"{tr.get('hard_cut_share', 0):.0%} hard cuts, {gradual:.0%} gradual -> crossfades at topics {'on' if gradual >= 0.2 else 'off'}")
    end = (m.get("ending") or {}).get("kind")
    if end:
        o["ending"] = end
        why.append(f"ending: {end}")
    return {"profile_overrides": o, "why": why, "reference": ref["name"]}


def apply(ref: dict, analysis_by_src: dict, brief: dict | None = None) -> dict:
    """Plan a DIFFERENT source with the reference's structure (director.plan with profile overrides). Content comes only
    from the user's own source; the reference contributes numbers."""
    from . import director
    brief = copy.deepcopy(brief or {})
    ov = to_overrides(ref)
    brief["profile_overrides"] = director._merge(brief.get("profile_overrides") or {}, ov["profile_overrides"])
    p = director.plan(analysis_by_src, brief)
    p["reference"] = {"name": ref["name"], "applied": ov["profile_overrides"], "why": ov["why"],
                      "note": "structure only: no footage, text or sound of the reference is used"}
    p["notes"].append(f"structure taken from reference '{ref['name']}': " + "; ".join(ov["why"]))
    return p


def compare(ref: dict, tl: dict) -> dict:
    """Reference vs edit on the structure metrics both have (timeline view)."""
    from . import growth
    st = growth.style_profile(None, tl)
    m = ref["metrics"]
    return {"cuts_per_min": (m.get("cuts_per_min"), st["cuts_per_min"]),
            "median_shot_s": ((m.get("shot_length") or {}).get("median"), st["median_shot_s"]),
            "time_to_first_speech": ((m.get("hook") or {}).get("first_speech_s"), st["time_to_first_speech"]),
            "duration": (m.get("duration"), st["duration"])}


