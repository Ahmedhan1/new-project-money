"""Retention rubric, automatic retention pass, hook/title variants and reference-video style profiles.

Nothing here predicts views. Every number is a measurable property of the timeline or the analysis, compared against
editorial rules of thumb (PLATFORM_NORMS). "Learning from what works" is implemented as structure only: a style profile is
pacing, caption and loudness statistics of a reference video the user is allowed to analyse; no text, image or sound of
the reference is ever copied into a timeline.
"""
from __future__ import annotations

import copy
import re
import statistics

from . import shorts as sh
from .timeline.render import clip_duration, clip_starts, total_duration

# Editorial rules of thumb, NOT platform data and NOT guarantees. `hard_max` values are upload limits as commonly
# documented at the time of writing and change often: check the platform before relying on them.
PLATFORM_NORMS: dict[str, dict] = {
    "shorts":   {"vertical": True,  "ideal_duration": (20, 60),   "hard_max": 180,  "first_speech_max": 1.5, "first_visual_max": 1.0,
                 "mean_shot_max": 3.5, "static_max": 6.0,  "interrupt_every": 4.0,  "captions_required": True,  "dead_air_max": 1.2,
                 "music_expected": True, "loop_preferred": True,  "safe_top": 0.10, "safe_bottom": 0.80},
    "tiktok":   {"vertical": True,  "ideal_duration": (15, 60),   "hard_max": 600,  "first_speech_max": 1.0, "first_visual_max": 1.0,
                 "mean_shot_max": 3.0, "static_max": 5.0,  "interrupt_every": 3.5,  "captions_required": True,  "dead_air_max": 1.0,
                 "music_expected": True, "loop_preferred": True,  "safe_top": 0.10, "safe_bottom": 0.78},
    "reels":    {"vertical": True,  "ideal_duration": (15, 60),   "hard_max": 180,  "first_speech_max": 1.5, "first_visual_max": 1.0,
                 "mean_shot_max": 3.5, "static_max": 6.0,  "interrupt_every": 4.0,  "captions_required": True,  "dead_air_max": 1.2,
                 "music_expected": True, "loop_preferred": True,  "safe_top": 0.12, "safe_bottom": 0.78},
    "youtube":  {"vertical": False, "ideal_duration": (240, 1200), "hard_max": None, "first_speech_max": 5.0, "first_visual_max": 1.0,
                 "mean_shot_max": 8.0, "static_max": 15.0, "interrupt_every": 12.0, "captions_required": False, "dead_air_max": 2.5,
                 "music_expected": False, "loop_preferred": False, "safe_top": 0.0, "safe_bottom": 1.0},
    "linkedin": {"vertical": False, "ideal_duration": (30, 180),  "hard_max": 600,  "first_speech_max": 3.0, "first_visual_max": 1.0,
                 "mean_shot_max": 6.0, "static_max": 10.0, "interrupt_every": 8.0,  "captions_required": True,  "dead_air_max": 2.0,
                 "music_expected": False, "loop_preferred": False, "safe_top": 0.0, "safe_bottom": 1.0},
}
SEVERITY_WEIGHT = {"major": 3, "minor": 1}
# fix ids apply_retention_pass can apply without a human; everything else is reported as manual
AUTO_FIXES = ("captions_from_words", "pop_captions", "captions_middle", "remove_lead_fade", "trim_tail", "tighten_pauses", "trim_lead",
              "add_punch_ins", "add_progress_bar", "add_whoosh")
LEAD_KEEP = 0.15     # seconds kept before the first word when trimming leading dead air
TAIL_KEEP = 0.40     # seconds kept after the last word when trimming trailing dead air
PAUSE_KEEP = (0.12, 0.10)
MOTION_FX = {"punch_in", "zoom", "shake", "flash", "glitch", "blur", "speed_ramp"}


def platform_norms() -> dict[str, dict]:
    return copy.deepcopy(PLATFORM_NORMS)


def _c(name: str, ok: bool, detail: str, severity: str = "major", fix: str | None = None) -> dict:
    # same shape as qa._c; retention findings are never "critical" (they do not make a file unusable)
    # `weight` is what the check is worth in the 0-100 score whether it passes or fails
    return {"check": name, "ok": bool(ok), "detail": detail, "severity": "info" if ok else severity, "fix": None if ok else fix,
            "weight": SEVERITY_WEIGHT.get(severity, 1)}


# ---------------------------------------------------------------- measurements
def _merge(spans: list[tuple[float, float]], join: float = 0.3) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= join:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def speech_spans(tl: dict, analysis_by_src: dict | None = None) -> tuple[list[list[float]], str, list[dict]]:
    """(merged speech intervals in programme time, where they came from, programme words)."""
    words = sh.programme_words(tl, analysis_by_src)
    if words:
        src = "transcript" if analysis_by_src and any(analysis_by_src.get(c.get("src", "")) for c in tl["clips"]) else "subtitle words"
        return _merge([(w["start"], w["end"]) for w in words]), src, words
    cues = [(q["start"], q["end"]) for q in tl.get("subtitles", [])]
    if cues:
        return _merge(cues), "subtitle cues", []
    narr = [(a.get("start", 0.0), a.get("start", 0.0) + float(a["duration"])) for a in tl.get("audio", [])
            if a.get("role", "narration") == "narration" and a.get("duration")]
    return (_merge(narr), "narration entries", []) if narr else ([], "none", [])


