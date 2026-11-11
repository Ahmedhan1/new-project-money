"""Timeline -> video.

Three stages, each cached so that changing one scene only re-renders that scene:
  1. every clip -> a normalised segment (same size/fps/codec), cached by content hash
  2. segments -> programme (stream-copy concat, or xfade when crossfades are requested)
  3. final mix: narration + ducked music + two-pass loudness normalisation (+ burned captions)
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .. import config, db
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


def clip_duration(c: dict) -> float:
    if c["type"] == "video":
        return (c["out"] - c["in"]) / float(c.get("speed", 1.0))
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


def segment_key(c: dict, tl: dict) -> str:
    payload = {k: v for k, v in c.items() if k not in ("id", "label", "notes", "transition", "transition_dur")}
    payload["_g"] = [tl["width"], tl["height"], tl.get("fps", config.FPS), tl.get("grade", "none"), tl.get("lang", "en")]
    if c.get("src"):
        payload["_s"] = _src_sig(c["src"])
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def render_segment(c: dict, tl: dict, cache: Path) -> tuple[Path, bool]:
    """Render one clip to a normalised segment. Returns (path, was_cached)."""
    W, H, fps = tl["width"], tl["height"], tl.get("fps", config.FPS)
    out = cache / f"seg_{segment_key(c, tl)}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out, True
    cache.mkdir(parents=True, exist_ok=True)
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
        v = f"setpts=(PTS-STARTPTS)/{speed},fps={fps},{_fit(W, H, c.get('fit', 'cover'), focus)}"
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
    chain = v + (f",{grade}" if grade else "") + ",format=yuv420p,setsar=1"
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
    a = []
    if has_audio:
        sp = float(c.get("speed", 1.0))
        while sp > 2.0:
            a.append("atempo=2.0"); sp /= 2
        while sp < 0.5:
            a.append("atempo=0.5"); sp *= 2
        if abs(sp - 1.0) > 1e-3:
            a.append(f"atempo={sp}")
        fx = AUDIO_FX.get(c.get("audio_fx") or tl.get("audio_fx", "none"), "")
        if fx:
            a.append(fx)
        if c.get("volume", 1.0) != 1.0:
            a.append(f"volume={c['volume']}")
        if fi:
            a.append(f"afade=t=in:st=0:d={fi}")
        if fo:
            a.append(f"afade=t=out:st={max(0, dur - fo):.3f}:d={fo}")
    a += [f"aresample={config.AUDIO_RATE}", "aformat=channel_layouts=stereo", f"apad=whole_dur={dur:.3f}", f"atrim=0:{dur:.3f}"]
    fc.append(f"{a_src}{','.join(a)}[a]")
    tmp = out.with_suffix(".tmp.mp4")
    ff.run([*args, "-filter_complex", ";".join(fc), "-map", f"[{last}]", "-map", "[a]", "-frames:v", frames,
            "-r", fps, *config.VCODEC, "-g", fps * 2, *config.ACODEC, "-video_track_timescale", "90000",
            "-movflags", "+faststart", tmp], op="render-segment")
    tmp.rename(out)
    return out, False


def _concat(segs: list[Path], tl: dict, out: Path) -> None:
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
            fc.append(f"[{vlast}][pv{i}]xfade=transition={c.get('xfade', 'fade')}:duration={d}:offset={t - d:.3f},settb=AVTB[xv{i}]")
            fc.append(f"[{alast}][{i}:a]acrossfade=d={d}[xa{i}]")
            t += clip_duration(c) - d
        else:
            fc.append(f"[{vlast}][pv{i}]concat=n=2:v=1:a=0,settb=AVTB[xv{i}]")
            fc.append(f"[{alast}][{i}:a]concat=n=2:v=0:a=1[xa{i}]")
            t += clip_duration(c)
        vlast, alast = f"xv{i}", f"xa{i}"
    ff.run([*args, "-filter_complex", ";".join(fc), "-map", f"[{vlast}]", "-map", f"[{alast}]", "-r", tl.get("fps", config.FPS),
            *config.VCODEC, *config.ACODEC, "-movflags", "+faststart", out], op="concat-xfade")


def _mix_graph(tl: dict, n_narr: int, has_music: bool, dur: float) -> tuple[list[str], str]:
    fc = []
    speech = ["[0:a]"]
    for i, a in enumerate(tl.get("audio", [])):
        ms = int(float(a.get("start", 0)) * 1000)
        fc.append(f"[{i + 1}:a]aresample={config.AUDIO_RATE},aformat=channel_layouts=stereo,"
                  f"volume={a.get('volume', 1.0)},adelay={ms}:all=1[n{i}]")
        speech.append(f"[n{i}]")
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
            fc.append("[sp1][duck]amix=inputs=2:normalize=0:duration=first[pre]")
        else:
            fc.append("[sp][mus]amix=inputs=2:normalize=0:duration=first[pre]")
    else:
        fc.append("[sp]anull[pre]")
    return fc, "pre"


def render(tl: dict, out: Path, workdir: Path, on_progress=None) -> dict:
    """Render the full timeline. Returns stats incl. how many segments were reused from cache."""
    t0 = time.time()
    workdir.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    cache = config.CACHE / "segments"
    segs, cached = [], 0
    for i, c in enumerate(tl["clips"]):
        p, was = render_segment(c, tl, cache)
        segs.append(p)
        cached += was
        if on_progress:
            on_progress(0.7 * (i + 1) / len(tl["clips"]))
    prog = workdir / f"programme_{out.stem}.mp4"
    _concat(segs, tl, prog)
    dur = ff.duration(prog)
    W, H = tl["width"], tl["height"]
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
    burn = tl.get("burn_subtitles", True) and tl.get("subtitles")
    if burn:
        ass = subs.write_ass(tl["subtitles"], workdir / f"{out.stem}.ass", W, H, tl.get("lang", "en"), tl.get("subtitle_style"))
        esc = str(ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        fc.append(f"[0:v]ass='{esc}'[vout]")
        vmap = ["-map", "[vout]"]
        vcodec = config.VCODEC
    else:
        vcodec = ["-c:v", "copy"]
    ff.run([*inputs, "-filter_complex", ";".join(fc), *vmap, "-map", "[aout]", *vcodec, *config.ACODEC, "-t", f"{dur:.3f}",
            "-movflags", "+faststart", out], op="final-mix")
    sidecars = {}
    if tl.get("subtitles"):
        sidecars["srt"] = str(subs.write_srt(tl["subtitles"], out.with_suffix(".srt")))
        sidecars["vtt"] = str(subs.write_vtt(tl["subtitles"], out.with_suffix(".vtt")))
    elapsed = time.time() - t0
    db.metric("render_rtf", elapsed / max(dur, 0.1), cached=cached, clips=len(segs))
    if on_progress:
        on_progress(1.0)
    return {"output": str(out), "duration": dur, "elapsed": round(elapsed, 2), "segments": len(segs),
            "segments_from_cache": cached, "sidecars": sidecars}
