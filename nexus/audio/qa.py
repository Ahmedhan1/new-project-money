"""Audio QA on rendered outputs (Account 2, Agent C). Measurements only; a person must still listen.

    sync_offsets(output, tl, starts)    per video clip: cross-correlation of the clip's SOURCE audio against the OUTPUT
                                        around its programme start -> offset in ms (+ = output audio late) and the
                                        correlation peak (confidence). Band 200-4000 Hz, 16 kHz, +-500 ms search.
    loudness_consistency(paths, target) integrated LUFS / true peak per deliverable, spread across them
    level_jumps(x, sr)                  adjacent speech segments whose levels differ by > 6 dB (speaker/level jumps)
    gaps(x, sr)                         digital-silence gaps and long dead-air stretches inside the programme
    audio_review(output, tl, ...)       all of it as QA checks in the qa_continuity / qa format
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..qa import _c
from . import measure as M

STAGE = "3b-audio"
SYNC_SR = 16000
SEARCH_S = 0.5
JUMP_DB = 6.0
DEAD_AIR_S = 3.0
GAP_S = 0.25
LATE_VISIBLE_MS, EARLY_VISIBLE_MS = 125.0, 45.0   # ITU-R BT.1359 detectability thresholds


def _band(x: np.ndarray, sr: int) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt
    return sosfiltfilt(butter(4, [200, 4000], "band", fs=sr, output="sos"), x.astype(np.float64))


def xcorr_offset(ref: np.ndarray, sig: np.ndarray, sr: int, search_s: float = SEARCH_S) -> tuple[float, float, float]:
    """Lag (seconds) at which `ref` best matches `sig`, where sig was cut search_s earlier than ref's nominal position
    and is len(ref) + 2*search_s long. Returns (offset_s, normalised correlation peak, distinctness); offset > 0 = sig
    late. Distinctness = peak minus the best correlation more than 5 ms away: ~0 for periodic sound (a steady tone
    matches at every period), so such a measurement is reported as not measurable instead of as an offset."""
    ref, sig = _band(ref, sr), _band(sig, sr)
    n = len(ref)
    if n < sr * 0.2 or len(sig) < n:
        return 0.0, 0.0, 0.0
    m = len(sig) - n + 1
    F = np.fft.rfft(sig, len(sig) + n)
    G = np.fft.rfft(ref[::-1], len(sig) + n)
    cc = np.fft.irfft(F * G, len(sig) + n)[n - 1:n - 1 + m]
    c2 = np.concatenate([[0], np.cumsum(sig ** 2)])
    energy = np.sqrt((c2[n:n + m] - c2[:m]) * np.sum(ref ** 2)) + 1e-12
    nc = cc / energy
    k = int(np.argmax(nc))
    frac = 0.0
    if 0 < k < m - 1:   # parabolic sub-sample refinement
        a, b, c = nc[k - 1], nc[k], nc[k + 1]
        frac = 0.5 * (a - c) / (a - 2 * b + c + 1e-12)
    away = np.ones(m, bool)
    away[max(0, k - int(0.005 * sr)):k + int(0.005 * sr) + 1] = False
    second = float(nc[away].max()) if away.any() else 0.0
    return (k + frac) / sr - search_s, float(nc[k]), float(nc[k] - second)


def stream_starts(path: str | Path) -> tuple[float, float]:
    """(video start_time, audio start_time) in seconds; a player shows video frame t at t + video start."""
    import json
    import subprocess
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=120)
    v = a = 0.0
    try:
        for st in json.loads(p.stdout).get("streams", []):
            t = float(st.get("start_time") or 0.0)
            if st.get("codec_type") == "video":
                v = t
            elif st.get("codec_type") == "audio":
                a = t
    except (ValueError, TypeError):
        pass
    return v, a


def sync_offsets(output: str | Path, tl: dict, starts: list[float], max_clips: int = 12, window: float = 2.0) -> list[dict]:
    """offset_ms is audio relative to PICTURE: the decoded audio lag minus the video stream's start offset (the
    renderer's outputs start video at 0.021 s), so 0 means lips and sound line up for a viewer."""
    rows = []
    v0, a0 = stream_starts(output)
    out_audio = M.mono(M.decode(output, SYNC_SR, 1))
    for i, c in enumerate(tl.get("clips", [])):
        if len(rows) >= max_clips:
            break
        if c.get("type") != "video" or c.get("mute") or float(c.get("speed", 1.0)) != 1.0 or c.get("freeze") or c.get("keyframes", {}).get("volume"):
            continue
        dur = float(c["out"]) - float(c["in"])
        lead = 0.3
        w = min(window, dur - 2 * lead)
        if w < 0.5 or not Path(c["src"]).exists():
            continue
        ref = M.mono(M.decode(c["src"], SYNC_SR, 1, float(c["in"]) + lead, w))
        a = int((starts[i] + lead - SEARCH_S) * SYNC_SR)
        if len(ref) < int(0.5 * SYNC_SR):
            continue
        need = len(ref) + int(2 * SEARCH_S * SYNC_SR)
        sig = out_audio[max(0, a):a + need]
        if a < 0:   # the search window starts before the programme: zero-pad (nothing can match there)
            sig = np.concatenate([np.zeros(-a, sig.dtype), sig])
        if np.sqrt(np.mean(ref ** 2)) < 1e-4:
            rows.append({"clip": c.get("id", i), "t": round(starts[i], 3), "offset_ms": None, "corr": 0.0, "why": "silent source"})
            continue
        off, corr, dist = xcorr_offset(ref, sig, SYNC_SR)
        rows.append({"clip": c.get("id", i), "t": round(starts[i], 3), "offset_ms": round((off - (v0 - a0)) * 1000, 1), "corr": round(corr, 3),
                     "distinct": round(dist, 3)})
    return rows


