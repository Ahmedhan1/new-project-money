"""Encoder detection and codec/quality argument presets (WS E / MED-1).

`detect()` proves what this FFmpeg can really encode: every candidate gets a tiny test encode and is listed only if
it produced a decodable file. `ffmpeg -encoders` is not proof: builds list NVENC/QSV/AMF/VAAPI even on machines without
the hardware, and those fail at open time. The result is cached on disk keyed by the FFmpeg version line.

`video_args()` returns complete output arguments (codec, preset, rate control, pix_fmt, container tag) with CPU fallback.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
from pathlib import Path

from .. import config
from . import ff

QUALITIES = ("draft", "fast", "balanced", "high", "master")
CODECS = ("h264", "hevc", "av1")

# Preference order inside each family: software first is NOT assumed — `hw="auto"` picks a working hardware encoder.
CANDIDATES: dict[str, list[str]] = {
    "h264": ["libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox", "h264_vaapi"],
    "hevc": ["libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf"],
    "av1": ["libsvtav1", "libaom-av1", "librav1e", "av1_nvenc", "av1_qsv"],
}
SOFTWARE = {"libx264", "libx265", "libsvtav1", "libaom-av1", "librav1e"}
# h264_vaapi needs an input-side device + hwupload filter, which output args alone cannot express: it is detected and
# reported, but video_args() never auto-selects it (documented in docs/MEDIA_ENGINE.md).
NOT_AUTO = {"h264_vaapi"}
CACHE_VERSION = 1


def _cache_file() -> Path:
    return config.CACHE / "encoders.json"


def _key() -> str:
    cands = json.dumps(CANDIDATES, sort_keys=True)
    return hashlib.sha256(f"{CACHE_VERSION}|{ff.version()}|{cands}".encode()).hexdigest()[:16]


def _probe_args(enc: str) -> tuple[list[str], list[str]]:
    """(input-side args, output-side args) for a minimal test encode of `enc`."""
    pre: list[str] = []
    if enc.endswith("_vaapi"):
        pre = ["-vaapi_device", "/dev/dri/renderD128"]
        return pre, ["-vf", "format=nv12,hwupload", "-c:v", enc]
    out = ["-c:v", enc, "-pix_fmt", "nv12" if enc.endswith(("_qsv", "_videotoolbox")) else "yuv420p"]
    if enc == "libx265":
        out += ["-x265-params", "log-level=error"]
    if enc == "libaom-av1":
        out += ["-cpu-used", "8", "-row-mt", "1"]
    if enc == "libsvtav1":
        out += ["-preset", "12"]
    if enc == "librav1e":
        out += ["-speed", "10"]
    return pre, out


def probe_encoder(enc: str, workdir: Path | None = None, timeout: int = 60) -> dict:
    """Try a real 0.2 s 256x144 encode with `enc`; ok only if the output exists and decodes to the expected codec."""
    family = next((f for f, c in CANDIDATES.items() if enc in c), None)
    pre, out = _probe_args(enc)
    t0 = time.time()
    with tempfile.TemporaryDirectory(dir=workdir) as td:
        dst = Path(td) / "probe.mp4"
        try:
            ff.run([*pre, "-f", "lavfi", "-i", "testsrc2=s=256x144:r=25:d=0.2", *out, "-frames:v", "5", "-an", dst],
                   op=f"encoder-probe-{enc}", timeout=timeout)
            if not dst.exists() or dst.stat().st_size == 0:
                return {"encoder": enc, "ok": False, "why": "no output file", "seconds": round(time.time() - t0, 2)}
            info = ff.probe(dst)
            got = (info.get("video") or {}).get("codec")
            if family and got != family:
                return {"encoder": enc, "ok": False, "why": f"output codec {got!r}, expected {family}",
                        "seconds": round(time.time() - t0, 2)}
            return {"encoder": enc, "ok": True, "bytes": dst.stat().st_size, "seconds": round(time.time() - t0, 2)}
        except ff.FFError as e:
            lines = [ln.strip() for ln in (e.stderr_tail or str(e)).splitlines() if ln.strip()]
            useful = [ln for ln in lines if ln.startswith(f"[{enc} @") or "Unknown encoder" in ln
                      or "global options" in ln or "No such file" in ln]
            why = (useful or lines or [str(e)])[0]
            return {"encoder": enc, "ok": False, "why": why[:300],
                    "seconds": round(time.time() - t0, 2)}


def detect_report(refresh: bool = False, cache_file: Path | None = None) -> dict:
    """Full detection report: {"key","ffmpeg","detected":{family:[...]},"hw":[...],"results":[per-encoder]}."""
    cf = cache_file or _cache_file()
    key = _key()
    if not refresh and cf.exists():
        try:
            rep = json.loads(cf.read_text(encoding="utf-8"))
            if rep.get("key") == key:
                rep["cached"] = True
                return rep
        except ValueError:
            pass
    results = [probe_encoder(e) for fam in CANDIDATES.values() for e in fam]
    ok = {r["encoder"] for r in results if r["ok"]}
    detected = {fam: [e for e in encs if e in ok] for fam, encs in CANDIDATES.items()}
    rep = {"key": key, "ffmpeg": ff.version(), "date": time.strftime("%Y-%m-%d %H:%M:%S"), "detected": detected,
           "hw": [e for e in ok if e not in SOFTWARE], "results": results, "cached": False}
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        tmp = cf.with_suffix(".tmp")
        tmp.write_text(json.dumps(rep, indent=1), encoding="utf-8")
        tmp.replace(cf)
    except OSError:
        pass
    return rep


def detect(refresh: bool = False, cache_file: Path | None = None) -> dict:
    """{"h264":[...],"hevc":[...],"av1":[...],"hw":[...]} — only encoders that really produced a file here."""
    rep = detect_report(refresh, cache_file)
    return {**{f: list(rep["detected"].get(f, [])) for f in CODECS}, "hw": list(rep["hw"])}


# ---------------------------------------------------------------- argument presets
_X264 = {"draft": ("ultrafast", 28), "fast": ("veryfast", 23), "balanced": ("medium", 21), "high": ("slow", 19),
         "master": ("slower", 17)}
_X265 = {"draft": ("ultrafast", 30), "fast": ("veryfast", 26), "balanced": ("medium", 24), "high": ("slow", 22),
         "master": ("slower", 20)}
_SVT = {"draft": (12, 42), "fast": (10, 36), "balanced": (8, 32), "high": (6, 28), "master": (4, 24)}
_AOM = {"draft": (8, 42), "fast": (7, 36), "balanced": (6, 32), "high": (4, 28), "master": (3, 24)}
_RAV1E = {"draft": (10, 160), "fast": (9, 130), "balanced": (7, 100), "high": (5, 80), "master": (4, 60)}
_NVENC = {"draft": ("p1", 30), "fast": ("p3", 26), "balanced": ("p5", 23), "high": ("p6", 21), "master": ("p7", 19)}
_QSV = {"draft": ("veryfast", 30), "fast": ("faster", 26), "balanced": ("medium", 23), "high": ("slow", 21),
        "master": ("veryslow", 19)}
_AMF = {"draft": ("speed", 30), "fast": ("speed", 26), "balanced": ("balanced", 23), "high": ("quality", 21),
        "master": ("quality", 19)}
_VT = {"draft": 45, "fast": 55, "balanced": 62, "high": 70, "master": 78}


def _quality(quality: str, draft: bool) -> str:
    if draft:
        return "draft"
    if quality not in QUALITIES:
        raise ValueError(f"unknown quality {quality!r}; use one of {', '.join(QUALITIES)}")
    return quality


def choose_encoder(codec: str = "h264", hw: str | bool | None = "auto", available: dict | None = None) -> str:
    """Pick the encoder for `codec`. hw: "auto" (working hardware if any, else CPU), "cpu"/False/None (software only),
    or a vendor ("nvenc", "qsv", "amf", "videotoolbox"). Falls back to the first working software encoder."""
    if codec not in CANDIDATES:
        raise ValueError(f"unknown codec {codec!r}; use one of {', '.join(CODECS)}")
    av = available if available is not None else detect()
    working = av.get(codec, [])
    if hw and hw not in ("cpu", "off", "none"):
        for e in working:
            if e in SOFTWARE or e in NOT_AUTO:
                continue
            if hw == "auto" or e.endswith("_" + str(hw)):
                return e
    for e in working:
        if e in SOFTWARE:
            return e
    raise RuntimeError(f"no working {codec} encoder in this FFmpeg build (detected: {av})")


def encoder_args(enc: str, quality: str = "balanced", *, bitrate: int | None = None, keyint: int | None = None) -> list[str]:
    """Output args for a specific encoder. bitrate (bits/s) switches from constant quality to average bitrate."""
    q = _quality(quality, False)
    a: list[str] = ["-c:v", enc]
    br = [] if bitrate is None else ["-b:v", str(int(bitrate)), "-maxrate", str(int(bitrate * 1.5)),
                                     "-bufsize", str(int(bitrate * 2))]
    if enc == "libx264":
        preset, crf = _X264[q]
        a += ["-preset", preset, *(br or ["-crf", str(crf)]), "-profile:v", "high", "-pix_fmt", "yuv420p", "-tag:v", "avc1"]
    elif enc == "libx265":
        preset, crf = _X265[q]
        a += ["-preset", preset, *(br or ["-crf", str(crf)]), "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
              "-x265-params", "log-level=error"]
    elif enc == "libsvtav1":
        preset, crf = _SVT[q]
        a += ["-preset", str(preset), *(["-b:v", str(int(bitrate))] if bitrate else ["-crf", str(crf)]), "-pix_fmt", "yuv420p"]
    elif enc == "libaom-av1":
        cpu, crf = _AOM[q]
        a += ["-cpu-used", str(cpu), "-row-mt", "1", *(["-b:v", str(int(bitrate))] if bitrate else ["-crf", str(crf), "-b:v", "0"]),
              "-pix_fmt", "yuv420p"]
    elif enc == "librav1e":
        speed, qp = _RAV1E[q]
        a += ["-speed", str(speed), *(["-b:v", str(int(bitrate))] if bitrate else ["-qp", str(qp)]), "-pix_fmt", "yuv420p"]
    elif enc.endswith("_nvenc"):
        preset, cq = _NVENC[q]
        a += ["-preset", preset, "-rc", "vbr", *(br or ["-cq", str(cq), "-b:v", "0"]), "-pix_fmt", "yuv420p"]
    elif enc.endswith("_qsv"):
        preset, gq = _QSV[q]
        a += ["-preset", preset, *(br or ["-global_quality", str(gq)]), "-pix_fmt", "nv12"]
    elif enc.endswith("_amf"):
        qual, qp = _AMF[q]
        a += ["-quality", qual, *(br or ["-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp + 2)]), "-pix_fmt", "yuv420p"]
    elif enc.endswith("_videotoolbox"):
        a += [*(br or ["-q:v", str(_VT[q])]), "-pix_fmt", "nv12"]
    else:
        raise ValueError(f"no argument preset for encoder {enc!r}")
    if enc.startswith("hevc_"):
        a += ["-tag:v", "hvc1"]  # Apple players/QuickTime need hvc1, not hev1, for HEVC in MP4
    if keyint:
        a += ["-g", str(int(keyint))]
    return a


def video_args(codec: str = "h264", quality: str = "balanced", draft: bool = False, hw: str | bool | None = "auto", *,
               bitrate: int | None = None, keyint: int | None = None, available: dict | None = None) -> list[str]:
    """Complete video encoder args for codec h264|hevc|av1 at draft|fast|balanced|high|master.

    hw="auto" uses a hardware encoder only if detect() proved it works here; otherwise CPU. If a hardware encode still
    fails at run time, `export.export()` retries with `video_args(..., hw="cpu")`.
    """
    q = _quality(quality, draft)
    return encoder_args(choose_encoder(codec, hw, available), q, bitrate=bitrate, keyint=keyint)


_AAC = {"draft": 96, "fast": 128, "balanced": 160, "high": 192, "master": 256}
_OPUS = {"draft": 64, "fast": 96, "balanced": 128, "high": 160, "master": 192}


def audio_bitrate(codec: str = "aac", quality: str = "balanced") -> int:
    """Audio bitrate in bits/s for a codec/quality (used by size targeting)."""
    table = {"aac": _AAC, "opus": _OPUS}.get(codec)
    if table is None:
        raise ValueError(f"unknown audio codec {codec!r}; use aac or opus")
    return table[_quality(quality, False)] * 1000


def audio_args(codec: str = "aac", quality: str = "balanced", *, bitrate: int | None = None, rate: int = 48000) -> list[str]:
    """Audio encoder args: AAC-LC (MP4 default, universal) or Opus (WebM/MKV/MP4, better at low bitrates)."""
    br = int(bitrate or audio_bitrate(codec, quality))
    enc = {"aac": "aac", "opus": "libopus"}[codec]
    return ["-c:a", enc, "-b:a", str(br), "-ar", str(rate), "-ac", "2"]


def container_args(out: str | Path) -> list[str]:
    """Container flags for the output: MP4/MOV get faststart (moov before mdat) for web playback."""
    return ["-movflags", "+faststart"] if Path(out).suffix.lower() in (".mp4", ".mov", ".m4v") else []


def run_probe_cli() -> None:  # pragma: no cover - manual helper
    print(json.dumps(detect_report(refresh=True), indent=1))


if __name__ == "__main__":  # pragma: no cover
    run_probe_cli()
