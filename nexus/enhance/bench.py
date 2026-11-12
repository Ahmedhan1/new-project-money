"""Enhancement benchmark on the licensed benchmark media (ad 320x240, interview 1280x720, broll 1920x1012).

    flock /tmp/nexus-heavy.lock python -m nexus.enhance.bench --out docs/results/account2_enhance [--ids ad,interview,broll]

Per source, on a short segment: downscale->upscale round trip (PSNR/SSIM vs the original for naive bicubic, the lanczos
baseline and Real-ESRGAN x4v3 when installed), native 2x upscale crops (before/after), minterpolate, denoise on
noise-added frames (PSNR vs the clean frames), deblock, film look, HDR->SDR tone mapping of a PQ conversion.
Speeds are wall-clock on whatever machine runs it (state the machine when quoting them).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import time
from pathlib import Path

import numpy as np

from .. import config
from ..generate import downloads, metrics
from ..media import ff
from . import run

SEG = {"ad": 60.0, "interview": 30.0, "broll": 40.0}


def _src(i: str) -> Path:
    base = Path(os.environ.get("NEXUS_BENCH_MEDIA", config.ROOT / ".nexus" / "benchmarks"))
    return base / i / "source.mp4"


def _cut(src: Path, out: Path, start: float, dur: float, vf: str | None = None, crf: int = 14) -> Path:
    ff.run(["-ss", start, "-t", dur, "-i", src, *(["-vf", vf] if vf else []), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", crf, "-c:a", "aac", out], op="bench-cut")
    return out


def _cmp(ref: Path, test: Path, n: int = 8) -> dict:
    a, b = metrics.read_frames(ref), metrics.read_frames(test)
    k = min(len(a), len(b))
    idx = np.linspace(0, k - 1, min(n, k)).astype(int)
    return {"psnr": round(float(np.mean([metrics.psnr(a[i], b[i]) for i in idx])), 2),
            "ssim": round(float(np.mean([metrics.ssim(a[i], b[i]) for i in idx])), 4), "frames_compared": len(idx)}


def _crop_strip(paths: list[Path], labels: list[str], out: Path, box: tuple[float, float, float] = (0.35, 0.3, 0.3)) -> Path:
    """Same crop (x, y, size as fraction of width) from the middle frame of each clip, side by side, labelled."""
    from PIL import Image, ImageDraw
    tiles = []
    for p in paths:
        f = metrics.read_frames(p)
        fr = f[len(f) // 2]
        h, w = fr.shape[:2]
        s = int(box[2] * w)
        x, y = int(box[0] * w), int(min(box[1] * h, h - s * 0.75))
        tiles.append(Image.fromarray(fr[y:y + int(s * 0.75), x:x + s]).resize((360, 270), Image.LANCZOS))
    strip = Image.new("RGB", (360 * len(tiles), 290), (20, 20, 20))
    d = ImageDraw.Draw(strip)
    for i, (t, lab) in enumerate(zip(tiles, labels)):
        strip.paste(t, (360 * i, 20))
        d.text((360 * i + 6, 4), lab, fill=(255, 255, 255))
    strip.save(out, quality=85)
    return out


def _timed(op, src, out, **p) -> dict:
    t = time.time()
    r = run(op, src, out, use_cache=False, **p)
    secs = time.time() - t
    v = ff.probe(r["path"])
    frames = int(round(v["duration"] * (v["video"]["fps"] or 25)))
    return {"seconds": round(secs, 2), "out_frames": frames, "s_per_frame": round(secs / max(1, frames), 4),
            "realtime_x": round(v["duration"] / secs, 3) if secs else None, "out": f"{v['video']['width']}x{v['video']['height']}",
            "path": r["path"]}


def bench_one(i: str, work: Path, out: Path, esrgan_s: float = 1.0) -> dict:
    """Intermediate videos go to `work` (cache); only JSON + JPEG crops go to `out`."""
    src = _src(i)
    w = work / i
    w.mkdir(parents=True, exist_ok=True)
    info = ff.probe(src)["video"]
    W, H = info["width"], info["height"]
    start = SEG.get(i, 30.0)
    rep: dict = {"source": i, "geometry": f"{W}x{H}", "fps": info["fps"], "segment": [start, 4.0]}
    seg = _cut(src, w / "seg.mp4", start, 4.0)
    seg1 = _cut(src, w / "seg1.mp4", start, esrgan_s)
    # --- round trip: downscale x2, upscale back; compare with the real original
    hw, hh = W // 4 * 2, H // 4 * 2
    small = _cut(seg1, w / "small.mp4", 0, esrgan_s, f"scale={hw}:{hh}:flags=area")
    naive = _cut(small, w / "rt_bicubic.mp4", 0, esrgan_s, f"scale={W}:{H}:flags=bicubic")
    rt = {"bicubic": _cmp(seg1, naive)}
    lz = _timed("upscale", small, w / "rt_lanczos.mp4", scale=2, method="lanczos", denoise=0.1, sharpen=0.4)
    lz_fix = _cut(Path(lz["path"]), w / "rt_lanczos_fit.mp4", 0, esrgan_s, f"scale={W}:{H}:flags=lanczos")
    rt["lanczos_baseline"] = {**_cmp(seg1, lz_fix), **{k: lz[k] for k in ("seconds", "s_per_frame")}}
    have_esr = downloads.installed("realesr-general-x4v3")
    paths, labels = [seg1, naive, lz_fix], ["original", "bicubic x2", "lanczos+unsharp x2"]
    if have_esr:
        es = _timed("upscale", small, w / "rt_esrgan.mp4", scale=2, method="esrgan", tile=256)
        es_fix = _cut(Path(es["path"]), w / "rt_esrgan_fit.mp4", 0, esrgan_s, f"scale={W}:{H}:flags=lanczos")
        rt["esrgan_x4v3"] = {**_cmp(seg1, es_fix), **{k: es[k] for k in ("seconds", "s_per_frame")}}
        paths.append(es_fix)
        labels.append("Real-ESRGAN x4v3 x2")
    rep["roundtrip_x2"] = rt
    _crop_strip(paths, labels, out / f"{i}_roundtrip_crops.jpg")
    # --- native 2x upscale of the real source (no reference exists: crops only)
    if W <= 1280:
        nat = {"lanczos": _timed("upscale", seg1, w / "nat_lanczos.mp4", scale=2, method="lanczos")}
        npaths, nlabels = [Path(nat["lanczos"]["path"])], ["lanczos+denoise+unsharp x2"]
        if have_esr:
            nat["esrgan"] = _timed("upscale", seg1, w / "nat_esrgan.mp4", scale=2, method="esrgan", tile=256)
            npaths.append(Path(nat["esrgan"]["path"]))
            nlabels.append("Real-ESRGAN x4v3 x2")
        bic = _cut(seg1, w / "nat_bicubic.mp4", 0, esrgan_s, f"scale={W * 2}:{H * 2}:flags=bicubic")
        _crop_strip([bic, *npaths], ["bicubic x2", *nlabels], out / f"{i}_native2x_crops.jpg")
        rep["native_x2"] = {k: {x: v[x] for x in ("seconds", "s_per_frame", "out")} for k, v in nat.items()}
    # --- other ops on the 4 s segment
    ops = {"interpolate_mci_2x": ("interpolate", {"fps": round(2 * info["fps"], 3), "method": "mci"}),
           "interpolate_blend_2x": ("interpolate", {"fps": round(2 * info["fps"], 3), "method": "blend"}),
           "denoise_hqdn3d": ("denoise", {"method": "hqdn3d", "strength": 0.5}),
           "denoise_nlmeans": ("denoise", {"method": "nlmeans", "strength": 0.5}),
           "deblock": ("deblock", {"strength": 0.7}), "sharpen_cas": ("sharpen", {"method": "cas", "amount": 0.5}),
           "film_look": ("film_look", {"grain": 0.35, "halation": 0.4, "weave": 0.3})}
    rep["ops"] = {}
    for name, (op, p) in ops.items():
        src_for = seg1 if (name.startswith("interpolate_mci") or "nlmeans" in name) and W > 1280 else seg
        rep["ops"][name] = {**_timed(op, src_for, w / f"{name}.mp4", **p), "input_s": 1.0 if src_for == seg1 else 4.0}
        rep["ops"][name].pop("path")
    # --- denoise quality: add noise, denoise, compare with the clean segment
    noisy = _cut(seg1, w / "noisy.mp4", 0, esrgan_s, "noise=alls=18:allf=t", crf=8)
    dq = {"noisy": _cmp(seg1, noisy)}
    for m in ("hqdn3d", "nlmeans"):
        r = run("denoise", noisy, w / f"dn_{m}.mp4", use_cache=False, method=m, strength=0.6)
        dq[m] = _cmp(seg1, Path(r["path"]))
    rep["denoise_quality"] = dq
    _crop_strip([seg1, noisy, w / "dn_hqdn3d.mp4", w / "dn_nlmeans.mp4"], ["clean", "noise added", "hqdn3d", "nlmeans"],
                out / f"{i}_denoise_crops.jpg")
    _crop_strip([seg, w / "film_look.mp4"], ["source", "film look"], out / f"{i}_filmlook_crops.jpg", box=(0.2, 0.15, 0.6))
    # --- HDR -> SDR: convert the segment to PQ (BT.2020, 1000 nits), then tone map it back
    pq = w / "pq.mkv"
    ff.run(["-i", seg1, "-vf", "zscale=tin=bt709:pin=bt709:min=bt709:t=smpte2084:p=bt2020:m=bt2020nc:npl=1000,format=yuv420p10le",
            "-c:v", "libx265", "-x265-params", "log-level=error", "-color_trc", "smpte2084", "-color_primaries", "bt2020",
            "-colorspace", "bt2020nc", "-an", pq], op="bench-pq")
    tm = _timed("tonemap", pq, w / "tonemapped.mp4", algorithm="hable")
    rep["tonemap_hable"] = {**{k: tm[k] for k in ("seconds", "s_per_frame")}, "vs_original_sdr": _cmp(seg1, Path(tm["path"]))}
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(config.ROOT / "docs" / "results" / "account2_enhance"))
    ap.add_argument("--ids", default="ad,interview,broll")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = config.CACHE / "enhance_bench"
    res = {"date": time.strftime("%Y-%m-%d"), "machine": {"platform": platform.platform(), "cpus": os.cpu_count(),
                                                          "note": "shared 4-vCPU container, no GPU; other jobs ran concurrently"},
           "esrgan_installed": downloads.installed("realesr-general-x4v3"), "sources": {}}
    for i in a.ids.split(","):
        t = time.time()
        res["sources"][i] = bench_one(i, work, out)
        res["sources"][i]["bench_seconds"] = round(time.time() - t, 1)
        (out / "bench.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(i, json.dumps(res["sources"][i])[:400], flush=True)
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