def loudness_consistency(paths: list[str | Path], target_lufs: float | None = None, tol_lu: float = 1.0) -> dict:
    rows = []
    for p in paths:
        x = M.decode(p, 48000, 2)
        lv = M.loudness(x, 48000)
        rows.append({"path": str(p), **lv})
    lv = [r["lufs"] for r in rows if r["lufs"] > -70]
    spread = round(max(lv) - min(lv), 2) if lv else 0.0
    off = [r for r in rows if target_lufs is not None and abs(r["lufs"] - target_lufs) > tol_lu]
    hot = [r for r in rows if r["true_peak"] > -1.0]
    return {"deliverables": rows, "spread_lu": spread, "off_target": [r["path"] for r in off], "true_peak_over": [r["path"] for r in hot],
            "ok": spread <= tol_lu and not off and not hot, "tolerance_lu": tol_lu, "target_lufs": target_lufs}


def level_jumps(x: np.ndarray, sr: int, jump_db: float = JUMP_DB, max_gap: float = 1.5) -> list[dict]:
    """Adjacent speech segments (gap < max_gap s) whose median speech levels differ by more than jump_db."""
    segs = M.active_segments(x, sr, min_len=0.8)
    m = M.mono(M.as2d(x))
    lv = []
    for s in segs:
        d = M.block_db(m[int(s["start"] * sr):int(s["end"] * sr)], sr)
        lv.append(float(np.median(d[d > np.percentile(d, 90) - 25])) if len(d) >= 10 else None)
    out = []
    for (a, la), (b, lb) in zip(zip(segs, lv), zip(segs[1:], lv[1:])):
        if la is None or lb is None or b["start"] - a["end"] > max_gap:
            continue
        if abs(lb - la) > jump_db:
            out.append({"t": b["start"], "from_db": round(la, 1), "to_db": round(lb, 1), "jump_db": round(lb - la, 1)})
    return out


def gaps(x: np.ndarray, sr: int, edge: float = 0.5) -> dict:
    from .dialogue import digital_silence_gaps
    dur = len(x) / sr
    dig = [(s, e) for s, e in digital_silence_gaps(x, sr, GAP_S) if s > edge and e < dur - edge]
    db = M.block_db(x, sr)
    nf, _ = M.noise_floor(db)
    quiet = db < max(nf + 3, -70)
    dead, i = [], 0
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            s, e = i * M.BLOCK_S, j * M.BLOCK_S
            if e - s >= DEAD_AIR_S and s > edge and e < dur - edge:
                dead.append((round(s, 2), round(e, 2)))
            i = j
        else:
            i += 1
    return {"digital_silence": dig, "dead_air": dead}


