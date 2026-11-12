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
    # speech edits: as `voice`, plus dynamic level matching so passages recorded at different levels meet at the cut
    "voice_level": "highpass=f=80,afftdn=nr=10:nf=-30,dynaudnorm=f=250:g=15:p=0.9:m=8:s=6,"
                   "acompressor=threshold=-20dB:ratio=3:attack=10:release=150:makeup=2,equalizer=f=3200:t=q:w=1.2:g=2.5",
}
SPEECH_ROLES = (None, "", "narration", "dialogue", "voice", "voiceover")  # these key the music ducking; sfx/ambience never do
DRAFT_SCALE = 0.5
_dirs_lock = threading.Lock()


def clip_duration(c: dict) -> float:
    if c["type"] == "video":
        rd = fxe.ramp_duration(c) if any((e or {}).get("type") == "speed_ramp" for e in c.get("effects") or []) else None
        base = rd if rd is not None else (c["out"] - c["in"]) / float(c.get("speed", 1.0))
        return base + (fxp.freeze_total(c) if c.get("freeze") else 0.0)
    return float(c["duration"])


def kf_expr(points, var: str = "t") -> str | None:
    """Keyframes [[t, value, "linear"|"ease"], ...] -> an FFmpeg expression in `var` (held before the first and after
    the last key)."""
    pts = sorted((float(p[0]), float(p[1]), (p[2] if len(p) > 2 else "linear")) for p in points or [] if len(p) >= 2)
    if not pts:
        return None
    if len(pts) == 1:
        return f"{pts[0][1]:.5f}"
    expr = f"{pts[-1][1]:.5f}"
    for (t0, v0, mode), (t1, v1, _) in reversed(list(zip(pts, pts[1:]))):
        u = f"(({var}-{t0:.4f})/{max(t1 - t0, 1e-4):.4f})"
        if mode == "ease":
            u = f"({u}*{u}*(3-2*{u}))"
        expr = f"if(lt({var},{t1:.4f}),{v0:.5f}+({v1 - v0:.5f})*{u},{expr})"
    return f"if(lt({var},{pts[0][0]:.4f}),{pts[0][1]:.5f},{expr})"


def _fit_kf(W: int, H: int, c: dict, focus) -> str:
    """Cover fit whose crop position and zoom follow clip['keyframes'] x/y (0..1 subject position) and scale (>= 1)."""
    kf = c.get("keyframes") or {}
    xe = kf_expr(kf.get("x")) or f"{float(focus[0]):.4f}"
    ye = kf_expr(kf.get("y")) or f"{float(focus[1]):.4f}"
    f = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
         f"crop={W}:{H}:x='(iw-{W})*clip({xe},0,1)':y='(ih-{H})*clip({ye},0,1)'")
    ze = kf_expr(kf.get("scale"), "ot")
    if ze:
        f += f",zoompan=z='max(1,{ze})':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s={W}x{H}"
    return f


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
        if "clean_speech" in (tl.get("audio_fx"), c.get("audio_fx")):
            from ..audio import post as apost
            payload["_afx_model"] = apost.chain_signature()  # model installed or not, and the exact filters, change the sound
    if draft:
        payload["_draft"] = DRAFT_SCALE
    for k in ("src",):
        if c.get(k):
            payload["_s"] = _src_sig(c[k])
    if isinstance(c.get("pip"), dict) and c["pip"].get("src"):
        payload["_ps"] = _src_sig(c["pip"]["src"])
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:20]