def _is_cut(prev: dict, c: dict) -> bool:
    """A visible cut: different source/type, or a jump in source time. Contiguous splits of the same shot are not cuts."""
    if prev["type"] != c["type"] or prev.get("src") != c.get("src"):
        return True
    if c["type"] == "video":
        return abs(c["in"] - prev["out"]) > 0.04
    return True


def visual_events(tl: dict) -> list[tuple[float, str]]:
    """Programme times where the picture changes: cuts, effect starts/ends, text overlays, picture-in-picture."""
    ev: list[tuple[float, str]] = []
    starts = clip_starts(tl)
    for k, (c, st) in enumerate(zip(tl["clips"], starts)):
        d = clip_duration(c)
        if k and _is_cut(tl["clips"][k - 1], c):
            ev.append((st, "cut"))
        for e in c.get("effects") or []:
            if e.get("type") not in MOTION_FX:
                continue
            t0 = float(e.get("at", e.get("start", 0.0)))
            ev.append((st + min(t0, d), e["type"]))
            if e.get("type") == "punch_in" and e.get("end") is not None and float(e["end"]) < d - 0.05:
                ev.append((st + float(e["end"]), "punch_out"))
        for t in c.get("text") or []:
            ev.append((st + float(t.get("start", 0.0)), "text"))
        if c.get("pip"):
            ev.append((st, "pip"))
    return sorted((round(t, 3), k) for t, k in ev)


def _shots(tl: dict) -> list[float]:
    """Shot lengths in programme time; a cut or a punch-in boundary starts a new shot."""
    total = total_duration(tl)
    bounds = sorted({0.0, total, *[t for t, k in visual_events(tl) if k in ("cut", "punch_in", "punch_out")]})
    return [b - a for a, b in zip(bounds, bounds[1:]) if b - a > 1e-3]


def _gaps(spans: list[list[float]], total: float) -> list[tuple[float, float]]:
    pts = [0.0] + [x for s in spans for x in s] + [total]
    return [(pts[k], pts[k + 1]) for k in range(0, len(pts), 2) if pts[k + 1] - pts[k] > 1e-3]


def _text_anchor(style: str) -> float | None:
    try:
        from .timeline import text
        return text.STYLES.get(style, text.STYLES["title"])[1]
    except Exception:
        return None


