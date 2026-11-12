"""Measured quality score (WS G, QSC-1): a weighted 0-100 score with a reason and a fix for every failed check.

Principle: a technically valid render is not a good video. Nothing here is labelled premium because rendering
completed; every check below is a real measurement on the rendered file (FFmpeg filters + numpy), on the timeline,
or on both. What cannot be measured without a model (subject consistency, generative artefacts, translation
accuracy, voice naturalness, storytelling) is listed in `not_measured` with the reason and is never counted as passed.

Thresholds are engineering defaults unless a standard is named (EBU R128 / ITU-R BS.1770 loudness, WCAG 2.x contrast,
EBU R95 safe areas, the Netflix Timed Text Style Guide for reading speed and line length). The score describes the
measured technical and editorial craft of one file; it is not a prediction of audience performance.

Public API
  score(path, tl=None, analysis=None, lang=None, platform=None, *, target=None, overrides=None, signoff=None) -> report
  fixes(report, tl) -> [op]                 failed checks -> concrete timeline operations
  apply_ops(tl, ops) -> (new_tl, applied, skipped)
  improve(tl, render_fn, max_rounds=2, **score_kw) -> record with every round's file, score and ops (no overwrite)
  table() -> the visible check/weight/threshold table (also rendered in docs/QUALITY.md)
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import numpy as np

from . import config, costguard
from .media import ff
from .timeline.render import clip_duration, clip_starts, total_duration

VERSION = 1
CATEGORIES = ("visual", "editing", "audio", "localization")
CATEGORY_WEIGHTS = {"visual": 0.30, "editing": 0.25, "audio": 0.30, "localization": 0.15}
CATEGORY_BAR = {"visual": 85.0, "editing": 85.0, "audio": 85.0, "localization": 85.0}   # premium needs every measured category >= bar
PREMIUM_SCORE, PASS_SCORE, FAIL_SCORE = 90.0, 75.0, 60.0
HARD_CAP, CRITICAL_CAP = 40.0, 59.0
LEVEL_SCORE = {"ok": 1.0, "warn": 0.5, "fail": 0.0}
GRADES = ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (0, "F"))
SPEECH_ROLES = ("narration", "dialogue")
SHORT_FORM = ("youtube_shorts", "tiktok", "instagram_reels", "snapchat_spotlight", "shorts")
DEFAULT_LUFS = -14.0          # common streaming playback reference (engineering default; EBU R128 broadcast is -23)


def _D(cat, sev, w, warn, fail, why, fix, hard=False, unit="", higher_is_better=False, std=None):
    return {"category": cat, "severity": sev, "weight": w, "warn": warn, "fail": fail, "why": why, "fix": fix, "hard": hard,
            "unit": unit, "higher_is_better": higher_is_better, "standard": std}


# The visible weight/threshold table. Severity: critical -> verdict fail; major -> at best needs_work when failed,
# never premium when warned; minor -> lowers the score only. `hard` failures cap the score at HARD_CAP.
CHECKS: dict[str, dict] = {
    # ------------------------------------------------------------------ visual
    "visual.decode": _D("visual", "critical", 5, 1, 1, "every frame and sample must decode", "rerender", True, "errors"),
    "visual.streams": _D("visual", "critical", 3, 1, 1, "a video must have a picture stream", "rerender", True),
    "visual.resolution": _D("visual", "major", 3, None, None, "delivery size must match the intended frame (or be at least 720p)", "reframe"),
    "visual.frame_rate": _D("visual", "minor", 1, 0.05, 0.5, "frame rate must match the timeline", "set-fps", unit="fps"),
    "visual.sharpness": _D("visual", "major", 3, 0.42, 0.55, "soft/out-of-focus picture (no-reference blur metric, Crete-Roffet 2007; 0 sharp .. 1 blurred)",
                           "replace-or-upscale-source"),
    "visual.blockiness": _D("visual", "minor", 2, 1.25, 1.5, "8x8 block-edge discontinuity ratio (compression blocking)", "raise-bitrate", unit="ratio"),
    "visual.banding": _D("visual", "minor", 1, 0.15, 0.35, "share of flat tiles with a 1-4 level step to a flat neighbour (false contouring proxy)",
                         "add-dither-or-raise-bitrate", unit="share"),
    "visual.flicker": _D("visual", "major", 2, 0.01, 0.03, "share of frames whose brightness oscillates (+/-) outside cuts and fades",
                         "deflicker-source", unit="share"),
    "visual.black_frames": _D("visual", "critical", 4, 0.5, 2.0, "unintended black picture (blackdetect); > 2 s is a hard fail", "cut-span", True, "s"),
    "visual.frozen_frames": _D("visual", "major", 3, 2.0, 4.0, "unintended frozen picture (freezedetect) outside cards and marked stills", "cut-span", unit="s"),
    "visual.gaps": _D("visual", "major", 2, 1, 2, "short black flashes between shots (unintended gaps)", "close-gap", unit="count"),
    "visual.continuity": _D("visual", "major", 2, 1, 2, "colour/brightness/level consistency across cuts (qa_continuity)", "match-grade", unit="flags"),
    "visual.repeated_imagery": _D("visual", "minor", 1, 0.15, 0.35, "share of shots whose picture repeats a non-adjacent shot (dHash)", "replace-repeated-shot",
                                  unit="share"),
    "visual.framing": _D("visual", "minor", 2, 0.04, 0.10, "unintended letterbox/pillarbox bars (cropdetect)", "reframe-cover", unit="share"),
    "visual.exposure": _D("visual", "major", 2, 0.08, 0.20, "share of crushed (<=4) or clipped (>=251) luma pixels on detailed frames", "grade-exposure",
                          unit="share"),
    # ------------------------------------------------------------------ editing
    "editing.pacing": _D("editing", "minor", 2, None, None, "mean/longest shot length vs the intended pacing", "repace", unit="s"),
    "editing.flash_cut": _D("editing", "major", 3, 1, 1, "shots shorter than 0.5 s read as mistakes unless marked intentional", "replace-flash-cut", unit="count"),
    "editing.clip_repetition": _D("editing", "minor", 1, 1, 0.25, "the same source span used twice", "replace-repeated-clip", unit="count"),
    "editing.dead_air": _D("editing", "major", 3, 4.0, 6.0, "programme silence (< -50 dBFS) longer than 4 s", "remove-dead-air", unit="s"),
    "editing.long_pause": _D("editing", "minor", 1, 2.0, 3.5, "pauses inside the programme longer than 2 s", "tighten-pauses", unit="s"),
    "editing.opening": _D("editing", "minor", 2, 3.0, 6.0, "sound or a visual change must start within the first seconds", "trim-head", unit="s"),
    "editing.ending": _D("editing", "minor", 1, None, None, "the programme must not stop mid-sound / mid-sentence; it needs an out", "add-out"),
    "editing.subtitle_overlap": _D("editing", "major", 3, 1, 1, "overlapping cues are drawn on top of each other", "retime-subtitles", unit="count"),
    "editing.subtitle_range": _D("editing", "major", 2, 1, 1, "cues with end <= start or outside the programme", "retime-subtitles", unit="count"),
    "editing.subtitle_sync": _D("editing", "minor", 2, 0.10, 0.25, "share of cues not aligned with speech", "retime-subtitles", unit="share"),
    "editing.caption_speed": _D("editing", "major", 2, 20.0, 25.0, "reading speed (characters per second)", "resegment-captions", unit="cps",
                                std="Netflix Timed Text Style Guide (20 cps adult, Latin)"),
    "editing.caption_line_length": _D("editing", "minor", 1, 42, 60, "characters per caption line", "resegment-captions", unit="chars",
                                      std="Netflix Timed Text Style Guide (42 chars, Latin)"),
    "editing.caption_contrast": _D("editing", "major", 3, 4.5, 3.0, "contrast of the burned caption text against the measured pixels behind it",
                                   "caption-contrast", unit="ratio", higher_is_better=True, std="WCAG 2.x contrast ratio (4.5:1 AA)"),
    "editing.caption_safe_area": _D("editing", "major", 2, None, None, "captions inside the title-safe / platform-safe area", "captions-safe-zone",
                                    std="EBU R95 title-safe (5% margins)"),
    # ------------------------------------------------------------------ audio
    "audio.stream": _D("audio", "critical", 3, 1, 1, "a programme needs a decodable audio stream", "rerender", True),
    "audio.loudness": _D("audio", "major", 4, 1.5, 3.0, "integrated loudness vs target", "loudness-target", unit="LU off",
                         std="EBU R128 / ITU-R BS.1770-4"),
    "audio.true_peak": _D("audio", "major", 3, -1.0, 0.0, "true peak (inter-sample)", "limiter", unit="dBTP", std="EBU R128 (-1 dBTP max)"),
    "audio.clipping": _D("audio", "critical", 4, 0.01, 3.0, "flat-top sample runs (>= 3 samples at >= -0.13 dBFS) per minute", "limiter", True, "runs/min"),
    "audio.loudness_range": _D("audio", "minor", 1, 15.0, 20.0, "loudness range (LRA); spoken content above ~15 LU needs riding", "compress", unit="LU",
                               std="EBU Tech 3342"),
    "audio.level_jumps": _D("audio", "minor", 2, 1, 3, "sudden > 10 dB momentary-level jumps not explained by a cut or narration boundary", "level-match",
                            unit="count"),
    "audio.noise_floor": _D("audio", "minor", 1, -60.0, -48.0, "noise floor in pauses", "denoise", unit="dBFS"),
    "audio.speech_music_balance": _D("audio", "major", 3, 10.0, 6.0, "speech stem minus (ducked) music stem loudness", "lower-music", unit="LU",
                                     higher_is_better=True),
    "audio.av_sync": _D("audio", "major", 3, 0.04, 0.12, "audio vs video stream start/duration offset", "rerender", unit="s"),
    "audio.head_tail_silence": _D("audio", "minor", 2, None, None, "silence at the head (> 1 s) or tail (> 2 s)", "trim-head-tail"),
    "audio.phase": _D("audio", "major", 2, None, None, "stereo correlation (< 0 cancels in mono) and channel balance", "fix-phase"),
    # ------------------------------------------------------------------ localization
    "localization.script": _D("localization", "major", 3, 0.02, 0.05, "share of cues in the wrong script (untranslated leftovers)", "retranslate", unit="share"),
    "localization.coverage": _D("localization", "major", 2, 0.90, 0.75, "share of source speech/cues covered by target cues", "retranslate", unit="share",
                                higher_is_better=True),
    "localization.reading_speed": _D("localization", "minor", 2, 0.10, 0.25, "share of cues above the language's reading speed", "resegment-captions",
                                     unit="share", std="Netflix Timed Text Style Guides (per language)"),
    "localization.bidi": _D("localization", "minor", 1, 1, 3, "RTL text with unbalanced or missing bidi controls", "fix-bidi", unit="count"),
    "localization.dub_overrun": _D("localization", "major", 3, 0.3, 1.0, "dubbed lines running past their slot or the programme", "retime-dub", unit="s"),
}

# Criteria the system cannot measure without a model or a person. `critical_when` names the condition that makes the
# gap block `premium`.
NOT_MEASURABLE = {
    "visual.subject_consistency": ("face/subject identity across shots needs a face or subject-embedding model", "generated_or_characters"),
    "visual.generative_artifacts": ("artefacts of generative video/image models need a learned detector or a human", "generated"),
    "visual.content_relevance": ("whether the picture matches what is said needs a vision-language model or a human", None),
    "localization.translation_accuracy": ("meaning-level translation accuracy needs a bilingual reviewer (or a model-based metric)", "localized"),
    "audio.voice_naturalness": ("naturalness of synthetic voices needs listening tests (MOS) or a learned predictor", "dubbed"),
    "editing.storytelling": ("story, argument and emotional arc are creative judgements for a human", None),
}

SCRIPTS = {"arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)), "hebrew": ((0x0590, 0x05FF),),
           "cyrillic": ((0x0400, 0x04FF),), "greek": ((0x0370, 0x03FF),), "devanagari": ((0x0900, 0x097F),), "thai": ((0x0E00, 0x0E7F),),
           "cjk": ((0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7AF)), "latin": ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F))}
LANG_SCRIPT = {"ar": "arabic", "fa": "arabic", "ur": "arabic", "he": "hebrew", "ru": "cyrillic", "uk": "cyrillic", "bg": "cyrillic", "sr": "cyrillic",
               "el": "greek", "hi": "devanagari", "mr": "devanagari", "ne": "devanagari", "th": "thai", "zh": "cjk", "ja": "cjk", "ko": "cjk"}
RTL = ("ar", "fa", "ur", "he")
CPS_BY_LANG = {"default": 20.0, "zh": 9.0, "ja": 4.0, "ko": 12.0}   # engineering defaults after the Netflix per-language guides
BIDI_OPEN, BIDI_ISO = "\u202a\u202b\u202d\u202e", "\u2066\u2067\u2068"
SUB_STYLE = {"size": 0.048, "margin_v": 0.07, "outline": 3, "primary": "&H00FFFFFF", "back": "&H96000000", "outline_colour": "&H00101010"}

FLASH_S = 0.5
SILENCE_DB = -50.0
CLIP_LEVEL = 0.985
AUDIO_SR = 48000
BLOCK_S = 0.05
THUMB_W, THUMB_H = 64, 36


# ====================================================================== configuration
def _config(overrides: dict | None) -> dict:
    cfg = {"checks": copy.deepcopy(CHECKS), "category_weights": dict(CATEGORY_WEIGHTS), "category_bar": dict(CATEGORY_BAR)}
    layers = []
    user = config.HOME / "quality.yaml"
    if user.exists():
        try:
            import yaml
            layers.append(yaml.safe_load(user.read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    if overrides:
        layers.append(overrides)
    for o in layers:
        for cid, vals in (o.get("checks") or {}).items():
            if cid in cfg["checks"]:
                cfg["checks"][cid].update({k: v for k, v in vals.items() if k in ("weight", "warn", "fail", "severity", "hard")})
        for k in ("category_weights", "category_bar"):
            cfg[k].update(o.get(k) or {})
    return cfg


def table() -> list[dict]:
    """The check table in display order (docs/QUALITY.md renders the same data)."""
    return [{"id": k, **{x: v[x] for x in ("category", "severity", "weight", "warn", "fail", "hard", "unit", "why", "fix", "standard")}}
            for k, v in CHECKS.items()]


# ====================================================================== low-level measurements
def _probe_streams(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)], capture_output=True, text=True)
    if p.returncode != 0:
        return {}
    return json.loads(p.stdout or "{}")


def _luma_pass(path: Path, fps: float) -> dict:
    """One streaming decode at 64x36 grey: per-frame mean, change vs previous/first frame, detail, 64-bit dHash."""
    costguard.authorize("ffmpeg", "quality-luma")
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-an", "-vf", f"scale={THUMB_W}:{THUMB_H}:flags=area,format=gray",
                             "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fsz = THUMB_W * THUMB_H
    means, diffs, first_d, stds, hashes = [], [], [], [], []
    prev = first = None
    buf = b""
    bits = (1 << np.arange(64, dtype=np.uint64)).astype(np.uint64)
    while True:
        chunk = proc.stdout.read(fsz * 128)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // fsz
        if not n:
            continue
        fr = np.frombuffer(buf[: n * fsz], dtype=np.uint8).reshape(n, THUMB_H, THUMB_W).astype(np.float32)
        buf = buf[n * fsz:]
        if first is None:
            first = fr[0]
        means.append(fr.mean(axis=(1, 2)))
        stds.append(fr.std(axis=(1, 2)))
        pr = np.concatenate([fr[:1] if prev is None else prev[None], fr[:-1]])
        diffs.append(np.abs(fr - pr).mean(axis=(1, 2)))
        first_d.append(np.abs(fr - first).mean(axis=(1, 2)))
        prev = fr[-1]
        small = fr.reshape(n, 4, 9, 8, 8).mean(axis=(2, 4)) if False else _thumb9x8(fr)
        d = (small[:, :, 1:] > small[:, :, :-1]).reshape(n, 64)
        hashes.append((d.astype(np.uint64) * bits).sum(axis=1).astype(np.uint64))
    proc.wait()
    cat = lambda xs, dt=np.float32: np.concatenate(xs) if xs else np.zeros(0, dtype=dt)
    return {"fps": fps, "mean": cat(means), "diff": cat(diffs), "first": cat(first_d), "std": cat(stds), "hash": cat(hashes, np.uint64)}


def _thumb9x8(fr: np.ndarray) -> np.ndarray:
    """(n,36,64) -> (n,8,9) by area averaging on an index grid (dHash input)."""
    ys = np.linspace(0, THUMB_H, 9).astype(int)
    xs = np.linspace(0, THUMB_W, 10).astype(int)
    out = np.empty((fr.shape[0], 8, 9), dtype=np.float32)
    for i in range(8):
        for j in range(9):
            out[:, i, j] = fr[:, ys[i]:ys[i + 1], xs[j]:xs[j + 1]].mean(axis=(1, 2))
    return out


def _cuts_from_luma(lp: dict) -> list[int]:
    d = lp["diff"]
    if len(d) < 3:
        return []
    k = 15
    pad = np.pad(d, k, mode="edge")
    med = np.array([np.median(pad[i:i + 2 * k + 1]) for i in range(len(d))])
    return [i for i in range(1, len(d)) if d[i] > 18 and d[i] > 3 * med[i] + 2]


def _audio_blocks(path: Path) -> dict | None:
    """Streaming stereo decode in 50 ms blocks: levels, correlation sums, flat-top clipping runs."""
    costguard.authorize("ffmpeg", "quality-audio")
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "2", "-ar", str(AUDIO_SR), "-f", "f32le", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bs = int(AUDIO_SR * BLOCK_S)
    bsz = bs * 2 * 4
    m_db, l_ms, r_ms, lr, n_clip_runs, n_near, n_total, peak = [], [], [], [], 0, 0, 0, 0.0
    buf = b""
    while True:
        chunk = proc.stdout.read(bsz * 40)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // bsz
        if not n:
            continue
        x = np.frombuffer(buf[: n * bsz], dtype=np.float32).reshape(n, bs, 2).astype(np.float64)
        buf = buf[n * bsz:]
        L, R = x[..., 0], x[..., 1]
        m_db.append(20 * np.log10(np.sqrt((((L + R) / 2) ** 2).mean(1)) + 1e-9))
        l_ms.append((L ** 2).mean(1))
        r_ms.append((R ** 2).mean(1))
        lr.append((L * R).mean(1))
        a = np.abs(x).max(axis=2).reshape(-1)
        peak = max(peak, float(a.max()))
        hot = a >= CLIP_LEVEL
        n_near += int(hot.sum())
        n_total += a.size
        if hot.any():
            dd = np.diff(np.concatenate([[0], hot.astype(np.int8), [0]]))
            n_clip_runs += int(((np.flatnonzero(dd == -1) - np.flatnonzero(dd == 1)) >= 3).sum())
    proc.wait()
    if not m_db:
        return None
    return {"db": np.concatenate(m_db), "l_ms": np.concatenate(l_ms), "r_ms": np.concatenate(r_ms), "lr": np.concatenate(lr),
            "clip_runs": n_clip_runs, "near_fs": n_near / max(n_total, 1), "peak": peak}


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    dd = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(dd == 1).tolist(), np.flatnonzero(dd == -1).tolist()))


def _frame(path: Path, t: float, w: int, h: int) -> np.ndarray | None:
    costguard.authorize("ffmpeg", "quality-frame")
    vf = ["-vf", f"scale={w}:{h}"]
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1", *vf,
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if p.returncode != 0 or len(p.stdout) < w * h * 3:
        return None
    return np.frombuffer(p.stdout[: w * h * 3], dtype=np.uint8).reshape(h, w, 3)


def _luma(rgb: np.ndarray) -> np.ndarray:
    f = rgb.astype(np.float32)
    return 0.2126 * f[..., 0] + 0.7152 * f[..., 1] + 0.0722 * f[..., 2]


def _box1d(a: np.ndarray, axis: int, k: int = 9) -> np.ndarray:
    c = np.cumsum(np.pad(a, [(k // 2, k // 2) if i == axis else (0, 0) for i in range(a.ndim)], mode="edge"), axis=axis)
    c = np.concatenate([np.zeros_like(np.take(c, [0], axis=axis)), c], axis=axis)
    n = a.shape[axis]
    return (np.take(c, range(k, k + n), axis=axis) - np.take(c, range(0, n), axis=axis)) / k


def blur_metric(y: np.ndarray) -> float:
    """No-reference blur (Crete-Roffet et al. 2007): 0 = sharp, 1 = fully blurred."""
    out = []
    for ax in (0, 1):
        b = _box1d(y, ax)
        dF = np.abs(np.diff(y, axis=ax))
        dB = np.abs(np.diff(b, axis=ax))
        sF = dF.sum()
        if sF <= 0:
            return 1.0
        out.append((sF - np.maximum(0, dF - dB).sum()) / sF)
    return float(max(out))


def blockiness(y: np.ndarray) -> float:
    """Mean |difference| across 8x8 block edges divided by the mean |difference| elsewhere (1.0 = no blocking)."""
    r = []
    for ax in (0, 1):
        d = np.abs(np.diff(y, axis=ax))
        idx = np.arange(d.shape[ax])
        edge = (idx % 8) == 7
        de = np.take(d, np.flatnonzero(edge), axis=ax).mean()
        dn = np.take(d, np.flatnonzero(~edge), axis=ax).mean()
        r.append(de / max(dn, 1e-3))
    return float(np.mean(r))


def banding(y: np.ndarray, t: int = 16) -> float:
    H, W = (y.shape[0] // t) * t, (y.shape[1] // t) * t
    if H < t * 2 or W < t * 2:
        return 0.0
    tiles = y[:H, :W].reshape(H // t, t, W // t, t)
    m, s = tiles.mean(axis=(1, 3)), tiles.std(axis=(1, 3))
    flat = s < 0.35
    pairs = band = 0
    for a, b, fa, fb in ((m[:, 1:], m[:, :-1], flat[:, 1:], flat[:, :-1]), (m[1:], m[:-1], flat[1:], flat[:-1])):
        both = fa & fb
        step = np.abs(a - b)
        pairs += int(both.sum())
        band += int((both & (step >= 0.75) & (step <= 4.5)).sum())
    return band / pairs if pairs > 8 else 0.0


def _ass_colour(s: str) -> tuple[tuple[int, int, int], float]:
    """&HAABBGGRR -> ((r,g,b), opacity 0..1)."""
    h = s.strip().lstrip("&Hh").rstrip("&").rjust(8, "0")
    a, b, g, r = (int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
    return (r, g, b), (255 - a) / 255


def _rel_lum(rgb) -> float:
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return float(0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]) if c.ndim == 1 else 0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]


def contrast_ratio(l1: float, l2: float) -> float:
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _sub_style(tl: dict) -> dict:
    try:
        from .timeline import subs
        base = dict(getattr(subs, "DEFAULT_STYLE", {}) or {})
    except Exception:
        base = {}
    return {**SUB_STYLE, **base, **(tl.get("subtitle_style") or {})}


def caption_box(tl: dict, text: str, W: int, H: int) -> dict:
    """Where the caption renderer puts a cue: font size, margins and estimated lines. Timelines with tl["captions"] use
    the same layout function as the burner (fx/captions._layout, incl. platform safe-zone margins); others the legacy
    subs.write_ass style."""
    try:
        from .fx import captions as fxc
        o = fxc.options(tl.get("captions"))
    except Exception:  # noqa: BLE001
        o = None
    if o is not None:
        L = fxc._layout(W, H, o, o["style"])
        size, ml, avail = L["size"], L["side"], W - 2 * L["side"]
        lines = sum(max(1, math.ceil(len(seg) * 0.55 * size / max(avail, 1))) for seg in text.split("\n"))
        if o["position"] == "bottom":
            bottom = H - L["mv"]
            top = max(0, int(bottom - lines * size * 1.2))
        elif o["position"] == "top":
            top = L["mv"]
            bottom = int(top + lines * size * 1.2)
        else:
            top = int(H / 2 - lines * size * 0.6)
            bottom = int(H / 2 + lines * size * 0.6)
        return {"size": size, "margin_v": H - bottom, "margin_lr": ml, "lines": lines, "top": top, "bottom": bottom,
                "style": {**SUB_STYLE, "caption_options": o}}  # colours: white text, dark outline (all fx caption styles)
    st = _sub_style(tl)
    size = int(H * st["size"]) if W >= H else int(W * st["size"] * 1.25)
    mv = int(H * st["margin_v"]) if W >= H else int(H * 0.16)
    ml = int(W * 0.06)
    avail = W - 2 * ml
    lines = sum(max(1, math.ceil(len(seg) * 0.55 * size / max(avail, 1))) for seg in text.split("\n"))
    bottom = H - mv
    top = max(0, int(bottom - lines * size * 1.2))
    return {"size": size, "margin_v": mv, "margin_lr": ml, "lines": lines, "top": top, "bottom": bottom, "style": st}


def script_of(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ch in text:
        o = ord(ch)
        for name, ranges in SCRIPTS.items():
            if any(a <= o <= b for a, b in ranges):
                counts[name] = counts.get(name, 0) + 1
                break
    return counts


# ====================================================================== the report builder
class _Run:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.checks: list[dict] = []
        self.nm: list[dict] = []

    def add(self, cid: str, value, level: str, detail: str, threshold=None, **extra) -> dict:
        d = self.cfg["checks"][cid]
        if threshold is None:
            threshold = {"warn": d["warn"], "fail": d["fail"]} if d["warn"] is not None else None
        c = {"id": cid, "category": d["category"], "ok": level == "ok", "level": level, "value": value, "threshold": threshold,
             "weight": d["weight"], "severity": d["severity"], "hard": bool(d["hard"]), "why": d["why"], "detail": detail,
             "fix": None if level == "ok" else d["fix"], **extra}
        self.checks.append(c)
        return c

    def lvl(self, cid: str, value: float, detail: str, **extra) -> dict:
        d = self.cfg["checks"][cid]
        w, f = d["warn"], d["fail"]
        if d["higher_is_better"]:
            level = "ok" if value >= w else "warn" if value >= f else "fail"
        else:
            level = "ok" if value < w else "warn" if value < f else "fail"
            if w == f:
                level = "ok" if value < w else "fail"
        return self.add(cid, _r(value), level, detail, **extra)

    def missing(self, cid: str, why: str, critical: bool = False) -> None:
        self.nm.append({"id": cid, "why": why, "critical": bool(critical)})


def _r(v):
    if isinstance(v, (float, np.floating)):
        return round(float(v), 4)
    if isinstance(v, np.integer):
        return int(v)
    return v


def _programme_analysis(tl: dict | None, analysis: dict | None) -> tuple[list[list[float]], list[dict]]:
    """Speech spans + words in programme time. `analysis` may describe the programme itself (has `segments`/`transcript`)
    or be {source path: analysis} for the timeline's sources."""
    if not analysis:
        return [], []
    single = "segments" in analysis or "transcript" in analysis
    if single:
        segs = analysis.get("segments") or (analysis.get("transcript") or {}).get("segments") or []
        words = [w for s in segs for w in (s.get("words") or [])]
        spans = sorted([float(s["start"]), float(s["end"])] for s in segs)
        return spans, words
    if tl is None:
        return [], []
    try:
        from . import growth
        spans, _, words = growth.speech_spans(tl, analysis)
        return spans, words
    except Exception:
        return [], []


