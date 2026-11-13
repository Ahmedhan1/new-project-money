"""Large-file workflow: inspect, proxies, thumbnails, keyframe-aligned chunks, proxy -> original mapping (WS E / MED-1).

Rules
  * The source is opened read-only and is never modified, moved or deleted.
  * Proxies are cached by a content signature (size + mtime + hash of three 1 MiB windows), so a multi-GB file is
    never read in full just to find its proxy.
  * Writes are atomic: every file is produced under `*.part.*` and renamed into place, so an interrupted run never
    leaves a half file under the final name. Chunked mode (long sources) is resumable: finished chunks are kept and
    a re-run encodes only the missing ones.
  * Proxies keep the source's timestamps (`-fps_mode passthrough`, same start), so an edit made on the proxy uses the
    same timecodes on the original. `to_original()` swaps the files back for the final render.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from typing import Callable

from .. import config
from . import ff

PROXY_VERSION = 1
CHUNKED_ABOVE_SECONDS = 300.0       # sources longer than this are proxied in resumable chunks
DEFAULT_CHUNK_SECONDS = 60.0
WINDOW = 1 << 20                    # 1 MiB per signature window


# ---------------------------------------------------------------- signature
def signature(src: str | Path) -> dict:
    """Cheap content signature: size, mtime and SHA-256 over the first, middle and last MiB (<= 3 MiB read)."""
    p = Path(src)
    st = p.stat()
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for off in sorted({0, max(0, st.st_size // 2 - WINDOW // 2), max(0, st.st_size - WINDOW)}):
            f.seek(off)
            h.update(f.read(WINDOW))
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "partial_sha256": h.hexdigest()}


def _sig_key(sig: dict, *extra) -> str:
    return hashlib.sha256(json.dumps([sig, PROXY_VERSION, *extra], sort_keys=True).encode()).hexdigest()[:20]


# ---------------------------------------------------------------- inspect
def _ffprobe_csv(args: list[str], timeout: int = 600) -> list[str]:
    p = subprocess.run(["ffprobe", "-v", "error", *args], capture_output=True, text=True, timeout=timeout,
                       errors="replace", stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise ff.FFError(f"ffprobe failed: {ff.tail(p.stderr, 5, 400)}", op="ffprobe", returncode=p.returncode,
                         stderr=p.stderr)
    return [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]


def _ratio(s: str | None) -> float:
    try:
        n, d = (s or "0/1").split("/") if "/" in (s or "") else (s, "1")
        return float(n) / float(d) if float(d) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _bit_depth(st: dict) -> int:
    b = st.get("bits_per_raw_sample")
    if b and str(b).isdigit():
        return int(b)
    pf = st.get("pix_fmt") or ""
    for n in (16, 14, 12, 10, 9):
        if f"p{n}" in pf or f"{n}le" in pf or f"{n}be" in pf:
            return n
    return 8


def _rotation(st: dict) -> int:
    for sd in st.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    try:
        return int((st.get("tags") or {}).get("rotate", 0)) % 360
    except ValueError:
        return 0


def frame_times(src: str | Path, max_frames: int | None = 300) -> list[float]:
    """Presentation timestamps (s) of the first video packets, sorted. Reads packet headers only, no decode."""
    args = ["-select_streams", "v:0", "-show_entries", "packet=pts_time,dts_time", "-of", "csv=p=0"]
    if max_frames:
        args += ["-read_intervals", f"%+#{int(max_frames)}"]
    out = []
    for ln in _ffprobe_csv([*args, str(src)]):
        parts = [x for x in ln.split(",") if x and x != "N/A"]
        if parts:
            try:
                out.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(out)


def _vfr(times: list[float]) -> dict:
    d = [b - a for a, b in zip(times, times[1:]) if b > a]
    if len(d) < 4:
        return {"vfr": False, "checked_frames": len(times)}
    med = statistics.median(d)
    tol = max(0.0015, med * 0.1)          # timebase rounding (e.g. 1/90000, 1/1000) is not VFR
    irregular = sum(1 for x in d if abs(x - med) > tol)
    return {"vfr": irregular > max(1, len(d) // 100), "checked_frames": len(times), "median_interval": round(med, 6),
            "min_interval": round(min(d), 6), "max_interval": round(max(d), 6), "irregular_intervals": irregular}


def inspect(src: str | Path, deep: bool = False) -> dict:
    """Container/stream metadata plus warnings for things that break an edit (VFR, HDR, rotation, odd sizes...).

    deep=True also runs FFmpeg's `idet` on the first 300 frames to detect interlacing that is not flagged."""
    p = Path(src)
    raw = ff.probe_raw(p)
    fmt = raw.get("format", {})
    vids = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"
            and (s.get("disposition") or {}).get("attached_pic") != 1]
    auds = [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"]
    meta: dict = {"path": str(p), "size": p.stat().st_size, "container": fmt.get("format_name"),
                  "duration": float(fmt.get("duration") or 0), "start_time": float(fmt.get("start_time") or 0),
                  "bit_rate": int(fmt.get("bit_rate") or 0), "streams": [
                      {"index": s.get("index"), "type": s.get("codec_type"), "codec": s.get("codec_name")}
                      for s in raw.get("streams", [])],
                  "video": None, "audio": None, "audio_streams": len(auds), "warnings": []}
    w = meta["warnings"]
    if vids:
        v = vids[0]
        rot = _rotation(v)
        sar = _ratio(v.get("sample_aspect_ratio")) or 1.0
        W, H = int(v.get("width") or 0), int(v.get("height") or 0)
        dw, dh = (H, W) if rot in (90, 270) else (W, H)
        trc, prim = v.get("color_transfer"), v.get("color_primaries")
        hdr = trc in ("smpte2084", "arib-std-b67") or (prim == "bt2020" and _bit_depth(v) >= 10)
        fo = v.get("field_order") or "unknown"
        vfr = _vfr(frame_times(p))
        meta["video"] = {"codec": v.get("codec_name"), "profile": v.get("profile"), "width": W, "height": H,
                         "display_width": int(round(dw * (sar if rot not in (90, 270) else 1))), "display_height": dh,
                         "sar": round(sar, 4), "pix_fmt": v.get("pix_fmt"), "bit_depth": _bit_depth(v),
                         "fps": round(_ratio(v.get("avg_frame_rate")), 3), "r_frame_rate": v.get("r_frame_rate"),
                         "color_primaries": prim, "color_transfer": trc, "color_space": v.get("color_space"),
                         "color_range": v.get("color_range"), "hdr": hdr, "rotation": rot, "field_order": fo,
                         "interlaced": fo not in ("progressive", "unknown"), "vfr": vfr["vfr"], "vfr_check": vfr,
                         "duration": float(v.get("duration") or meta["duration"])}
        mv = meta["video"]
        if deep:
            mv["idet"] = _idet(p)
            if mv["idet"].get("interlaced_share", 0) > 0.3:
                mv["interlaced"] = True
        if mv["vfr"]:
            w.append("variable frame rate: frame timing is irregular; conform to a constant rate before editing "
                     "(export(fps=...)) or audio sync can drift")
        if hdr:
            w.append(f"HDR source ({trc}/{prim}): needs tone-mapping to SDR (BT.709) or it will look washed out")
        if rot:
            w.append(f"rotation flag {rot}°: players rotate on display; FFmpeg auto-rotates when re-encoding "
                     f"(display size {dw}x{dh})")
        if W % 2 or H % 2:
            w.append(f"odd dimensions {W}x{H}: 4:2:0 encoders need even sizes; output will be scaled/cropped by 1 px")
        if abs(sar - 1.0) > 0.01:
            w.append(f"non-square pixels (SAR {v.get('sample_aspect_ratio')}): scale to square pixels on export")
        if mv["interlaced"]:
            w.append(f"interlaced ({fo}): deinterlace (bwdif) before scaling or editing")
        if mv["bit_depth"] > 8 and not hdr:
            w.append(f"{mv['bit_depth']}-bit source: 8-bit H.264 exports will reduce precision (fine for delivery)")
    else:
        w.append("no video stream")
    if auds:
        a = auds[0]
        meta["audio"] = {"codec": a.get("codec_name"), "channels": a.get("channels"),
                         "sample_rate": int(a.get("sample_rate") or 0), "duration": float(a.get("duration") or meta["duration"])}
    else:
        w.append("no audio stream")
    if meta["size"] > 2 * 1024 ** 3:
        w.append("large file (> 2 GB): edit with a proxy (proxy.ensure) and render from the original")
    return meta


def _idet(src: Path, frames: int = 300) -> dict:
    err = ff.run(["-i", src, "-an", "-frames:v", frames, "-vf", "idet", "-f", "null", "-"], op="idet")
    import re
    m = re.findall(r"Multi frame detection: TFF:\s*(\d+) BFF:\s*(\d+) Progressive:\s*(\d+)", err)
    if not m:
        return {}
    tff, bff, prog = (int(x) for x in m[-1])
    tot = max(1, tff + bff + prog)
    return {"tff": tff, "bff": bff, "progressive": prog, "interlaced_share": round((tff + bff) / tot, 3)}


# ---------------------------------------------------------------- keyframes and chunks
def keyframes(src: str | Path) -> tuple[list[float], list[float]]:
    """(all video packet pts, keyframe pts), sorted, in seconds relative to the file start (as `-ss` reads them).

    Reads packet headers for the whole file (I/O, no decoding)."""
    start = float(ff.probe_raw(src).get("format", {}).get("start_time") or 0)
    rows = _ffprobe_csv(["-select_streams", "v:0", "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", str(src)])
    allp, kf = [], []
    for ln in rows:
        parts = ln.split(",")
        t = next((x for x in parts[:2] if x not in ("", "N/A")), None)
        if t is None:
            continue
        try:
            tt = round(float(t) - start, 6)
        except ValueError:
            continue
        allp.append(tt)
        if "K" in (parts[2] if len(parts) > 2 else ""):
            kf.append(tt)
    return sorted(allp), sorted(kf)


def chunks(src: str | Path, seconds: float = DEFAULT_CHUNK_SECONDS) -> list[dict]:
    """Keyframe-aligned plan for staged processing: [{"index","start","end","frames"}], each about `seconds` long.

    Every chunk starts on a keyframe, so a chunk can be decoded on its own; together they cover every frame once."""
    allp, kf = keyframes(src)
    if not allp:
        return []
    kf = kf or [allp[0]]
    dur = ff.duration(src)
    step = (allp[-1] - allp[-2]) if len(allp) > 1 else 1 / 30
    end_all = max(dur, allp[-1] + step)
    bounds = [kf[0]]
    for k in kf[1:]:
        if k - bounds[-1] >= seconds:
            bounds.append(k)
    out = []
    for i, s in enumerate(bounds):
        e = bounds[i + 1] if i + 1 < len(bounds) else end_all
        n = sum(1 for t in allp if s - 1e-6 <= t < e - 1e-6) if i + 1 < len(bounds) else sum(1 for t in allp if t >= s - 1e-6)
        out.append({"index": i, "start": round(s, 6), "end": round(e, 6), "frames": n})
    return out


# ---------------------------------------------------------------- proxies
def _proxy_dir(cache_dir: Path | None) -> Path:
    return Path(cache_dir) if cache_dir else config.CACHE / "proxies"


def _video_filter(height: int, src_meta: dict) -> str:
    v = src_meta.get("video") or {}
    h = min(int(height), int(v.get("display_height") or height))
    h -= h % 2
    return f"scale=-2:{h}:flags=bicubic,setsar=1"


def _proxy_vargs(fps: float) -> list[str]:
    # Short GOP (0.5 s) so editors can scrub; CRF 26 veryfast keeps proxies small and fast.
    g = max(1, int(round((fps or 30) / 2)))
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-g", str(g),
            "-fps_mode", "passthrough"]


