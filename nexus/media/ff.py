"""Thin FFmpeg/ffprobe wrapper. All calls go through the Cost Guard."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .. import costguard, db


class FFError(RuntimeError):
    pass


def run(args: list[str], op: str = "ffmpeg", timeout: int = 3600) -> str:
    """Run ffmpeg; returns stderr (where filters print their analysis)."""
    costguard.authorize("ffmpeg", op)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *[str(a) for a in args]]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    if p.returncode != 0:
        raise FFError(f"ffmpeg failed ({op}): {p.stderr[-1500:]}")
    db.metric("ffmpeg_seconds", time.time() - t0, op=op)
    return p.stderr


def probe(path: str | Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise FFError(f"ffprobe failed for {path}: {p.stderr[-500:]}")
    raw = json.loads(p.stdout)
    info: dict = {"path": str(path), "duration": float(raw["format"].get("duration", 0)),
                  "size": int(raw["format"].get("size", 0)), "format": raw["format"].get("format_name"),
                  "video": None, "audio": None, "streams": len(raw["streams"])}
    for s in raw["streams"]:
        if s["codec_type"] == "video" and info["video"] is None and s.get("disposition", {}).get("attached_pic") != 1:
            num, den = (s.get("avg_frame_rate") or "0/1").split("/")
            info["video"] = {"codec": s["codec_name"], "width": s["width"], "height": s["height"],
                             "fps": round(float(num) / float(den), 3) if float(den) else 0.0,
                             "pix_fmt": s.get("pix_fmt"), "duration": float(s.get("duration", info["duration"]))}
        elif s["codec_type"] == "audio" and info["audio"] is None:
            info["audio"] = {"codec": s["codec_name"], "channels": s.get("channels"),
                             "sample_rate": int(s.get("sample_rate", 0)),
                             "duration": float(s.get("duration", info["duration"]))}
    return info


def duration(path: str | Path) -> float:
    return probe(path)["duration"]


def loudness(path: str | Path) -> dict:
    """EBU R128 integrated loudness, loudness range and true peak."""
    err = run(["-i", path, "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"], op="measure-loudness")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        raise FFError("could not measure loudness")
    j = json.loads(m.group(0))
    f = lambda k: float(j[k]) if j[k] not in ("-inf", "inf") else -99.0
    return {"lufs": f("input_i"), "true_peak": f("input_tp"), "lra": f("input_lra"), "thresh": f("input_thresh"),
            "offset": f("target_offset")}


def detect_silence(path: str | Path, noise_db: float = -35, min_dur: float = 0.6) -> list[tuple[float, float]]:
    err = run(["-i", path, "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
              op="detect-silence")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", err)]
    if len(starts) > len(ends):
        ends.append(duration(path))
    return [(max(0.0, s), e) for s, e in zip(starts, ends)]


def detect_black(path: str | Path, min_dur: float = 0.5) -> list[tuple[float, float]]:
    err = run(["-i", path, "-an", "-vf", f"blackdetect=d={min_dur}:pix_th=0.10", "-f", "null", "-"], op="detect-black")
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]


def detect_freeze(path: str | Path, min_dur: float = 2.0) -> list[tuple[float, float]]:
    err = run(["-i", path, "-an", "-vf", f"freezedetect=n=-60dB:d={min_dur}", "-f", "null", "-"], op="detect-freeze")
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", err)]
    if len(starts) > len(ends):
        ends.append(duration(path))
    return list(zip(starts, ends))


def decode_check(path: str | Path) -> list[str]:
    """Full decode; returns decoder error lines (empty list = file is intact)."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    return [l for l in p.stderr.splitlines() if l.strip()]


def extract_audio(path: str | Path, out: Path, rate: int = 16000) -> Path:
    run(["-i", path, "-vn", "-ac", "1", "-ar", rate, "-c:a", "pcm_s16le", out], op="extract-audio")
    return out
