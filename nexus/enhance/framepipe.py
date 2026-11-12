"""Run a per-frame NumPy function over a video (decode pipe -> fn -> encode pipe), in resumable chunks.

    process_video(src, out, fn, out_size=None, start=0, duration=None, chunk_s=10, checkpoint=None, audio=True)
        fn(frame_rgb_uint8, index) -> frame_rgb_uint8 (or RGBA when `alpha=True`: written as PNG-in-MOV with alpha)
    Each chunk is encoded to its own file and recorded in a cache.Checkpoint, so an interrupted job resumes at the first
    unfinished chunk; chunks are joined with the concat demuxer and the source audio (same time range) is muxed back.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import numpy as np

from .. import costguard
from ..generate.cache import Checkpoint
from ..media import ff


def video_info(src: str | Path) -> dict:
    info = ff.probe(src)
    if not info.get("video"):
        raise ValueError(f"{src} has no video stream")
    return info


def _decode(src, w, h, start, dur, fps):
    vf = f"fps={fps}" if fps else "null"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
           "-an", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _encoder(out, w, h, fps, alpha, crf):
    if alpha:
        codec = ["-c:v", "png", "-pix_fmt", "rgba"]
        pix = "rgba"
    else:
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
        pix = "rgb24"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", pix, "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-", *codec, str(out)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def process_video(src: str | Path, out: str | Path, fn: Callable[[np.ndarray, int], np.ndarray], *,
                  out_size: tuple[int, int] | None = None, start: float = 0.0, duration: float | None = None,
                  chunk_s: float = 10.0, checkpoint: str | Path | None = None, audio: bool = True, alpha: bool = False,
                  fps: float | None = None, crf: int = 16) -> dict:
    costguard.authorize("ffmpeg", "framepipe")
    info = video_info(src)
    v = info["video"]
    w, h = v["width"], v["height"]
    rate = fps or v["fps"] or 25.0
    total = max(0.0, (duration if duration is not None else info["duration"] - start))
    ow, oh = out_size or (w, h)
    out = Path(out)
    work = out.with_name(out.stem + ".chunks")
    work.mkdir(parents=True, exist_ok=True)
    ck = Checkpoint(checkpoint or work / "checkpoint.json")
    n_chunks = max(1, int(np.ceil(total / chunk_s - 1e-6)))
    ext = ".mov" if alpha else ".mp4"
    frames_done = 0
    index = 0
    for c in range(n_chunks):
        cs = start + c * chunk_s
        cd = min(chunk_s, start + total - cs)
        part = work / f"chunk_{c:04d}{ext}"
        if ck.done(f"chunk-{c}") and part.exists():
            n = int(ck.state["done"][f"chunk-{c}"].get("frames", 0))
            frames_done += n
            index += n
            continue
        dec = _decode(src, w, h, cs, cd, fps)
        enc = _encoder(part, ow, oh, rate, alpha, crf)
        n = 0
        fsize = w * h * 3
        try:
            while True:
                buf = dec.stdout.read(fsize)
                if len(buf) < fsize:
                    break
                frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                res = fn(frame, index)
                exp = (oh, ow, 4 if alpha else 3)
                if res.shape != exp:
                    raise ValueError(f"frame function returned {res.shape}, expected {exp}")
                enc.stdin.write(np.ascontiguousarray(res, np.uint8).tobytes())
                n += 1
                index += 1
        finally:
            dec.stdout.close()
            dec.wait()
            enc.stdin.close()
            err = enc.stderr.read().decode(errors="replace")
            enc.wait()
        if enc.returncode != 0:
            raise ff.FFError(f"framepipe encode failed: {ff.tail(err)}", op="framepipe")
        ck.mark(f"chunk-{c}", {"frames": n})
        frames_done += n
    lst = work / "list.txt"
    lst.write_text("\n".join(ff.concat_list_line(work / f"chunk_{c:04d}{ext}") for c in range(n_chunks)) + "\n", encoding="utf-8")
    has_audio = audio and info.get("audio") is not None and not alpha
    args = ["-f", "concat", "-safe", "0", "-i", lst]
    if has_audio:
        args += ["-ss", f"{start:.3f}", "-t", f"{total:.3f}", "-i", src, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a",
                 "192k", "-shortest"]
    args += ["-c:v", "copy", out]
    ff.run(args, op="framepipe-join")
    return {"path": str(out), "frames": frames_done, "width": ow, "height": oh, "fps": rate, "chunks": n_chunks,
            "audio": has_audio}
