"""Editing proposals: analyse first, propose, and only build a timeline when the proposal is accepted."""
from __future__ import annotations

from . import project as prj
from .media import analyze, ff
from .timeline import ops, subs
from .timeline.render import clip_duration, clip_starts


def _speech_ranges(an: dict, pad: float = 0.15, max_gap: float = 0.6) -> list[dict]:
    """Keep-ranges built from transcript segments, merged when the pause between them is short."""
    dur = an["probe"]["duration"]
    segs = an["transcript"]["segments"] if an.get("transcript") else []
    dropped = {r["earlier"] for r in an.get("repetitions", [])}  # keep the LATER take of a repeated line
    ranges: list[dict] = []
    for i, s in enumerate(segs):
        if i in dropped:
            continue
        a, b = max(0.0, s["start"] - pad), min(dur, s["end"] + pad)
        if ranges and a - ranges[-1]["out"] <= max_gap:
            ranges[-1]["out"] = b
            ranges[-1]["segs"].append(i)
            ranges[-1]["score"] = max(ranges[-1]["score"], s.get("score", 0))
        else:
            ranges.append({"in": a, "out": b, "segs": [i], "score": s.get("score", 0)})
    return ranges


def propose(an: dict, goal: str = "tighten", target_duration: float | None = None, style: dict | None = None) -> dict:
    """Return a human-readable, machine-applicable proposal. Nothing is modified."""
    dur = an["probe"]["duration"]
    style = style or {}
    actions, removed = [], []
    ranges = _speech_ranges(an) if an.get("transcript") and an["transcript"]["segments"] else \
        [{"in": a, "out": b, "segs": [], "score": 1.0} for a, b in (an["scenes"] or [(0.0, dur)])]
    for r in an.get("repetitions", []):
        s = an["transcript"]["segments"][r["earlier"]]
        removed.append({"in": s["start"], "out": s["end"], "reason": f"repeated take: \"{s['text'][:60]}\""})
    for a, b in an.get("silences", []):
        if b - a >= 0.9:
            removed.append({"in": round(a, 2), "out": round(b, 2), "reason": f"silence {b - a:.1f}s"})
    for a, b in an.get("black", []):
        removed.append({"in": a, "out": b, "reason": "black frames"})
        ranges = [r for r in ranges if not (r["in"] >= a and r["out"] <= b)]
    if goal == "highlights" or target_duration:
        budget = target_duration or min(60.0, dur * 0.4)
        chosen, used = [], 0.0
        for r in sorted(ranges, key=lambda r: -r["score"]):
            d = r["out"] - r["in"]
            if used + d <= budget or not chosen:
                chosen.append(r)
                used += d
        for r in ranges:
            if r not in chosen:
                removed.append({"in": round(r["in"], 2), "out": round(r["out"], 2), "reason": "lower-scoring segment (highlight cut)"})
        ranges = sorted(chosen, key=lambda r: r["in"])
        actions.append(f"highlight cut: keep the {len(ranges)} strongest segments (~{used:.0f}s of {dur:.0f}s)")
    else:
        actions.append(f"tighten: keep {len(ranges)} speech ranges, remove pauses and repeated takes")
    lufs = (an.get("loudness") or {}).get("lufs")
    audio_fx = "voice" if an["probe"]["audio"] else "none"
    if lufs is not None:
        actions.append(f"audio: denoise + voice EQ/compression, normalise {lufs} -> -14 LUFS")
    grade = style.get("grade", "cinematic")
    actions.append(f"colour: '{grade}' grade; captions from transcript; fade in/out")
    q = an.get("quality") or {}
    if q.get("face_focus_x") is not None:
        actions.append(f"reframing focus from detected faces at x={q['face_focus_x']}")
    kept = sum(r["out"] - r["in"] for r in ranges)
    return {"goal": goal, "source_duration": round(dur, 2), "proposed_duration": round(kept, 2),
            "keep": [{"in": round(r["in"], 3), "out": round(r["out"], 3), "score": r["score"], "segs": r["segs"]} for r in ranges],
            "removed": sorted(removed, key=lambda r: r["in"]), "actions": actions, "grade": grade, "audio_fx": audio_fx,
            "focus_x": q.get("face_focus_x"), "source_issues": an.get("issues", []), "status": "awaiting approval"}


def build_timeline(src: str, an: dict, proposal: dict, width: int | None = None, height: int | None = None,
                   title: str | None = None) -> dict:
    v = an["probe"]["video"] or {"width": 1920, "height": 1080}
    tl = prj.new_timeline(width or v["width"], height or v["height"], (an.get("transcript") or {}).get("language", "en"))
    tl["grade"], tl["audio_fx"] = proposal["grade"], proposal["audio_fx"]
    focus = (proposal.get("focus_x") or 0.5, 0.5)
    if title:
        tl["clips"].append(ops.color_clip(2.0, text=[{"text": title, "style": "title"}], fade_in=0.4, fade_out=0.3))
    for k in proposal["keep"]:
        tl["clips"].append(ops.video_clip(src, k["in"], k["out"], focus=list(focus), segs=k.get("segs", [])))
    if tl["clips"]:
        tl["clips"][-1]["fade_out"] = 0.5
        if not title:
            tl["clips"][0]["fade_in"] = 0.4
    retime_subtitles(tl, {src: an})
    return tl


def retime_subtitles(tl: dict, analyses: dict[str, dict]) -> dict:
    """Rebuild programme-time captions from source transcripts after any cut/move/trim."""
    cues = []
    for c, start in zip(tl["clips"], clip_starts(tl)):
        an = analyses.get(c.get("src", ""))
        if c["type"] != "video" or not an or not an.get("transcript") or c.get("mute"):
            continue
        sp = float(c.get("speed", 1.0))
        words = [w for s in an["transcript"]["segments"] for w in s["words"] if c["in"] - 0.05 <= w["start"] and w["end"] <= c["out"] + 0.25]
        local = subs.cues_from_transcript([{"start": 0, "end": 0, "text": "", "words": words}]) if words else []
        end_of_clip = start + clip_duration(c)
        for q in local:
            s0 = start + (q["start"] - c["in"]) / sp
            cues.append({"start": round(max(start, s0), 3), "end": round(min(end_of_clip, start + (q["end"] - c["in"]) / sp), 3),
                         "text": q["text"]})
    tl["subtitles"] = subs.fix_timing([q for q in cues if q["end"] > q["start"]])
    return tl