def _intended_spans(tl: dict | None, kinds: tuple[str, ...]) -> list[tuple[float, float]]:
    """Programme spans where black/static picture is by design: colour cards, marked stills, fades."""
    if not tl:
        return []
    out = []
    for c, st in zip(tl["clips"], clip_starts(tl)):
        d = clip_duration(c)
        if "card" in kinds and (c["type"] == "color" and not _is_black_colour(c.get("color", "")) or c.get("still") or c.get("intended_black")):
            out.append((st, st + d))
        if "black" in kinds and c.get("intended_black"):
            out.append((st, st + d))
        if "fade" in kinds:
            fi, fo = float(c.get("fade_in", 0) or 0), float(c.get("fade_out", 0) or 0)
            if fi:
                out.append((st - 0.1, st + fi + 0.1))
            if fo:
                out.append((st + d - fo - 0.1, st + d + 0.1))
    return out


def _is_black_colour(col: str) -> bool:
    col = str(col).lower().replace("#", "0x")
    if col in ("black",):
        return True
    m = re.fullmatch(r"0x([0-9a-f]{6})", col)
    if not m:
        return False
    r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    return max(r, g, b) < 20


def _uncovered(span: tuple[float, float], allowed: list[tuple[float, float]]) -> float:
    s, e = span
    cov = 0.0
    for a, b in sorted(allowed):
        cov += max(0.0, min(e, b) - max(s, a))
    return max(0.0, (e - s) - cov)