# ---------------------------------------------------------------- report
def retention_report(tl: dict, analysis_by_src: dict | None = None, platform: str = "shorts") -> dict:
    """Retention rubric in the qa stage shape. Each failed check carries a fix id; AUTO_FIXES can be applied by apply_retention_pass."""
    N = PLATFORM_NORMS[platform]
    short_form = N["vertical"]
    total = total_duration(tl)
    spans, sp_src, words = speech_spans(tl, analysis_by_src)
    checks = []
    clips = tl["clips"]
    first_speech = spans[0][0] if spans else None

    # hook: speech (or a hook text) early
    first_text = min((st + float(t.get("start", 0)) for c, st in zip(clips, clip_starts(tl)) for t in c.get("text") or []), default=None)
    hook_t = min(x for x in (first_speech, first_text) if x is not None) if (first_speech is not None or first_text is not None) else None
    lead_trimmable = bool(first_speech is not None and clips and clips[0]["type"] == "video" and first_speech > N["first_speech_max"]
                          and first_speech - LEAD_KEEP < clip_duration(clips[0]))
    checks.append(_c(f"hook within {N['first_speech_max']}s", hook_t is not None and hook_t <= N["first_speech_max"],
                     f"first speech at {first_speech if first_speech is not None else 'none'}s, first text at "
                     f"{round(first_text, 2) if first_text is not None else 'none'}s",
                     "major", "trim_lead" if lead_trimmable else "add_hook_text"))
    # something on screen in the first second
    c0 = clips[0] if clips else None
    fade = float(c0.get("fade_in", 0)) if c0 else 0.0
    blank = c0 is None or (c0["type"] == "color" and not any(float(t.get("start", 0)) <= N["first_visual_max"] for t in c0.get("text") or []))
    checks.append(_c(f"picture in the first {N['first_visual_max']}s", not blank and fade <= 0.5,
                     "empty programme" if c0 is None else f"first clip {c0['type']}, fade-in {fade}s",
                     "major", "remove_lead_fade" if not blank else "add_hook_visual"))
    # pacing
    shots = _shots(tl)
    mean_shot = total / max(len(shots), 1)
    checks.append(_c("average shot length", mean_shot <= N["mean_shot_max"],
                     f"{mean_shot:.1f}s over {len(shots)} shots (rule of thumb <= {N['mean_shot_max']}s; punch-ins count as shot changes)",
                     "minor", "add_punch_ins"))
    ev = [t for t, _ in visual_events(tl)]
    pts = sorted({0.0, total, *ev})
    stretches = [(a, b) for a, b in zip(pts, pts[1:])]
    longest = max(stretches, key=lambda s: s[1] - s[0], default=(0.0, 0.0))
    checks.append(_c("longest static stretch", longest[1] - longest[0] <= N["static_max"],
                     f"{longest[1] - longest[0]:.1f}s at {longest[0]:.1f}-{longest[1]:.1f}s (rule of thumb <= {N['static_max']}s)",
                     "major", "add_punch_ins"))
    interval = total / (len([t for t in ev if 0 < t < total]) + 1) if total else 0.0
    checks.append(_c("pattern interrupts", interval <= N["interrupt_every"],
                     f"one every {interval:.1f}s on average (cuts, effects, text; rule of thumb every {N['interrupt_every']}s)",
                     "minor", "add_punch_ins"))
    # captions
    speech_time = sum(b - a for a, b in spans)
    cues = tl.get("subtitles") or []
    if speech_time > 0:
        covered = sum(max(0.0, min(b, q["end"]) - max(a, q["start"])) for a, b in spans for q in cues)
        cov = covered / speech_time
        checks.append(_c("caption coverage of speech", cov >= 0.9, f"{cov:.0%} of {speech_time:.1f}s speech (from {sp_src}) has a caption",
                         "major" if N["captions_required"] else "minor", "captions_from_words" if words and not cues else "retime-subtitles"))
    else:
        checks.append(_c("caption coverage of speech", True, "no speech detected"))
    if short_form and cues:
        style = (tl.get("captions") or {}).get("style")
        checks.append(_c("animated captions", style in ("pop", "karaoke"), f"caption style {style or 'default (static)'}", "minor", "pop_captions"))
    # dead air (neither speech nor music)
    has_music = bool((tl.get("music") or {}).get("src"))
    if spans:
        gaps = [(a, b) for a, b in _gaps(spans, total) if b - a > N["dead_air_max"]]
        dead = 0.0 if has_music else sum(b - a for a, b in gaps)
        inner = [g for g in gaps if g[0] > 0 and g[1] < total]
        fix = "tighten_pauses" if inner else "trim_lead" if gaps and gaps[0][0] == 0 and lead_trimmable else "trim_tail" if gaps and gaps[-1][1] >= total else None
        checks.append(_c("dead air", dead == 0.0, f"{dead:.1f}s in {len(gaps)} pause(s) > {N['dead_air_max']}s" + (" (music covers them)" if has_music and gaps else ""),
                         "major" if short_form else "minor", fix))
    checks.append(_c("music bed", has_music or not N["music_expected"], "music present" if has_music else "no music bed", "minor", "add_music"))
    # ending
    if spans:
        tail = total - spans[-1][1]
        last_txt = (cues[-1]["text"] if cues else "").strip()
        closing = last_txt.endswith((".", "!")) or bool(words and words[-1]["word"].endswith((".", "!")))
        loopable = tail <= 0.6 and not (clips and float(clips[-1].get("fade_out", 0)) > 0.3)
        ok = tail <= 1.5 and (closing or (N["loop_preferred"] and loopable))
        checks.append(_c("ending", ok, f"{tail:.1f}s after the last word; closing line: {'yes' if closing else 'no'}; loopable: {'yes' if loopable else 'no'}",
                         "minor", "trim_tail" if tail > 1.5 else "end_on_closing_line"))
    lo, hi = N["ideal_duration"]
    over_hard = N["hard_max"] is not None and total > N["hard_max"]
    checks.append(_c("duration for platform", lo <= total <= hi, f"{total:.1f}s (rule of thumb {lo}-{hi}s" +
                     (f", over the {N['hard_max']}s upload limit)" if over_hard else ")"), "major" if over_hard else "minor", "choose_window"))
    # vertical framing and safe zones
    if short_form:
        vert = tl["height"] > tl["width"] and abs(tl["width"] / tl["height"] - 9 / 16) < 0.02
        checks.append(_c("vertical 9:16", vert, f"{tl['width']}x{tl['height']}", "major", "reframe_vertical"))
        unsafe = []
        for c in clips:
            for t in c.get("text") or []:
                y = _text_anchor(t.get("style", "title"))
                if y is not None and not (N["safe_top"] <= y <= N["safe_bottom"]):
                    unsafe.append(t.get("style"))
        pos = (tl.get("captions") or {}).get("position", "bottom")
        checks.append(_c("safe zones", not unsafe and (not cues or pos != "top"), f"text styles outside safe area: {unsafe or 'none'}; captions {pos}",
                         "minor", "captions_middle" if not unsafe else "move_text"))
        if cues:
            checks.append(_c("captions clear of platform UI", pos == "middle", f"captions at {pos} (bottom ~20% is covered by buttons/description)",
                             "minor", "captions_middle"))
        has_bar = any(o.get("type") == "progress_bar" for o in tl.get("overlays") or [])
        checks.append(_c("progress indicator", has_bar, "progress bar present" if has_bar else "none", "minor", "add_progress_bar"))
        section_cuts = [k for k in range(1, len(clips)) if clips[k].get("src") != clips[k - 1].get("src") or clips[k]["type"] != clips[k - 1]["type"]]
        if section_cuts:
            sfx_t = [a.get("start", 0) for a in tl.get("audio", []) if a.get("role") == "sfx"]
            st = clip_starts(tl)
            bare = [k for k in section_cuts if not any(abs(st[k] - t) <= 0.4 for t in sfx_t)]
            checks.append(_c("sound accent on section cuts", not bare, f"{len(section_cuts) - len(bare)}/{len(section_cuts)} section cuts have an sfx",
                             "minor", "add_whoosh"))
    weight = sum(c["weight"] for c in checks)
    got = sum(c["weight"] for c in checks if c["ok"])
    fixes = list(dict.fromkeys(c["fix"] for c in checks if not c["ok"] and c["fix"]))
    return {"stage": "retention", "platform": platform, "checks": checks, "score": round(100 * got / max(weight, 1)),
            "fixes": fixes, "auto_fixes": [f for f in fixes if f in AUTO_FIXES], "manual_fixes": [f for f in fixes if f not in AUTO_FIXES],
            "metrics": {"duration": round(total, 2), "first_speech": None if first_speech is None else round(float(first_speech), 3), "mean_shot": round(mean_shot, 2),
                        "longest_static": round(longest[1] - longest[0], 2), "interrupt_interval": round(interval, 2),
                        "speech_time": round(float(speech_time), 2), "speech_source": sp_src},
            "data": {"speech_spans": spans, "words": words},
            "norms": dict(N), "note": "editorial heuristics (rules of thumb), not platform data and not a prediction of views"}