def _atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def proxy_path(src: str | Path, height: int = 540, cache_dir: Path | None = None) -> Path:
    """Where the proxy for `src` at `height` lives (whether or not it exists yet)."""
    sig = signature(src)
    return _proxy_dir(cache_dir) / f"{Path(src).stem[:40]}_{_sig_key(sig, height)}_{height}p.mp4"


def _valid(final: Path, sig: dict) -> bool:
    side = Path(str(final) + ".json")
    if not (final.exists() and side.exists()):
        return False
    try:
        return json.loads(side.read_text(encoding="utf-8")).get("signature") == sig
    except ValueError:
        return False


def build(src: str | Path, height: int = 540, *, cache_dir: Path | None = None, chunk_seconds: float | None = None,
          on_progress: Callable[[dict], None] | None = None, threads: int | None = None) -> dict:
    """Create (or reuse) the proxy; returns {"path","cached","mode","encoded_chunks","reused_chunks","seconds"}."""
    src = Path(src)
    sig = signature(src)
    final = proxy_path(src, height, cache_dir)
    t0 = time.time()
    if _valid(final, sig):
        return {"path": final, "cached": True, "mode": "cached", "encoded_chunks": 0, "reused_chunks": 0, "seconds": 0.0}
    final.parent.mkdir(parents=True, exist_ok=True)
    meta = inspect(src)
    dur = meta["duration"]
    vf = _video_filter(height, meta)
    fps = (meta.get("video") or {}).get("fps") or 30
    chunked = chunk_seconds is not None or dur > CHUNKED_ABOVE_SECONDS
    part = final.with_name(final.stem + ".part.mp4")
    has_audio = meta["audio"] is not None
    aargs = ["-map", "0:a:0", "-c:a", "aac", "-b:a", "96k", "-ac", "2"] if has_audio else ["-an"]
    encoded = reused = 0
    try:
        if not chunked:
            ff.run(["-i", src, "-map", "0:v:0", *aargs, "-vf", vf, *_proxy_vargs(fps), "-movflags", "+faststart", part],
                   op="proxy", threads=threads, total=dur,
                   on_progress=(lambda i: on_progress({**i, "stage": "proxy"})) if on_progress else None)
            encoded = 1
        else:
            plan = chunks(src, chunk_seconds or DEFAULT_CHUNK_SECONDS)
            pdir = final.with_name(final.stem + ".parts")
            pdir.mkdir(exist_ok=True)
            _atomic_json(pdir / "plan.json", {"signature": sig, "plan": plan})
            files = []
            for c in plan:
                cf = pdir / f"chunk_{c['index']:05d}.mp4"
                files.append(cf)
                if cf.exists():
                    reused += 1
                    continue
                cpart = pdir / f"chunk_{c['index']:05d}.part.mp4"
                base = c["start"] / dur if dur else 0.0
                span = (c["end"] - c["start"]) / dur if dur else 1.0

                def prog(i, c=c, base=base, span=span):
                    if on_progress:
                        on_progress({**i, "stage": "proxy-chunk", "chunk": c["index"], "chunks": len(plan),
                                     "fraction": min(1.0, base + span * (i.get("fraction") or 0))})
                try:
                    ff.run(["-ss", f"{c['start']:.6f}", "-i", src, "-map", "0:v:0", "-an", "-frames:v", c["frames"],
                            "-vf", vf, *_proxy_vargs(fps), cpart], op="proxy-chunk", threads=threads,
                           total=c["end"] - c["start"], on_progress=prog if on_progress else None)
                except BaseException:
                    cpart.unlink(missing_ok=True)
                    raise
                os.replace(cpart, cf)
                encoded += 1
            lst = pdir / "list.txt"
            lst.write_text("\n".join(ff.concat_list_line(f.resolve()) for f in files) + "\n", encoding="utf-8")
            ff.run(["-f", "concat", "-safe", "0", "-i", lst, "-i", src, "-map", "0:v:0",
                    *(["-map", "1:a:0", "-c:a", "aac", "-b:a", "96k", "-ac", "2"] if has_audio else ["-an"]),
                    "-c:v", "copy", "-movflags", "+faststart", part], op="proxy-join", threads=threads)
        _atomic_json(Path(str(final) + ".json"), {
            "source": str(src.resolve()), "signature": sig, "height": height, "start_time": meta["start_time"],
            "offset": 0.0, "duration": dur, "created": time.time(), "mode": "chunked" if chunked else "single",
            "note": "proxy timestamps start at 0 = source start; same timecodes apply to the original"})
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if chunked:
        shutil.rmtree(final.with_name(final.stem + ".parts"), ignore_errors=True)
    return {"path": final, "cached": False, "mode": "chunked" if chunked else "single", "encoded_chunks": encoded,
            "reused_chunks": reused, "seconds": round(time.time() - t0, 3)}


