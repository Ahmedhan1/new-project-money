"""Timeline -> video.

Three stages, each cached so that changing one scene only re-renders that scene:
  1. every clip -> a normalised segment (same size/fps/codec), cached by content hash; segments render in parallel
  2. segments -> programme (stream-copy concat, or xfade when crossfades are requested)
  3. final mix: dialogue/narration + role-aware ducked music + sfx/ambience + two-pass loudness normalisation
     (+ burned captions in the timeline's caption style + programme-wide overlays)

Per-clip post-production (all optional, documented in docs/TIMELINE.md): orientation (rotate/hflip/vflip), stabilisation
(vidstab two-pass or deshake), colour correction (`correct`), creative grade (`grade`, film looks in fx.post.GRADES),
effects (`effects`: punch_in, zoom, shake, vignette, grain, letterbox, blur, flash, glitch, speed_ramp), static text
(`text`), animated titles and lower thirds (`titles`), picture-in-picture / cutaways (`pip`), xfade transitions.
A timeline that uses none of these renders exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config, db
from ..fx import effects as fxe, post as fxp
from ..media import ff
from . import subs, text

GRADES = fxp.GRADES  # superset of the historical table; the original seven entries are byte-identical
AUDIO_FX = {
    "none": "",
    "denoise": "highpass=f=80,afftdn=nr=12:nf=-30",
    "voice": "highpass=f=80,afftdn=nr=10:nf=-30,acompressor=threshold=-20dB:ratio=3:attack=10:release=150:makeup=2,"
             "equalizer=f=3200:t=q:w=1.2:g=2.5",
}
SPEECH_ROLES = (None, "", "narration", "dialogue", "voice", "voiceover")  # these key the music ducking; sfx/ambience never do
DRAFT_SCALE = 0.5
_dirs_lock = threading.Lock()


def clip_duration(c: dict) -> float:
    if c["type"] == "video":
        rd = fxe.ramp_duration(c) if any((e or {}).get("type") == "speed_ramp" for e in c.get("effects") or []) else None
        return rd if rd is not None else (c["out"] - c["in"]) / float(c.get("speed", 1.0))
    return float(c["duration"])


def total_duration(tl: dict) -> float:
    d, xf = 0.0, 0.0
    for i, c in enumerate(tl["clips"]):
        d += clip_duration(c)
        if i and c.get("transition") == "crossfade":
            xf += float(c.get("transition_dur", 0.5))
    return d - xf


def clip_starts(tl: dict) -> list[float]:
    out, t = [], 0.0
    for i, c in enumerate(tl["clips"]):
        if i and c.get("transition") == "crossfade":
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


def _size(tl: dict, draft: bool) -> tuple[int, int, float]:
    W, H = tl["width"], tl["height"]
    if not draft:
        return W, H, 1.0
    return max(2, int(W * DRAFT_SCALE) // 2 * 2), max(2, int(H * DRAFT_SCALE) // 2 * 2), DRAFT_SCALE


def segment_key(c: dict, tl: dict, draft: bool = False) -> str:
    """Every clip key except pure metadata, the global look, and the sources' signatures. Unknown keys are included
    on purpose: a new pixel- or sound-changing key can never be served a stale segment."""
    payload = {k: v for k, v in c.items() if k not in ("id", "label", "notes", "transition", "transition_dur")}
    payload["_g"] = [tl["width"], tl["height"], tl.get("fps", config.FPS), tl.get("grade", "none"), tl.get("lang", "en")]
    if tl.get("audio_fx", "none") != "none":
        payload["_afx"] = tl["audio_fx"]  # was missing before: changing the global audio chain must re-render
    if draft:
        payload["_draft"] = DRAFT_SCALE
    for k in ("src",):
        if c.get(k):
            payload["_s"] = _src_sig(c[k])
    if isinstance(c.get("pip"), dict) and c["pip"].get("src"):
        payload["_ps"] = _src_sig(c["pip"]["src"])
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:20]


def _vcodec(draft: bool, threads: int | None) -> list:
    args: list = []
    try:  # media engine (workstream E) when present: hardware encoders + quality presets
        from ..media import encoders
        args = list(encoders.video_args(codec="h264", quality="draft" if draft else "balanced", draft=draft, hw="auto"))
    except Exception:  # noqa: BLE001 - optional module
        args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p"] if draft else list(config.VCODEC)
    if threads:
        args += ["-threads", threads]
    return args


def render_segment(c: dict, tl: dict, cache: Path, draft: bool = False, threads: int | None = None) -> tuple[Path, bool]:
    """Render one clip to a normalised segment. Returns (path, was_cached)."""
    W, H, scale = _size(tl, draft)
    fps = tl.get("fps", config.FPS)
    out = cache / f"seg_{segment_key(c, tl, draft)}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out, True
    with _dirs_lock:
        cache.mkdir(parents=True, exist_ok=True)
    dur = clip_duration(c)
    frames = max(1, round(dur * fps))
    dur = frames / fps
    notes: list[str] = []
    grade = GRADES.get(c.get("grade") or tl.get("grade", "none"), "")
    focus = tuple(c.get("focus", (0.5, 0.5)))
    args: list = []
    has_audio = False
    ramp = None
    if c["type"] == "video":
        info = ff.probe(c["src"])
        if info.get("video") is None:
            raise ff.FFError(f"clip {c.get('id')}: {c['src']} has no video stream")
        src_dur = float(info.get("duration") or 0)
        if src_dur and c["in"] >= src_dur - 1.0 / fps:
            raise ff.FFError(f"clip {c.get('id')}: in-point {c['in']:.3f}s is past the end of {Path(c['src']).name} ({src_dur:.3f}s)")
        has_audio = info["audio"] is not None and not c.get("mute")
        args += ["-ss", f"{c['in']:.3f}", "-t", f"{c['out'] - c['in']:.3f}", "-i", c["src"]]
        speed = float(c.get("speed", 1.0))
        ramp = fxe.ramp_setpts(c)
        pre = []
        so = fxp.stab_opts(c)
        if so:
            method = fxp.stab_method(so)
            if method:
                trf = fxp.vidstab_trf(c, so, cache, _src_sig(c["src"]), threads) if method == "vidstab" else None
                pre.append(fxp.stab_filter(so, method, trf))
            else:
                notes.append("stabilize requested but no stabilisation filter in this FFmpeg build")
        pre += fxp.orient_filters(c, notes)
        timing = ramp if ramp else f"setpts=(PTS-STARTPTS)/{speed}"
        interp = fxp.interp_filter(c, fps, notes) if (speed < 1.0 or ramp) else None
        v = ",".join([*pre, timing, f"fps={fps}", *([interp] if interp else []), _fit(W, H, c.get("fit", "cover"), focus)])
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
    geo, look = fxe.video_filters(c, W, H, fps, dur, scale)
    correct = fxp.correct_filters(c.get("correct"), notes)
    chain = ",".join([v, *geo, *correct, *([grade] if grade else []), *look, "format=yuv420p", "setsar=1"])
    fi, fo = float(c.get("fade_in", 0)), float(c.get("fade_out", 0))
    if fi:
        chain += f",fade=t=in:st=0:d={fi}"
    if fo:
        chain += f",fade=t=out:st={max(0, dur - fo):.3f}:d={fo}"
    fc = [f"[0:v]{chain}[v0]"]
    last = "v0"
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
    if c.get("titles"):
        from ..fx import titles
        targs, tlines, tlast = titles.title_graph(c, W, H, fps, dur, tl.get("lang", "en"), n_in, f"[{last}]")
        if tlines:
            args += targs
            fc += tlines
            n_in += len(targs) // 6  # each title adds one "-loop 1 -t d -i png" input (6 args)
            last = tlast.strip("[]")
    if isinstance(c.get("pip"), dict):
        pargs, plines = fxe.pip_graph(c["pip"], W, H, fps, dur, n_in, f"[{last}]", "[vp]")
        if plines:
            args += pargs
            fc += plines
            n_in += 3
            last = "vp"
        else:
            notes.append("pip source missing or unusable: picture-in-picture skipped")
    a = []
    if has_audio:
        if ramp:
            # audio follows the ramp's average rate (pitch-preserving atempo); exact per-sample ramping is not done
            a += fxe.atempo_chain((c["out"] - c["in"]) / max(dur, 1e-3))
        else:
            a += fxe.atempo_chain(float(c.get("speed", 1.0)))
        afx = AUDIO_FX.get(c.get("audio_fx") or tl.get("audio_fx", "none"), "")
        if afx:
            a.append(afx)
        if c.get("volume", 1.0) != 1.0:
            a.append(f"volume={c['volume']}")
        if fi:
            a.append(f"afade=t=in:st=0:d={fi}")
        if fo:
            a.append(f"afade=t=out:st={max(0, dur - fo):.3f}:d={fo}")
    a += [f"aresample={config.AUDIO_RATE}", "aformat=channel_layouts=stereo", f"apad=whole_dur={dur:.3f}", f"atrim=0:{dur:.3f}"]
    fc.append(f"{a_src}{','.join(a)}[a]")
    tmp = out.with_suffix(f".{threading.get_ident()}.tmp.mp4")
    ff.run([*args, "-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "[a]", "-frames:v", frames,
            "-r", fps, *_vcodec(draft, threads), "-g", fps * 2, *config.ACODEC, "-video_track_timescale", "90000",
            "-movflags", "+faststart", tmp], op="render-segment")
    got = ff.probe(tmp)
    if got.get("video") is None or float(got.get("duration") or 0) <= 0:
        tmp.unlink(missing_ok=True)
        raise ff.FFError(f"clip {c.get('id')}: FFmpeg produced an empty segment (in={c.get('in')}, out={c.get('out')}, "
                         f"src={c.get('src')})")
    os.replace(tmp, out)
    if notes:
        out.with_suffix(".notes.json").write_text(json.dumps(notes))
    return out, False


def _concat(segs: list[Path], tl: dict, out: Path, draft: bool = False) -> None:
    clips = tl["clips"]
    if not any(i and c.get("transition") == "crossfade" for i, c in enumerate(clips)):
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{s}'\n" for s in segs))
        ff.run(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out], op="concat")
        return
    args: list = []
    for s in segs:
        args += ["-i", s]
    # xfade needs identical timebases on both inputs
    fc = [f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[pv{i}]" for i in range(len(segs))]
    vlast, alast, t = "pv0", "0:a", clip_duration(clips[0])
    for i in range(1, len(segs)):
        c = clips[i]
        if c.get("transition") == "crossfade":
            d = float(c.get("transition_dur", 0.5))
            name = c.get("xfade", "fade")
            try:
                from ..fx import transitions
                name = transitions.resolve(name) if hasattr(transitions, "resolve") else name
            except Exception:  # noqa: BLE001
                pass
            fc.append(f"[{vlast}][pv{i}]xfade=transition={name}:duration={d}:offset={t - d:.3f},settb=AVTB[xv{i}]")
            fc.append(f"[{alast}][{i}:a]acrossfade=d={d}[xa{i}]")
            t += clip_duration(c) - d
        else:
            fc.append(f"[{vlast}][pv{i}]concat=n=2:v=1:a=0,settb=AVTB[xv{i}]")
            fc.append(f"[{alast}][{i}:a]concat=n=2:v=0:a=1[xa{i}]")
            t += clip_duration(c)
        vlast, alast = f"xv{i}", f"xa{i}"
    ff.run([*args, "-filter_complex", ";".join(fc), "-map", f"[{vlast}]", "-map", f"[{alast}]", "-r", tl.get("fps", config.FPS),
            *_vcodec(draft, None), *config.ACODEC, "-movflags", "+faststart", out], op="concat-xfade")


def _mix_graph(tl: dict, n_narr: int, has_music: bool, dur: float) -> tuple[list[str], str]:
    """Programme audio [0:a] and narration/dialogue items key the music ducking; sfx/ambience/music-role items are
    mixed in after the ducking so a whoosh never pumps the music and never counts as speech."""
    fc = []
    speech, other = ["[0:a]"], []
    for i, a in enumerate(tl.get("audio", [])):
        ms = int(float(a.get("start", 0)) * 1000)
        fc.append(f"[{i + 1}:a]aresample={config.AUDIO_RATE},aformat=channel_layouts=stereo,"
                  f"volume={a.get('volume', 1.0)},adelay={ms}:all=1[n{i}]")
        (speech if a.get("role") in SPEECH_ROLES else other).append(f"[n{i}]")
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
    if other:
        fc.append(f"[pre0]{''.join(other)}amix=inputs={len(other) + 1}:normalize=0:duration=first[pre]")
    else:
        fc.append("[pre0]anull[pre]")
    return fc, "pre"


def _workers(n_clips: int, workers: int | None) -> tuple[int, int | None]:
    cpu = os.cpu_count() or 2
    w = workers if workers is not None else max(1, min(4, cpu // 2))
    w = max(1, min(w, n_clips or 1))
    return w, (max(1, cpu // w) if w > 1 else None)


def render(tl: dict, out: Path, workdir: Path, on_progress=None, draft: bool = False, workers: int | None = None) -> dict:
    """Render the full timeline. Returns stats incl. how many segments were reused from cache.
    `draft` renders at half resolution with a fast preset (separate cache entries); `workers` segments render at once."""
    t0 = time.time()
    workdir.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    cache = config.CACHE / "segments"
    clips = tl["clips"]
    w, threads = _workers(len(clips), workers)
    done = [0]
    lock = threading.Lock()

    def one(c):
        r = render_segment(c, tl, cache, draft, threads)
        with lock:
            done[0] += 1
            if on_progress:
                on_progress(0.7 * done[0] / len(clips))
        return r

    if w > 1:
        with ThreadPoolExecutor(max_workers=w) as ex:
            results = list(ex.map(one, clips))
    else:
        results = [one(c) for c in clips]
    segs = [p for p, _ in results]
    cached = sum(1 for _, was in results if was)
    seg_notes = []
    for s in segs:
        nf = s.with_suffix(".notes.json")
        if nf.exists():
            seg_notes += json.loads(nf.read_text())
    prog = workdir / f"programme_{out.stem}.mp4"
    _concat(segs, tl, prog, draft)
    dur = ff.duration(prog)
    W, H, _ = _size(tl, draft)
    narr = tl.get("audio", [])
    music = tl.get("music") or {}
    has_music = bool(music.get("src"))
    inputs: list = ["-i", prog]
    for a in narr:
        inputs += ["-i", a["src"]]
    if has_music:
        inputs += ["-stream_loop", "-1", "-i", music["src"]]
    fc, pre = _mix_graph(tl, len(narr), has_music, dur)
    target = float(tl.get("target_lufs", -14.0))
    # pass 1: measure the mixed programme
    err = ff.run([*inputs, "-filter_complex", ";".join(fc + [f"[{pre}]loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json[m]"]),
                  "-map", "[m]", "-vn", "-f", "null", "-"], op="mix-measure")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    meas = json.loads(m.group(0)) if m else None
    if meas and meas["input_i"] not in ("-inf", "inf") and float(meas["input_i"]) > -60:
        ln = (f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
              f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true")
    else:
        ln = "anull"  # silent programme: nothing to normalise
    fc.append(f"[{pre}]{ln},aresample={config.AUDIO_RATE},alimiter=limit=0.79:level=disabled[aout]")
    vmap = ["-map", "0:v"]
    vlast = "0:v"
    vlines: list[str] = []
    burn = tl.get("burn_subtitles", True) and tl.get("subtitles")
    if burn:
        from ..fx import captions
        ass = captions.write_ass(tl["subtitles"], workdir / f"{out.stem}.ass", W, H, tl.get("lang", "en"),
                                 tl.get("captions"), tl.get("subtitle_style"))
        esc = str(ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        vlines.append(f"[{vlast}]ass='{esc}'[vcap]")
        vlast = "vcap"
    oargs: list = []
    if fxe.overlays_signature(tl):
        n_in = 1 + len(narr) + (1 if has_music else 0)
        oargs, olines, olast = fxe.overlay_graph(tl["overlays"], W, H, tl.get("fps", config.FPS), dur, 0.0, dur,
                                                 tl.get("lang", "en"), n_in, f"[{vlast}]")
        vlines += olines
        vlast = olast.strip("[]")
    if vlines:
        fc += vlines
        vmap = ["-map", f"[{vlast}]"]
        vcodec = _vcodec(draft, None)
    else:
        vcodec = ["-c:v", "copy"]
    ff.run([*inputs, *oargs, "-filter_complex", ";".join(fc), *vmap, "-map", "[aout]", *vcodec, *config.ACODEC, "-t", f"{dur:.3f}",
            "-movflags", "+faststart", out], op="final-mix")
    sidecars = {}
    if tl.get("subtitles"):
        sidecars["srt"] = str(subs.write_srt(tl["subtitles"], out.with_suffix(".srt")))
        sidecars["vtt"] = str(subs.write_vtt(tl["subtitles"], out.with_suffix(".vtt")))
    elapsed = time.time() - t0
    db.metric("render_rtf", elapsed / max(dur, 0.1), cached=cached, clips=len(segs), draft=draft, workers=w)
    if on_progress:
        on_progress(1.0)
    return {"output": str(out), "duration": dur, "elapsed": round(elapsed, 2), "segments": len(segs),
            "segments_from_cache": cached, "sidecars": sidecars, "draft": draft, "workers": w, "notes": sorted(set(seg_notes))}
