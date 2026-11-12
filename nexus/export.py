"""Export: codec/quality/size presets, size-targeted two-pass encodes, quality comparison and output verification.

WS E / MED-1. Contract (docs/MASTER_ROADMAP.md §5):
  profiles(); export(src, out, *, codec, quality, width, height, fps, max_bytes, on_progress) -> report;
  verify(path, expect=None) -> report; compare(src, variants) -> table.
The source is only read. Outputs are written to `<out>.part.<ext>` and renamed into place when complete.
"""
from __future__ import annotations

import os
import re
import shutil
import statistics
import struct
import tempfile
import time
from pathlib import Path
from typing import Callable

from .media import encoders, ff
from .media import proxy as _proxy

# ---------------------------------------------------------------- size-targeting constants
# "Watchable" floor in bits per pixel per frame at the OUTPUT resolution/fps. Below this, blocking is obvious on
# typical footage. Calibrated loosely on CPU encoders at their slower presets; AV1/HEVC need fewer bits than H.264.
FLOOR_BPP = {"h264": 0.025, "hevc": 0.017, "av1": 0.014}
MIN_VIDEO_BPS = 100_000                      # never below 100 kbit/s whatever the resolution
CONTAINER_OVERHEAD = 0.01                    # MP4 index etc. ~0.3-1 % at these bitrates; budget 1 %
CONTAINER_OVERHEAD_FIXED = 32 * 1024         # + 32 KiB fixed (moov for short clips)
SAFETY = 0.97                                # aim 3 % under the limit: rate control overshoots a little
DOWNSCALE_LADDER = (2160, 1440, 1080, 720, 540, 480, 360)
MIN_AUDIO_BPS = {"aac": 64_000, "opus": 32_000}
MAX_ATTEMPTS = 3
TWO_PASS = {"libx264", "libx265", "libaom-av1"}


class ExportError(RuntimeError):
    pass


# ---------------------------------------------------------------- profiles
PRESETS = {
    "web_1080p": {"codec": "h264", "quality": "balanced", "width": 1920, "height": 1080, "fps": None, "audio": "aac",
                  "description": "Universal H.264/AAC 1080p MP4 for web players and upload."},
    "web_720p_fast": {"codec": "h264", "quality": "fast", "width": 1280, "height": 720, "fps": None, "audio": "aac",
                      "description": "Quick 720p H.264 export; ~4-6x faster than balanced, larger files."},
    "vertical_1080x1920": {"codec": "h264", "quality": "balanced", "width": 1080, "height": 1920, "fps": None,
                           "audio": "aac", "description": "9:16 vertical (letterboxed if the source is not 9:16)."},
    "draft_preview": {"codec": "h264", "quality": "draft", "width": 960, "height": 540, "fps": None, "audio": "aac",
                      "description": "Fastest preview for review; not for delivery."},
    "master": {"codec": "h264", "quality": "master", "width": None, "height": None, "fps": None, "audio": "aac",
               "description": "Archive/master: source resolution, slowest preset, CRF 17, AAC 256k."},
    "hevc_efficient": {"codec": "hevc", "quality": "balanced", "width": None, "height": None, "fps": None,
                       "audio": "aac", "description": "HEVC (hvc1) ~30-40 % smaller than H.264 at similar quality; "
                       "slower to encode, not supported by every browser."},
    "av1_small": {"codec": "av1", "quality": "balanced", "width": None, "height": None, "fps": None, "audio": "opus",
                  "description": "AV1 + Opus: smallest files; slowest encode; modern players only."},
    "size_25mb": {"codec": "h264", "quality": "balanced", "width": None, "height": None, "fps": None, "audio": "aac",
                  "max_bytes": 25 * 1000 * 1000, "description": "Fit under 25 MB (two-pass; may downscale)."},
    "size_100mb": {"codec": "h264", "quality": "balanced", "width": None, "height": None, "fps": None, "audio": "aac",
                   "max_bytes": 100 * 1000 * 1000, "description": "Fit under 100 MB (two-pass; may downscale)."},
}