def _shots_tl(tl: dict) -> list[tuple[float, float, list[int]]]:
    """Shots as (start, end, clip indexes); contiguous splits of one take are one shot."""
    out: list[tuple[float, float, list[int]]] = []
    for i, (c, st) in enumerate(zip(tl["clips"], clip_starts(tl))):
        d = clip_duration(c)
        if out and i:
            p = tl["clips"][i - 1]
            same = p["type"] == c["type"] == "video" and p.get("src") == c.get("src") and abs(c["in"] - p["out"]) <= 0.04
            if same:
                s0, _, idx = out[-1]
                out[-1] = (s0, st + d, idx + [i])
                continue
        out.append((st, st + d, [i]))
    return out


# ====================================================================== score
def score(path: str | Path, tl: dict | None = None, analysis: dict | None = None, lang: str | None = None, platform: str | None = None, *,
          target: dict | None = None, overrides: dict | None = None, signoff: dict | None = None) -> dict:
    """Measure one rendered file. `target` = {"width","height","fps","lufs"} overrides what tl/platform imply.
    `signoff` = {criterion id: "who/when"} records a human review for a not-measurable criterion."""
    path = Path(path)
    cfg = _config(overrides)
    run = _Run(cfg)
    target = dict(target or {})
    lang = lang or (tl or {}).get("lang") if (lang or tl) else lang
    prof = _platform_profile(platform)
    exp_w = target.get("width") or (tl or {}).get("width") or prof.get("width")
    exp_h = target.get("height") or (tl or {}).get("height") or prof.get("height")
    exp_fps = target.get("fps") or (tl or {}).get("fps")
    lufs_t = float(target.get("lufs") or prof.get("lufs") or DEFAULT_LUFS)
    measured: dict = {}

    if not path.exists() or path.stat().st_size < 1000:
        run.add("visual.decode", "missing", "fail", "missing or empty output")
        return _finish(run, cfg, path, measured, tl, lang, signoff)
    errs = ff.decode_check(path)
    run.add("visual.decode", len(errs), "ok" if not errs else "fail", "decodes cleanly" if not errs else f"{len(errs)} decoder errors: {errs[0][:120]}")
    info = ff.probe(path)
    raw = _probe_streams(path)
    v, a = info["video"], info["audio"]
    dur = float(info["duration"] or 0.0)
    measured["file"] = {"duration": round(dur, 3), "width": v and v["width"], "height": v and v["height"], "fps": v and v["fps"],
                        "size": info["size"]}
    run.add("visual.streams", 1 if v else 0, "ok" if v else "fail", "picture stream present" if v else "no video stream")
    run.add("audio.stream", 1 if a else 0, "ok" if a else "fail", "audio stream present" if a else "no audio stream")

    lp = None
    if v:
        lp = _luma_pass(path, v["fps"] or 30.0)
        _visual(run, path, v, dur, tl, lp, exp_w, exp_h, exp_fps, analysis)
    ab = _audio_blocks(path) if a else None
    if a and ab is not None:
        _audio(run, path, info, raw, dur, tl, ab, lufs_t, lp)
    elif a:
        run.missing("audio.*", "audio stream did not decode", critical=True)
    else:
        run.missing("audio.*", "no audio stream to measure", critical=not (tl or {}).get("allow_silent"))
    _editing(run, path, v, dur, tl, analysis, lp, ab, platform)
    if lang or (tl and tl.get("subtitles") and tl.get("lang")):
        _localization(run, tl, analysis, lang or tl.get("lang"), dur)
    return _finish(run, cfg, path, measured, tl, lang, signoff)