def ensure(src: str | Path, height: int = 540, **kw) -> Path:
    """Path of an edit proxy for `src` (created once, then reused while the source is unchanged)."""
    return build(src, height, **kw)["path"]


# ---------------------------------------------------------------- proxy -> original
def original_for(proxy: str | Path) -> dict | None:
    """Sidecar record of a proxy ({"source","signature","offset",...}) or None if `proxy` is not a proxy."""
    side = Path(str(proxy) + ".json")
    if not side.exists():
        return None
    try:
        return json.loads(side.read_text(encoding="utf-8"))
    except ValueError:
        return None


def to_original(tl: dict) -> tuple[dict, list[str]]:
    """Copy of a timeline with every proxy `src` replaced by its original, for the final render.

    Timecodes are kept: proxies are made with the source's timestamps (`offset` 0). If a sidecar ever records a
    non-zero offset it is added to in/out. Refuses (ValueError) when the original changed since the proxy was made."""
    out = copy.deepcopy(tl)
    notes: list[str] = []

    def swap(item: dict, keys: tuple[str, ...]) -> None:
        rec = original_for(item.get("src", ""))
        if not rec:
            return
        orig = Path(rec["source"])
        if not orig.exists():
            raise ValueError(f"original of proxy {item['src']} is missing: {orig}")
        if signature(orig) != rec["signature"]:
            raise ValueError(f"original {orig} changed since its proxy was made; re-check the edit")
        off = float(rec.get("offset") or 0.0)
        for k in keys:
            if off and k in item:
                item[k] = float(item[k]) + off
        notes.append(f"{Path(item['src']).name} -> {orig.name} (offset {off:+.3f}s)")
        item["src"] = str(orig)

    for c in out.get("clips", []):
        if c.get("type") == "video":
            swap(c, ("in", "out"))
    for a in out.get("audio", []):
        swap(a, ("in", "out"))
    return out, notes