def profiles() -> dict:
    """Export settings the UI can offer: codecs (with the encoders proven to work here), quality levels, presets."""
    try:
        av = encoders.detect()
    except Exception as e:  # noqa: BLE001 - profile listing must not fail because detection failed
        av = {"h264": [], "hevc": [], "av1": [], "hw": [], "error": str(e)}
    return {
        "codecs": {c: {"encoders": av.get(c, []), "available": bool(av.get(c))} for c in encoders.CODECS},
        "hw": av.get("hw", []),
        "qualities": {"draft": "fastest, visibly soft", "fast": "quick, slightly larger", "balanced": "default",
                      "high": "slower, fewer artefacts", "master": "slowest, archive quality"},
        "audio": ["aac", "opus", None],
        "fps": [None, 24, 25, 30, 50, 60],
        "presets": PRESETS,
        "size_target": {"floor_bits_per_pixel": FLOOR_BPP, "min_video_bps": MIN_VIDEO_BPS,
                        "note": "max_bytes encodes two-pass and verifies; refused below the floor"},
    }


# ---------------------------------------------------------------- geometry
def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def fit_dims(src_w: int, src_h: int, *, sar: float = 1.0, rotation: int = 0, width: int | None = None,
             height: int | None = None, mode: str = "pad") -> dict:
    """Output geometry without aspect distortion.

    Display aspect = stored size x SAR, swapped for 90/270 rotation (FFmpeg auto-rotates). With both width and
    height, the picture is fitted inside and padded ("pad") or filled and cropped ("crop"); with one, the other
    follows the aspect. Sizes are even (4:2:0). Returns {"scale":(w,h),"out":(W,H),"filter": str}.
    """
    dw, dh = float(src_w) * (sar or 1.0), float(src_h)
    if rotation % 180 == 90:
        dw, dh = dh, dw
    aspect = dw / dh
    if width and height:
        W, H = _even(width), _even(height)
        if mode == "crop":
            s = max(W / dw, H / dh)
        else:
            s = min(W / dw, H / dh)
        sw, sh = _even(dw * s), _even(dh * s)
        if mode != "crop":
            sw, sh = min(sw, W), min(sh, H)
    elif width:
        W = _even(width)
        H = _even(W / aspect)
        sw, sh = W, H
    elif height:
        H = _even(height)
        W = _even(H * aspect)
        sw, sh = W, H
    else:
        W, H = _even(dw), _even(dh)
        sw, sh = W, H
    flags = "lanczos" if sw * sh < dw * dh else "bicubic"
    f = [f"scale={sw}:{sh}:flags={flags}", "setsar=1"]
    if (sw, sh) != (W, H):
        f.append(f"crop={W}:{H}" if mode == "crop" else f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
    return {"scale": (sw, sh), "out": (W, H), "filter": ",".join(f)}


def floor_bps(codec: str, W: int, H: int, fps: float) -> int:
    """Lowest video bitrate (bits/s) treated as watchable at this size and frame rate."""
    return int(max(MIN_VIDEO_BPS, FLOOR_BPP[codec] * W * H * (fps or 30)))


def plan_size(duration: float, max_bytes: int, *, codec: str, W: int, H: int, fps: float, audio_bps: int) -> dict:
    """Video bitrate budget for a byte limit. ok False when it is below the watchable floor."""
    usable = max_bytes * (1 - CONTAINER_OVERHEAD) * SAFETY - CONTAINER_OVERHEAD_FIXED
    v = int(usable * 8 / max(duration, 0.01) - audio_bps)
    fl = floor_bps(codec, W, H, fps)
    return {"video_bps": v, "audio_bps": audio_bps, "floor_bps": fl, "ok": v >= fl, "size": (W, H), "fps": fps}


# ---------------------------------------------------------------- export
def _report_progress(cb, base: float, span: float, stage: str):
    if not cb:
        return None

    def f(info):
        cb({**info, "stage": stage, "fraction": base + span * (info.get("fraction") or 0.0)})
    return f


def _container_check(out: Path, codec: str, audio: str | None) -> None:
    ext = out.suffix.lower()
    if ext == ".webm" and (codec != "av1" or audio not in (None, "opus")):
        raise ValueError("WebM holds AV1/VP9 video with Opus audio only; use .mp4 or .mkv for this codec/audio")
    if ext not in (".mp4", ".mov", ".m4v", ".mkv", ".webm"):
        raise ValueError(f"unsupported output container {ext!r}; use .mp4, .mov, .mkv or .webm")


def export(src: str | Path, out: str | Path, *, codec: str = "h264", quality: str = "balanced",
           width: int | None = None, height: int | None = None, fps: float | None = None,
           max_bytes: int | None = None, audio: str | None = "aac", audio_quality: str | None = None,
           on_progress: Callable[[dict], None] | None = None, fast: bool = False, hw: str | bool | None = "auto",
           fit: str = "pad", allow_downscale: bool = True, verify_output: bool = True,
           threads: int | None = None) -> dict:
    """Encode `src` to `out`. Returns a report; with max_bytes, ok False + "refused" when the limit is unreachable.

    fast=True trades quality for speed (the "fast" preset level, single pass for size targets).
    """
    src, out = Path(src), Path(out)
    _container_check(out, codec, audio)
    t0 = time.time()
    q = "draft" if quality == "draft" else ("fast" if fast else quality)
    meta = _proxy.inspect(src)
    v = meta.get("video")
    if not v:
        raise ExportError(f"{src} has no video stream")
    dur = float(v.get("duration") or meta["duration"])
    src_fps = v.get("fps") or 30.0
    out_fps = float(fps) if fps else src_fps
    has_audio = bool(meta.get("audio")) and audio is not None
    aq = audio_quality or q
    a_bps = encoders.audio_bitrate(audio, aq) if has_audio else 0
    rep: dict = {"ok": False, "src": str(src), "out": str(out), "codec": codec, "quality": q, "fast": fast,
                 "max_bytes": max_bytes, "warnings": list(meta["warnings"]), "attempts": []}

    geo_kw = {"sar": v.get("sar") or 1.0, "rotation": v.get("rotation") or 0, "mode": fit}
    geo = fit_dims(v["width"], v["height"], width=width, height=height, **geo_kw)
    v_bps = None
    if max_bytes:
        a_ladder = [a_bps] + ([b for b in (96_000, MIN_AUDIO_BPS[audio]) if b < a_bps] if has_audio else [])
        heights = [geo["out"][1]]
        if allow_downscale and not (width or height):
            heights += [h for h in DOWNSCALE_LADDER if h < geo["out"][1]]
        tried = []
        chosen = None
        for h in heights:
            g = geo if h == geo["out"][1] else fit_dims(v["width"], v["height"], height=h, **geo_kw)
            for ab in a_ladder:
                pl = plan_size(dur, max_bytes, codec=codec, W=g["out"][0], H=g["out"][1], fps=out_fps, audio_bps=ab)
                tried.append({"size": list(g["out"]), "audio_bps": ab, "video_bps": pl["video_bps"], "floor_bps": pl["floor_bps"]})
                if pl["ok"]:
                    chosen = (g, pl)
                    break
            if chosen:
                break
        rep["size_plan"] = tried
        if not chosen:
            last = tried[-1]
            rep.update(refused=True, reason=(
                f"{max_bytes / 1e6:.2f} MB for {dur:.1f}s leaves {max(0, last['video_bps']) / 1000:.0f} kbit/s for video "
                f"at {last['size'][0]}x{last['size'][1]} (audio {last['audio_bps'] / 1000:.0f} kbit/s), below the "
                f"watchable floor of {last['floor_bps'] / 1000:.0f} kbit/s ({FLOOR_BPP[codec]} bits/pixel/frame, "
                f"min {MIN_VIDEO_BPS // 1000} kbit/s). Raise the limit, shorten the video"
                + (", or allow a lower resolution." if not allow_downscale or width or height else ".")),
                seconds=round(time.time() - t0, 3))
            return rep
        geo, pl = chosen
        if pl["size"] != fit_dims(v["width"], v["height"], width=width, height=height, **geo_kw)["out"]:
            rep["warnings"].append(f"downscaled to {pl['size'][0]}x{pl['size'][1]} to fit {max_bytes / 1e6:.2f} MB")
        if pl["audio_bps"] != a_bps:
            rep["warnings"].append(f"audio reduced to {pl['audio_bps'] // 1000} kbit/s to fit the size limit")
        a_bps, v_bps = pl["audio_bps"], pl["video_bps"]

    vf = []
    if v.get("interlaced"):
        vf.append("bwdif")
    if fps:
        vf.append(f"fps={out_fps:g}")
    vf.append(geo["filter"])
    if v.get("hdr"):
        rep["warnings"].append("HDR source exported without tone-mapping (zscale/tonemap not applied): colours may be off")
    W, H = geo["out"]
    rep.update(width=W, height=H, fps=out_fps)

    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.stem + ".part" + out.suffix)
    passdir = Path(tempfile.mkdtemp(prefix="nexus-pass-"))
    hw_mode = hw
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            enc = encoders.choose_encoder(codec, hw_mode)
            keyint = int(round(out_fps * 2))
            vargs = encoders.encoder_args(enc, q, bitrate=v_bps, keyint=keyint)
            aargs = (["-map", "0:a:0", *encoders.audio_args(audio, aq, bitrate=a_bps)] if has_audio else ["-an"])
            common = ["-i", src, "-map", "0:v:0", "-vf", ",".join(vf)]
            two = bool(max_bytes) and not fast and enc in TWO_PASS
            ta = time.time()
            try:
                if two:
                    plog = str(passdir / "pass")
                    ff.run([*common, *_pass_args(enc, vargs, 1, plog), "-an", "-f", "null", "-"], op="export-pass1",
                           total=dur, threads=threads, on_progress=_report_progress(on_progress, 0.0, 0.45, "pass1"))
                    ff.run([*common, *_pass_args(enc, vargs, 2, plog), *aargs, *encoders.container_args(out), part],
                           op="export-pass2", total=dur, threads=threads,
                           on_progress=_report_progress(on_progress, 0.45, 0.55, "pass2"))
                else:
                    ff.run([*common, *vargs, *aargs, *encoders.container_args(out), part], op="export", total=dur,
                           threads=threads, on_progress=_report_progress(on_progress, 0.0, 1.0, "encode"))
            except ff.FFError as e:
                if enc not in encoders.SOFTWARE:
                    rep["warnings"].append(f"hardware encoder {enc} failed ({e.stderr_tail.splitlines()[-1:]}); "
                                           "retrying on CPU")
                    hw_mode = "cpu"
                    continue
                raise
            size = part.stat().st_size
            rep["attempts"].append({"attempt": attempt, "encoder": enc, "two_pass": two, "video_bps": v_bps,
                                    "audio_bps": a_bps if has_audio else 0, "bytes": size,
                                    "seconds": round(time.time() - ta, 3)})
            if not max_bytes or size <= max_bytes:
                break
            # Over the limit: shrink the video budget by the measured overshoot (video share only) and retry.
            a_bytes = a_bps * dur / 8 if has_audio else 0
            got_v = max(1.0, size - a_bytes - CONTAINER_OVERHEAD_FIXED)
            want_v = max_bytes * (1 - CONTAINER_OVERHEAD) * SAFETY - a_bytes - CONTAINER_OVERHEAD_FIXED
            v_bps = int(v_bps * want_v / got_v * 0.98)
            fl = floor_bps(codec, W, H, out_fps)
            if v_bps < fl:
                part.unlink(missing_ok=True)
                rep.update(refused=True, reason=f"encoder overshot the limit; the corrected budget "
                           f"{v_bps / 1000:.0f} kbit/s is below the watchable floor {fl / 1000:.0f} kbit/s")
                return rep
        else:
            part.unlink(missing_ok=True)
            rep.update(refused=True, reason=f"still over {max_bytes} bytes after {MAX_ATTEMPTS} attempts")
            return rep
        os.replace(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(passdir, ignore_errors=True)
    size = out.stat().st_size
    rep.update(ok=True, encoder=rep["attempts"][-1]["encoder"], size=size,
               bitrate=int(size * 8 / dur) if dur else 0, duration=dur,
               encode_seconds=round(sum(a["seconds"] for a in rep["attempts"]), 3), seconds=round(time.time() - t0, 3),
               under_limit=(size <= max_bytes) if max_bytes else None)
    if verify_output:
        exp = {"codec": codec, "width": W, "height": H, "audio": has_audio, "duration": dur}
        if fps:
            exp["fps"] = out_fps
        if max_bytes:
            exp["max_bytes"] = max_bytes
        rep["verify"] = verify(out, exp)
        rep["ok"] = rep["verify"]["ok"]
    return rep


def _pass_args(enc: str, vargs: list[str], n: int, log: str) -> list[str]:
    if enc == "libx265":
        a = list(vargs)
        i = a.index("-x265-params")
        # x265-params is split on ':'; av_get_token honours backslash escapes, so a drive colon is written as '\:'.
        stats = (log + ".x265").replace("\\", "/").replace(":", "\\:")
        a[i + 1] = f"{a[i + 1]}:pass={n}:stats={stats}"
        return a
    return [*vargs, "-pass", str(n), "-passlogfile", log]


# ---------------------------------------------------------------- verify
def mp4_boxes(path: str | Path) -> dict:
    """Top-level ISO-BMFF boxes: {"boxes":[(type, offset, size)], "complete": bool, "faststart": bool|None}."""
    p = Path(path)
    total = p.stat().st_size
    boxes, complete, off = [], True, 0
    with open(p, "rb") as f:
        while off < total:
            f.seek(off)
            hdr = f.read(8)
            if len(hdr) < 8:
                complete = False
                break
            size, typ = struct.unpack(">I4s", hdr)
            hl = 8
            if size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    complete = False
                    break
                size, hl = struct.unpack(">Q", ext)[0], 16
            elif size == 0:
                size = total - off
            name = typ.decode("latin-1")
            if size < hl or not re.fullmatch(r"[\x20-\x7e]{4}", name):
                complete = False
                break
            boxes.append((name, off, size))
            if off + size > total:
                complete = False
                break
            off += size
    names = [b[0] for b in boxes]
    fs = (names.index("moov") < names.index("mdat")) if ("moov" in names and "mdat" in names) else None
    return {"boxes": boxes, "complete": complete and off == total, "faststart": fs}


def verify(path: str | Path, expect: dict | None = None, *, deep: bool = True) -> dict:
    """Check an exported file. expect keys (all optional): codec, width, height, fps, pix_fmt, duration,
    duration_tol, audio (bool), max_bytes, faststart, av_sync_tol, max_black, max_freeze.

    ok is False when any critical check fails. Loudness/true-peak and black/frozen spans are summarised.
    """
    expect = dict(expect or {})
    p = Path(path)
    checks: list[dict] = []

    def c(cid, ok, detail, severity="critical"):
        checks.append({"id": cid, "ok": bool(ok), "detail": detail, "severity": severity})

    rep: dict = {"path": str(p), "checks": checks}
    if not p.exists() or p.stat().st_size == 0:
        c("readable", False, "file missing or empty")
        rep["ok"] = False
        return rep
    try:
        info = ff.probe(p)
        c("readable", True, f"{info['format']}, {info['streams']} stream(s)")
    except ff.FFError as e:
        c("readable", False, f"container not readable: {e}")
        rep["ok"] = False
        return rep
    rep["info"] = info
    vinfo, ainfo = info.get("video"), info.get("audio")
    is_mp4 = p.suffix.lower() in (".mp4", ".mov", ".m4v")
    if is_mp4:
        bx = mp4_boxes(p)
        c("complete_tail", bx["complete"], "top-level boxes end exactly at end of file" if bx["complete"]
          else f"truncated or corrupt: boxes {[b[0] for b in bx['boxes']]} do not span the file")
        if expect.get("faststart", True):
            c("faststart", bool(bx["faststart"]), "moov before mdat (playback can start while downloading)"
              if bx["faststart"] else "moov after mdat (or missing): not web-optimised", "warning")
    c("video_present", vinfo is not None, (f"{vinfo['codec']} {vinfo['width']}x{vinfo['height']} {vinfo['fps']} fps "
                                           f"{vinfo['pix_fmt']}") if vinfo else "no video stream")
    if deep:
        an = ff.analyze_pass(p, black_min=0.5, freeze_min=float(expect.get("max_freeze", 2.0)))
        rep.update(loudness=an["loudness"], black=an["black"], frozen=an["frozen"])
        c("full_decode", not an["errors"], "decoded without errors" if not an["errors"]
          else f"{len(an['errors'])} decoder error(s): {an['errors'][:3]}")
    else:
        errs = ff.decode_check(p)
        c("full_decode", not errs, "decoded without errors" if not errs else f"{len(errs)} decoder error(s): {errs[:3]}")
    dur = info["duration"]
    if "duration" in expect:
        tol = float(expect.get("duration_tol", max(0.15, 0.01 * float(expect["duration"]))))
        c("duration", abs(dur - float(expect["duration"])) <= tol,
          f"{dur:.3f}s vs expected {float(expect['duration']):.3f}s (±{tol:.2f})")
    if vinfo:
        if "codec" in expect:
            c("codec", vinfo["codec"] == expect["codec"], f"{vinfo['codec']} (expected {expect['codec']})")
        for k in ("width", "height"):
            if k in expect:
                c(k, vinfo[k] == int(expect[k]), f"{vinfo[k]} (expected {expect[k]})")
        if "fps" in expect:
            c("fps", abs(vinfo["fps"] - float(expect["fps"])) <= 0.05, f"{vinfo['fps']} (expected {expect['fps']})")
        if "pix_fmt" in expect:
            c("pix_fmt", vinfo["pix_fmt"] == expect["pix_fmt"], f"{vinfo['pix_fmt']} (expected {expect['pix_fmt']})")
    if expect.get("audio"):
        c("audio_present", ainfo is not None, f"{ainfo['codec']} {ainfo['channels']}ch {ainfo['sample_rate']} Hz"
          if ainfo else "audio expected but the file has no audio stream")
    if vinfo and ainfo:
        tol = float(expect.get("av_sync_tol", 0.15))
        delta = round(ainfo["duration"] - vinfo["duration"], 3)
        c("av_sync", abs(delta) <= tol, f"audio-video duration delta {delta:+.3f}s (tolerance {tol}s)")
    if "max_bytes" in expect:
        c("max_bytes", info["size"] <= int(expect["max_bytes"]), f"{info['size']} bytes (limit {expect['max_bytes']})")
    if deep and rep.get("loudness"):
        L = rep["loudness"]
        c("loudness", True, f"{L['lufs']} LUFS integrated, true peak {L['true_peak']} dBTP, LRA {L['lra']} LU", "info")
        if L["true_peak"] > -1.0:
            c("true_peak", False, f"true peak {L['true_peak']} dBTP above -1 dBTP: may clip after lossy re-encode",
              "warning")
    if deep and vinfo:
        mb = float(expect.get("max_black", 1.5))
        long_black = [b for b in rep.get("black", []) if b[1] - b[0] > mb]
        c("black_frames", not long_black, f"{len(rep.get('black', []))} black span(s); longer than {mb}s: {long_black}",
          "warning")
        c("frozen_frames", True, f"{len(rep.get('frozen', []))} frozen span(s) >= "
          f"{expect.get('max_freeze', 2.0)}s: {rep.get('frozen', [])[:5]}", "info")
    rep["ok"] = all(x["ok"] for x in checks if x["severity"] == "critical")
    rep["failed"] = [x["id"] for x in checks if not x["ok"]]
    return rep


# ---------------------------------------------------------------- compare
def vmaf_available() -> bool:
    import subprocess
    p = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return " libvmaf " in p.stdout


def quality_metrics(distorted: str | Path, reference: str | Path, *, fps: float | None = None) -> dict:
    """SSIM (All, 0-1) and PSNR (average dB) of `distorted` against `reference`, at the reference's display size.
    Adds VMAF only when this FFmpeg has libvmaf."""
    ref = _proxy.inspect(reference)["video"]
    W, H = _even(ref["display_width"]), _even(ref["display_height"])
    ffps = f"fps={fps:g}," if fps else ""
    graph = (f"[0:v]{ffps}scale={W}:{H}:flags=bicubic,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[d];"
             f"[1:v]{ffps}scale={W}:{H}:flags=bicubic,setsar=1,format=yuv420p,setpts=PTS-STARTPTS,split=2[r1][r2];"
             "[d][r1]ssim[x];[x][r2]psnr")
    err = ff.run(["-i", distorted, "-i", reference, "-lavfi", graph, "-an", "-f", "null", "-"], op="quality-metrics")
    m_s = re.findall(r"SSIM .*?All:([\d.]+)", err)
    m_p = re.findall(r"PSNR .*?average:([\d.]+|inf)", err)
    out = {"ssim": float(m_s[-1]) if m_s else None,
           "psnr": (99.0 if m_p and m_p[-1] == "inf" else float(m_p[-1])) if m_p else None, "vmaf": None}
    if vmaf_available():
        e2 = ff.run(["-i", distorted, "-i", reference, "-lavfi",
                     f"[0:v]{ffps}scale={W}:{H},setsar=1,format=yuv420p[d];[1:v]{ffps}scale={W}:{H},setsar=1,format=yuv420p[r];[d][r]libvmaf",
                     "-f", "null", "-"], op="vmaf")
        mv = re.findall(r"VMAF score: ([\d.]+)", e2)
        out["vmaf"] = float(mv[-1]) if mv else None
    return out


def compare(src: str | Path, variants: list[dict], workdir: str | Path | None = None, keep: bool = False) -> dict:
    """Encode each variant (export() kwargs + optional "name"/"ext") and tabulate size, bitrate, encode time, SSIM, PSNR.

    Returns {"rows":[...], "vmaf": bool, "note": ...}. Files are deleted unless keep=True."""
    src = Path(src)
    wd = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nexus-compare-"))
    wd.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        for i, var in enumerate(variants):
            kw = {k: v for k, v in var.items() if k not in ("name", "ext")}
            name = var.get("name") or f"{kw.get('codec', 'h264')}_{kw.get('quality', 'balanced')}_{i}"
            out = wd / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}{var.get('ext', '.mp4')}"
            r = export(src, out, verify_output=False, **kw)
            row = {"name": name, **{k: kw.get(k) for k in ("codec", "quality", "width", "height", "fps", "max_bytes")}}
            if not r["ok"]:
                row.update(ok=False, reason=r.get("reason"))
            else:
                row.update(ok=True, encoder=r["encoder"], bytes=r["size"], kbps=round(r["bitrate"] / 1000, 1),
                           encode_seconds=r["encode_seconds"], width=r["width"], height=r["height"],
                           **quality_metrics(out, src, fps=kw.get("fps")))
                if keep:
                    row["path"] = str(out)
            rows.append(row)
    finally:
        if not keep and not workdir:
            shutil.rmtree(wd, ignore_errors=True)
    return {"src": str(src), "rows": rows, "vmaf": vmaf_available(),
            "note": "SSIM 1.0 = identical (>0.98 is hard to tell apart, <0.93 visibly degraded); PSNR in dB "
                    "(>40 excellent, 35-40 good, <30 poor). Measured against the source at its own resolution."}


# ---------------------------------------------------------------- benchmark (python -m nexus.export bench)
def _median_spread(xs: list[float]) -> dict:
    return {"median": round(statistics.median(xs), 3), "min": round(min(xs), 3), "max": round(max(xs), 3), "runs": xs}