# ---------------------------------------------------------------- automatic pass
def _shift_items(tl: dict, t: float, delta: float) -> None:
    """Move every programme-time item at or after t by delta (negative = earlier); items inside a removed span are clamped."""
    lim = t + delta  # start of the removed span when delta < 0
    for q in tl.get("subtitles", []):
        for k in ("start", "end"):
            if q[k] >= t:
                q[k] = round(q[k] + delta, 3)
            elif q[k] > lim:
                q[k] = round(lim, 3)
        for w in q.get("words") or []:
            for k in ("start", "end"):
                if w[k] >= t:
                    w[k] = round(w[k] + delta, 3)
                elif w[k] > lim:
                    w[k] = round(lim, 3)
    tl["subtitles"] = [q for q in tl.get("subtitles", []) if q["end"] > q["start"]]
    for a in tl.get("audio", []):
        s = a.get("start", 0.0)
        a["start"] = round(max(0.0, s + delta), 3) if s >= t else round(min(s, max(0.0, lim)), 3) if s > lim else s
    for m in tl.get("markers", []):
        if m["t"] >= t:
            m["t"] = round(m["t"] + delta, 3)


def _cut_span(tl: dict, t0: float, t1: float, protected: list[list[float]]) -> float:
    """Remove programme interval [t0, t1] if it lies inside one video clip and touches no protected (speech) span. Returns removed seconds."""
    if t1 - t0 < 0.05 or any(a < t1 and t0 < b for a, b in protected):
        return 0.0
    starts = clip_starts(tl)
    for k, (c, st) in enumerate(zip(tl["clips"], starts)):
        d = clip_duration(c)
        if c["type"] != "video" or not (st - 1e-3 <= t0 and t1 <= st + d + 1e-3):
            continue
        sp = float(c.get("speed", 1.0))
        l0, l1 = max(0.0, t0 - st), min(d, t1 - st)
        a_out, b_in = round(c["in"] + l0 * sp, 3), round(c["in"] + l1 * sp, 3)
        pieces = []
        if l0 > 0.05:
            A = copy.deepcopy(c)
            A["out"] = a_out
            A.pop("fade_out", None)
            A["effects"] = [{**e, **({"end": min(float(e["end"]), l0)} if e.get("end") is not None else {})}
                            for e in c.get("effects") or [] if float(e.get("start", e.get("at", 0.0))) < l0]
            A["text"] = [{**t, "end": min(float(t.get("end", d)), l0)} for t in c.get("text") or [] if float(t.get("start", 0)) < l0]
            pieces.append(A)
        if d - l1 > 0.05:
            B = copy.deepcopy(c)
            B["in"] = b_in
            if pieces:
                B["id"] = sh.ops.cid()
                B.pop("fade_in", None)
            B["effects"] = [{**e, **{key: round(max(0.0, float(e[key]) - l1), 3) for key in ("start", "end", "at") if e.get(key) is not None}}
                            for e in c.get("effects") or [] if e.get("end") is None or float(e["end"]) > l1]
            B["text"] = [{**t, "start": round(max(0.0, float(t.get("start", 0)) - l1), 3), "end": round(float(t.get("end", d)) - l1, 3)}
                         for t in c.get("text") or [] if float(t.get("end", d)) - l1 > 0.3]
            pieces.append(B)
        for p in pieces:
            if not p.get("text"):
                p.pop("text", None)
        tl["clips"][k:k + 1] = pieces
        removed = l1 - l0
        _shift_items(tl, st + l1, -removed)
        return removed
    return 0.0