def _vcodec(draft: bool, threads: int | None) -> list:
    args: list = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p"] if draft else list(config.VCODEC)
    try:  # a hardware H.264 encoder that encoders.detect() PROVED works here; CPU keeps the measured x264 settings above
        from ..media import encoders
        enc = encoders.choose_encoder("h264", "auto")
        if enc not in encoders.SOFTWARE:
            args = list(encoders.video_args(codec="h264", quality="draft" if draft else "high", draft=draft, hw="auto"))
    except Exception:  # noqa: BLE001 - detection unavailable: stay on the CPU settings
        pass
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
        v_pre = ",".join([*pre, timing, f"fps={fps}", *([interp] if interp else [])])
        kfit = c.get("keyframes") and any(k in c["keyframes"] for k in ("x", "y", "scale")) and c.get("fit", "cover") == "cover"
        fit = _fit_kf(W, H, c, focus) if kfit else _fit(W, H, c.get("fit", "cover"), focus)
        v = f"{v_pre},{fit}"
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
    chain = ",".join([*geo, *correct, *([grade] if grade else []), *look, "format=yuv420p", "setsar=1"])
    fi, fo = float(c.get("fade_in", 0)), float(c.get("fade_out", 0))
    if fi:
        chain += f",fade=t=in:st=0:d={fi}"
    if fo:
        chain += f",fade=t=out:st={max(0, dur - fo):.3f}:d={fo}"
    fc: list[str] = []
    sopts = fxp.split_opts(c) if c["type"] == "video" else None
    if c["type"] == "video" and (c.get("freeze") or sopts):
        fc.append(f"[0:v]{v_pre}[vb]")
        cur = "[vb]"
        if c.get("freeze"):
            cur = fxp.freeze_video(fc, cur, dur - fxp.freeze_total(c), c, fps)
        if sopts:
            sargs, cur = fxp.split_graph(sopts, fc, cur, W, H, fps, dur, n_in, scale)
            args += sargs
            n_in += sum(1 for x in sargs if x == "-i")
        else:
            fc.append(f"{cur}{fit}[vf]")
            cur = "[vf]"
    else:
        fc.append(f"[0:v]{v}[vf]")
        cur = "[vf]"
    ko = fxp.chroma_opts(c)
    if ko:
        cargs, cur = fxp.chroma_graph(ko, fc, cur, W, H, fps, dur, n_in, "ck", notes)
        args += cargs
        n_in += sum(1 for x in cargs if x == "-i")
    fc.append(f"{cur}{chain}[v0]")
    last = "v0"
    if fxp.masks(c):
        margs, mcur = fxp.mask_graph(c, fc, "[v0]", W, H, dur, n_in, scale, "mk")
        args += margs
        n_in += sum(1 for x in margs if x == "-i")
        last = mcur.strip("[]")
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
            n_in += sum(1 for a in pargs if a == "-i")
            last = "vp"
        else:
            notes.append("pip source missing or unusable: picture-in-picture skipped")
    if isinstance(c.get("credits"), dict):
        from ..fx import titles
        cargs, clines, clast = titles.credits_lines(c["credits"], W, H, dur, tl.get("lang", "en"), n_in, f"[{last}]")
        args += cargs
        fc += clines
        n_in += 1
        last = clast.strip("[]")
    a = []
    if has_audio:
        # speaker level matching first (source time: the segment's audio starts at the clip's in-point), so the
        # gate and leveller below see both voices at the same level
        for gs in c.get("gain_spans") or []:
            a.append(f"volume=enable='between(t,{float(gs['in']) - c['in']:.3f},{float(gs['out']) - c['in']:.3f})':"
                     f"volume={float(gs['db']):.2f}dB")
        if ramp:
            # audio follows the ramp's average rate (pitch-preserving atempo); exact per-sample ramping is not done
            a += fxe.atempo_chain((c["out"] - c["in"]) / max(dur, 1e-3))
        else:
            a += fxe.atempo_chain(float(c.get("speed", 1.0)))
        afx_name = c.get("audio_fx") or tl.get("audio_fx", "none")
        if afx_name == "clean_speech":
            from ..audio import post as apost
            afx = apost.segment_chain("clean_speech")
        else:
            afx = AUDIO_FX.get(afx_name, "")
        if afx:
            a.append(afx)
        if c.get("volume", 1.0) != 1.0:
            a.append(f"volume={c['volume']}")
        ve = kf_expr((c.get("keyframes") or {}).get("volume"))
        if ve:
            a.append(f"volume='{ve}':eval=frame")
        afi = max(fi, float(c.get("audio_fade_in", 0)))   # J/L-cuts: the neighbour's audio crosses the cut
        afo = max(fo, float(c.get("audio_fade_out", 0)))
        if afi:
            a.append(f"afade=t=in:st=0:d={afi}")
        if afo:
            a.append(f"afade=t=out:st={max(0, dur - afo):.3f}:d={afo}")
    if c.get("freeze") and has_audio:
        fc.append(f"{a_src}{','.join(a) or 'anull'}[ab]")
        a_src, a = fxp.freeze_audio(fc, "[ab]", dur - fxp.freeze_total(c), c), []
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
        lst.write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")  # FFmpeg reads concat lists as UTF-8
        ff.run(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", "-movflags", "+faststart", out], op="concat")
        return
    args: list = []
    for s in segs:
        args += ["-i", s]
    # Offsets come from the frame-rounded lengths actually rendered, and every segment's audio is held to its picture
    # (pad/trim): planned durations drifted 0.3 s against the picture over a 44-clip edit with crossfades.
    vd = []
    for s, c in zip(segs, clips):
        pv = ff.probe(s).get("video") or {}
        vd.append(float(pv.get("duration") or clip_duration(c)))
    # xfade needs identical timebases on both inputs
    fc = [f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[pv{i}]" for i in range(len(segs))]
    fc += [f"[{i}:a]apad,atrim=0:{vd[i]:.6f},asetpts=PTS-STARTPTS[pa{i}]" for i in range(len(segs))]
    vlast, alast, t = "pv0", "pa0", vd[0]
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
            fc.append(f"[{alast}][pa{i}]acrossfade=d={d}[xa{i}]")
            t += vd[i] - d
        else:
            fc.append(f"[{vlast}][pv{i}]concat=n=2:v=1:a=0,settb=AVTB[xv{i}]")
            fc.append(f"[{alast}][pa{i}]concat=n=2:v=0:a=1[xa{i}]")
            t += vd[i]
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


def _jl_cuts(tl: dict, workdir: Path) -> dict:
    """J-cut (clip['audio_lead']=L: its audio starts L s before its picture) and L-cut (clip['audio_tail']=T: its audio
    continues T s under the next picture). The extra audio is taken from the source beyond the cut and mixed in as
    dialogue; the neighbour's own audio fades across the same span. Returns a modified copy of the timeline."""
    if not any(c.get("audio_lead") or c.get("audio_tail") for c in tl["clips"]):
        return tl
    import copy as _copy
    tl = _copy.deepcopy(tl)
    starts = clip_starts(tl)
    for i, c in enumerate(tl["clips"]):
        if c["type"] != "video" or c.get("mute"):
            continue
        sp = float(c.get("speed", 1.0))
        for key, neighbour in (("audio_lead", i - 1), ("audio_tail", i + 1)):
            span = float(c.get(key) or 0)
            if span <= 0 or not (0 <= neighbour < len(tl["clips"])):
                continue
            span = min(span, clip_duration(tl["clips"][neighbour]) * 0.8)
            a0 = c["in"] - span * sp if key == "audio_lead" else c["out"]
            if a0 < 0:
                continue
            wav = workdir / f"jl_{c['id']}_{key}_{span:.2f}.wav"
            ff.run(["-ss", f"{a0:.3f}", "-t", f"{span * sp:.3f}", "-i", c["src"], "-vn", "-ac", "2", "-ar", config.AUDIO_RATE,
                    "-af", f"{','.join(fxe.atempo_chain(sp)) or 'anull'},afade=t={'in' if key == 'audio_lead' else 'out'}:"
                           f"st={0 if key == 'audio_lead' else max(0.0, span - 0.25):.3f}:d={min(0.25, span / 2):.3f}", wav],
                   op="jl-cut")
            at = starts[i] - span if key == "audio_lead" else starts[i] + clip_duration(c)
            tl.setdefault("audio", []).append({"src": str(wav), "start": round(at, 3), "role": "dialogue", "volume": 1.0,
                                               "clip": c["id"], "jl": key})
            nb = tl["clips"][neighbour]
            nb["audio_fade_out" if key == "audio_lead" else "audio_fade_in"] = round(span, 3)
    return tl


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
    tl = _jl_cuts(tl, workdir)
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
            seg_notes += json.loads(nf.read_text(encoding="utf-8"))
    prog = workdir / f"programme_{out.stem}.mp4"
    _concat(segs, tl, prog, draft)
    # The picture defines the programme length: each concatenated segment carries a few ms of AAC priming/padding, which
    # added up to 0.3 s of extra audio over 44 segments in the acceptance edit (QA: A/V stream length mismatch).
    _pp = ff.probe(prog)
    dur = float((_pp.get("video") or {}).get("duration") or _pp["duration"])
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