def audio_review(output: str | Path, tl: dict | None = None, starts: list[float] | None = None,
                 deliverables: list[str | Path] | None = None, target_lufs: float | None = None) -> dict:
    """Audio checks for a rendered programme in the qa format ({check, ok, detail, severity, fix})."""
    output = Path(output)
    checks: list[dict] = []
    x = M.decode(output, 48000, 2)
    if not len(x):
        return {"stage": STAGE, "checks": [_c("audio stream", False, "no decodable audio", "major", fix="rerender")], "passed": False}
    fps = float((tl or {}).get("fps", 30))
    frame_ms = 1000.0 / fps
    meas: dict = {}
    if tl and starts is not None:
        rows = sync_offsets(output, tl, starts)
        meas["sync"] = rows
        good = [r for r in rows if r.get("offset_ms") is not None and r["corr"] >= 0.5 and r.get("distinct", 0) >= 0.05]
        bad = [r for r in good if abs(r["offset_ms"]) > frame_ms]
        for r in sorted(bad, key=lambda r: -abs(r["offset_ms"]))[:6]:
            # one frame is the editing target; ITU-R BT.1359 detectability (audio 45 ms early / 125 ms late) makes it major
            visible = r["offset_ms"] > LATE_VISIBLE_MS or r["offset_ms"] < -EARLY_VISIBLE_MS
            checks.append(_c(f"audio/video sync at {r['t']:.2f}s", False, f"clip {r['clip']}: audio {r['offset_ms']:+.1f} ms vs its source "
                             f"(target one frame = {frame_ms:.1f} ms; detectable beyond +{LATE_VISIBLE_MS:.0f}/-{EARLY_VISIBLE_MS:.0f} ms; "
                             f"correlation {r['corr']:.2f})", "major" if visible else "minor", fix="resync-audio") | {"t": r["t"]})
        if good and not bad:
            worst = max(abs(r["offset_ms"]) for r in good)
            checks.append(_c("audio/video sync", True, f"{len(good)} clip(s) measured against their sources, worst {worst:.1f} ms (limit {frame_ms:.1f} ms)"))
        if len(good) < len(rows):
            checks.append(_c("sync not measurable", True, f"{len(rows) - len(good)} clip(s) with low or ambiguous correlation (processed, quiet or periodic audio)"))
    lv = M.loudness(x, 48000)
    meas["loudness"] = lv
    if target_lufs is not None:
        checks.append(_c("programme loudness", abs(lv["lufs"] - target_lufs) <= 1.0, f"{lv['lufs']:.1f} LUFS (target {target_lufs:.1f})",
                         "major", fix="renormalise"))
    checks.append(_c("true peak", lv["true_peak"] <= -1.0, f"{lv['true_peak']:.1f} dBTP (limit -1.0)", "major", fix="renormalise"))
    cl = M.clipping(x)
    checks.append(_c("clipping", cl["clipped_runs"] == 0, f"{cl['clipped_samples']} clipped sample(s) in {cl['clipped_runs']} run(s)", "major", fix="renormalise"))
    jumps = level_jumps(x, 48000)
    meas["level_jumps"] = jumps
    checks.append(_c("speaker/level jumps", not jumps, "no adjacent speech segments differ by > 6 dB" if not jumps else
                     "; ".join(f"{j['t']:.1f}s {j['jump_db']:+.1f} dB" for j in jumps[:4]), "minor", fix="level-match-cut")
                  | ({"t": jumps[0]["t"]} if jumps else {}))
    g = gaps(x, 48000)
    meas["gaps"] = g
    checks.append(_c("silence gaps", not g["digital_silence"], "no digital-silence gap > 0.25 s inside the programme" if not g["digital_silence"]
                     else f"{len(g['digital_silence'])} digital-silence gap(s), first {g['digital_silence'][0][0]:.2f}s: fill with room tone", "minor",
                     fix="room-tone-fill") | ({"t": g["digital_silence"][0][0]} if g["digital_silence"] else {}))
    if g["dead_air"]:
        checks.append(_c("dead air", False, f"{len(g['dead_air'])} stretch(es) >= {DEAD_AIR_S:.0f} s at the noise floor, first {g['dead_air'][0]}",
                         "minor", fix="tighten-pause") | {"t": g["dead_air"][0][0]})
    if deliverables:
        lc = loudness_consistency(deliverables, target_lufs)
        meas["deliverables"] = lc
        checks.append(_c("loudness across deliverables", lc["ok"], f"spread {lc['spread_lu']:.1f} LU over {len(deliverables)} file(s)"
                         + (f"; off target: {lc['off_target']}" if lc["off_target"] else "") + (f"; true peak over: {lc['true_peak_over']}" if lc["true_peak_over"] else ""),
                         "major", fix="renormalise"))
    checks.append(_c("measured (audio)", True, "objective measurements only - human listening still required"))
    return {"stage": STAGE, "checks": checks, "measurements": meas,
            "passed": all(c["ok"] or c["severity"] in ("minor", "info") for c in checks)}
