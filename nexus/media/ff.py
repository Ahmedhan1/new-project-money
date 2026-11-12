"""Thin FFmpeg/ffprobe wrapper. All calls go through the Cost Guard.

Hardening (WS E / MED-1):
  * `FFError` carries the operation, command, return code and the tail of stderr (`.stderr_tail`).
  * every call uses `-nostdin` and a timeout; a timeout raises `FFError(timed_out=True)`.
  * `run(..., on_progress=cb, total=seconds)` streams `-progress` output to a callback (`parse_progress`).
  * `run(..., threads=n)` caps encoder/filter threads so parallel callers do not oversubscribe the CPU;
    `NEXUS_FF_THREADS` sets a process-wide default (unset = FFmpeg's own choice, as before).
  * `filter_path()` / `escape_filter_value()` escape paths and text for filter arguments (Windows drive colons,
    backslashes, quotes, commas, brackets).
  * `analyze_pass()` does decode-check + black + freeze + EBU R128 in ONE decode instead of four.
Every pre-existing function keeps its signature (render.py, qa.py and others import them).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .. import costguard, db


class FFError(RuntimeError):
    """FFmpeg/ffprobe failure with enough context to diagnose it (the tail of stderr, not the whole log)."""

    def __init__(self, message: str, *, op: str = "ffmpeg", cmd: list[str] | None = None, returncode: int | None = None,
                 stderr: str = "", timed_out: bool = False):
        super().__init__(message)
        self.op, self.cmd, self.returncode, self.timed_out = op, cmd or [], returncode, timed_out
        self.stderr_tail = tail(stderr)


def tail(text: str, lines: int = 25, chars: int = 1500) -> str:
    """Last `lines` lines (at most `chars` characters) of an FFmpeg log: where the actual error is."""
    return "\n".join((text or "").strip().splitlines()[-lines:])[-chars:]


# ---------------------------------------------------------------- threads
def default_threads() -> int | None:
    """Process-wide thread cap from NEXUS_FF_THREADS (None/0 = let FFmpeg decide)."""
    v = os.environ.get("NEXUS_FF_THREADS", "").strip()
    return int(v) if v.isdigit() and int(v) > 0 else None


def threads_for(parallel_jobs: int = 1) -> int:
    """Threads each of `parallel_jobs` concurrent FFmpeg processes should use on this machine (>= 1)."""
    return max(1, (os.cpu_count() or 1) // max(1, parallel_jobs))


def _with_threads(args: list, n: int | None) -> list:
    """Insert `-threads n -filter_complex_threads n` as output options (just before the last output)."""
    if not n or not args:
        return list(args)
    return [*args[:-1], "-threads", str(n), "-filter_complex_threads", str(n), args[-1]]


# ---------------------------------------------------------------- progress
def parse_progress(lines) -> list[dict]:
    """Parse FFmpeg `-progress` key=value output into one dict per report block (block ends at `progress=`)."""
    blocks, cur = [], {}
    for line in (lines.splitlines() if isinstance(lines, str) else lines):
        k, sep, v = line.strip().partition("=")
        if not sep:
            continue
        cur[k] = v
        if k == "progress":
            blocks.append(_progress_block(cur))
            cur = {}
    return blocks


def _progress_block(raw: dict) -> dict:
    us = raw.get("out_time_us") or raw.get("out_time_ms")  # both are microseconds in FFmpeg
    try:
        t = max(0.0, int(us) / 1e6) if us not in (None, "N/A") else 0.0
    except ValueError:
        t = 0.0
    sp = (raw.get("speed") or "").rstrip("x").strip()
    return {"out_time": t, "frame": int(raw["frame"]) if raw.get("frame", "").isdigit() else None,
            "fps": _num(raw.get("fps")), "speed": _num(sp), "total_size": _num(raw.get("total_size")),
            "done": raw.get("progress") == "end"}


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- run
def run(args: list, op: str = "ffmpeg", timeout: int = 3600, *, threads: int | None = None,
        on_progress: Callable[[dict], None] | None = None, total: float | None = None) -> str:
    """Run ffmpeg; returns stderr (where filters print their analysis).

    on_progress(info) receives {"out_time","fraction"(0-1 when `total` is given),"speed","fps","frame","done"}.
    If the callback raises, FFmpeg is killed and the exception propagates (used for cancellation).
    """
    costguard.authorize("ffmpeg", op)
    args = _with_threads([str(a) for a in args], threads if threads is not None else default_threads())
    t0 = time.time()
    if on_progress is None:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        except subprocess.TimeoutExpired as e:
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            raise FFError(f"ffmpeg timed out after {timeout}s ({op}): {tail(err, 8)}", op=op, cmd=cmd, stderr=err,
                          timed_out=True) from None
        rc, err = p.returncode, p.stderr
    else:
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
        rc, err, timed_out = _run_streaming(cmd, timeout, on_progress, total)
        if timed_out:
            raise FFError(f"ffmpeg timed out after {timeout}s ({op}): {tail(err, 8)}", op=op, cmd=cmd, stderr=err,
                          timed_out=True)
    if rc != 0:
        raise FFError(f"ffmpeg failed ({op}): {tail(err)}", op=op, cmd=cmd, returncode=rc, stderr=err)
    try:
        db.metric("ffmpeg_seconds", time.time() - t0, op=op)
    except Exception:  # noqa: BLE001 - a metrics write must never fail an encode (e.g. no NEXUS_HOME yet)
        pass
    return err


def _run_streaming(cmd: list[str], timeout: int, cb, total: float | None) -> tuple[int, str, bool]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, stdin=subprocess.DEVNULL, text=True,
                             errors="replace")
        fired = threading.Event()

        def _kill():
            fired.set()
            p.kill()
        timer = threading.Timer(timeout, _kill)
        timer.start()
        try:
            cur: dict = {}
            for line in p.stdout:
                k, sep, v = line.strip().partition("=")
                if not sep:
                    continue
                cur[k] = v
                if k == "progress":
                    info = _progress_block(cur)
                    cur = {}
                    info["fraction"] = (min(1.0, info["out_time"] / total) if total else None)
                    if info["done"]:
                        info["fraction"] = 1.0
                    cb(info)
            p.wait()
        except BaseException:
            p.kill()
            p.wait()
            raise
        finally:
            timer.cancel()
            if p.stdout:
                p.stdout.close()
        errf.seek(0)
        return p.returncode, errf.read(), fired.is_set()


# ---------------------------------------------------------------- filter-argument escaping
def escape_filter_value(value: str) -> str:
    """Escape a value for use inside a filtergraph option (`filter=key=VALUE`), both escaping levels.

    Level 1 (option value): \\ ' : are backslash-escaped. Level 2 (filtergraph): \\ ' [ ] , ; are escaped again.
    Works unquoted, so the value can contain any of those characters.
    """
    l1 = "".join("\\" + c if c in "\\':" else c for c in str(value))
    return "".join("\\" + c if c in "\\'[],;" else c for c in l1)


def filter_path(path: str | Path) -> str:
    r"""A file path ready to drop into a filter argument, e.g. `subtitles=filename=` + filter_path(p).

    Windows paths: backslashes become forward slashes (FFmpeg accepts them on Windows) and the drive colon is
    escaped, so `C:\Users\me\a b.srt` -> `C\\\:/Users/me/a b.srt`.
    """
    return escape_filter_value(str(path).replace("\\", "/"))


def concat_list_line(path: str | Path) -> str:
    """One `file '...'` line for the concat demuxer (single quotes escaped as '\\'')."""
    return "file '" + str(path).replace("\\", "/").replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- probe
def probe_raw(path: str | Path, extra: list[str] | None = None, timeout: int = 120) -> dict:
    """Full ffprobe JSON (format + streams, incl. side data) — for callers that need more than `probe()`."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", *(extra or []), str(path)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace",
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise FFError(f"ffprobe timed out for {path}", op="ffprobe", cmd=cmd, timed_out=True) from None
    if p.returncode != 0:
        raise FFError(f"ffprobe failed for {path}: {tail(p.stderr, 8, 500)}", op="ffprobe", cmd=cmd,
                      returncode=p.returncode, stderr=p.stderr)
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise FFError(f"ffprobe returned no JSON for {path}", op="ffprobe", cmd=cmd) from None


def probe(path: str | Path) -> dict:
    raw = probe_raw(path)
    if "format" not in raw:
        raise FFError(f"ffprobe found no container in {path}", op="ffprobe")
    info: dict = {"path": str(path), "duration": float(raw["format"].get("duration", 0)),
                  "size": int(raw["format"].get("size", 0)), "format": raw["format"].get("format_name"),
                  "video": None, "audio": None, "streams": len(raw.get("streams", []))}
    for s in raw.get("streams", []):
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


def version() -> str:
    """First line of `ffmpeg -version` (cache key for encoder detection)."""
    try:
        p = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return (p.stdout.splitlines() or ["unknown"])[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


# ---------------------------------------------------------------- analysis
def loudness(path: str | Path) -> dict:
    """EBU R128 integrated loudness, loudness range and true peak."""
    err = run(["-i", path, "-vn", "-af", "loudnorm=print_format=json", "-f", "null", "-"], op="measure-loudness")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        raise FFError("could not measure loudness", op="measure-loudness", stderr=err)
    j = json.loads(m.group(0))

    def f(k):
        return float(j[k]) if j[k] not in ("-inf", "inf") else -99.0
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


def _black(err: str) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]


def _freeze(err: str, end: float) -> list[tuple[float, float]]:
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", err)]
    if len(starts) > len(ends):
        ends.append(end)
    return list(zip(starts, ends))


def detect_black(path: str | Path, min_dur: float = 0.5) -> list[tuple[float, float]]:
    err = run(["-i", path, "-an", "-vf", f"blackdetect=d={min_dur}:pic_th=0.99:pix_th=0.035", "-f", "null", "-"], op="detect-black")
    return _black(err)


def detect_freeze(path: str | Path, min_dur: float = 2.0) -> list[tuple[float, float]]:
    err = run(["-i", path, "-an", "-vf", f"freezedetect=n=-60dB:d={min_dur}", "-f", "null", "-"], op="detect-freeze")
    return _freeze(err, duration(path) if "freeze_start" in err else 0.0)


def decode_check(path: str | Path, timeout: int = 3600) -> list[str]:
    """Full decode; returns decoder error lines (empty list = file is intact)."""
    try:
        p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
                           capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return [f"decode check timed out after {timeout}s"]
    lines = [ln for ln in p.stderr.splitlines() if ln.strip()]
    if p.returncode != 0 and not lines:
        lines = [f"ffmpeg exited with code {p.returncode}"]
    return lines


def analyze_pass(path: str | Path, *, black_min: float = 0.5, freeze_min: float = 2.0, video: bool = True,
                 audio: bool = True, timeout: int = 3600) -> dict:
    """One full decode that yields decode errors, black and frozen spans and EBU R128 loudness/true peak.

    Returns {"errors":[...], "black":[(s,e)], "frozen":[(s,e)], "loudness": {"lufs","lra","true_peak"} | None}.
    """
    costguard.authorize("ffmpeg", "analyze-pass")
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "level+info", "-i", str(path)]
    if video:
        cmd += ["-map", "0:v:0?", "-vf", f"blackdetect=d={black_min}:pic_th=0.99:pix_th=0.035,"
                f"freezedetect=n=-60dB:d={freeze_min}", "-f", "null", "-"]
    if audio:
        cmd += ["-map", "0:a:0?", "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"errors": [f"analysis timed out after {timeout}s"], "black": [], "frozen": [], "loudness": None}
    err = p.stderr
    errors = [ln for ln in err.splitlines() if "[error]" in ln or "[fatal]" in ln]
    if p.returncode != 0 and not errors:
        errors = [f"ffmpeg exited with code {p.returncode}: {tail(err, 3, 300)}"]
    loud = None
    summ = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    mi, mlra, mtp = (re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summ), re.search(r"LRA:\s+(-?[\d.]+) LU", summ),
                     re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", summ, re.S))
    if mi:
        def f(m):
            return -99.0 if (m is None or "inf" in m.group(1)) else float(m.group(1))
        loud = {"lufs": f(mi), "lra": f(mlra), "true_peak": f(mtp)}
    end = 0.0
    if "freeze_start" in err:
        try:
            end = duration(path)
        except FFError:
            pass
    return {"errors": errors, "black": _black(err), "frozen": _freeze(err, end), "loudness": loud}


def extract_audio(path: str | Path, out: Path, rate: int = 16000) -> Path:
    run(["-i", path, "-vn", "-ac", "1", "-ar", rate, "-c:a", "pcm_s16le", out], op="extract-audio")
    return out
