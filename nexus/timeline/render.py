"""Timeline -> video.

Stages, each cached so that changing one scene only re-renders that scene:
  1. every clip -> normalised parts (same size/fps/codec), cached by content hash: a video-only body (plus a head
     and/or tail when a transition overlaps the clip) and the clip's audio as sample-exact PCM. Clips render in parallel.
  2. pieces: transition pieces (xfade of tail + head) and bodies, with captions / programme overlays burned in at their
     programme time. Burned pieces are a second cache level, so a caption-only edit re-burns only the pieces it touches
     and never re-renders a clip from its source. Pieces without captions/overlays are used as they are.
  3. final pass: pieces are joined by stream copy (no full-programme video re-encode) and the mix is built from the
     programme audio: narration + ducked music + SFX + two-pass loudness normalisation.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .. import config, db
from ..fx import captions as fxcap
from ..fx import effects, transitions
from ..media import ff
from . import subs, text

GRADES = {
    "none": "",
    "cinematic": "eq=contrast=1.08:saturation=0.92,curves=preset=medium_contrast,colorbalance=rs=-0.04:bs=0.06:rh=0.05:bh=-0.05",
    "warm": "colorbalance=rm=0.06:gm=0.02:bm=-0.06,eq=saturation=1.08",
    "cool": "colorbalance=rm=-0.05:bm=0.07,eq=saturation=0.95",
    "bw": "hue=s=0,eq=contrast=1.12",
    "vivid": "eq=saturation=1.3:contrast=1.06",
    "documentary": "eq=contrast=1.04:saturation=0.88,curves=preset=lighter",
}
AUDIO_FX = {
    "none": "",
    "denoise": "highpass=f=80,afftdn=nr=12:nf=-30",
    "voice": "highpass=f=80,afftdn=nr=10:nf=-30,acompressor=threshold=-20dB:ratio=3:attack=10:release=150:makeup=2,"
             "equalizer=f=3200:t=q:w=1.2:g=2.5",
}
FX_VERSION = 1          # bump when the meaning of a cached part/piece changes
DRAFT_SCALE = 0.5
_build_lock = threading.Lock()  # graph building writes shared PNG caches


def _is_xf(c: dict) -> bool:
    return transitions.is_transition(c.get("transition"))


def clip_duration(c: dict) -> float:
    if c["type"] == "video":
        r = effects.ramp_duration(c)
        return r if r is not None else (c["out"] - c["in"]) / float(c.get("speed", 1.0))
    return float(c["duration"])


def total_duration(tl: dict) -> float:
    d, xf = 0.0, 0.0
    for i, c in enumerate(tl["clips"]):
        d += clip_duration(c)
        if i and _is_xf(c):
            xf += float(c.get("transition_dur", 0.5))
    return d - xf


def clip_starts(tl: dict) -> list[float]:
    out, t = [], 0.0
    for i, c in enumerate(tl["clips"]):
        if i and _is_xf(c):
            t -= float(c.get("transition_dur", 0.5))
        out.append(t)
        t += clip_duration(c)
    return out


def _fit(W: int, H: int, mode: str, focus: tuple[float, float]) -> str:
    fx, fy = focus
    if mode == "contain":
        return (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black")
    if mode == "blur":
        return (f"split[bg][fg];[bg]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,"
                f"eq=brightness=-0.12[bgb];[fg]scale={W}:{H}:force_original_aspect_ratio=decrease[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2")
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}:(iw-{W})*{fx}:(ih-{H})*{fy}"


def _src_sig(path: str) -> str:
    p = Path(path)
    return f"{p}:{p.stat().st_size}:{int(p.stat().st_mtime)}" if p.exists() else f"{p}:missing"


# ------------------------------------------------------------------ encoders (libx264, optional hardware)
HW_CANDIDATES = ("h264_nvenc", "h264_qsv", "h264_vaapi", "h264_videotoolbox", "h264_amf")
_hw_cache: dict | None = None
_VAAPI_DEV = "/dev/dri/renderD128"


def _hw_spec(name: str, draft: bool = False) -> dict:
    q = 30 if draft else 21
    pre = ["-vaapi_device", _VAAPI_DEV] if name == "h264_vaapi" else []
    suffix = "format=nv12,hwupload" if name == "h264_vaapi" else ""
    codec = {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p1" if draft else "p4", "-rc", "vbr", "-cq", q, "-b:v", "0", "-pix_fmt", "yuv420p"],
        "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", q, "-pix_fmt", "nv12"],
        "h264_vaapi": ["-c:v", "h264_vaapi", "-qp", q],
        "h264_videotoolbox": ["-c:v", "h264_videotoolbox", "-q:v", 50 if draft else 65, "-pix_fmt", "yuv420p"],
        "h264_amf": ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", q, "-qp_p", q, "-pix_fmt", "yuv420p"],
    }[name]
    return {"name": name, "pre": pre, "codec": codec, "suffix": suffix}


def hw_encoders(refresh: bool = False) -> dict[str, bool]:
    """Probe which H.264 hardware encoders really work here (each encodes one frame). Cached on disk per FFmpeg build."""
    global _hw_cache
    if _hw_cache is not None and not refresh:
        return _hw_cache
    ver = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True).stdout.splitlines()[:1]
    f = config.CACHE / "hwenc.json"
    if f.exists() and not refresh:
        try:
            data = json.loads(f.read_text())
            if data.get("ffmpeg") == ver:
                _hw_cache = data["encoders"]
                return _hw_cache
        except (ValueError, KeyError):
            pass
    from .. import costguard
    costguard.authorize("ffmpeg", "probe-hwenc")
    res = {}
    for name in HW_CANDIDATES:
        sp = _hw_spec(name)
        vf = "format=yuv420p" + (f",{sp['suffix']}" if sp["suffix"] else "")
        try:
            p = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", *sp["pre"], "-f", "lavfi", "-i",
                                "color=c=gray:s=256x144:r=30:d=0.2", "-frames:v", "1", "-vf", vf,
                                *[str(a) for a in sp["codec"]], "-f", "null", "-"], capture_output=True, text=True, timeout=30)
            res[name] = p.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            res[name] = False
    _hw_cache = res
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"ffmpeg": ver, "encoders": res}))
    except OSError:
        pass
    return res


def _hw_opt_in() -> bool:
    if os.environ.get("NEXUS_HWENC") is not None:
        return os.environ.get("NEXUS_HWENC") == "1"
    return bool(config.settings().get("hwenc", False))


def encoder(draft: bool = False) -> dict:
    """Encoder used for every intermediate piece: a working hardware H.264 encoder when opted in, else libx264."""
    if _hw_opt_in():
        for name, ok in hw_encoders().items():
            if ok:
                return _hw_spec(name, draft)
    if draft:
        codec = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p"]
    else:
        codec = list(config.VCODEC)
    return {"name": "libx264", "pre": [], "codec": codec, "suffix": ""}


def _enc_sig(enc: dict) -> str:
    return " ".join(str(a) for a in enc["codec"])


def _vout(enc: dict, fps: int, frames: int, threads: int | None) -> list:
    th = ["-threads", threads] if threads and enc["name"] == "libx264" else []
    return [*enc["codec"], *th, "-g", fps * 2, "-frames:v", frames, "-r", fps, "-video_track_timescale", "90000"]


# ------------------------------------------------------------------ keys
def _geometry(tl: dict, draft: bool) -> tuple[int, int, float]:
    W, H = tl["width"], tl["height"]
    if not draft:
        return W, H, 1.0
    return max(16, int(W * DRAFT_SCALE) // 2 * 2), max(16, int(H * DRAFT_SCALE) // 2 * 2), DRAFT_SCALE


def segment_key(c: dict, tl: dict, draft: bool = False) -> str:
    """Hash of everything that changes a clip's pixels or sound (clip keys incl. effects/pip/text, frame geometry,
    grade, language, audio fx, encoder settings, source files)."""
    payload = {k: v for k, v in c.items() if k not in ("id", "label", "notes", "transition", "transition_dur", "xfade")}
    payload["_g"] = [tl["width"], tl["height"], tl.get("fps", config.FPS), tl.get("grade", "none"), tl.get("lang", "en"),
                     tl.get("audio_fx", "none")]
    payload["_v"] = [FX_VERSION, bool(draft), _enc_sig(encoder(draft))]
    if c.get("src"):
        payload["_s"] = _src_sig(c["src"])
    if isinstance(c.get("pip"), dict) and c["pip"].get("src"):
        payload["_p"] = _src_sig(c["pip"]["src"])
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:20]


# ------------------------------------------------------------------ stage 1: clip graph
def _clip_graph(c: dict, tl: dict, W: int, H: int, scale: float) -> dict:
    """FFmpeg inputs + filter graph for one clip. Video ends at label `v` (>= frames frames), audio at `a` (exact)."""
    fps = tl.get("fps", config.FPS)
    dur = clip_duration(c)
    frames = max(1, round(dur * fps))
    dur = frames / fps
    grade = GRADES.get(c.get("grade") or tl.get("grade", "none"), "")
    focus = tuple(c.get("focus", (0.5, 0.5)))
    args: list = []
    has_audio = False
    if c["type"] == "video":
        info = ff.probe(c["src"])
        has_audio = info["audio"] is not None and not c.get("mute")
        args += ["-ss", f"{c['in']:.3f}", "-t", f"{c['out'] - c['in']:.3f}", "-i", c["src"]]
        speed = float(c.get("speed", 1.0))
        sp = effects.ramp_setpts(c) or f"setpts=(PTS-STARTPTS)/{speed}"
        v = f"{sp},fps={fps},{_fit(W, H, c.get('fit', 'cover'), focus)}"
    elif c["type"] == "image":
        args += ["-i", c["src"]]
        kb = c.get("kenburns", "in")
        z0, z1 = {"in": (1.0, 1.14), "out": (1.14, 1.0), "none": (1.0, 1.0)}.get(kb, (1.0, 1.14))
        px = {"left": 0.0, "right": 1.0}.get(c.get("pan", ""), focus[0])
        # 2x supersample keeps zoompan free of the usual pixel jitter
        v = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2}:(iw-{W * 2})*{focus[0]}:(ih-{H * 2})*{focus[1]},"
             f"zoompan=z='{z0}+({z1}-{z0})*on/{frames}':x='(iw-iw/zoom)*{px}':y='(ih-ih/zoom)*{focus[1]}':d={frames}:s={W}x{H}:fps={fps}")
    elif c["type"] == "color":
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"color=c={c.get('color', '0x0b1020')}:s={W}x{H}:r={fps}"]
        v = "null"
    else:
        raise ValueError(f"unknown clip type {c['type']}")
    n_in = 1
    if not has_audio:
        args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"anullsrc=r={config.AUDIO_RATE}:cl=stereo"]
        a_src = f"[{n_in}:a]"
        n_in += 1
    else:
        a_src = "[0:a]"
    geo, look = effects.video_filters(c, W, H, fps, dur, scale)
    chain = ",".join([v, *geo] + ([grade] if grade else []) + ["format=yuv420p", "setsar=1", *look])
    fi, fo = float(c.get("fade_in", 0)), float(c.get("fade_out", 0))
    if fi:
        chain += f",fade=t=in:st=0:d={fi}"
    if fo:
        chain += f",fade=t=out:st={max(0, dur - fo):.3f}:d={fo}"
    fc = [f"[0:v]{chain}[v0]"]
    last = "v0"
    if isinstance(c.get("pip"), dict):
        pargs, plines = effects.pip_graph(c["pip"], W, H, fps, dur, n_in, f"[{last}]", "[vpip]")
        if plines:
            args += pargs
            fc += plines
            n_in += 3
            last = "vpip"
    for i, t in enumerate(c.get("text", [])):
        png = text.overlay_png(t["text"], t.get("style", "title"), W, H, t.get("lang", tl.get("lang", "en")))
        st, en = float(t.get("start", 0)), float(t.get("end", dur))
        en = min(en, dur)
        args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png)]
        fd = min(0.3, max(0.05, (en - st) / 4))
        fc.append(f"[{n_in}:v]format=rgba,fade=t=in:st={st}:d={fd}:alpha=1,fade=t=out:st={en - fd:.3f}:d={fd}:alpha=1[o{i}]")
        fc.append(f"[{last}][o{i}]overlay=0:0:enable='between(t,{st},{en})'[v{i + 1}]")
        last = f"v{i + 1}"
        n_in += 1
    # never run short of frames (sources a frame shy of the nominal length); outputs cut to the exact count
    fc.append(f"[{last}]tpad=stop_mode=clone:stop={fps}[v]")
    a = []
    pre_a = None
    if has_audio:
        ramp = effects.ramp_audio(c, "[0:a]", "[ar]")
        if ramp:
            pre_a, a_src = ramp, "[ar]"
        else:
            a += effects.atempo_chain(float(c.get("speed", 1.0)))
        afx = AUDIO_FX.get(c.get("audio_fx") or tl.get("audio_fx", "none"), "")
        if afx:
            a.append(afx)
        if c.get("volume", 1.0) != 1.0:
            a.append(f"volume={c['volume']}")
        if fi:
            a.append(f"afade=t=in:st=0:d={fi}")
        if fo:
            a.append(f"afade=t=out:st={max(0, dur - fo):.3f}:d={fo}")
    ns = round(frames * config.AUDIO_RATE / fps)
    a += [f"aresample={config.AUDIO_RATE}", "aformat=sample_fmts=s16:channel_layouts=stereo", f"apad=whole_len={ns}", f"atrim=end_sample={ns}"]
    if pre_a:
        fc.append(pre_a)
    fc.append(f"{a_src}{','.join(a)}[a]")
    return {"args": args, "fc": fc, "frames": frames, "dur": dur, "fps": fps}


def render_segment(c: dict, tl: dict, cache: Path, draft: bool = False, threads: int | None = None) -> tuple[Path, bool]:
    """Render one clip to a self-contained normalised segment (h264 + AAC mp4). Returns (path, was_cached)."""
    W, H, scale = _geometry(tl, draft)
    out = cache / f"seg_{segment_key(c, tl, draft)}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out, True
    cache.mkdir(parents=True, exist_ok=True)
    enc = encoder(draft)
    with _build_lock:
        g = _clip_graph(c, tl, W, H, scale)
    fc = g["fc"] + [f"[v]{enc['suffix'] or 'null'}[vo]"]
    tmp = out.with_suffix(".tmp.mp4")
    ff.run([*enc["pre"], *g["args"], "-filter_complex", ";".join(fc), "-map", "[vo]", "-map", "[a]",
            *_vout(enc, g["fps"], g["frames"], threads), *config.ACODEC, "-movflags", "+faststart", tmp], op="render-segment")
    tmp.replace(out)
    return out, False


def _render_parts(c: dict, tl: dict, cache: Path, head: int, tail: int, draft: bool, threads: int | None) -> tuple[dict, bool]:
    """Render a clip as video-only head / body / tail parts (frame counts) + PCM audio, in one FFmpeg run."""
    W, H, scale = _geometry(tl, draft)
    key = f"{segment_key(c, tl, draft)}_h{head}t{tail}"
    paths = {"body": cache / f"part_{key}_b.mp4", "audio": cache / f"part_{key}_a.wav"}
    if head:
        paths["head"] = cache / f"part_{key}_h.mp4"
    if tail:
        paths["tail"] = cache / f"part_{key}_t.mp4"
    if all(p.exists() and p.stat().st_size > 0 for p in paths.values()):
        return paths, True
    cache.mkdir(parents=True, exist_ok=True)
    enc = encoder(draft)
    with _build_lock:
        g = _clip_graph(c, tl, W, H, scale)
    n, fps = g["frames"], g["fps"]
    ranges = [("head", 0, head)] if head else []
    ranges += [("body", head, n - tail)] + ([("tail", n - tail, n)] if tail else [])
    fc = list(g["fc"])
    fc.append(f"[v]split={len(ranges)}" + "".join(f"[s{k}]" for k in range(len(ranges))))
    outs: list = []
    tmps = {}
    for k, (name, a, b) in enumerate(ranges):
        suf = f",{enc['suffix']}" if enc["suffix"] else ""
        fc.append(f"[s{k}]trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS{suf}[o{k}]")
        tmps[name] = paths[name].with_suffix(".tmp.mp4")
        outs += ["-map", f"[o{k}]", "-an", *_vout(enc, fps, b - a, threads), "-movflags", "+faststart", tmps[name]]
    tmps["audio"] = paths["audio"].with_suffix(".tmp.wav")
    outs += ["-map", "[a]", "-vn", "-c:a", "pcm_s16le", "-ar", config.AUDIO_RATE, "-ac", 2, tmps["audio"]]
    th = ["-filter_complex_threads", threads] if threads else []
    ff.run([*enc["pre"], *g["args"], *th, "-filter_complex", ";".join(fc), *outs], op="render-segment")
    for name, t in tmps.items():
        t.replace(paths[name])
    return paths, False


# ------------------------------------------------------------------ stage 2: pieces
def _esc_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _burn_piece(piece: dict, tl: dict, cache: Path, burn: dict, draft: bool, threads: int | None) -> tuple[Path, bool]:
    """Encode a transition piece and/or burn captions + overlays into a piece. Returns (path, was_cached)."""
    W, H, _ = _geometry(tl, draft)
    fps = tl.get("fps", config.FPS)
    off, n = piece["off"] / fps, piece["frames"]
    cues = burn["cues"]
    ovs = burn["overlays"]
    total = burn["total"]
    sig = {"src": [Path(s).name for s in piece["inputs"]], "xf": piece.get("xfade"), "n": n, "W": W, "H": H, "fps": fps,
           "cues": cues, "cap": burn["cap"], "legacy": burn["legacy"], "lang": burn["lang"], "ov": ovs,
           "off": round(off, 4) if (cues or ovs) else None, "total": round(total, 4) if ovs else None,
           "enc": _enc_sig(encoder(draft)), "v": FX_VERSION}
    key = hashlib.sha1(json.dumps(sig, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:20]
    out = cache / f"piece_{key}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out, True
    cache.mkdir(parents=True, exist_ok=True)
    enc = encoder(draft)
    args: list = []
    for s in piece["inputs"]:
        args += ["-i", s]
    n_in = len(piece["inputs"])
    fc = [f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,fps={fps}[i{i}]" for i in range(n_in)]
    if piece.get("xfade"):
        fc.append(f"[i0][i1]xfade=transition={piece['xfade']}:duration={n / fps:.4f}:offset=0[x]")
        last = "[x]"
    else:
        last = "[i0]"
    with _build_lock:
        oargs, olines, last = effects.overlay_graph(ovs, W, H, fps, total, off, n / fps, burn["lang"], n_in, last)
        args += oargs
        fc += olines
        if cues:
            ass = fxcap.write_ass(cues, out.with_suffix(".ass"), W, H, burn["lang"], burn["cap"], burn["legacy"])
            # captions are timed in programme seconds: shift the piece onto the programme clock for libass, then back
            fc.append(f"{last}setpts=PTS+{off:.6f}/TB,ass='{_esc_path(ass)}',setpts=PTS-STARTPTS[cv]")
            last = "[cv]"
    suf = f",{enc['suffix']}" if enc["suffix"] else ""
    fc.append(f"{last}format=yuv420p{suf}[out]")
    tmp = out.with_suffix(".tmp.mp4")
    th = ["-filter_complex_threads", threads] if threads else []
    ff.run([*enc["pre"], *args, *th, "-filter_complex", ";".join(fc), "-map", "[out]", "-an", *_vout(enc, fps, n, threads),
            "-movflags", "+faststart", tmp], op="burn-piece" if not piece.get("xfade") else "transition-piece")
    tmp.replace(out)
    out.with_suffix(".ass").unlink(missing_ok=True)
    return out, False


def _layout(tl: dict) -> tuple[list[int], list[int], list[str | None]]:
    """Frame counts per clip, transition frames into each clip (0 = cut) and xfade names, clamped so every body
    keeps at least one frame."""
    fps = tl.get("fps", config.FPS)
    clips = tl["clips"]
    n = [max(1, round(clip_duration(c) * fps)) for c in clips]
    d = [0] * len(clips)
    names: list[str | None] = [None] * len(clips)
    for i in range(1, len(clips)):
        c = clips[i]
        if _is_xf(c):
            d[i] = max(1, round(float(c.get("transition_dur", 0.5)) * fps))
            t = c.get("transition")
            names[i] = transitions.resolve(c.get("xfade") or (t if t != "crossfade" else None))
    for i in range(1, len(clips)):  # head of clip i and tail of clip i-1 must leave a body frame on both sides
        if d[i]:
            room_prev = n[i - 1] - 1 - d[i - 1]
            room_cur = n[i] - 1 - (d[i + 1] if i + 1 < len(clips) else 0)
            d[i] = max(0, min(d[i], room_prev, room_cur))
            if not d[i]:
                names[i] = None
    return n, d, names


def _concat(segs: list[Path], tl: dict, out: Path) -> None:
    """Join self-contained segments (render_segment output): stream copy for cuts, xfade/acrossfade otherwise."""
    clips = tl["clips"]
    if not any(i and _is_xf(c) for i, c in enumerate(clips)):
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{s}'\n" for s in segs))
        ff.run(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out], op="concat")
        return
    args: list = []
    for s in segs:
        args += ["-i", s]
    fc = [f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[pv{i}]" for i in range(len(segs))]
    vlast, alast, t = "pv0", "0:a", clip_duration(clips[0])
    for i in range(1, len(segs)):
        c = clips[i]
        if _is_xf(c):
            d = float(c.get("transition_dur", 0.5))
            name = transitions.resolve(c.get("xfade") or (c.get("transition") if c.get("transition") != "crossfade" else None))
            fc.append(f"[{vlast}][pv{i}]xfade=transition={name}:duration={d}:offset={t - d:.3f},settb=AVTB[xv{i}]")
            fc.append(f"[{alast}][{i}:a]acrossfade=d={d}[xa{i}]")
            t += clip_duration(c) - d
        else:
            fc.append(f"[{vlast}][pv{i}]concat=n=2:v=1:a=0,settb=AVTB[xv{i}]")
            fc.append(f"[{alast}][{i}:a]concat=n=2:v=0:a=1[xa{i}]")
            t += clip_duration(c)
        vlast, alast = f"xv{i}", f"xa{i}"
    ff.run([*args, "-filter_complex", ";".join(fc), "-map", f"[{vlast}]", "-map", f"[{alast}]", "-r", tl.get("fps", config.FPS),
            *config.VCODEC, *config.ACODEC, "-movflags", "+faststart", out], op="concat-xfade")


def _programme_audio(parts: list[dict], d: list[int], fps: int, out: Path) -> Path:
    """Sample-exact programme audio from the clips' PCM parts (acrossfade where clips overlap)."""
    args: list = []
    for p in parts:
        args += ["-i", p["audio"]]
    fc, last = [], "[0:a]"
    for i in range(1, len(parts)):
        if d[i]:
            fc.append(f"{last}[{i}:a]acrossfade=ns={round(d[i] * config.AUDIO_RATE / fps)}:c1=tri:c2=tri[pa{i}]")
        else:
            fc.append(f"{last}[{i}:a]concat=n=2:v=0:a=1[pa{i}]")
        last = f"[pa{i}]"
    if not fc:
        fc = ["[0:a]anull[pa0]"]
        last = "[pa0]"
    ff.run([*args, "-filter_complex", ";".join(fc), "-map", last, "-c:a", "pcm_s16le", "-ar", config.AUDIO_RATE, "-ac", 2, out],
           op="programme-audio")
    return out