def _add_punch_ins(tl: dict, every: float, min_part: float = 1.2) -> int:
    """Break every unchanging stretch longer than `every` into equal parts with punch-ins (a punch-in already running
    across a split point is released there instead, so scales never stack). Returns the number of changes made."""
    import math
    added = 0
    for c, st in zip(tl["clips"], clip_starts(tl)):
        d = clip_duration(c)
        local = sorted({0.0, d, *[t - st for t, _ in visual_events(tl) if st - 1e-6 <= t <= st + d]})
        effects = c.get("effects") or []
        for a, b in zip(local, local[1:]):
            n = math.ceil((b - a) / every - 1e-9)
            part = (b - a) / max(n, 1)
            if n < 2 or part < min_part:
                continue
            for k in range(1, n):
                t = round(a + k * part, 3)
                running = next((e for e in effects if e.get("type") == "punch_in" and float(e.get("start", 0)) < t < float(e.get("end", d))), None)
                if running is not None:
                    running["end"] = t
                else:
                    effects.append({"type": "punch_in", "start": t, "end": round(min(b, t + part / 2), 3), "scale": 1.12 if k % 2 else 1.08,
                                    "auto": "retention"})
                added += 1
        if effects:
            c["effects"] = effects
    return added


def apply_retention_pass(tl: dict, report: dict, analysis_by_src: dict | None = None) -> dict:
    """Apply the safe fixes from `report` and return a NEW timeline. Spoken content is never removed: only pauses outside speech
    spans (with breathing room) are cut, and every cut is refused if it would touch a word. What was done is in tl["retention_pass"]."""
    tl = copy.deepcopy(tl)
    N = PLATFORM_NORMS[report.get("platform", "shorts")]
    fixes = set(report.get("fixes", []))
    spans = [list(s) for s in report.get("data", {}).get("speech_spans", [])]
    words = report.get("data", {}).get("words", [])
    if analysis_by_src:
        spans, _, words = speech_spans(tl, analysis_by_src)
    applied, skipped = [], []
    caps = {**sh.DEFAULT_CAPTIONS, **(tl.get("captions") or {})}
    if "captions_from_words" in fixes:
        if words and not tl.get("subtitles"):
            tl["subtitles"] = sh.word_cues(words, total_duration(tl), int(caps["words_per_cue"]))
            tl["captions"] = {**caps, "style": "pop"}
            applied.append(f"added {len(tl['subtitles'])} word-level pop captions")
        else:
            skipped.append("captions_from_words: no word timings")
    if "pop_captions" in fixes and tl.get("subtitles"):
        tl["captions"] = {**caps, **(tl.get("captions") or {}), "style": "pop"}
        applied.append("caption style -> pop")
    if "captions_middle" in fixes and tl.get("subtitles"):
        tl["captions"] = {**caps, **(tl.get("captions") or {}), "position": "middle"}
        applied.append("captions moved to the middle safe zone")
    if "remove_lead_fade" in fixes and tl["clips"] and float(tl["clips"][0].get("fade_in", 0)) > 0.5:
        tl["clips"][0]["fade_in"] = 0.0
        applied.append("removed the slow fade-in on the first shot")
    # cuts: from the end backwards so that earlier speech spans stay valid
    protected = [[a - 0.05, b + 0.05] for a, b in spans]
    if spans and fixes & {"trim_tail", "tighten_pauses", "trim_lead"}:
        total = total_duration(tl)
        if "trim_tail" in fixes and total - spans[-1][1] > 1.5:
            r = _cut_span(tl, spans[-1][1] + TAIL_KEEP, total, protected)
            (applied if r else skipped).append(f"trim_tail: {'removed %.2fs of trailing dead air' % r if r else 'tail is not one video clip'}")
        if "tighten_pauses" in fixes:
            gaps = [(a, b) for a, b in _gaps(spans, total) if b - a > N["dead_air_max"] and a > 0 and b < total]
            n_rem, rem = 0, 0.0
            for a, b in sorted(gaps, reverse=True):
                r = _cut_span(tl, a + PAUSE_KEEP[0], b - PAUSE_KEEP[1], protected)
                n_rem += bool(r)
                rem += r
            if gaps:
                (applied if n_rem else skipped).append(f"tighten_pauses: removed {rem:.2f}s in {n_rem}/{len(gaps)} pauses")
        if "trim_lead" in fixes and spans[0][0] > N["first_speech_max"]:
            r = _cut_span(tl, 0.0, spans[0][0] - LEAD_KEEP, protected)
            (applied if r else skipped).append(f"trim_lead: {'removed %.2fs before the first word' % r if r else 'lead is not trimmable'}")
    if "add_punch_ins" in fixes:
        n = _add_punch_ins(tl, min(N["interrupt_every"], N["mean_shot_max"]))
        (applied if n else skipped).append(f"add_punch_ins: {n} punch-in(s) on static stretches")
    if "add_progress_bar" in fixes and not any(o.get("type") == "progress_bar" for o in tl.get("overlays") or []):
        tl.setdefault("overlays", []).append({"type": "progress_bar", "color": "#3EC5FF", "height": 8})
        applied.append("added a progress bar")
    if "add_whoosh" in fixes:
        p = sh._sfx_path("whoosh")
        if p:
            st = clip_starts(tl)
            cl = tl["clips"]
            ts = [st[k] for k in range(1, len(cl)) if cl[k].get("src") != cl[k - 1].get("src") or cl[k]["type"] != cl[k - 1]["type"]]
            tl["audio"] += [{"src": p, "start": round(max(0.0, t - 0.12), 3), "volume": 0.35, "role": "sfx"} for t in ts]
            applied.append(f"added {len(ts)} whoosh(es) on section cuts")
        else:
            skipped.append("add_whoosh: nexus.fx.sfx not available")
    manual = [f for f in fixes if f not in AUTO_FIXES]
    tl["retention_pass"] = {"applied": applied, "skipped": skipped, "manual": manual, "score_before": report.get("score")}
    return tl


