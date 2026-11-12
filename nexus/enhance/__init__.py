"""Media-to-media enhancement processors. Each takes an input file + parameters and writes a NEW cached file; sources
are never modified. Every result carries a GeneratedMediaManifest (classification `edited-source`) and the op's
EffectManifest entry, so a timeline clip can point its `src` at the result (`apply(clip, op, **params)`).

    upscale(src, scale=2, method="lanczos"|"esrgan")    lanczos+denoise+unsharp baseline, or Real-ESRGAN x4v3 (ONNX, CPU)
    interpolate(src, fps=60, method="mci"|"blend")      FFmpeg minterpolate (motion-compensated) / frame blending
    denoise(src, method="hqdn3d"|"nlmeans"|"bm3d", strength=0.5)
    deblock(src, strength=0.5)                           FFmpeg deblock (8x8 block edges of over-compressed sources)
    sharpen(src, amount=0.5, method="unsharp"|"cas")
    film_look(src, grain=0.3, halation=0.3, weave=0.3)   grain + highlight glow (halation-like) + sub-pixel gate weave
    tonemap(src, algorithm="hable")                      HDR (PQ/HLG) -> SDR BT.709; an SDR source is returned unchanged
    effect_manifest() -> EffectManifest                  what is available on this machine, parameters, cost class

Upscaling never recovers real detail: lanczos interpolates, Real-ESRGAN synthesises plausible texture. Outputs are
labelled `edited-source`, never generated.
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import config, contracts
from ..generate import cache as gcache
from ..generate import classify, downloads
from ..media import ff

OPS: dict[str, dict] = {
    "upscale": {"params": {"scale": "int 2|3|4", "method": "lanczos|esrgan", "denoise": "0..1", "sharpen": "0..1"},
                "cost": {"lanczos": "light", "esrgan": "heavy (CPU: seconds per frame)"}, "model": {"esrgan": "realesr-general-x4v3"}},
    "interpolate": {"params": {"fps": "target fps", "method": "mci|blend"}, "cost": {"mci": "heavy", "blend": "light"}},
    "denoise": {"params": {"method": "hqdn3d|nlmeans|bm3d", "strength": "0..1"},
                "cost": {"hqdn3d": "light", "nlmeans": "medium", "bm3d": "heavy"}},
    "deblock": {"params": {"strength": "0..1"}, "cost": {"default": "light"}},
    "sharpen": {"params": {"amount": "0..1", "method": "unsharp|cas"}, "cost": {"default": "light"}},
    "film_look": {"params": {"grain": "0..1", "halation": "0..1", "weave": "0..1"}, "cost": {"default": "light"}},
    "tonemap": {"params": {"algorithm": "hable|mobius|reinhard|clip", "peak": "nits (optional)"}, "cost": {"default": "medium"},
                "note": "only acts on PQ/HLG sources"},
}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
FILTERS_NEEDED = {"interpolate": "minterpolate", "denoise": "hqdn3d", "deblock": "deblock", "sharpen": "unsharp",
                  "tonemap": "zscale"}


def _filters() -> set[str]:
    import subprocess
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 2 and ln.startswith(" ")}


def effect_manifest() -> dict:
    fl = _filters()
    effects = []
    for name, spec in OPS.items():
        need = FILTERS_NEEDED.get(name)
        e = {"type": f"enhance.{name}", "params": spec["params"], "cost": spec["cost"], "kind": "media-to-media",
             "available": (need in fl) if need else True, "label": "edited-source"}
        if name == "upscale":
            e["methods"] = {"lanczos": True, "esrgan": downloads.installed("realesr-general-x4v3")}
            e["needs_download"] = {"esrgan": downloads.plan("realesr-general-x4v3")["total_gb"]}
        if name == "denoise":
            e["methods"] = {m: m in fl for m in ("hqdn3d", "nlmeans", "bm3d")}
        effects.append(e)
    return contracts.make("EffectManifest", effects=effects)


def _clamp(x, lo=0.0, hi=1.0) -> float:
    return max(lo, min(hi, float(x)))


def _vfilter(op: str, p: dict, info: dict) -> str:
    v = info["video"]
    if op == "upscale":
        s = int(p.get("scale", 2))
        d, sh = _clamp(p.get("denoise", 0.15)), _clamp(p.get("sharpen", 0.4))
        f = [f"scale={v['width'] * s}:{v['height'] * s}:flags=lanczos"]
        if d > 0:
            f.append(f"hqdn3d={2 * d:.2f}:{1.5 * d:.2f}:{3 * d:.2f}:{2.2 * d:.2f}")
        if sh > 0:
            f.append(f"unsharp=5:5:{0.9 * sh:.2f}:5:5:0")
        return ",".join(f)
    if op == "interpolate":
        fps = float(p.get("fps", 60))
        if p.get("method", "mci") == "blend":
            return f"minterpolate=fps={fps}:mi_mode=blend"
        return f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
    if op == "denoise":
        s = _clamp(p.get("strength", 0.5))
        m = p.get("method", "hqdn3d")
        if m == "nlmeans":
            return f"nlmeans=s={1 + 6 * s:.2f}:p=7:r=15"
        if m == "bm3d":
            return f"format=yuv420p,bm3d=sigma={2 + 10 * s:.1f}:block=8:bstep=4:group=8:estim=basic"
        return f"hqdn3d={6 * s:.2f}:{4.5 * s:.2f}:{9 * s:.2f}:{6.75 * s:.2f}"
    if op == "deblock":
        s = _clamp(p.get("strength", 0.5))
        return f"deblock=filter={'strong' if s > 0.5 else 'weak'}:block=8:alpha={0.05 + 0.15 * s:.3f}:beta={0.03 + 0.1 * s:.3f}"
    if op == "sharpen":
        a = _clamp(p.get("amount", 0.5))
        if p.get("method") == "cas":
            return f"cas=strength={a:.2f}"
        return f"unsharp=5:5:{1.5 * a:.2f}:5:5:0"
    raise ValueError(op)


def _film_graph(p: dict) -> str:
    g, hal, wv = _clamp(p.get("grain", 0.3)), _clamp(p.get("halation", 0.3)), _clamp(p.get("weave", 0.3))
    parts = ["[0:v]format=gbrp,split[base][hi]",
             # halation-like glow: only the brightest highlights, blurred wide, tinted red-orange, screened back
             "[hi]curves=all='0/0 0.72/0 1/1',gblur=sigma=14,colorchannelmixer=rr=1:gg=0.38:bb=0.18[glow]",
             f"[base][glow]blend=all_mode=screen:all_opacity={0.85 * hal:.3f},format=yuv420p[h]"]
    last = "h"
    if g > 0:
        parts.append(f"[{last}]noise=c0s={int(4 + 22 * g)}:c0f=t+u:c1s={int(2 + 6 * g)}:c1f=t+u:c2s={int(2 + 6 * g)}:c2f=t+u[gr]")
        last = "gr"
    if wv > 0:
        a = 0.4 + 1.4 * wv
        parts.append(f"[{last}]scale=iw+8:ih+8:flags=bicubic,crop=iw-8:ih-8:x='4+{a:.2f}*sin(n*0.61)+{a / 2:.2f}*sin(n*1.73)':"
                     f"y='4+{a * 0.7:.2f}*sin(n*0.47+1)'[wv]")
        last = "wv"
    return ";".join(parts) + f";[{last}]null[out]"


def is_hdr(src: str | Path) -> dict:
    raw = ff.probe_raw(src)
    vs = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
    tr = vs.get("color_transfer", "")
    return {"hdr": tr in HDR_TRANSFERS, "transfer": tr, "primaries": vs.get("color_primaries"), "matrix": vs.get("color_space")}


def _manifest(op: str, src, out: Path, params: dict, seconds: float, model: str | None = None) -> dict:
    prov = {"kind": "enhanced", "method": f"enhance.{op}", "params": params, "model": model, "task": op}
    return classify.manifest(op, prov, out, inputs=[contracts.artifact(src, role="source", hash_it=False)],
                             compute={"device": "cpu", "seconds": round(seconds, 2)}, seconds=seconds, path_check=False)


def run(op: str, src: str | Path, out: str | Path | None = None, *, start: float = 0.0, duration: float | None = None,
        use_cache: bool = True, **params) -> dict:
    """Run one enhancement op on src[start:start+duration]. Returns {"path","manifest","seconds","cached","params"}."""
    if op not in OPS:
        raise ValueError(f"unknown enhancement {op!r}; available: {sorted(OPS)}")
    src = Path(src)
    info = ff.probe(src)
    model = OPS[op].get("model", {}).get(params.get("method", ""))
    if op == "tonemap":
        h = is_hdr(src)
        if not h["hdr"]:
            return {"path": str(src), "manifest": None, "seconds": 0.0, "cached": False, "params": params,
                    "note": f"source transfer is {h['transfer'] or 'unspecified'} (SDR): tone mapping not applied"}
    k = gcache.key(f"enhance.{op}", {**params, "start": start, "duration": duration}, [src], model)
    mc = gcache.MediaCache()
    hit = mc.get(k, ".mp4") if use_cache and out is None else None
    if hit:
        meta = mc.meta(k) or {}
        return {"path": str(hit), "manifest": meta.get("manifest"), "seconds": 0.0, "cached": True, "params": params}
    dest = Path(out) if out else mc.path_for(k, ".mp4")
    t0 = time.time()
    seg = ["-ss", f"{start:.3f}"] + (["-t", f"{duration:.3f}"] if duration else [])
    audio = ["-c:a", "aac", "-b:a", "192k"] if info.get("audio") else ["-an"]
    if op == "upscale" and params.get("method") == "esrgan":
        _esrgan(src, dest, params, start, duration, info)
    elif op == "film_look":
        ff.run([*seg, "-i", src, "-filter_complex", _film_graph(params), "-map", "[out]", "-map", "0:a?", *config.VCODEC,
                *audio, dest], op="enhance-film-look")
    elif op == "tonemap":
        alg = params.get("algorithm", "hable")
        peak = f":peak={float(params['peak']) / 100:.2f}" if params.get("peak") else ""
        vf = (f"zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap={alg}:desat=0{peak},"
              "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
        ff.run([*seg, "-i", src, "-vf", vf, *config.VCODEC, "-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709", *audio, dest], op="enhance-tonemap")
    else:
        ff.run([*seg, "-i", src, "-vf", _vfilter(op, params, info), *config.VCODEC, *audio, dest], op=f"enhance-{op}")
    secs = time.time() - t0
    man = _manifest(op, src, dest, params, secs, model)
    if out is None:
        mc.put(k, dest, {"op": op, "params": params, "manifest": man})
    return {"path": str(dest), "manifest": man, "seconds": round(secs, 2), "cached": False, "params": params}


def _esrgan(src: Path, dest: Path, params: dict, start: float, duration: float | None, info: dict) -> None:
    import cv2

    from . import esrgan, framepipe
    if not downloads.installed(esrgan.MODEL_ID):
        p = downloads.plan(esrgan.MODEL_ID)
        raise downloads.ConsentRequired(f"Real-ESRGAN weights ({p['total_gb']} GB, {p['licence']['weights_licence']}) are not "
                                        "downloaded; call generate.downloads.fetch() with the user's consent", p)
    wpath = downloads.path(esrgan.MODEL_ID)
    s = int(params.get("scale", 2))
    v = info["video"]
    ow, oh = v["width"] * s // 2 * 2, v["height"] * s // 2 * 2
    tile = int(params.get("tile", 256))

    def fn(frame, _i):
        y = esrgan.upscale_rgb(frame, wpath, tile=tile)
        return y if (y.shape[1], y.shape[0]) == (ow, oh) else cv2.resize(y, (ow, oh), interpolation=cv2.INTER_AREA)
    framepipe.process_video(src, dest, fn, out_size=(ow, oh), start=start, duration=duration, chunk_s=float(params.get("chunk_s", 5)),
                            job_key=f"esrgan:{s}:{tile}")


def upscale(src, out=None, scale: int = 2, method: str = "lanczos", **kw) -> dict:
    return run("upscale", src, out, scale=scale, method=method, **kw)


def interpolate(src, out=None, fps: float = 60, method: str = "mci", **kw) -> dict:
    return run("interpolate", src, out, fps=fps, method=method, **kw)


def denoise(src, out=None, method: str = "hqdn3d", strength: float = 0.5, **kw) -> dict:
    return run("denoise", src, out, method=method, strength=strength, **kw)


def deblock(src, out=None, strength: float = 0.5, **kw) -> dict:
    return run("deblock", src, out, strength=strength, **kw)


def sharpen(src, out=None, amount: float = 0.5, method: str = "unsharp", **kw) -> dict:
    return run("sharpen", src, out, amount=amount, method=method, **kw)


def film_look(src, out=None, grain: float = 0.3, halation: float = 0.3, weave: float = 0.3, **kw) -> dict:
    return run("film_look", src, out, grain=grain, halation=halation, weave=weave, **kw)


def tonemap(src, out=None, algorithm: str = "hable", **kw) -> dict:
    return run("tonemap", src, out, algorithm=algorithm, **kw)


def apply(clip: dict, op: str, **params) -> dict:
    """Return a NEW clip whose `src` is the processed segment [in, out] of the original (cached). Clip keys stay;
    `in`/`out` are rebased to the new file; the original reference is kept in `enhanced_from` for undo."""
    if clip.get("type") != "video":
        raise ValueError("apply() processes video clips; process image clips with the op directly")
    t_in, t_out = float(clip.get("in", 0)), float(clip["out"])
    r = run(op, clip["src"], start=t_in, duration=t_out - t_in, **params)
    new = dict(clip)
    hist = list(clip.get("enhanced_from") or [])
    hist.append({"src": clip["src"], "in": t_in, "out": t_out, "op": op, "params": params})
    new.update({"src": r["path"], "in": 0.0, "out": round(t_out - t_in, 3), "enhanced_from": hist})
    if r.get("manifest"):
        new["provenance"] = {**(clip.get("provenance") or {"kind": "footage"}), "processing": [h["op"] for h in hist]}
    return new
