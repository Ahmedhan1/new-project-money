"""Piece pipeline (stage 2 of render.render): segments -> cached programme pieces -> stream-copy join.

Ported from the measured ideas of the earlier alternative renderer (commit ffe6cfa) onto the current renderer:
  * the programme is cut into pieces at frame accuracy: a transition piece (xfade of the tail of clip i-1 and the head
    of clip i) and a body piece for every clip;
  * captions, programme overlays and multi-track layers are burned into each piece at its programme time, and every
    piece is cached by what it shows: a caption-only edit re-encodes only the pieces the changed cue touches, a clip
    edit only that clip's pieces; a body with nothing burned in and no transition is the segment itself (stream copy);
  * programme audio comes from the segments' audio (decode only), is mixed / loudness-normalised in a thread running
    alongside the piece encodes, and the finished AAC track is cached;
  * the final pass is a pure stream copy (concat demuxer + the cached audio): no full-programme re-encode.
Every video frame goes through at most two encodes (segment, piece), against three before (segment, xfade concat,
final mix with captions). The picture defines the programme length: sum of clip frames minus transition frames.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from .. import config
from ..media import ff

PIECE_VERSION = 1
_png_lock = threading.Lock()  # caption/overlay/title PNG + ASS writers share cache files


def layout(tl: dict) -> tuple[list[int], list[int], list[str | None]]:
    """Frames per clip (exactly what render_segment writes), transition frames into each clip, xfade names.
    Transitions are clamped so every clip keeps at least one frame of its own."""
    from .render import clip_duration
    from ..fx import transitions
    fps = tl.get("fps", config.FPS)
    clips = tl["clips"]
    n = [max(1, round(clip_duration(c) * fps)) for c in clips]
    d = [0] * len(clips)
    names: list[str | None] = [None] * len(clips)
    for i in range(1, len(clips)):
        c = clips[i]
        if c.get("transition") == "crossfade":
            d[i] = max(1, round(float(c.get("transition_dur", 0.5)) * fps))
            names[i] = transitions.resolve(c.get("xfade", "fade"))
    for i in range(1, len(clips)):
        if d[i]:
            room_prev = n[i - 1] - 1 - d[i - 1]
            room_cur = n[i] - 1 - (d[i + 1] if i + 1 < len(clips) else 0)
            d[i] = max(0, min(d[i], room_prev, room_cur))
            if not d[i]:
                names[i] = None
    return n, d, names


def plan(tl: dict) -> tuple[list[dict], int]:
    """Pieces in programme order: {"kind": "xf"|"body", "clip", "off" (programme frame), "frames",
    "src": [(clip index, first frame), ...]}. Returns (pieces, total frames)."""
    n, d, names = layout(tl)
    out, off = [], 0
    for i in range(len(n)):
        if d[i]:
            out.append({"kind": "xf", "clip": i, "off": off, "frames": d[i], "xfade": names[i],
                        "src": [(i - 1, n[i - 1] - d[i]), (i, 0)]})
            off += d[i]
        nb = n[i] - d[i] - (d[i + 1] if i + 1 < len(n) else 0)
        out.append({"kind": "body", "clip": i, "off": off, "frames": nb, "src": [(i, d[i])], "full": nb == n[i]})
        off += nb
    return out, off


def _esc(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def piece_key(pc: dict, segs: list[Path], burn: dict, enc_sig: str) -> str:
    sig = {"v": PIECE_VERSION, "kind": pc["kind"], "src": [[segs[i].stem, a] for i, a in pc["src"]], "n": pc["frames"],
           "xf": pc.get("xfade"), "enc": enc_sig, **burn}
    return hashlib.sha1(json.dumps(sig, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:20]


def burn_spec(tl: dict, pc: dict, fps: int, total: float, layers: list[dict], W: int, H: int) -> dict:
    """Everything burned into one piece (empty dict = nothing)."""
    from ..fx import captions, effects
    from . import tracks
    t0, t1 = pc["off"] / fps, (pc["off"] + pc["frames"]) / fps
    out: dict = {}
    cues = tl.get("subtitles") or []
    if tl.get("burn_subtitles", True) and cues:
        inwin = captions.cues_in(cues, t0, t1)
        if inwin:
            out.update(cues=inwin, cap=tl.get("captions"), legacy=tl.get("subtitle_style"), lang=tl.get("lang", "en"))
    ovs = effects.overlays_signature(tl)
    if ovs:
        out.update(ov=ovs, total=round(total, 4), off=round(t0, 4), lang=tl.get("lang", "en"))
    if layers:
        ls = tracks.signature(layers, t0, t1)
        if ls:
            out.update(layers=ls, off=round(t0, 4), lang=tl.get("lang", "en"))
    if out:
        out["geom"] = [W, H, fps]
    return out


def build(pc: dict, segs: list[Path], tl: dict, cache: Path, W: int, H: int, total: float, layers: list[dict],
          vcodec: list, enc_sig: str, threads: int | None) -> tuple[Path, bool]:
    """Render (or reuse) one piece. Returns (path, was_cached)."""
    fps = tl.get("fps", config.FPS)
    burn = burn_spec(tl, pc, fps, total, layers, W, H)
    key = piece_key(pc, segs, burn, enc_sig if (burn or pc["kind"] == "xf" or not pc.get("full")) else "copy")
    out = cache / f"piece_{key}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out, True
    cache.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{threading.get_ident()}.tmp.mp4")
    n = pc["frames"]
    if pc["kind"] == "body" and pc.get("full") and not burn:
        ff.run(["-i", segs[pc["clip"]], "-map", "0:v", "-c", "copy", "-an", "-movflags", "+faststart", tmp], op="piece-copy")
        tmp.replace(out)
        return out, False
    args: list = []
    fc: list[str] = []
    for k, (ci, a) in enumerate(pc["src"]):
        if a > 0:
            args += ["-ss", f"{(a - 0.5) / fps:.6f}"]
        args += ["-i", segs[ci]]
        fc.append(f"[{k}:v]setpts=PTS-STARTPTS,trim=end_frame={n},settb=AVTB,setpts=PTS-STARTPTS[i{k}]")
    if pc["kind"] == "xf":
        fc.append(f"[i0][i1]xfade=transition={pc['xfade']}:duration={n / fps:.6f}:offset=0,settb=AVTB[x]")
        last = "[x]"
    else:
        last = "[i0]"
    n_in = len(pc["src"])
    t0 = pc["off"] / fps
    lang = tl.get("lang", "en")
    with _png_lock:
        if burn.get("layers"):
            from . import tracks
            largs, llines, last = tracks.layer_graph(layers, W, H, fps, t0, t0 + n / fps, lang, n_in, last)
            args += largs
            fc += llines
            n_in += sum(1 for x in largs if x == "-i")
        if burn.get("cues"):
            from ..fx import captions
            ass = captions.write_ass(burn["cues"], out.with_suffix(".ass"), W, H, lang, burn.get("cap"), burn.get("legacy"))
            # cues are timed in programme seconds: put the piece on the programme clock for libass, then back
            fc.append(f"{last}setpts=PTS+{t0:.6f}/TB,ass='{_esc(ass)}',setpts=PTS-STARTPTS[cv]")
            last = "[cv]"
        if burn.get("ov"):
            from ..fx import effects
            oargs, olines, last = effects.overlay_graph(burn["ov"], W, H, fps, total, t0, n / fps, lang, n_in, last)
            args += oargs
            fc += olines
            n_in += sum(1 for x in oargs if x == "-i")
    fc.append(f"{last}format=yuv420p,setsar=1[out]")
    th = ["-threads", threads, "-filter_complex_threads", threads] if threads else []
    ff.run([*args, "-filter_complex", ";".join(fc), "-map", "[out]", "-an", "-frames:v", n, "-r", fps, *vcodec, *th,
            "-g", fps * 2, "-video_track_timescale", "90000", "-movflags", "+faststart", tmp],
           op="piece-xfade" if pc["kind"] == "xf" else "piece-burn" if burn else "piece-trim")
    out.with_suffix(".ass").unlink(missing_ok=True)
    tmp.replace(out)
    return out, False


def programme_audio(segs: list[Path], n: list[int], d: list[int], fps: int, out: Path) -> Path:
    """Programme audio from the segments' own audio: each held to its picture, joined with acrossfades where clips
    overlap (sample-exact lengths)."""
    args: list = []
    for s in segs:
        args += ["-vn", "-i", s]
    fc = [f"[{i}:a]apad,atrim=0:{n[i] / fps:.6f},asetpts=PTS-STARTPTS[pa{i}]" for i in range(len(segs))]
    last = "pa0"
    for i in range(1, len(segs)):
        if d[i]:
            fc.append(f"[{last}][pa{i}]acrossfade=ns={round(d[i] * config.AUDIO_RATE / fps)}[ja{i}]")
        else:
            fc.append(f"[{last}][pa{i}]concat=n=2:v=0:a=1[ja{i}]")
        last = f"ja{i}"
    ff.run([*args, "-filter_complex", ";".join(fc), "-map", f"[{last}]", "-c:a", "pcm_s16le", "-ar", config.AUDIO_RATE,
            "-ac", 2, out], op="programme-audio")
    return out


def audio_digest(seg: Path) -> str:
    """MD5 of a segment's encoded audio packets (stream copy, no decode), cached next to the segment. A picture-only
    edit (grade, effect, caption...) re-renders a segment with identical audio, so the mixed programme audio stays
    cached; if the encoder were ever not bit-reproducible this only costs a cache miss."""
    side = seg.with_suffix(".amd5")
    try:
        return side.read_text().strip()
    except OSError:
        pass
    tmp = seg.with_suffix(f".{threading.get_ident()}.tmp.amd5")
    try:
        ff.run(["-i", seg, "-map", "0:a", "-c", "copy", "-f", "md5", tmp], op="audio-digest")
        val = tmp.read_text().strip().removeprefix("MD5=")
        tmp.replace(side)
        side.write_text(val)
        return val
    except (ff.FFError, OSError):
        tmp.unlink(missing_ok=True)
        return seg.stem


def audio_key(tl: dict, segs: list[Path], n: list[int], d: list[int], fps: int, src_sig) -> str:
    items = []
    for a in tl.get("audio") or []:
        sig = (f"{Path(a['src']).name}:{Path(a['src']).stat().st_size if Path(a['src']).exists() else 'missing'}"
               if a.get("jl") else src_sig(a["src"]))
        items.append({**{k: v for k, v in a.items() if k != "src"}, "_s": sig})
    music = tl.get("music") or {}
    sig = {"v": PIECE_VERSION, "segs": [audio_digest(s) for s in segs], "n": n, "d": d, "fps": fps, "items": items,
           "music": {**music, "_s": src_sig(music["src"])} if music.get("src") else None,
           "target": tl.get("target_lufs", -14.0), "acodec": config.ACODEC}
    return hashlib.sha1(json.dumps(sig, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:20]