# ---------------------------------------------------------------- hooks / titles
PATTERNS = {
    "question": "asks a question the video answers",
    "number": "leads with a concrete number from the content",
    "contrarian": "challenges a common belief the content addresses",
    "how-to": "promises an explanation the content gives",
    "curiosity gap": "names the topic and withholds the answer",
    "stakes": "says why it matters to the viewer",
}
CONTRARIAN_RE = re.compile(r"\b(not|isn't|aren't|don't|doesn't|myth|wrong|actually|instead|misconception|never)\b", re.I)
HOWTO_RE = re.compile(r"\b(how|works?|because|by|step|way|turns?|makes?|uses?)\b", re.I)
STAKES_RE = re.compile(r"\b(cost|costs|save|saves|money|bill|bills|price|risk|lose|safe|health|time|cheap|cheaper|expensive|jobs?)\b", re.I)


def _topic(text: str) -> str:
    """Topic phrase: the adjacent pair of content words whose words recur most, favouring the first sentence (talks usually
    name their subject first). Exact duplicate sentences (retakes) are counted once."""
    words = text.split()
    if len(words) <= 8:
        return re.sub(r"[.!?]+$", "", text.strip())
    sents = list(dict.fromkeys(s.strip().lower() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()))
    toks_by_sent = [sh._tokens(s) for s in sents]
    freq: dict[str, int] = {}
    for ts in toks_by_sent:
        for t in ts:
            if t not in sh.STOP and len(t) > 2:
                freq[t] = freq.get(t, 0) + 1
    if not freq:
        return text.strip()[:40]
    best, best_s = "", -1.0
    for si, ts in enumerate(toks_by_sent):
        for x, y in zip(ts, ts[1:]):
            if x in freq and y in freq:
                sc = freq[x] + freq[y] + (2 if si == 0 else 0) - 0.01 * si
                if sc > best_s:
                    best, best_s = f"{x} {y}", sc
    return best or max(freq, key=lambda w: freq[w])


def _norm_title(t: str) -> set[str]:
    return set(re.findall(r"\w+", t.lower()))


def _title_score(title: str, supported: bool) -> int:
    n = len(title.split())
    s = 50 + (15 if 3 <= n <= 8 else 0) + (10 if len(title) <= 60 else -10) + (15 if supported else 0)
    s += 5 * bool(re.search(r"\d", title)) + 5 * bool(sh.SECOND_PERSON & set(sh._tokens(title)))
    return max(0, min(100, s))