# ---------------------------------------------------------------- thumbnails
def thumbnails(src: str | Path, n: int = 8, out_dir: Path | None = None, width: int = 320) -> list[Path]:
    """n evenly spaced JPEG thumbnails (fast input seek per frame; never decodes the whole file)."""
    src = Path(src)
    d = ff.duration(src)
    out_dir = Path(out_dir) if out_dir else config.CACHE / "thumbs" / _sig_key(signature(src))
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(int(n)):
        t = d * (i + 0.5) / n
        p = out_dir / f"thumb_{i:03d}.jpg"
        tmp = out_dir / f"thumb_{i:03d}.part.jpg"
        ff.run(["-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-q:v", "4",
                "-update", "1", tmp], op="thumbnail")
        os.replace(tmp, p)
        paths.append(p)
    return paths


def contact_sheet(src: str | Path, out: Path | None = None, cols: int = 4, rows: int = 3, width: int = 320) -> Path:
    """One JPEG grid of cols x rows evenly spaced frames."""
    src = Path(src)
    thumbs = thumbnails(src, cols * rows, width=width)
    out = Path(out) if out else thumbs[0].parent / "contact_sheet.jpg"
    tmp = out.with_name(out.stem + ".part.jpg")
    ff.run(["-framerate", "1", "-i", thumbs[0].parent / "thumb_%03d.jpg", "-vf", f"tile={cols}x{rows}:padding=4:margin=4",
            "-frames:v", "1", "-q:v", "4", "-update", "1", tmp], op="contact-sheet")
    os.replace(tmp, out)
    return out