# ------------------------------------------------------------------ stage 3: mix
def _mix_graph(tl: dict, n_narr: int, has_music: bool, dur: float) -> tuple[list[str], str]:
    """Input 0 = programme audio, 1..n_narr = tl['audio'], then music. Narration ducks the music; entries with
    role 'sfx' or 'ambience' are mixed in without ducking it."""
    fc = []
    speech, extra = ["[0:a]"], []
    for i, a in enumerate(tl.get("audio", [])):
        ms = int(float(a.get("start", 0)) * 1000)
        fc.append(f"[{i + 1}:a]aresample={config.AUDIO_RATE},aformat=channel_layouts=stereo,"
                  f"volume={a.get('volume', 1.0)},adelay={ms}:all=1[n{i}]")
        (extra if a.get("role") in ("sfx", "ambience") else speech).append(f"[n{i}]")
    if len(speech) > 1:
        fc.append(f"{''.join(speech)}amix=inputs={len(speech)}:normalize=0:duration=first[sp]")
    else:
        fc.append("[0:a]anull[sp]")
    if has_music:
        m = tl["music"]
        idx = n_narr + 1
        fo = max(0.0, dur - 2.0)
        fc.append(f"[{idx}:a]aresample={config.AUDIO_RATE},aformat=channel_layouts=stereo,volume={m.get('volume', 0.5)},"
                  f"atrim=0:{dur:.3f},afade=t=in:st=0:d=1,afade=t=out:st={fo:.3f}:d=2[mus]")
        if m.get("duck", True):
            fc.append("[sp]asplit[sp1][sp2]")
            fc.append("[mus][sp2]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=450:makeup=1[duck]")
            fc.append("[sp1][duck]amix=inputs=2:normalize=0:duration=first[pre0]")
        else:
            fc.append("[sp][mus]amix=inputs=2:normalize=0:duration=first[pre0]")
    else:
        fc.append("[sp]anull[pre0]")
    if extra:
        fc.append(f"[pre0]{''.join(extra)}amix=inputs={len(extra) + 1}:normalize=0:duration=first[pre]")
    else:
        fc.append("[pre0]anull[pre]")
    return fc, "pre"