def hook_variants(topic_or_text: str, n: int = 5, use_llm: bool = False) -> list[dict]:
    """Title/hook variants across PATTERNS. With source text (> 12 words) a variant that makes a claim is only produced when a
    source sentence supports it, and `supported_by` quotes that sentence. With a bare topic, claim patterns are flagged `verify`."""
    text = topic_or_text.strip()
    is_text = len(text.split()) > 12
    sents = re.split(r"(?<=[.!?])\s+", text) if is_text else []
    topic = _topic(text) if is_text else re.sub(r"[.!?]+$", "", text)
    T = topic[:1].upper() + topic[1:]

    def find(rx) -> str | None:
        return next((s for s in sents if rx.search(s)), None)

    cands: list[dict] = []

    def add(pattern: str, title: str, support: str | None, needs_support: bool = True) -> None:
        if is_text and needs_support and not support:
            return
        cands.append({"title": title, "pattern": pattern, "rule": PATTERNS[pattern], "supported_by": (support or "")[:160] or None,
                      "verify": None if (support or not needs_support) else "make sure the video actually delivers this", "source": "template"})

    q = find(re.compile(r"\?\s*$"))
    if q and len(q.split()) <= 14:
        add("question", q.strip(), q)
    add("question", f"{T}: worth it or not?", find(STAKES_RE) or (sents[0] if sents else None),
        needs_support=is_text)
    nums = list(re.finditer(r"\b(\d[\d.,]*)\s*(%|percent\b|[a-zA-Z]+)?", text)) if is_text else []
    m = next((x for x in nums if (x.group(2) or "") in ("%", "percent")), nums[0] if nums else None)  # percentages first
    if m:
        num, unit = m.group(1).rstrip(".,"), (m.group(2) or "").strip()
        phrase = f"{num}%" if unit in ("%", "percent") else f"{num} {unit}".strip() if unit.lower() not in sh.STOP else num
        add("number", f"{T}: the {phrase} explained", find(re.compile(r"(?<![\d.])" + re.escape(num) + r"(?![\d])")))
    elif not is_text:
        add("number", f"3 things to know about {topic}", None)
    add("contrarian", f"What most people get wrong about {topic}", find(CONTRARIAN_RE))
    add("how-to", f"{T}: how it actually works", find(HOWTO_RE))
    add("curiosity gap", f"The part of {topic} nobody explains", sents[0] if sents else None, needs_support=False)
    add("stakes", f"{T}: why it matters for you", find(STAKES_RE))
    if use_llm:
        cands += _llm_variants(text, topic, is_text)
    for c in cands:
        c["score"] = _title_score(c["title"], bool(c["supported_by"]))
    out: list[dict] = []
    for c in sorted(cands, key=lambda c: (-c["score"], c["pattern"])):
        a = _norm_title(c["title"])
        if any(len(a & _norm_title(o["title"])) / max(len(a | _norm_title(o["title"])), 1) >= 0.8 for o in out):
            continue
        out.append(c)
    # one per pattern first, then fill
    first, rest, seen = [], [], set()
    for c in out:
        (rest if c["pattern"] in seen else first).append(c)
        seen.add(c["pattern"])
    return (first + rest)[:n]


def _llm_variants(text: str, topic: str, is_text: bool) -> list[dict]:
    try:
        from . import llm
        schema = {"type": "object", "properties": {"variants": {"type": "array", "items": {"type": "object", "properties": {
            "pattern": {"type": "string", "enum": list(PATTERNS)}, "title": {"type": "string"}}, "required": ["pattern", "title"]}}}, "required": ["variants"]}
        d = llm.chat_json(f"Write 6 short video titles (max 9 words each), one per pattern {list(PATTERNS)}, about: {topic}. "
                          f"Only claims the text supports. TEXT: {text[:1500]}", schema=schema, require="variants", max_tokens=400, retries=1)
    except Exception:  # local model unavailable or answer unusable: templates stand alone
        return []
    vocab = set(sh._tokens(text)) | set(sh._tokens(topic))
    out = []
    for v in d.get("variants", []):
        t = str(v.get("title", "")).strip()
        nums = re.findall(r"\d+", t)
        if not t or len(t.split()) > 9 or v.get("pattern") not in PATTERNS or any(x not in text for x in nums):
            continue
        support = next((s for s in re.split(r"(?<=[.!?])\s+", text) if set(sh.content_words(t)) & set(sh.content_words(s))), None) if is_text else None
        if is_text and (not support or not set(sh.content_words(t)) <= vocab | set(sh.STOP) | {"why", "how", "really", "actually", "truth"}):
            continue
        out.append({"title": t, "pattern": v["pattern"], "rule": PATTERNS[v["pattern"]], "supported_by": support[:160] if support else None,
                    "verify": None if support else "make sure the video actually delivers this", "source": "llm"})
    return out


# ---------------------------------------------------------------- style profiles
def _stats(xs: list[float]) -> tuple[float, float]:
    return (round(statistics.fmean(xs), 3), round(statistics.median(xs), 3)) if xs else (0.0, 0.0)