def _platform_profile(platform: str | None) -> dict:
    if not platform:
        return {}
    out: dict = {"short_form": platform in SHORT_FORM}
    try:
        from . import platforms
        p = platforms.get(platform) or {}
        for k_out, keys in (("width", ("width",)), ("height", ("height",)), ("lufs", ("lufs", "loudness_lufs", "target_lufs"))):
            for k in keys:
                val = p.get(k) if isinstance(p, dict) else None
                if val is None and isinstance(p, dict):
                    val = (p.get("video") or {}).get(k) or (p.get("audio") or {}).get(k)
                if val is not None:
                    out[k_out] = val
                    break
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------- visual
def _visual(run: _Run, path: Path, v: dict, dur: float, tl: dict | None, lp: dict, exp_w, exp_h, exp_fps, analysis) -> None:
    W, H = v["width"], v["height"]
    if exp_w and exp_h:
        ok = (W, H) == (int(exp_w), int(exp_h))
        run.add("visual.resolution", f"{W}x{H}", "ok" if ok else "fail", f"{W}x{H}, intended {exp_w}x{exp_h}", threshold=f"{exp_w}x{exp_h}")
    else:
        ok = min(W, H) >= 720
        run.add("visual.resolution", f"{W}x{H}", "ok" if ok else "warn", f"{W}x{H}; no intended size given, minimum 720p short side", threshold=">=720p")
    if exp_fps:
        run.lvl("visual.frame_rate", abs(v["fps"] - float(exp_fps)), f"{v['fps']} fps, intended {exp_fps}")
    else:
        run.missing("visual.frame_rate", "no intended frame rate (pass tl or target)")

    # sampled frames at native size (cap 1920 wide)
    sw, sh = (W, H) if W <= 1920 else (1920, int(H * 1920 / W) // 2 * 2)
    n = int(min(16, max(6, dur / 2)))
    ys, detail_ys = [], []
    for i in range(n):
        fr = _frame(path, (i + 0.5) * dur / n, sw, sh)
        if fr is None:
            continue
        y = _luma(fr)
        ys.append(y)
        if y.std() >= 8:
            detail_ys.append(y)
    if detail_ys:
        blur = float(np.median([blur_metric(y) for y in detail_ys]))
        run.lvl("visual.sharpness", blur, f"median blur {blur:.2f} over {len(detail_ys)} detailed frames")
        blk = float(np.median([blockiness(y) for y in detail_ys]))
        run.lvl("visual.blockiness", blk, f"block-edge ratio {blk:.2f}")
        expo = float(np.mean([((y <= 4) | (y >= 251)).mean() for y in detail_ys]))
        run.lvl("visual.exposure", expo, f"{expo:.1%} crushed/clipped pixels on average")
    else:
        for cid in ("visual.sharpness", "visual.blockiness", "visual.exposure"):
            run.missing(cid, "no frame with enough detail to judge (flat cards only)")
    if ys:
        bnd = float(np.median([banding(y) for y in ys]))
        run.lvl("visual.banding", bnd, f"{bnd:.0%} of flat tile pairs show a small step")

    fps = lp["fps"] or 30.0
    cuts = _cuts_from_luma(lp)
    if tl:
        cut_t = [s for s, _, _ in _shots_tl(tl)][1:]
        cut_f = sorted({int(round(t * fps)) for t in cut_t} | set(cuts))
    else:
        cut_f = cuts
    near = np.zeros(len(lp["mean"]), dtype=bool)
    for f in cut_f:
        near[max(0, f - 2): f + 3] = True
    for s, e in _intended_spans(tl, ("fade",)):
        near[max(0, int(s * fps)): int(e * fps) + 1] = True
    dm = np.diff(lp["mean"]) if len(lp["mean"]) > 1 else np.zeros(0)
    osc = 0
    for i in range(1, len(dm)):
        if near[i] or near[i + 1]:
            continue
        if abs(dm[i - 1]) > 2.5 and abs(dm[i]) > 2.5 and np.sign(dm[i - 1]) != np.sign(dm[i]):
            osc += 1
    fl = osc / max(len(dm), 1)
    run.lvl("visual.flicker", fl, f"{osc} oscillating frame(s) of {len(dm)}")

    # black frames (blackdetect) - long and short
    blacks = ff.detect_black(path, 0.04)
    allowed_black = _intended_spans(tl, ("black", "fade")) + [(-1.0, 0.25), (dur - 0.25, dur + 1.0)]
    long_black = [(s, e) for s, e in blacks if e - s >= 0.5]
    unint = sum(_uncovered(sp, allowed_black) for sp in long_black)
    c = run.lvl("visual.black_frames", unint, f"{unint:.2f}s unintended black" + (f" at {[(round(s, 2), round(e, 2)) for s, e in long_black[:3]]}" if long_black else ""),
                spans=[[round(s, 3), round(e, 3)] for s, e in long_black])
    if c["level"] == "fail" and c["value"] >= run.cfg["checks"]["visual.black_frames"]["fail"]:
        c["hard_reason"] = f"> {run.cfg['checks']['visual.black_frames']['fail']} s of black"
    gaps = [(s, e) for s, e in blacks if e - s < 0.5 and s > 0.25 and e < dur - 0.25 and _uncovered((s, e), allowed_black) > 0.02]
    run.lvl("visual.gaps", len(gaps), f"{len(gaps)} short black flash(es)" + (f" at {[round(s, 2) for s, _ in gaps[:4]]}" if gaps else ""),
            spans=[[round(s, 3), round(e, 3)] for s, e in gaps])

    # frozen frames (freezedetect) outside cards / marked stills
    frozen = ff.detect_freeze(path, 1.0)
    allowed_static = _intended_spans(tl, ("card",)) + [(s, e) for s, e in long_black]
    fz = [(s, e, _uncovered((s, e), allowed_static)) for s, e in frozen]
    worst = max((u for _, _, u in fz), default=0.0)
    run.lvl("visual.frozen_frames", worst, f"longest unintended freeze {worst:.2f}s" + (f"; frozen spans {[(round(s, 2), round(e, 2)) for s, e, _ in fz[:3]]}" if fz else ""),
            spans=[[round(s, 3), round(e, 3)] for s, e, u in fz if u >= 1.0])

    # continuity across cuts (scene-to-scene) - needs the timeline
    if tl and len(tl.get("clips", [])) > 1:
        try:
            from .qa_continuity import continuity_review
            an_map = analysis if analysis and not ("segments" in analysis or "transcript" in analysis) else None
            rv = continuity_review(path, tl, an_map)
            bad = [c for c in rv["checks"] if not c["ok"] and c.get("fix") in ("match-grade", "level-match-cut")]
            majors = [c for c in bad if c["severity"] == "major"]
            run.add("visual.continuity", len(bad), "ok" if not bad else "fail" if len(majors) >= 2 else "warn",
                    "; ".join(f"{c['check']}: {c['detail']}" for c in bad[:3]) or "no unexplained colour/brightness/level jump at cuts",
                    items=[{k: c.get(k) for k in ("check", "detail", "fix", "t")} for c in bad])
        except Exception as e:  # pragma: no cover - reported, never silently passed
            run.missing("visual.continuity", f"continuity review failed: {e}")
    else:
        run.missing("visual.continuity", "needs the timeline (cut positions and sources)")

    # repeated imagery across non-adjacent shots (dHash of each shot's middle frame)
    bounds = [0] + sorted(set(cut_f)) + [len(lp["mean"])]
    mids = [(a + b) // 2 for a, b in zip(bounds, bounds[1:]) if b - a >= 3]
    mids = [m for m in mids if m < len(lp["hash"]) and lp["std"][m] >= 6][:60]
    rep = set()
    for i in range(len(mids)):
        for j in range(i + 2, len(mids)):
            if bin(int(lp["hash"][mids[i]]) ^ int(lp["hash"][mids[j]])).count("1") <= 5:
                rep.add(j)
    share = len(rep) / max(len(mids), 1)
    if len(mids) >= 3:
        run.lvl("visual.repeated_imagery", share, f"{len(rep)} of {len(mids)} shots repeat earlier imagery")
    else:
        run.add("visual.repeated_imagery", 0.0, "ok", f"{len(mids)} detailed shot(s); nothing to compare")

    # letterbox / pillarbox
    try:
        err = ff.run(["-i", path, "-an", "-vf", "fps=2,cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"], op="quality-cropdetect")
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", err)
        if m:
            cw, ch = int(m[-1][0]), int(m[-1][1])
            bars = max((H - ch) / H, (W - cw) / W)
            intended = bool(tl and (tl.get("framing") in ("letterbox", "pillarbox") or any(c.get("fit") in ("contain", "blur") for c in tl["clips"])))
            c = run.lvl("visual.framing", 0.0 if intended else bars, f"content box {cw}x{ch} in {W}x{H}" + (" (bars intended by fit=contain)" if intended and bars > 0.04 else ""))
            c["measured_bars"] = round(bars, 4)
        else:
            run.missing("visual.framing", "cropdetect printed nothing (all-black picture?)")
    except ff.FFError as e:
        run.missing("visual.framing", f"cropdetect failed: {str(e)[:80]}")


# ---------------------------------------------------------------------- audio
def _audio(run: _Run, path: Path, info: dict, raw: dict, dur: float, tl: dict | None, ab: dict, lufs_t: float, lp: dict | None) -> None:
    try:
        L = ff.loudness(path)
    except ff.FFError:
        L = {"lufs": -99.0, "true_peak": -99.0, "lra": 0.0}
    silent_ok = bool((tl or {}).get("allow_silent")) and L["lufs"] <= -60
    if silent_ok:
        run.add("audio.loudness", L["lufs"], "ok", "intentionally silent programme")
    else:
        run.lvl("audio.loudness", abs(L["lufs"] - lufs_t), f"{L['lufs']} LUFS integrated, target {lufs_t} (+/-1.5 LU)", measured_lufs=L["lufs"], target_lufs=lufs_t)
    run.lvl("audio.true_peak", L["true_peak"], f"{L['true_peak']} dBTP (max -1.0)")
    per_min = ab["clip_runs"] / max(dur / 60.0, 1e-3)
    run.lvl("audio.clipping", per_min, f"{ab['clip_runs']} flat-top run(s), {ab['near_fs']:.3%} samples at full scale, sample peak {ab['peak']:.3f}")
    run.lvl("audio.loudness_range", L["lra"], f"LRA {L['lra']} LU")

    db = ab["db"]
    nb = len(db)
    loud = db[db > -70]
    speech_lvl = float(np.percentile(loud, 90)) if len(loud) else -99.0
    silent = db < SILENCE_DB
    sruns = [(s * BLOCK_S, e * BLOCK_S) for s, e in _runs(silent)]
    # head / tail silence
    head = sruns[0][1] if sruns and sruns[0][0] <= 0.0 else 0.0
    tail = (nb * BLOCK_S - sruns[-1][0]) if sruns and sruns[-1][1] >= nb else 0.0
    if speech_lvl <= SILENCE_DB:
        run.add("audio.head_tail_silence", None, "ok" if silent_ok else "fail", "no programme sound at all")
    else:
        lvl = "ok" if head <= 1.0 and tail <= 2.0 else "warn" if head <= 2.0 and tail <= 4.0 else "fail"
        run.add("audio.head_tail_silence", round(max(head, tail), 3), lvl, f"head {head:.2f}s, tail {tail:.2f}s of silence",
                threshold={"head": 1.0, "tail": 2.0}, head=round(head, 3), tail=round(tail, 3))
    # momentary-level jumps (400 ms windows), excluding cuts and narration boundaries
    w = int(0.4 / BLOCK_S)
    k = nb // w
    if k > 2:
        e = 10 ** (db[: k * w].reshape(k, w) / 10)
        mdb = 10 * np.log10(e.mean(axis=1) + 1e-12)
        bounds = []
        if tl:
            bounds += [s for s, _, _ in _shots_tl(tl)][1:]
            for a in tl.get("audio", []):
                s0 = float(a.get("start", 0))
                bounds += [s0, s0 + float(a.get("duration") or 0)]
        elif lp is not None:
            bounds += [f / (lp["fps"] or 30) for f in _cuts_from_luma(lp)]
        jumps = [i * 0.4 for i in range(1, k) if abs(mdb[i] - mdb[i - 1]) > 10 and min(mdb[i], mdb[i - 1]) > -40
                 and not any(abs(i * 0.4 - b) < 0.6 for b in bounds)]
        run.lvl("audio.level_jumps", len(jumps), f"{len(jumps)} unexplained >10 dB jump(s)" + (f" at {[round(t, 1) for t in jumps[:4]]}s" if jumps else ""))
    # noise floor in pauses (blocks >= 25 dB below the speech level)
    gap = db[(db < speech_lvl - 25) & (db > -120)]
    if len(gap) >= int(0.5 / BLOCK_S) and speech_lvl > SILENCE_DB:
        floor = float(np.median(gap))
        run.lvl("audio.noise_floor", floor, f"median {floor:.1f} dBFS over {len(gap) * BLOCK_S:.1f}s of pauses")
    else:
        run.add("audio.noise_floor", None, "ok", "no pauses long enough to measure a noise floor")
    # A/V sync from stream start times and durations
    vs = [s for s in raw.get("streams", []) if s.get("codec_type") == "video" and (s.get("disposition") or {}).get("attached_pic") != 1]
    as_ = [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"]
    if vs and as_:
        st_d = abs(float(vs[0].get("start_time", 0) or 0) - float(as_[0].get("start_time", 0) or 0))
        du_d = abs(float(info["video"]["duration"]) - float(info["audio"]["duration"]))
        off = max(st_d, du_d / 2)
        run.lvl("audio.av_sync", off, f"start offset {st_d:.3f}s, stream length difference {du_d:.3f}s", start_offset=round(st_d, 4),
                length_delta=round(du_d, 4))
    # stereo phase / balance
    act = db > -50
    if act.sum() > 4 and info["audio"].get("channels", 2) >= 2:
        lrs, lls, rrs = ab["lr"][act].sum(), ab["l_ms"][act].sum(), ab["r_ms"][act].sum()
        corr = float(lrs / math.sqrt(max(lls * rrs, 1e-18)))
        bal = abs(10 * math.log10((lls + 1e-12) / (rrs + 1e-12)))
        lvl = "fail" if corr < -0.3 or bal > 20 else "warn" if corr < 0.0 or bal > 6 else "ok"
        run.add("audio.phase", round(corr, 3), lvl, f"stereo correlation {corr:+.2f}, L/R balance {bal:.1f} dB", threshold={"corr_min": 0.0, "balance_db": 6},
                balance_db=round(bal, 2))
    elif info["audio"].get("channels", 2) < 2:
        run.add("audio.phase", None, "ok", "mono programme")
    # speech vs music balance from per-role stems
    _speech_music(run, tl, dur)


def _stem_lufs(inputs: list, fc: list[str], label: str) -> float | None:
    try:
        err = ff.run([*inputs, "-filter_complex", ";".join(fc + [f"[{label}]loudnorm=print_format=json[o]"]), "-map", "[o]", "-f", "null", "-"],
                     op="quality-stem")
    except ff.FFError:
        return None
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        return None
    x = json.loads(m.group(0))["input_i"]
    return None if x in ("-inf", "inf") else float(x)


def _speech_music(run: _Run, tl: dict | None, dur: float) -> None:
    if not tl:
        run.missing("audio.speech_music_balance", "needs the timeline's audio roles")
        return
    items = [a for a in tl.get("audio", []) if a.get("src") and Path(a["src"]).exists()]
    speech = [a for a in items if a.get("role", "narration") in SPEECH_ROLES]
    music_items = [a for a in items if a.get("role") == "music"]
    m = tl.get("music") or {}
    has_bed = bool(m.get("src") and Path(m["src"]).exists())
    if not (has_bed or music_items):
        run.add("audio.speech_music_balance", None, "ok", "no music in the timeline")
        return
    if not speech:
        run.missing("audio.speech_music_balance", "music present but no narration/dialogue item to compare (source-clip speech has no role)")
        return
    inputs, fc, lab = [], [], []
    for i, a in enumerate(speech):
        inputs += ["-i", a["src"]]
        fc.append(f"[{i}:a]aresample={config.AUDIO_RATE},aformat=channel_layouts=stereo,volume={a.get('volume', 1.0)},"
                  f"adelay={int(float(a.get('start', 0)) * 1000)}:all=1,apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}[s{i}]")
        lab.append(f"[s{i}]")
    fc.append(f"{''.join(lab)}amix=inputs={len(lab)}:normalize=0:duration=longest[sp]" if len(lab) > 1 else f"{lab[0]}anull[sp]")
    sp_l = _stem_lufs(inputs, list(fc), "sp")
    mins, mfc, mlab = list(inputs), list(fc), []
    n = len(speech)
    if has_bed:
        mins += ["-stream_loop", "-1", "-i", m["src"]]
        mfc.append(f"[{n}:a]aresample={config.AUDIO_RATE},aformat=channel_layouts=stereo,volume={m.get('volume', 0.5)},atrim=0:{dur:.3f}[bed]")
        if m.get("duck", True):
            mfc.append("[sp]asplit[spa][spb]")
            mfc.append("[bed][spb]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=450:makeup=1[mb]")
            mfc.append("[spa]anullsink")
            mlab.append("[mb]")
        else:
            mfc.append("[sp]anullsink")
            mlab.append("[bed]")
        n += 1
    else:
        mfc.append("[sp]anullsink")
    for a in music_items:
        mins += ["-i", a["src"]]
        mfc.append(f"[{n}:a]aresample={config.AUDIO_RATE},aformat=channel_layouts=stereo,volume={a.get('volume', 1.0)},"
                   f"adelay={int(float(a.get('start', 0)) * 1000)}:all=1[m{n}]")
        mlab.append(f"[m{n}]")
        n += 1
    mfc.append(f"{''.join(mlab)}amix=inputs={len(mlab)}:normalize=0:duration=longest[mu]" if len(mlab) > 1 else f"{mlab[0]}anull[mu]")
    mu_l = _stem_lufs(mins, mfc, "mu")
    if sp_l is None or mu_l is None:
        run.missing("audio.speech_music_balance", "a stem could not be measured")
        return
    run.lvl("audio.speech_music_balance", sp_l - mu_l, f"speech stem {sp_l:.1f} LUFS, music stem {mu_l:.1f} LUFS" + (" (ducked as rendered)" if has_bed and m.get("duck", True) else ""),
            speech_lufs=sp_l, music_lufs=mu_l)


# ---------------------------------------------------------------------- editing
def _editing(run: _Run, path: Path, v: dict | None, dur: float, tl: dict | None, analysis, lp: dict | None, ab: dict | None, platform: str | None) -> None:
    short_form = bool(platform in SHORT_FORM) or bool(tl and tl["height"] > tl["width"])
    # shots
    if tl:
        shots = [(s, e) for s, e, _ in _shots_tl(tl)]
        src = "timeline"
    elif lp is not None and len(lp["mean"]):
        fps = lp["fps"] or 30.0
        b = [0] + _cuts_from_luma(lp) + [len(lp["mean"])]
        shots = [(x / fps, y / fps) for x, y in zip(b, b[1:])]
        src = "scene detection"
    else:
        shots, src = [], "none"
    lens = [e - s for s, e in shots]
    if len(lens) >= 2:
        pace = (tl or {}).get("pacing") or ({"min_mean": 1.0, "max_mean": 5.0, "max_shot": 10.0} if short_form else {"min_mean": 1.5, "max_mean": 15.0, "max_shot": 45.0})
        mean, longest = float(np.mean(lens)), max(lens)
        lvl = "ok" if pace["min_mean"] <= mean <= pace["max_mean"] and longest <= pace["max_shot"] else "fail" if longest > 2 * pace["max_shot"] else "warn"
        run.add("editing.pacing", round(mean, 3), lvl, f"{len(lens)} shots ({src}), mean {mean:.1f}s, longest {longest:.1f}s", threshold=pace)
        flash_min = float((tl or {}).get("flash_min_s", FLASH_S if tl else 0.3))
        flashes = []
        if tl:
            for s, e, idx in _shots_tl(tl):
                c = tl["clips"][idx[0]]
                if e - s < flash_min and not c.get("intentional_flash") and not any((x.get("type") == "flash") for x in c.get("effects") or []):
                    flashes.append({"t": round(s, 3), "dur": round(e - s, 3), "clip": c.get("id")})
        else:
            flashes = [{"t": round(s, 3), "dur": round(e - s, 3)} for s, e in shots[1:-1] if e - s < flash_min]
        run.lvl("editing.flash_cut", len(flashes), f"{len(flashes)} shot(s) under {flash_min}s" + (f" at {[f['t'] for f in flashes[:4]]}" if flashes else ""),
                items=flashes)
    else:
        run.add("editing.pacing", round(lens[0], 3) if lens else None, "ok", "single shot")
        run.add("editing.flash_cut", 0, "ok", "single shot")
    if tl:
        rep = 0
        vids = [(i, c) for i, c in enumerate(tl["clips"]) if c["type"] == "video"]
        for x in range(len(vids)):
            for y in range(x + 1, len(vids)):
                (i, a), (j, b) = vids[x], vids[y]
                if a["src"] == b["src"] and j != i + 1:
                    ov = min(a["out"], b["out"]) - max(a["in"], b["in"])
                    if ov > 0.5 * min(a["out"] - a["in"], b["out"] - b["in"]):
                        rep += 1
        imgs = [(i, c["src"]) for i, c in enumerate(tl["clips"]) if c["type"] == "image"]
        rep += sum(1 for x, (i, s) in enumerate(imgs) for (j, s2) in imgs[x + 1:] if s == s2 and j != i + 1)
        n = max(len(tl["clips"]), 1)
        lvl = "ok" if rep == 0 else "fail" if rep / n >= 0.25 else "warn"
        run.add("editing.clip_repetition", rep, lvl, f"{rep} repeated source span(s) in {n} clips", threshold={"warn": 1, "fail_share": 0.25})
    # dead air / long pauses / opening / ending from the audio blocks
    if ab is not None:
        db = ab["db"]
        nb = len(db)
        sr = [(s * BLOCK_S, e * BLOCK_S) for s, e in _runs(db < SILENCE_DB)]
        allow_silent = bool((tl or {}).get("allow_silent"))
        longest_sil = max((e - s for s, e in sr), default=0.0)
        if allow_silent and longest_sil >= nb * BLOCK_S - 0.1:
            run.add("editing.dead_air", 0.0, "ok", "intentionally silent programme")
        else:
            c = run.lvl("editing.dead_air", longest_sil, f"longest silence {longest_sil:.2f}s" + (f" at {[(round(s, 2), round(e, 2)) for s, e in sr if e - s >= 4.0][:3]}" if longest_sil >= 4 else ""),
                        spans=[[round(s, 3), round(e, 3)] for s, e in sr if e - s >= run.cfg["checks"]["editing.dead_air"]["warn"]])
        loud = db[db > -70]
        speech_lvl = float(np.percentile(loud, 90)) if len(loud) else -99.0
        inner = [(s, e) for s, e in _runs(db < max(SILENCE_DB, speech_lvl - 30))]
        inner = [(s * BLOCK_S, e * BLOCK_S) for s, e in inner if s > 0 and e < nb]
        lp_max = max((e - s for s, e in inner), default=0.0)
        if speech_lvl > SILENCE_DB:
            run.lvl("editing.long_pause", lp_max, f"longest inner pause {lp_max:.2f}s", spans=[[round(s, 3), round(e, 3)] for s, e in inner if e - s >= 2.0])
        on = np.flatnonzero(db > max(-40.0, speech_lvl - 20))
        sound_t = float(on[0] * BLOCK_S) if len(on) else None
        vis_t = None
        if lp is not None and len(lp["first"]):
            ch = np.flatnonzero(lp["first"] > 10)
            vis_t = float(ch[0] / (lp["fps"] or 30)) if len(ch) else None
        if tl and len(tl["clips"]) > 1:
            vis_t = min(x for x in (vis_t, _shots_tl(tl)[1][0] if len(_shots_tl(tl)) > 1 else None, 1e9) if x is not None)
        opening = min(x for x in (sound_t, vis_t, 1e9) if x is not None)
        run.lvl("editing.opening", opening if opening < 1e8 else dur, f"first sound at {sound_t if sound_t is not None else 'none'}s, first visual change at "
                f"{round(vis_t, 2) if vis_t is not None and vis_t < 1e8 else 'none'}s")
        tail_db = float(10 * np.log10(np.mean(10 ** (db[-max(1, int(0.25 / BLOCK_S)):] / 10)) + 1e-12)) if nb else -99.0
        words = _programme_analysis(tl, analysis)[1]
        mid_sentence = bool(words and float(words[-1]["end"]) > dur - 0.1)
        abrupt = speech_lvl > SILENCE_DB and tail_db > speech_lvl - 8
        lvl = "fail" if mid_sentence else "warn" if abrupt else "ok"
        run.add("editing.ending", round(tail_db, 2), lvl, f"last 0.25 s at {tail_db:.1f} dBFS vs programme {speech_lvl:.1f} dBFS"
                + ("; last word ends at the cut" if mid_sentence else "; sound runs to the last frame (no out)" if abrupt else "; has an out"),
                threshold={"tail_below_programme_db": 8})
    # subtitles / captions
    cues = sorted((tl or {}).get("subtitles") or [], key=lambda q: q["start"])
    if cues:
        _captions(run, path, v, dur, tl, cues, analysis, platform)
    elif tl is not None:
        spans, _ = _programme_analysis(tl, analysis)
        if spans:
            run.missing("editing.subtitle_sync", "speech present but the timeline has no captions")


def _captions(run: _Run, path: Path, v: dict | None, dur: float, tl: dict, cues: list[dict], analysis, platform: str | None) -> None:
    over = [round(b["start"], 3) for a, b in zip(cues, cues[1:]) if b["start"] < a["end"] - 0.02]
    run.lvl("editing.subtitle_overlap", len(over), f"{len(over)} overlapping cue(s)" + (f", first at {over[0]}s" if over else ""), items=over)
    bad = [round(q["start"], 3) for q in cues if q["end"] <= q["start"] or q["start"] < -0.01 or q["end"] > dur + 0.3]
    run.lvl("editing.subtitle_range", len(bad), f"{len(bad)} cue(s) empty or outside the {dur:.2f}s programme", items=bad)
    spans, _ = _programme_analysis(tl, analysis)
    if spans:
        mis = 0
        for q in cues:
            d = max(q["end"] - q["start"], 1e-3)
            ov = sum(max(0.0, min(q["end"], b) - max(q["start"], a)) for a, b in spans)
            mis += ov / d < 0.5
        run.lvl("editing.subtitle_sync", mis / len(cues), f"{mis} of {len(cues)} cues overlap speech by less than half their length")
    else:
        run.missing("editing.subtitle_sync", "no transcript/analysis to align cues with speech")
    cps = [len(q["text"].replace("\n", "")) / max(q["end"] - q["start"], 0.01) for q in cues]
    p90 = float(np.percentile(cps, 90))
    run.lvl("editing.caption_speed", p90, f"90th percentile {p90:.1f} chars/s over {len(cues)} cues (max {max(cps):.1f})",
            items=[round(q["start"], 3) for q, c in zip(cues, cps) if c > run.cfg["checks"]["editing.caption_speed"]["warn"]])
    W, H = (v["width"], v["height"]) if v else (tl["width"], tl["height"])
    lines = []
    for q in cues:
        box = caption_box(tl, q["text"], W, H)
        per_line = max(len(seg) for seg in q["text"].split("\n")) if box["lines"] <= 1 else math.ceil(len(q["text"]) / box["lines"])
        lines.append(per_line)
    run.lvl("editing.caption_line_length", max(lines), f"longest line ~{max(lines)} chars")
    # safe area
    box = caption_box(tl, max((q["text"] for q in cues), key=len), W, H)
    sz = None
    try:
        from . import platforms
        if platform:
            sz = platforms.safe_zone(platform, W, H)
    except Exception:
        sz = None
    need_b = int((sz or {}).get("bottom", 0)) or int(H * 0.05)
    need_s = int((sz or {}).get("left", 0)) or int(W * 0.05)
    top_lim = int((sz or {}).get("top", 0)) or int(H * 0.05)
    bm = H - box["bottom"]
    ok = bm >= need_b and box["margin_lr"] >= need_s and box["top"] >= top_lim
    run.add("editing.caption_safe_area", bm, "ok" if ok else "fail", f"caption bottom margin {bm}px (need {need_b}), side margin {box['margin_lr']}px (need {need_s}), "
            f"top of block {box['top']}px" + (f" [{platform} safe zone]" if sz else " [title-safe 5%]"), threshold={"bottom": need_b, "side": need_s, "top": top_lim},
            needed_margin_v=round(need_b / H + 0.01, 3))
    # measured contrast of burned captions
    if not tl.get("burn_subtitles", True):
        run.add("editing.caption_contrast", None, "ok", "captions are delivered as a sidecar, not burned in")
        return
    st = box["style"]
    (tr, tg, tb), _ = _ass_colour(str(st["primary"]))
    (orr, og, ob), oa = _ass_colour(str(st.get("outline_colour", SUB_STYLE["outline_colour"])))
    t_l = _rel_lum((tr, tg, tb))
    o_l = _rel_lum((orr, og, ob))
    outline_px = float(st.get("outline", 0) or 0)
    sample = cues if len(cues) <= 8 else [cues[int(i * len(cues) / 8)] for i in range(8)]
    vals, pix = [], []
    for q in sample:
        bx = caption_box(tl, q["text"], W, H)
        fr = _frame(path, (q["start"] + q["end"]) / 2, W, H) if v else None
        if fr is None:
            continue
        x0, x1 = int(W * 0.2), int(W * 0.8)
        band = fr[bx["top"]: bx["bottom"], x0:x1].reshape(-1, 3)
        if not len(band):
            continue
        bg = np.median(band, axis=0)
        bg_l = _rel_lum(bg)
        direct = contrast_ratio(t_l, bg_l)
        via_outline = contrast_ratio(t_l, o_l) if outline_px >= 2 and oa > 0.5 else 0.0
        vals.append(max(direct, via_outline))
        lum = _rel_lum(band.astype(np.float64))
        pix.append(contrast_ratio(float(np.percentile(lum, 99.5)), float(np.percentile(lum, 0.5))))
    if not vals:
        run.missing("editing.caption_contrast", "could not read caption frames", critical=True)
        return
    worst = float(min(vals))
    run.lvl("editing.caption_contrast", worst, f"worst {worst:.2f}:1 over {len(vals)} cue frame(s) (text {st['primary']}, outline {outline_px:g}px); "
            f"pixel spread in caption band {min(pix):.2f}:1")


# ---------------------------------------------------------------------- localization
def localization_checks(tl: dict | None, lang: str, analysis: dict | None = None, dur: float | None = None, run: _Run | None = None) -> list[dict]:
    """Model-free language checks on the timeline's cues and dub items. Usable without a rendered file."""
    run = run or _Run(_config(None))
    base = lang.split("-")[0].lower()
    cues = sorted((tl or {}).get("subtitles") or [], key=lambda q: q["start"])
    want = LANG_SCRIPT.get(base, "latin")
    if cues:
        wrong = []
        for q in cues:
            cnt = script_of(q["text"])
            tot = sum(cnt.values())
            if tot >= 3 and cnt.get(want, 0) / tot < 0.5:
                wrong.append(round(q["start"], 3))
        src_texts = set()
        spans, _ = _programme_analysis(tl, analysis)
        if analysis and ("segments" in analysis or "transcript" in analysis):
            src_texts = {re.sub(r"\W+", " ", s.get("text", "")).strip().lower() for s in (analysis.get("segments") or (analysis.get("transcript") or {}).get("segments") or [])}
        same = [round(q["start"], 3) for q in cues if len(q["text"]) > 8 and re.sub(r"\W+", " ", q["text"]).strip().lower() in src_texts]
        lv = sorted(set(wrong + same))
        run.lvl("localization.script", len(lv) / len(cues), f"{len(wrong)} cue(s) not in {want} script, {len(same)} identical to the source transcript", items=lv)
        if spans:
            sp = sum(b - a for a, b in spans)
            cov = sum(max(0.0, min(b, q["end"]) - max(a, q["start"])) for a, b in spans for q in cues) / max(sp, 1e-3)
            run.lvl("localization.coverage", min(cov, 1.0), f"{min(cov, 1.0):.0%} of {sp:.1f}s source speech has a target cue")
        else:
            run.missing("localization.coverage", "no source transcript to compare cue coverage against")
        lim = CPS_BY_LANG.get(base, CPS_BY_LANG["default"])
        fast = [q for q in cues if len(re.sub(r"\s", "", q["text"])) / max(q["end"] - q["start"], 0.01) > lim]
        run.lvl("localization.reading_speed", len(fast) / len(cues), f"{len(fast)} of {len(cues)} cues above {lim:g} chars/s for '{base}'")
        bidi = 0
        for q in cues:
            t = q["text"]
            opens = sum(t.count(ch) for ch in BIDI_OPEN)
            isos = sum(t.count(ch) for ch in BIDI_ISO)
            if opens != t.count("\u202c") or isos != t.count("\u2069"):
                bidi += 1
            elif base in RTL and re.match(r"^[\s\"'(]*[A-Za-z0-9]", t) and not t.startswith(("\u200f", "\u202b", "\u2067", "\u061c")):
                bidi += 1  # an RTL line starting with a Latin/digit run is laid out from the wrong side without a mark
            elif base not in RTL and any(ch in t for ch in "\u200f\u202b\u202e\u2067"):
                bidi += 1
        run.lvl("localization.bidi", bidi, f"{bidi} cue(s) with unbalanced or missing bidi controls")
    dubs = [a for a in (tl or {}).get("audio", []) if a.get("lang", "").split("-")[0] == base and a.get("role", "narration") in SPEECH_ROLES]
    if dubs and tl:
        total = dur or total_duration(tl)
        starts = sorted(float(a.get("start", 0)) for a in dubs)
        worst = 0.0
        for a in dubs:
            d = a.get("duration")
            if d is None and Path(a["src"]).exists():
                d = ff.duration(a["src"])
            if d is None:
                continue
            s = float(a.get("start", 0))
            end = s + float(d)
            slot = min([x for x in starts if x > s + 1e-3] + [total, float(a.get("slot_end", 1e9))])
            worst = max(worst, end - slot)
        run.lvl("localization.dub_overrun", max(worst, 0.0), f"worst dub overrun {max(worst, 0.0):.2f}s past its slot")
    # fold in the language workstream's own QA when it exists
    try:
        from . import localize
        fn = getattr(localize, "qa", None)
    except Exception:
        fn = None
    if fn is not None and tl is not None:
        try:
            ext = fn(tl, lang) or {}
            for c in ext.get("checks", []):
                name = c.get("id") or c.get("check") or "check"
                slug = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")[:40]
                sev = c.get("severity") if c.get("severity") in ("critical", "major", "minor") else "minor"
                if not c.get("ok", True):
                    sev = sev
                run.cfg["checks"].setdefault(f"localization.ext.{slug}", _D("localization", sev if sev != "info" else "minor", 2 if sev == "major" else 1, None, None,
                                                                             f"language QA: {name}", c.get("fix") or "language-review"))
                run.add(f"localization.ext.{slug}", c.get("value", c.get("detail")), "ok" if c.get("ok", True) else "fail", str(c.get("detail", ""))[:200])
        except Exception as e:
            run.missing("localization.ext", f"localize.qa raised: {str(e)[:80]}")
    return run.checks


def _localization(run: _Run, tl: dict | None, analysis, lang: str, dur: float) -> None:
    localization_checks(tl, lang, analysis, dur, run)


# ---------------------------------------------------------------------- roll-up
def _critical_context(tl: dict | None, lang: str | None) -> dict:
    clips = (tl or {}).get("clips", [])
    gen = any(((c.get("provenance") or {}).get("kind") in ("generated", "image_to_video", "text_to_video")) for c in clips)
    chars = bool((tl or {}).get("characters"))
    src_lang = (tl or {}).get("source_lang") or "en"
    localized = bool(lang) and lang.split("-")[0] != src_lang.split("-")[0]
    dubbed = localized and any(a.get("lang") for a in (tl or {}).get("audio", []))
    return {"generated": gen, "generated_or_characters": gen or chars, "localized": localized, "dubbed": dubbed}


def _finish(run: _Run, cfg: dict, path: Path, measured: dict, tl: dict | None, lang: str | None, signoff: dict | None) -> dict:
    ctx = _critical_context(tl, lang)
    signoff = signoff or {}
    reviewed = []
    for cid, (why, crit_when) in NOT_MEASURABLE.items():
        if cid in signoff:
            reviewed.append({"id": cid, "by": signoff[cid]})
            continue
        run.missing(cid, why, critical=bool(crit_when and ctx.get(crit_when)))
    cats = {}
    for cat in CATEGORIES:
        cs = [c for c in run.checks if c["category"] == cat]
        if not cs:
            cats[cat] = {"score": None, "measured": 0, "failed": 0, "not_measured": len([n for n in run.nm if n["id"].startswith(cat)])}
            continue
        w = sum(c["weight"] for c in cs)
        s = sum(c["weight"] * LEVEL_SCORE[c["level"]] for c in cs)
        cats[cat] = {"score": round(100 * s / w, 1), "measured": len(cs), "failed": len([c for c in cs if not c["ok"]]),
                     "not_measured": len([n for n in run.nm if n["id"].startswith(cat)]), "bar": cfg["category_bar"][cat]}
    present = {k: v for k, v in cats.items() if v["score"] is not None}
    tw = sum(cfg["category_weights"][k] for k in present) or 1.0
    total = sum(cfg["category_weights"][k] * v["score"] for k, v in present.items()) / tw if present else 0.0
    failed = [c for c in run.checks if not c["ok"]]
    hard = [c for c in failed if c["hard"] and c["level"] == "fail"]
    crit = [c for c in failed if c["severity"] == "critical" and c["level"] == "fail"]
    if hard:
        total = min(total, HARD_CAP)
    elif crit:
        total = min(total, CRITICAL_CAP)
    total = round(total, 1)
    verdict, reasons = verdict_for(total, run.checks, cats, run.nm, cfg)
    grade = next(g for lim, g in GRADES if total >= lim)
    return {"version": VERSION, "path": str(path), "score": total, "grade": grade, "verdict": verdict, "verdict_reasons": reasons,
            "categories": cats, "checks": run.checks,
            "failed": [{k: c.get(k) for k in ("id", "category", "level", "severity", "value", "threshold", "why", "detail", "fix")} for c in failed],
            "not_measured": run.nm, "human_review": reviewed, "measured": measured,
            "note": "Automated measurements of technical and editorial craft. Not a prediction of audience performance; creative quality still needs a human."}


def verdict_for(total: float, checks: list[dict], cats: dict, not_measured: list[dict], cfg: dict | None = None) -> tuple[str, list[str]]:
    """premium | pass | needs_work | fail, with the rules that decided it."""
    cfg = cfg or _config(None)
    failed = [c for c in checks if not c["ok"]]
    hard = [c["id"] for c in failed if c.get("hard") and c["level"] == "fail"]
    crit = [c["id"] for c in failed if c["severity"] == "critical" and c["level"] == "fail"]
    major_fail = [c["id"] for c in failed if c["severity"] == "major" and c["level"] == "fail"]
    major_warn = [c["id"] for c in failed if c["severity"] == "major" and c["level"] == "warn"]
    if hard:
        return "fail", [f"hard-fail rule: {', '.join(hard)}"]
    if crit:
        return "fail", [f"critical check failed: {', '.join(crit)}"]
    if total < FAIL_SCORE:
        return "fail", [f"score {total} < {FAIL_SCORE}"]
    if major_fail or total < PASS_SCORE:
        return "needs_work", ([f"major check failed: {', '.join(major_fail)}"] if major_fail else []) + ([f"score {total} < {PASS_SCORE}"] if total < PASS_SCORE else [])
    blockers = []
    if major_warn:
        blockers.append(f"major check below its bar: {', '.join(major_warn)}")
    if total < PREMIUM_SCORE:
        blockers.append(f"score {total} < {PREMIUM_SCORE}")
    low = [k for k, v in cats.items() if v.get("score") is not None and v["score"] < cfg["category_bar"][k]]
    if low:
        blockers.append(f"category below bar: {', '.join(low)}")
    crit_nm = [n["id"] for n in not_measured if n.get("critical")]
    if crit_nm:
        blockers.append(f"critical criterion not measured: {', '.join(crit_nm)}")
    if blockers:
        return "pass", blockers
    return "premium", ["no failed major check, every measured category above its bar, nothing critical unmeasured"]


# ====================================================================== auto-correction
def fixes(report: dict, tl: dict | None) -> list[dict]:
    """Map failed checks to concrete timeline operations. `supported: False` ops need a person or another workstream."""
    ops: list[dict] = []
    by = {c["id"]: c for c in report.get("checks", []) if not c["ok"]}
    total = total_duration(tl) if tl and tl.get("clips") else report.get("measured", {}).get("file", {}).get("duration") or 0.0

    def op(name, check, why, supported=True, **kw):
        ops.append({"op": name, "check": check, "why": why, "supported": supported, **kw})

    if "audio.loudness" in by:
        c = by["audio.loudness"]
        tgt = c.get("target_lufs", DEFAULT_LUFS)
        op("set_target_lufs", "audio.loudness", f"measured {c.get('measured_lufs')} LUFS vs target {tgt}", value=tgt)
    if "audio.true_peak" in by or "audio.clipping" in by:
        op("limiter", "audio.true_peak" if "audio.true_peak" in by else "audio.clipping", "keep true peak at or below -1.5 dBTP", true_peak_db=-1.5)
    if "audio.clipping" in by:
        op("declip_sources", "audio.clipping", "clipping already in a source cannot be undone by the limiter; re-record or apply adeclip upstream", supported=False)
    if "audio.head_tail_silence" in by and tl:
        c = by["audio.head_tail_silence"]
        if (c.get("head") or 0) > 1.0:
            op("trim_head", "audio.head_tail_silence", f"{c['head']:.2f}s of silence at the head", seconds=round(c["head"] - 0.2, 3))
        if (c.get("tail") or 0) > 2.0:
            op("trim_tail", "audio.head_tail_silence", f"{c['tail']:.2f}s of silence at the tail", seconds=round(c["tail"] - 0.5, 3))
    for cid in ("editing.dead_air", "editing.long_pause"):
        if cid in by and by[cid].get("spans") and tl:
            keep = 0.4 if cid == "editing.dead_air" else 0.6
            spans = [[s + keep / 2, e - keep / 2] for s, e in by[cid]["spans"] if e - s > keep + 0.1 and s > 0.05 and e < total - 0.05]
            if spans:
                op("remove_spans", cid, f"{len(spans)} silent span(s)", spans=spans)
    if any(k in by for k in ("editing.caption_speed", "editing.caption_line_length", "localization.reading_speed")):
        op("resegment_captions", next(k for k in ("editing.caption_speed", "editing.caption_line_length", "localization.reading_speed") if k in by),
           "split long/fast cues", max_chars=42, max_cps=CPS_BY_LANG.get(((tl or {}).get("lang") or "en").split("-")[0], 20.0))
    if "editing.subtitle_overlap" in by or "editing.subtitle_range" in by:
        op("fix_subtitle_timing", "editing.subtitle_overlap" if "editing.subtitle_overlap" in by else "editing.subtitle_range", "remove overlaps, clamp to programme")
    if "editing.caption_safe_area" in by:
        op("captions_safe_zone", "editing.caption_safe_area", "raise captions into the safe area", margin_v=by["editing.caption_safe_area"].get("needed_margin_v", 0.08))
    if "editing.caption_contrast" in by:
        op("caption_contrast", "editing.caption_contrast", "white text with a dark 3 px outline", primary="&H00FFFFFF", outline=3, outline_colour="&H00101010")
    if "editing.flash_cut" in by and tl:
        for it in by["editing.flash_cut"].get("items") or []:
            if it.get("clip"):
                op("replace_flash_cut", "editing.flash_cut", f"{it['dur']}s shot at {it['t']}s", clip=it["clip"])
    if "visual.resolution" in by and isinstance(by["visual.resolution"].get("threshold"), str) and "x" in by["visual.resolution"]["threshold"]:
        w, h = (int(x) for x in by["visual.resolution"]["threshold"].split("x"))
        op("reframe", "visual.resolution", f"render at {w}x{h}", width=w, height=h)
    if "visual.frame_rate" in by and tl:
        op("set_fps", "visual.frame_rate", "render at the timeline frame rate", fps=tl.get("fps", config.FPS))
    for cid in ("visual.black_frames", "visual.frozen_frames"):
        if cid in by and tl:
            for s, e in by[cid].get("spans") or []:
                if 0.3 < s and e < total - 0.3:
                    op("remove_spans", cid, f"unintended {'black' if 'black' in cid else 'frozen'} picture {s:.2f}-{e:.2f}s", spans=[[s, e]])
    if "audio.speech_music_balance" in by and tl:
        c = by["audio.speech_music_balance"]
        gain = 10 ** (-(10.0 - float(c["value"] or 0) + 1.0) / 20)
        op("music_gain", "audio.speech_music_balance", f"speech only {c['value']} LU above music", factor=round(gain, 3))
    manual = {"visual.decode": "rerender", "visual.continuity": "match grade / level at the listed cuts", "visual.sharpness": "replace or re-shoot soft sources",
              "visual.exposure": "grade exposure", "audio.phase": "flip polarity of one channel or fold to mono", "audio.av_sync": "re-mux / re-render",
              "localization.script": "re-translate leftover cues", "localization.dub_overrun": "re-time or re-synthesise the dub line",
              "visual.repeated_imagery": "replace repeated shots", "editing.clip_repetition": "replace repeated clips", "visual.framing": "reframe with fit=cover"}
    for cid, what in manual.items():
        if cid in by:
            op("manual", cid, what, supported=False)
    return ops


def _shift(tl: dict, t: float, delta: float) -> None:
    lim = t + delta
    for q in tl.get("subtitles", []):
        for k in ("start", "end"):
            if q[k] >= t:
                q[k] = round(q[k] + delta, 3)
            elif q[k] > lim:
                q[k] = round(lim, 3)
    tl["subtitles"] = [q for q in tl.get("subtitles", []) if q["end"] > q["start"] + 0.05]
    for a in tl.get("audio", []):
        s = float(a.get("start", 0.0))
        if s >= t:
            a["start"] = round(max(0.0, s + delta), 3)
    for m in tl.get("markers", []):
        if m["t"] >= t:
            m["t"] = round(m["t"] + delta, 3)


def cut_span(tl: dict, t0: float, t1: float) -> float:
    """Remove programme time [t0, t1] from the clips it covers (video: split the take; image/colour: shorten) and pull
    everything after it earlier. Returns the seconds removed. Works on hard cuts; crossfaded neighbours keep their overlap."""
    removed = 0.0
    starts = clip_starts(tl)
    for k in range(len(tl["clips"]) - 1, -1, -1):
        c, st = tl["clips"][k], starts[k]
        d = clip_duration(c)
        a, b = max(t0, st), min(t1, st + d)
        if b - a < 0.02:
            continue
        l0, l1 = a - st, b - st
        if c["type"] == "video":
            sp = float(c.get("speed", 1.0))
            pieces = []
            if l0 > 0.04:
                A = copy.deepcopy(c)
                A["out"] = round(c["in"] + l0 * sp, 3)
                A.pop("fade_out", None)
                A.pop("text", None)
                pieces.append(A)
            if d - l1 > 0.04:
                B = copy.deepcopy(c)
                B["in"] = round(c["in"] + l1 * sp, 3)
                if pieces:
                    B["id"] = f"{c.get('id', 'c')}b"
                    B.pop("fade_in", None)
                B.pop("text", None)
                pieces.append(B)
            tl["clips"][k:k + 1] = pieces
        else:
            nd = round(d - (l1 - l0), 3)
            if nd < 0.04:
                tl["clips"].pop(k)
            else:
                c["duration"] = nd
        _shift(tl, b, -(b - a))
        removed += b - a
    return removed


def _resegment(cues: list[dict], max_chars: int, max_cps: float, total: float) -> list[dict]:
    out = []
    for i, q in enumerate(sorted(cues, key=lambda x: x["start"])):
        words = q["text"].replace("\n", " ").split()
        chunks, cur = [], ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > max_chars:
                chunks.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            chunks.append(cur)
        if not chunks:
            continue
        nxt = min([c["start"] for c in cues if c["start"] > q["start"] + 1e-3] + [total])
        need = sum(len(c) for c in chunks) / max_cps
        end = max(q["end"], min(nxt - 0.04, q["start"] + need))
        span = end - q["start"]
        tot = sum(len(c) for c in chunks) or 1
        t = q["start"]
        for c in chunks:
            d = span * len(c) / tot
            out.append({**{k: v for k, v in q.items() if k not in ("text", "start", "end", "words")}, "text": c, "start": round(t, 3), "end": round(t + d, 3)})
            t += d
    return out


def apply_ops(tl: dict, ops: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """Apply supported ops to a COPY of the timeline. Returns (new timeline, applied ops, skipped ops)."""
    from .timeline import subs
    new = copy.deepcopy(tl)
    applied, skipped = [], []
    cuts: list[tuple[float, float, dict]] = []
    for o in ops:
        if not o.get("supported", True):
            skipped.append(o)
            continue
        name = o["op"]
        try:
            if name == "set_target_lufs":
                new["target_lufs"] = float(o["value"])
            elif name == "limiter":
                new["true_peak_db"] = float(o.get("true_peak_db", -1.5))  # renderer always limits; recorded for WS A
            elif name == "trim_head":
                cuts.append((0.0, float(o["seconds"]), o))
                continue
            elif name == "trim_tail":
                T = total_duration(new)
                cuts.append((T - float(o["seconds"]), T, o))
                continue
            elif name == "remove_spans":
                for s, e in o["spans"]:
                    cuts.append((float(s), float(e), o))
                continue
            elif name == "resegment_captions":
                new["subtitles"] = subs.fix_timing(_resegment(new.get("subtitles", []), int(o.get("max_chars", 42)), float(o.get("max_cps", 20.0)),
                                                              total_duration(new)))
            elif name == "fix_subtitle_timing":
                T = total_duration(new)
                new["subtitles"] = subs.fix_timing([{**q, "end": min(q["end"], T)} for q in new.get("subtitles", []) if q["start"] < T - 0.2])
            elif name == "captions_safe_zone":
                new["subtitle_style"] = {**(new.get("subtitle_style") or {}), "margin_v": max(float(o["margin_v"]), (new.get("subtitle_style") or {}).get("margin_v", 0))}
            elif name == "caption_contrast":
                new["subtitle_style"] = {**(new.get("subtitle_style") or {}), "primary": o["primary"], "outline": o["outline"]}
            elif name == "replace_flash_cut":
                ids = [c.get("id") for c in new["clips"]]
                if o["clip"] not in ids:
                    skipped.append(o)
                    continue
                k = ids.index(o["clip"])
                d = clip_duration(new["clips"][k])
                gone = new["clips"].pop(k)
                donor = new["clips"][k - 1] if k > 0 else (new["clips"][k] if k < len(new["clips"]) else None)
                if donor is None:
                    new["clips"].insert(k, gone)
                    skipped.append(o)
                    continue
                if donor["type"] == "video":
                    sp = float(donor.get("speed", 1.0))
                    src_len = ff.duration(donor["src"]) if Path(donor["src"]).exists() else donor["out"]
                    if k > 0 and donor["out"] + d * sp <= src_len:
                        donor["out"] = round(donor["out"] + d * sp, 3)
                    elif k == 0 or donor["in"] - d * sp < 0:
                        _shift(new, sum(clip_duration(c) for c in new["clips"][:k]), -d)
                    else:
                        nxt = new["clips"][k] if k < len(new["clips"]) else None
                        if nxt and nxt["type"] == "video" and nxt["in"] - d * float(nxt.get("speed", 1.0)) >= 0:
                            nxt["in"] = round(nxt["in"] - d * float(nxt.get("speed", 1.0)), 3)
                        else:
                            _shift(new, sum(clip_duration(c) for c in new["clips"][:k]), -d)
                else:
                    donor["duration"] = round(float(donor["duration"]) + d, 3)
            elif name == "reframe":
                new["width"], new["height"] = int(o["width"]), int(o["height"])
            elif name == "set_fps":
                new["fps"] = o["fps"]
            elif name == "music_gain":
                if new.get("music"):
                    new["music"]["volume"] = round(float(new["music"].get("volume", 0.5)) * float(o["factor"]), 4)
                for a in new.get("audio", []):
                    if a.get("role") == "music":
                        a["volume"] = round(float(a.get("volume", 1.0)) * float(o["factor"]), 4)
            else:
                skipped.append(o)
                continue
            applied.append(o)
        except Exception as e:
            skipped.append({**o, "error": str(e)[:120]})
    if cuts:
        done = []
        for s, e, o in sorted(cuts, key=lambda x: -x[0]):  # from the end so earlier times stay valid
            if e - s > 0.04 and cut_span(new, max(0.0, s), e) > 0 and o not in done:
                done.append(o)
        applied += done
        skipped += [o for _, _, o in cuts if o not in done and o not in skipped]
    return new, applied, skipped


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def improve(tl: dict, render_fn, max_rounds: int = 2, **score_kw) -> dict:
    """Render -> score -> apply supported fixes -> re-render, at most `max_rounds` correction rounds.
    `render_fn(tl, round_no) -> path` must write each round to a NEW path; every round's file is kept and listed.
    Returns {"rounds":[...], "before", "after", "best_round", "timeline": best timeline, "improved"}."""
    rounds = []
    cur = copy.deepcopy(tl)
    for r in range(max_rounds + 1):
        out = Path(render_fn(cur, r))
        if any(Path(x["path"]).resolve() == out.resolve() for x in rounds):
            raise ValueError(f"render_fn reused {out}; each round must keep its own file (no silent overwrite)")
        rep = score(out, cur, **score_kw)
        rec = {"round": r, "path": str(out), "sha256": _sha(out), "score": rep["score"], "verdict": rep["verdict"], "grade": rep["grade"],
               "failed": [c["id"] for c in rep["failed"]], "report": rep, "timeline": copy.deepcopy(cur), "applied": [], "skipped": []}
        rounds.append(rec)
        if r == max_rounds or not rep["failed"]:
            break
        ops = fixes(rep, cur)
        nxt, applied, skipped = apply_ops(cur, ops)
        rec["applied"], rec["skipped"] = applied, skipped
        if not applied:
            break
        cur = nxt
    for x in rounds:  # prove no earlier file was overwritten
        x["intact"] = Path(x["path"]).exists() and _sha(Path(x["path"])) == x["sha256"]
    best = max(rounds, key=lambda x: (x["score"], -x["round"]))
    return {"rounds": [{k: v for k, v in x.items() if k not in ("report", "timeline")} for x in rounds],
            "before": {k: rounds[0][k] for k in ("path", "score", "verdict", "failed")},
            "after": {k: best[k] for k in ("path", "score", "verdict", "failed")}, "best_round": best["round"],
            "timeline": best["timeline"], "report": best["report"], "improved": best["score"] > rounds[0]["score"]}


def summary(report: dict) -> dict:
    """Compact form for job records, approval reports and the UI panel."""
    return {"score": report["score"], "grade": report["grade"], "verdict": report["verdict"], "verdict_reasons": report.get("verdict_reasons", []),
            "categories": {k: v.get("score") for k, v in report["categories"].items()},
            "failed": [{k: c[k] for k in ("id", "level", "severity", "detail", "fix")} for c in report["failed"]],
            "not_measured": report["not_measured"], "note": report.get("note")}
