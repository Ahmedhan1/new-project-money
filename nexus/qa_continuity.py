"""Stage 3: scene-to-scene continuity, measured on the rendered file at every cut.

Checks (each failed check names the cut time and a `fix` id the correction loop can act on):
  a) audio level jump: RMS of up to 1.5 s before vs after a cut; > 6 dB is flagged unless narration starts/stops there
     (a boundary by design), a generated card sits on one side, or the clip is marked `continuity_ok`.
  b) brightness jump and c) colour-cast jump: mean luma / mean chroma of the 2 frames before vs the 2 frames after a cut.
     A hard cut between two clips of the SAME source with the SAME grade that jumps is flagged (that looks like a mistake);
     any other large jump is reported as information (different sources are expected to look different).
  d) flash frames: a clip shorter than 3 frames.
  e) narration clipped by a cut: a narration item running > 0.3 s past its clip into a different scene, unless marked
     `spans_cuts` on the audio item.
  f) caption continuity: overlapping cues, cues of source speech running into the next scene, and caption gaps at cuts
     that are far larger than the programme's usual gap.
Cost: one 8 kHz mono audio decode of the whole file plus one short seek per cut for a few 64x36 frames.
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

from . import config, costguard
from .media import ff
from .qa import _c
from .timeline.render import clip_duration, clip_starts, total_duration

STAGE = "3-scene-continuity"
LEVEL_JUMP_DB = 6.0
LUMA_JUMP = 40.0       # mean luma difference on a 0-255 scale
CHROMA_JUMP = 18.0     # distance of mean (U, V) offsets on a 0-255 scale
FLASH_FRAMES = 3
NARRATION_OVERRUN_S = 0.3
AUDIO_SR = 8000
FRAME_W, FRAME_H = 64, 36


def _cut_times(tl: dict) -> list[float]:
    """Programme times of each clip start. Without crossfades the renderer rounds every clip to whole frames, so the
    real cut positions are the cumulative rounded durations; with crossfades `clip_starts` is the reference."""
    fps = tl.get("fps", config.FPS)
    if any(i and c.get("transition") == "crossfade" for i, c in enumerate(tl["clips"])):
        return clip_starts(tl)
    out, n = [], 0
    for c in tl["clips"]:
        out.append(n / fps)
        n += max(1, round(clip_duration(c) * fps))
    return out


def _decode_audio(path: Path) -> np.ndarray:
    costguard.authorize("ffmpeg", "continuity-audio")
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(AUDIO_SR), "-f", "f32le", "-"],
                       capture_output=True)
    return np.frombuffer(p.stdout, dtype=np.float32) if p.returncode == 0 else np.zeros(0, dtype=np.float32)


def _rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-9)) if len(x) else -180.0


def _frames_around(path: Path, t: float, fps: float, n: int = 2, margin: int = 1) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Mean (Y, U, V) of the `n` frames ending `margin` frames before t and the `n` frames starting `margin` frames after t."""
    costguard.authorize("ffmpeg", "continuity-frames")
    span = (n + margin + 1) / fps
    start = max(0.0, t - span)
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.4f}", "-i", str(path), "-t", f"{(t - start) + span:.4f}",
                        "-an", "-vf", f"fps={fps},scale={FRAME_W}:{FRAME_H}:flags=area,format=yuv444p", "-f", "rawvideo", "-"],
                       capture_output=True)
    fsz = FRAME_W * FRAME_H * 3
    if p.returncode != 0 or len(p.stdout) < fsz:
        return None, None
    fr = np.frombuffer(p.stdout[: len(p.stdout) // fsz * fsz], dtype=np.uint8).reshape(-1, 3, FRAME_H, FRAME_W).astype(np.float64)
    times = start + np.arange(len(fr)) / fps
    before = fr[times < t - margin / fps + 1e-6][-n:]
    after = fr[times >= t + margin / fps - 1e-6][:n]
    m = lambda a: a.mean(axis=(0, 2, 3)) if len(a) else None
    return m(before), m(after)


def _is_card(c: dict) -> bool:
    return c["type"] in ("color", "image") and not c.get("derived_from")


def _source_of(c: dict) -> str | None:
    return c.get("src") if c["type"] == "video" else c.get("derived_from")


def _narration(tl: dict) -> list[tuple[float, float, dict]]:
    out = []
    for a in tl.get("audio", []):
        if a.get("role", "narration") != "narration" or not a.get("src"):
            continue
        d = a.get("duration")
        if d is None:
            p = Path(a["src"])
            if not p.exists():
                continue
            try:
                with wave.open(str(p), "rb") as w:
                    d = w.getnframes() / float(w.getframerate())
            except (wave.Error, EOFError):
                d = ff.duration(p)
        s = float(a.get("start", 0))
        out.append((s, s + float(d), a))
    return out


def continuity_review(video_path: str | Path, tl: dict) -> dict:
    video_path = Path(video_path)
    checks: list[dict] = []
    clips = tl.get("clips", [])
    if not video_path.exists() or len(clips) < 1:
        return {"stage": STAGE, "checks": [_c("rendered file", False, "missing output", fix="rerender")], "passed": False}
    fps = float(tl.get("fps", config.FPS))
    starts = _cut_times(tl)
    total = total_duration(tl)
    narr = _narration(tl)

    # d) flash frames ------------------------------------------------------------------
    flashes = [(i, starts[i]) for i, c in enumerate(clips) if clip_duration(c) < FLASH_FRAMES / fps]
    for i, t in flashes:
        checks.append(_c(f"flash frame at {t:.2f}s", False, f"clip {clips[i]['id']} lasts {clip_duration(clips[i]) * fps:.1f} frames (< {FLASH_FRAMES})",
                         "major", fix="remove-flash-frame") | {"t": round(t, 3), "clip": clips[i]["id"]})
    if not flashes:
        checks.append(_c("flash frames", True, f"no clip shorter than {FLASH_FRAMES} frames"))

    audio = _decode_audio(video_path)
    level_flags = vis_flags = 0
    info_jumps = []
    for i in range(1, len(clips)):
        t = starts[i]
        a, b = clips[i - 1], clips[i]
        soft = b.get("transition") == "crossfade" or a.get("fade_out") or b.get("fade_in")
        marked = a.get("continuity_ok") or b.get("continuity_ok")
        # a) audio level jump ------------------------------------------------------------
        w = min(1.5, clip_duration(a), clip_duration(b))
        if len(audio) and w > 0.2:
            g = 0.05
            pre = audio[int(max(0, t - w) * AUDIO_SR): int(max(0, t - g) * AUDIO_SR)]
            post = audio[int((t + g) * AUDIO_SR): int((t + w) * AUDIO_SR)]
            lp, la = _rms_db(pre), _rms_db(post)
            jump = la - lp
            nb = any(abs(s - t) < 0.5 or abs(e - t) < 0.5 for s, e, _ in narr)
            act = lambda x0, x1: any(s < x1 and e > x0 for s, e, _ in narr)
            nb = nb or act(t - w, t) != act(t, t + w)
            designed = nb or marked or _is_card(a) or _is_card(b) or max(lp, la) < -50
            if abs(jump) > LEVEL_JUMP_DB and not designed:
                level_flags += 1
                checks.append(_c(f"audio level jump at {t:.2f}s", False, f"{lp:.1f} dB -> {la:.1f} dB ({jump:+.1f} dB) across a hard cut",
                                 "major", fix="level-match-cut") | {"t": round(t, 3), "jump_db": round(jump, 1)})
            elif abs(jump) > LEVEL_JUMP_DB:
                info_jumps.append(f"{t:.1f}s {jump:+.0f} dB (by design)")
        # b, c) brightness / colour cast -------------------------------------------------
        if soft:
            continue
        ya, yb = _frames_around(video_path, t, fps)
        if ya is None or yb is None:
            continue
        dl = float(yb[0] - ya[0])
        dc = float(np.hypot(yb[1] - ya[1], yb[2] - ya[2]))
        same = (_source_of(a) and _source_of(a) == _source_of(b) and (a.get("grade") or tl.get("grade", "none")) == (b.get("grade") or tl.get("grade", "none"))
                and not a.get("effects") and not b.get("effects"))
        for kind, val, lim, fix in (("brightness", dl, LUMA_JUMP, "match-grade"), ("colour cast", dc, CHROMA_JUMP, "match-grade")):
            if abs(val) <= lim:
                continue
            if same and not marked:
                vis_flags += 1
                checks.append(_c(f"{kind} jump at {t:.2f}s", False,
                                 f"{'luma' if kind == 'brightness' else 'chroma'} {val:+.0f} on a hard cut within the same source and grade",
                                 "major", fix=fix) | {"t": round(t, 3), "delta": round(val, 1)})
            else:
                info_jumps.append(f"{t:.1f}s {kind} {val:+.0f} (different shots/sources)")
    if not level_flags:
        checks.append(_c("audio level across cuts", True, f"no unplanned jump > {LEVEL_JUMP_DB:.0f} dB at {len(clips) - 1} cut(s)"))
    if not vis_flags:
        checks.append(_c("brightness/colour across cuts", True, "no unexplained jump within a source"))
    if info_jumps:
        checks.append(_c("expected jumps (information)", True, "; ".join(info_jumps[:8]) + (" ..." if len(info_jumps) > 8 else "")))

    # e) narration clipped by a cut -----------------------------------------------------
    clipped = 0
    for s, e, a in narr:
        idx = max([i for i, st in enumerate(starts) if st <= s + 1e-3] or [0])
        end_clip = starts[idx] + clip_duration(clips[idx])
        if e <= end_clip + NARRATION_OVERRUN_S or a.get("spans_cuts") or idx + 1 >= len(clips):
            continue
        nxt = clips[idx + 1]
        same_scene = _source_of(nxt) and _source_of(nxt) == _source_of(clips[idx])
        if not same_scene:
            clipped += 1
            checks.append(_c(f"narration crosses cut at {end_clip:.2f}s", False,
                             f"{Path(a['src']).name} runs {e - end_clip:.2f}s into the next scene (mark spans_cuts if intended)",
                             "major", fix="extend-scene") | {"t": round(end_clip, 3)})
    if narr and not clipped:
        checks.append(_c("narration inside its scenes", True, f"{len(narr)} narration item(s) checked"))

    # f) caption continuity -------------------------------------------------------------
    cues = sorted(tl.get("subtitles", []), key=lambda q: q["start"])
    if cues:
        gaps = [(q2["start"] - q1["end"], q1, q2) for q1, q2 in zip(cues, cues[1:])]
        over = [(round(q2["start"], 2)) for g, q1, q2 in gaps if g < -0.02]
        checks.append(_c("caption overlap", not over, "no overlapping cues" if not over else f"{len(over)} overlapping cue(s), first at {over[0]}s",
                         "major", fix="retime-subtitles") | ({"t": over[0]} if over else {}))
        pos = sorted(g for g, _, _ in gaps if g >= 0)
        med = pos[len(pos) // 2] if pos else 0.0
        bad = []
        for t in starts[1:]:
            before = [q for q in cues if q["end"] <= t + 0.3 and q["end"] > t - 2.0]
            after = [q for q in cues if q["start"] >= t - 0.3 and q["start"] < t + 2.0]
            straddle = [q for q in cues if q["start"] < t - 0.3 and q["end"] > t + 0.3]
            if straddle and not any(s < t < e for s, e, _ in narr):
                bad.append((t, f"cue \"{straddle[0]['text'][:30]}\" runs across the cut"))
            elif before and after:
                g = min(after, key=lambda q: q["start"])["start"] - max(before, key=lambda q: q["end"])["end"]
                if g > max(1.0, 4 * med):
                    bad.append((t, f"caption gap {g:.2f}s at the cut vs usual {med:.2f}s"))
        for t, d in bad[:6]:
            checks.append(_c(f"caption continuity at {t:.2f}s", False, d, "minor", fix="retime-subtitles") | {"t": round(t, 3)})
        if not bad:
            checks.append(_c("caption continuity across cuts", True, f"{len(cues)} cues, usual gap {med:.2f}s"))
    checks.append(_c("measured", True, f"{len(clips) - 1} cut(s) over {total:.1f}s; audio RMS window 1.5 s, 2 frames each side"))
    return {"stage": STAGE, "checks": checks, "passed": all(c["ok"] or c["severity"] in ("minor", "info") for c in checks)}