def style_profile(analysis: dict | None, tl: dict | None = None) -> dict:
    """Measurable structure of a reference (analysis of a video the user may analyse, or a timeline). Numbers only, no content."""
    if tl is not None:
        total = total_duration(tl)
        shots = _shots_cuts_only(tl)
        words = sh.programme_words(tl, {(c.get("src") or ""): analysis for c in tl["clips"]} if analysis else None)
        spans = _merge([(w["start"], w["end"]) for w in words]) if words else _merge([(q["start"], q["end"]) for q in tl.get("subtitles", [])])
        ev = visual_events(tl)
        effects = [k for _, k in ev if k in MOTION_FX]
        texts = [k for _, k in ev if k == "text"]
        cues = tl.get("subtitles") or []
        caption_style = (tl.get("captions") or {}).get("style") or ("classic" if cues else None)
        loud = (analysis or {}).get("loudness") or {}
        source = "timeline"
    else:
        total = float(analysis["probe"]["duration"])
        scenes = [tuple(s) for s in analysis.get("scenes") or [] if s[1] > s[0]] or [(0.0, total)]
        shots = [b - a for a, b in scenes]
        words = [w for s in sh.sentences(analysis) for w in s["words"]]
        spans = _merge([(w["start"], w["end"]) for w in words])
        effects, texts, cues, caption_style = [], [], [], None
        loud = analysis.get("loudness") or {}
        source = "analysis"
    mins = max(total / 60.0, 1e-6)
    mean_shot, median_shot = _stats(shots)
    speech = sum(b - a for a, b in spans)
    first5 = {"cuts": sum(1 for t in _cut_times(shots) if 0 < t < 5.0), "words": sum(1 for w in words if w["start"] < 5.0),
              "speech_start": round(spans[0][0], 3) if spans else None, "has_text": bool(tl and any(t < 5.0 for t, k in visual_events(tl) if k == "text")),
              "effects": sum(1 for t, k in (visual_events(tl) if tl else []) if t < 5.0 and k in MOTION_FX)}
    cue_words = [len(q["text"].split()) for q in cues]
    return {"source": source, "duration": round(total, 3), "shots": len(shots), "cuts_per_min": round((len(shots) - 1) / mins, 2),
            "mean_shot_s": mean_shot, "median_shot_s": median_shot, "words": len(words), "wpm": round(len(words) / mins, 1),
            "wpm_speaking": round(len(words) / max(speech / 60.0, 1e-6), 1) if speech else 0.0,
            "speech_share": round(speech / max(total, 1e-6), 3), "time_to_first_speech": round(spans[0][0], 3) if spans else None,
            "effects_per_min": round(len(effects) / mins, 2), "text_per_min": round(len(texts) / mins, 2),
            "caption_style": caption_style, "caption_words_per_cue": round(statistics.fmean(cue_words), 2) if cue_words else None,
            "mean_word_chars": round(statistics.fmean(len(w["word"].strip(".,!?")) for w in words), 2) if words else None,
            "loudness_lufs": loud.get("lufs"), "first_5s": first5,
            "note": "structure only (pacing, caption and loudness statistics); no text, image or sound of the reference is kept"}


def _shots_cuts_only(tl: dict) -> list[float]:
    total = total_duration(tl)
    bounds = sorted({0.0, total, *[t for t, k in visual_events(tl) if k == "cut"]})
    return [b - a for a, b in zip(bounds, bounds[1:]) if b - a > 1e-3]


def _cut_times(shots: list[float]) -> list[float]:
    out, t = [], 0.0
    for d in shots[:-1]:
        t += d
        out.append(round(t, 3))
    return out


def apply_profile(tl: dict, profile: dict) -> dict:
    """Nudge a timeline toward a profile's structure: visual-change interval (punch-ins), effects density and caption style.
    Content (what is said and shown) is untouched; no cut removes anything."""
    tl = copy.deepcopy(tl)
    done = []
    cur = style_profile(None, tl)
    target = profile.get("mean_shot_s") or 0
    if target and cur["mean_shot_s"] > target * 1.3:
        n = _add_punch_ins(tl, max(1.5, float(target)))
        done.append(f"pacing: {n} punch-in(s) so the picture changes about every {target:.1f}s (was {cur['mean_shot_s']:.1f}s per shot)")
    want_fx = profile.get("effects_per_min") or 0
    have_fx = style_profile(None, tl)["effects_per_min"]
    if want_fx > have_fx * 1.2 and tl["clips"]:
        n = _add_punch_ins(tl, max(1.5, 60.0 / want_fx))
        done.append(f"effects density: +{n} punch-in(s) toward {want_fx}/min (was {have_fx}/min)")
    if tl.get("subtitles"):
        caps = dict(tl.get("captions") or {})
        if profile.get("caption_style"):
            caps["style"] = profile["caption_style"]
        if profile.get("caption_words_per_cue"):
            caps["words_per_cue"] = max(1, min(6, round(profile["caption_words_per_cue"])))
        elif profile.get("wpm_speaking"):
            caps["words_per_cue"] = 3 if profile["wpm_speaking"] >= 170 else 4
        if caps != (tl.get("captions") or {}):
            tl["captions"] = {**sh.DEFAULT_CAPTIONS, **caps}
            done.append(f"captions: style {tl['captions']['style']}, {tl['captions']['words_per_cue']} words per cue")
    tl["style_applied"] = {"changes": done, "profile_source": profile.get("source"), "note": "structure only; no content copied"}
    return tl
