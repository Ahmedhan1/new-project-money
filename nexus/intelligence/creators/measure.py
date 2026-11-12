"""Objective measurements of one LOCAL reference file (the owner has rights to it, or it is public-domain/CC).

Everything here is measured by FFmpeg/Whisper; nothing is interpreted. Reuses nexus.media.analyze / ff and
nexus.speech.asr. The transcript is used only to count words/markers; no script text is stored beyond short opening
phrases (<= 12 words) kept for the owner's own inspection when `keep_quote=True`.
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

from ...media import analyze as an
from ...media import ff

WINDOWS = (3.0, 5.0, 15.0)
BINS = 10  # pacing curve: one value per 10 % of runtime

# Opening-language markers (English + Arabic). Counted, never interpreted as quality on their own.
QUESTION_WORDS = {"what", "why", "how", "who", "where", "when", "which", "would", "could", "can", "did", "does", "is", "are",
                  "ما", "ماذا", "لماذا", "ليه", "كيف", "ازاي", "إزاي", "مين", "من", "فين", "أين", "متى", "امتى", "هل", "إيه", "ايه"}
STAKES_WORDS = {"million", "billion", "money", "dollars", "$", "win", "lose", "last", "survive", "never", "only", "first",
                "biggest", "dangerous", "deadly", "secret", "impossible", "challenge", "prize", "die", "risk", "every",
                "مليون", "مليار", "فلوس", "خطر", "سر", "أول", "آخر", "مستحيل", "تحدي", "جائزة", "أكبر", "أخطر"}
NUM_RE = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million|billion)\b|"
                    r"(واحد|اثنين|ثلاث|أربع|خمس|ست|سبع|ثمان|تسع|عشر|مية|مائة|ألف|مليون|مليار)")
TOKEN_RE = re.compile(r"[\w$%']+", re.U)


def _r(x, n=3):
    return None if x is None else round(float(x), n)


def cut_times(scenes: list) -> list[float]:
    return [float(s[0]) for s in scenes[1:]]


def pacing_curve(scenes: list, duration: float, bins: int = BINS) -> list[dict]:
    """Cuts per minute inside each 10 % slice of the runtime (measured from shot boundaries)."""
    cuts = cut_times(scenes)
    w = duration / bins if duration else 0.0
    out = []
    for k in range(bins):
        a, b = k * w, (k + 1) * w
        n = sum(1 for t in cuts if a <= t < b)
        out.append({"slice": f"{k * 10}-{(k + 1) * 10}%", "start_s": _r(a), "cuts": n,
                    "cuts_per_min": _r(n / (w / 60.0), 2) if w else 0.0})
    return out


def window_loudness(path: str | Path, start: float, dur: float) -> float | None:
    """Integrated loudness (LUFS) of one window, via FFmpeg's loudnorm analysis."""
    try:
        err = ff.run(["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", path, "-vn", "-af", "loudnorm=print_format=json",
                      "-f", "null", "-"], op="window-loudness")
        m = re.search(r'"input_i"\s*:\s*"([-\d.inf]+)"', err)
        v = float(m.group(1)) if m and "inf" not in m.group(1) else None
        return v
    except Exception:  # noqa: BLE001 - one missing window must not fail the profile
        return None


def _words(transcript: dict | None) -> list[dict]:
    if not transcript:
        return []
    out = []
    for s in transcript.get("segments") or []:
        ws = s.get("words") or []
        if ws:
            out += [{"start": float(w["start"]), "end": float(w["end"]), "word": str(w["word"])} for w in ws]
        elif s.get("text"):  # segment-level captions only: spread words evenly (marked in `word_timing`)
            toks = s["text"].split()
            d = (float(s["end"]) - float(s["start"])) / max(len(toks), 1)
            out += [{"start": float(s["start"]) + i * d, "end": float(s["start"]) + (i + 1) * d, "word": t, "spread": True}
                    for i, t in enumerate(toks)]
    return sorted(out, key=lambda w: w["start"])


def _merge(spans: list[tuple[float, float]], join: float = 0.3) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= join:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def text_markers(words: list[dict]) -> dict:
    toks = [t.lower() for w in words for t in TOKEN_RE.findall(w["word"])]
    raw = " ".join(w["word"] for w in words)
    return {"words": len(toks), "questions": raw.count("?") + raw.count("؟") + sum(1 for t in toks[:1] if t in QUESTION_WORDS),
            "question_words": sum(1 for t in toks if t in QUESTION_WORDS),
            "numbers": len(NUM_RE.findall(raw.lower())), "stakes_words": sum(1 for t in toks if t in STAKES_WORDS)}