def default_workers(jobs: int | None = None) -> tuple[int, int]:
    """(parallel FFmpeg processes, threads per process) from the CPU count; NEXUS_RENDER_WORKERS overrides."""
    cpu = os.cpu_count() or 2
    env = os.environ.get("NEXUS_RENDER_WORKERS")
    w = int(env) if env and env.isdigit() and int(env) > 0 else max(1, cpu // 2) if cpu >= 4 else cpu
    if jobs:
        w = max(1, min(w, jobs))
    return w, max(1, cpu // w)


def render(tl: dict, out: Path, workdir: Path, on_progress=None, draft: bool = False, workers: int | None = None) -> dict:
    """Render the full timeline. Returns stats incl. how many segments were reused from cache."""
    t0 = time.time()
    workdir.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    cache = config.CACHE / ("segments_draft" if draft else "segments")
    fps = tl.get("fps", config.FPS)
    clips = tl["clips"]
    n, d, names = _layout(tl)
    w_default, _ = default_workers(len(clips))
    nw = max(1, int(workers)) if workers else w_default
    threads = max(1, (os.cpu_count() or 2) // nw)
    timings: dict[str, float] = {}
    progress_lock = threading.Lock()
    done = [0]
    n_steps = len(clips) * 2 + 2

    def tick():
        with progress_lock:
            done[0] += 1
            if on_progress:
                on_progress(min(0.95, 0.95 * done[0] / n_steps))

    # stage 1: clip parts (parallel; identical clips are rendered once)
    jobs: dict[str, tuple] = {}
    for i, c in enumerate(clips):
        head, tail = d[i], (d[i + 1] if i + 1 < len(clips) else 0)
        jobs.setdefault(f"{segment_key(c, tl, draft)}_{head}_{tail}", (c, head, tail))
    with cf.ThreadPoolExecutor(max_workers=nw) as pool:
        futs = {k: pool.submit(_render_parts, c, tl, cache, h, t, draft, threads) for k, (c, h, t) in jobs.items()}
        res = {}
        for k, f in futs.items():
            res[k] = f.result()
            tick()
    parts, cached = [], 0
    for i, c in enumerate(clips):
        p, was = res[f"{segment_key(c, tl, draft)}_{d[i]}_{d[i + 1] if i + 1 < len(clips) else 0}"]
        parts.append(p)
        cached += was
    timings["segments"] = round(time.time() - t0, 2)

    # stage 2: pieces in programme order (+ programme audio and its loudness measurement, in parallel)
    total_frames = sum(n) - sum(d)
    dur = total_frames / fps
    cues = tl.get("subtitles") or []
    burn_on = bool(tl.get("burn_subtitles", True) and cues)
    cap = fxcap.options(tl.get("captions"))
    ovs = effects.overlays_signature(tl)
    pieces, off = [], 0
    for i in range(len(clips)):
        if d[i]:
            pieces.append({"inputs": [str(parts[i - 1]["tail"]), str(parts[i]["head"])], "xfade": names[i], "off": off, "frames": d[i]})
            off += d[i]
        nb = n[i] - d[i] - (d[i + 1] if i + 1 < len(clips) else 0)
        pieces.append({"inputs": [str(parts[i]["body"])], "off": off, "frames": nb})
        off += nb
    narr = tl.get("audio", [])
    music = tl.get("music") or {}
    has_music = bool(music.get("src"))
    prog_audio = workdir / f"programme_{out.stem}.wav"
    target = float(tl.get("target_lufs", -14.0))

    def audio_job():
        _programme_audio(parts, d, fps, prog_audio)
        ins: list = ["-i", prog_audio]
        for a in narr:
            ins += ["-i", a["src"]]
        if has_music:
            ins += ["-stream_loop", "-1", "-i", music["src"]]
        fc, pre = _mix_graph(tl, len(narr), has_music, dur)
        err = ff.run([*ins, "-filter_complex", ";".join(fc + [f"[{pre}]loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json[m]"]),
                      "-map", "[m]", "-vn", "-f", "null", "-"], op="mix-measure")
        return ins, fc, pre, err

    t1 = time.time()
    piece_paths: list[Path] = [Path()] * len(pieces)
    pieces_cached = 0
    with cf.ThreadPoolExecutor(max_workers=nw) as pool:
        afut = pool.submit(audio_job)
        pf = {}
        for k, pc in enumerate(pieces):
            a, b = pc["off"] / fps, (pc["off"] + pc["frames"]) / fps
            pcues = fxcap.cues_in(cues, a, b) if burn_on else []
            if not pcues and not ovs and not pc.get("xfade"):
                piece_paths[k] = Path(pc["inputs"][0])
                pieces_cached += 1
                continue
            burn = {"cues": pcues, "cap": cap, "legacy": tl.get("subtitle_style"), "lang": tl.get("lang", "en"), "overlays": ovs, "total": dur}
            pf[k] = pool.submit(_burn_piece, pc, tl, cache, burn, draft, threads)
        for k, f in pf.items():
            piece_paths[k], was = f.result()
            pieces_cached += was
        for _ in range(len(clips)):
            tick()
        ins, fc, pre, err = afut.result()
    timings["pieces_and_audio"] = round(time.time() - t1, 2)

    # stage 3: final pass: video stream copy, audio mixed + normalised once
    t2 = time.time()
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    meas = json.loads(m.group(0)) if m else None
    if meas and meas["input_i"] not in ("-inf", "inf") and float(meas["input_i"]) > -60:
        gain = target - float(meas["input_i"])
        tp = float(meas["input_tp"]) if meas["input_tp"] not in ("-inf", "inf") else -99.0
        if tp + gain <= -1.5:
            ln = (f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
                  f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true")
        else:
            # loud transients (SFX hits, plosives) would push loudnorm into its dynamic mode, which undershoots the
            # target: apply the measured gain and catch the peaks with a look-ahead limiter instead
            ln = f"volume={gain:.2f}dB,alimiter=limit=0.71:attack=3:release=60:level=disabled"
    else:
        ln = "anull"  # silent programme: nothing to normalise
    fc = fc + [f"[{pre}]{ln},aresample={config.AUDIO_RATE},alimiter=limit=0.79:level=disabled[aout]"]
    lst = workdir / f"pieces_{out.stem}.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in piece_paths))
    vidx = len(narr) + 1 + int(has_music)
    ff.run([*ins, "-f", "concat", "-safe", "0", "-i", lst, "-filter_complex", ";".join(fc), "-map", f"{vidx}:v", "-map", "[aout]",
            "-c:v", "copy", *config.ACODEC, "-t", f"{dur:.3f}", "-movflags", "+faststart", out], op="final-mix")
    timings["final"] = round(time.time() - t2, 2)
    sidecars = {}
    if tl.get("subtitles"):
        sidecars["srt"] = str(subs.write_srt(tl["subtitles"], out.with_suffix(".srt")))
        sidecars["vtt"] = str(subs.write_vtt(tl["subtitles"], out.with_suffix(".vtt")))
    elapsed = time.time() - t0
    db.metric("render_rtf", elapsed / max(dur, 0.1), cached=cached, clips=len(clips), draft=draft, workers=nw)
    if on_progress:
        on_progress(1.0)
    return {"output": str(out), "duration": dur, "elapsed": round(elapsed, 2), "segments": len(clips),
            "segments_from_cache": cached, "sidecars": sidecars, "draft": draft, "workers": nw,
            "pieces": len(pieces), "pieces_from_cache": pieces_cached, "timings": timings}