def from_parts(*, duration: float, scenes: list, silences: list | None = None, transcript: dict | None = None,
               loudness: dict | None = None, window_lufs: dict | None = None, video: dict | None = None,
               has_audio: bool = True, keep_quote: bool = False, quality: dict | None = None) -> dict:
    """Pure function: measurements -> the `measured` block of a CreatorReferenceProfile. Testable without media."""
    duration = float(duration)
    scenes = [tuple(map(float, s)) for s in scenes if float(s[1]) > float(s[0])] or [(0.0, duration)]
    shots = [b - a for a, b in scenes]
    cuts = cut_times(scenes)
    mins = max(duration / 60.0, 1e-6)
    words = _words(transcript)
    speech = _merge([(w["start"], w["end"]) for w in words])
    speech_s = sum(b - a for a, b in speech)
    sil = [(float(a), float(b)) for a, b in (silences or [])]
    silence_s = sum(min(b, duration) - a for a, b in sil if b > a)
    # Non-speech, non-silent audio: an UPPER BOUND on music/effects share (no music classifier is deployed).
    other_audio = max(0.0, duration - speech_s - silence_s) if has_audio else 0.0
    onset = speech[0][0] if speech else None
    opening = {}
    for w in WINDOWS:
        ww = [x for x in words if x["start"] < w]
        opening[f"first_{int(w)}s"] = {
            "cuts": sum(1 for t in cuts if 0 < t < w),
            "speech_onset_s": _r(onset) if onset is not None and onset < w else None,
            "words": len(ww), "words_per_s": _r(len(ww) / w, 2),
            **{k: v for k, v in text_markers(ww).items() if k != "words"},
            "loudness_lufs": (window_lufs or {}).get(f"first_{int(w)}s"),
            "on_screen_text": "not_measured (no OCR model deployed)"}
    ow = [x for x in words if x["start"] < 15.0]
    if keep_quote and ow:
        opening["opening_phrase"] = " ".join(x["word"] for x in ow[:12])
    rest = (window_lufs or {}).get("rest")
    first5 = (window_lufs or {}).get("first_5s")
    m = {
        "duration_s": _r(duration), "video": video or {}, "quality": quality,
        "shots": len(shots), "cuts_per_min": _r(len(cuts) / mins, 2),
        "mean_shot_s": _r(statistics.fmean(shots)), "median_shot_s": _r(statistics.median(shots)),
        "shortest_shot_s": _r(min(shots)), "longest_shot_s": _r(max(shots)),
        "pacing_curve": pacing_curve(scenes, duration),
        "opening": opening,
        "loudness_jump_db": _r(first5 - rest, 2) if first5 is not None and rest is not None else None,
        "speech": {"available": bool(transcript), "words": len(words), "wpm": _r(len(words) / mins, 1),
                   "wpm_speaking": _r(len(words) / (speech_s / 60.0), 1) if speech_s else None,
                   "speech_share": _r(speech_s / duration) if duration else None, "time_to_first_speech_s": _r(onset),
                   "word_timing": "spread_from_segments" if any(w.get("spread") for w in words) else
                   ("whisper_words" if words else None),
                   "language": (transcript or {}).get("language"), **text_markers(words)},
        "audio": {"has_audio": has_audio, "silence_share": _r(silence_s / duration) if duration and has_audio else None,
                  "non_speech_audio_share_upper_bound": _r(other_audio / duration) if duration and transcript else None,
                  "integrated_lufs": (loudness or {}).get("lufs"), "lra": (loudness or {}).get("lra"),
                  "music_detection": "not_measured (no music classifier deployed; non-speech share is an upper bound)"},
    }
    return m


def measure_file(path: str | Path, *, transcribe: bool = True, transcript: dict | None = None, language: str | None = None,
                 scene_threshold: float = 0.3, keep_quote: bool = False) -> dict:
    """Run the measurements on a local file. Whisper runs under jobs.heavy_lock (shared machine)."""
    path = Path(path)
    info = ff.probe(path)
    dur = float(info["duration"])
    scenes = an.detect_scenes_ffmpeg(path, threshold=scene_threshold) if info.get("video") else [(0.0, dur)]
    video = {"w": info["video"]["width"], "h": info["video"]["height"], "fps": info["video"]["fps"]} if info.get("video") else {}
    has_audio = bool(info.get("audio"))
    quality = None
    if info.get("video"):
        try:
            q = an.visual_quality_ffmpeg(path)
            quality = {k: q[k] for k in ("sharpness", "brightness", "issues", "method")}
        except Exception:  # noqa: BLE001
            quality = None
    silences, loud, wl = [], None, {}
    if has_audio:
        silences = ff.detect_silence(path)
        try:
            loud = ff.loudness(path)
        except Exception:  # noqa: BLE001
            loud = None
        for w in (5.0, 15.0):
            if dur > w:
                wl[f"first_{int(w)}s"] = window_loudness(path, 0.0, w)
        if dur > 10.0:
            wl["rest"] = window_loudness(path, 5.0, max(1.0, dur - 5.0))
        if transcript is None and transcribe:
            from ... import jobs, llm
            from ...speech import asr
            with jobs.heavy_lock():
                try:
                    transcript = asr.transcribe(path, language)
                finally:
                    asr.unload()
                    try:
                        llm.unload()
                    except Exception:  # noqa: BLE001
                        pass
    return from_parts(duration=dur, scenes=scenes, silences=silences, transcript=transcript, loudness=loud,
                      window_lufs=wl, video=video, has_audio=has_audio, keep_quote=keep_quote,
                      quality=quality)
