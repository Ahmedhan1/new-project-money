"""Asset previews, rendered on this machine on first request and cached in NEXUS_HOME/assets/thumbnails.

LUTs, effects, transitions, overlays and motion presets are previewed by running the REAL renderer path (the same
FFmpeg filters the render uses) on a generated reference chart, so a preview that renders proves the asset works with
the installed FFmpeg. Sounds get a waveform image; fonts a type specimen.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import wave
from pathlib import Path

import numpy as np

from .. import config

W, H = 480, 270


def _thumbs() -> Path:
    d = config.ASSETS / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reference(variant: str = "a") -> Path:
    """A deterministic test picture: sky gradient, skin-tone patches, colour chips, grey ramp."""
    from PIL import Image, ImageDraw
    out = _thumbs() / f"_ref_{variant}_v1.png"
    if out.exists():
        return out
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    top, bot = ((40, 90, 160), (230, 180, 120)) if variant == "a" else ((20, 60, 40), (120, 170, 90))
    for y in range(H):
        k = y / H
        d.line([0, y, W, y], fill=tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)))
    for i, c in enumerate([(255, 220, 190), (224, 172, 135), (180, 120, 85), (110, 70, 45)]):
        d.ellipse([30 + i * 60, 60, 80 + i * 60, 120], fill=c)
    chips = [(200, 40, 40), (40, 160, 60), (40, 70, 200), (230, 200, 30), (200, 60, 180), (30, 190, 200)]
    for i, c in enumerate(chips if variant == "a" else chips[::-1]):
        d.rectangle([290 + (i % 3) * 58, 50 + (i // 3) * 58, 340 + (i % 3) * 58, 100 + (i // 3) * 58], fill=c)
    for i in range(16):
        g = int(i * 255 / 15)
        d.rectangle([i * W // 16, H - 40, (i + 1) * W // 16, H], fill=(g, g, g))
    d.rectangle([180, 140, 300, 220], fill=(250, 250, 250) if variant == "a" else (15, 15, 15))
    img.save(out)
    return out


def _ff(args: list, out: Path) -> Path:
    tmp = out.with_name(f".{out.stem}.tmp{out.suffix}")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y", *map(str, args), str(tmp)], capture_output=True, text=True)
    if r.returncode or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"preview failed: {r.stderr[-400:]}")
    tmp.replace(out)
    return out


def _key(e: dict) -> str:
    return hashlib.sha1(json.dumps([e.get("apply"), e.get("params"), e.get("sha256"), e.get("kind"), 2], sort_keys=True, default=str).encode()).hexdigest()[:10]


def _label(text: str) -> str:
    """drawtext with the font path and text escaped for the filtergraph (Windows paths such as C:\\Windows\\Fonts\\x.ttf
    contain ':' and '\\', which break an unescaped filter - found by the Windows clean-machine run)."""
    from ..media import ff
    t = text.replace("\\", "").replace(":", " ").replace("'", "")[:40]
    return (f"drawtext=fontfile={ff.filter_path(config.FONT_LATIN)}:text={ff.escape_filter_value(t)}"
            ":x=6:y=6:fontsize=16:fontcolor=white:box=1:boxcolor=black@0.55")


def _effect(e: dict, out: Path) -> Path:
    from ..fx import effects as fxe
    clip = {"effects": [e["apply"]["effect"]]}
    geo, look = fxe.video_filters(clip, W, H, 30, 2.0, 1.0)
    chain = ",".join(["format=yuv420p", *geo, *look, "format=yuv420p"])
    return _ff(["-loop", "1", "-t", "2", "-r", "30", "-i", reference("a"), "-filter_complex", f"[0:v]{chain},{_label(e['name'])}[o]",
                "-map", "[o]", "-ss", "1.2", "-frames:v", "1"], out)


def _transition(e: dict, out: Path) -> Path:
    from ..fx import transitions
    f = transitions.filter_for(e["apply"]["transition"], 1.0, 0.5)
    return _ff(["-loop", "1", "-t", "2", "-r", "30", "-i", reference("a"), "-loop", "1", "-t", "2", "-r", "30", "-i", reference("b"),
                "-filter_complex", f"[0:v]format=yuv420p[x];[1:v]format=yuv420p[y];[x][y]{f},{_label(e['name'])}[o]",
                "-map", "[o]", "-ss", "1.0", "-frames:v", "1"], out)


def _lut(e: dict, path: str, out: Path) -> Path:
    from ..media import ff
    p = ff.filter_path(path)
    return _ff(["-i", reference("a"), "-filter_complex",
                f"[0:v]split[a][b];[b]lut3d=file={p}[g];[a]crop=iw/2:ih:0:0[l];[g]crop=iw/2:ih:iw/2:0[r];[l][r]hstack,{_label(e['name'] + ' (left original)')}[o]",
                "-map", "[o]", "-frames:v", "1"], out)


def _overlay(e: dict, path: str, out: Path) -> Path:
    return _ff(["-i", reference("a"), "-i", path, "-filter_complex",
                f"[1:v]scale={W}:{H},format=rgba[ov];[0:v][ov]overlay=0:0,{_label(e['name'])}[o]", "-map", "[o]", "-frames:v", "1"], out)


def _motion(e: dict, out: Path) -> Path:
    """Three frames (start, middle, end) of the preset rendered by the real segment renderer."""
    import tempfile

    from ..timeline import ops, render
    from . import apply_to_timeline
    ref = str(reference("a"))
    tl = {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none", "clips": [],
          "audio": [], "music": None, "subtitles": [], "markers": []}
    c = ops.image_clip(ref, 2.0, kenburns="none")
    if "title_anim" in e["apply"]:
        c = ops.color_clip(2.0, "0x0b1020")
        c["titles"] = [{"text": "Title", "style": "title", "start": 0.0, "end": 2.0}]
    tl["clips"] = [c]
    tl, _ = apply_to_timeline(tl, e["id"], "clip", c["id"])
    seg, _ = render.render_segment(tl["clips"][0], tl, config.CACHE / "previews", draft=False)
    with tempfile.TemporaryDirectory(dir=str(_thumbs())) as d:
        frames = []
        for i, t in enumerate((0.1, 0.5, 1.6)):
            frames.append(_ff(["-ss", t, "-i", seg, "-frames:v", "1", "-vf", f"scale={W // 3}:-2"], Path(d) / f"f{i}.png"))
        return _ff([*sum((["-i", f] for f in frames), []), "-filter_complex", f"[0][1][2]hstack=inputs=3,pad={W}:{H}:0:(oh-ih)/2,{_label(e['name'])}[o]",
                    "-map", "[o]", "-frames:v", "1"], out)


def _template(e: dict, path: str, out: Path) -> Path:
    from PIL import Image

    from ..fx import titles
    tpl = json.loads(Path(path).read_text(encoding="utf-8"))
    img = Image.open(reference("a")).convert("RGBA")
    if tpl.get("insert_color_clip") or tpl.get("append_color_clip"):
        img = Image.new("RGBA", (W, H), (11, 16, 32, 255))
    for t in tpl.get("titles") or []:
        img.alpha_composite(Image.open(titles.title_png(t, W, H, "en")).convert("RGBA"))
    if tpl.get("credits"):
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        for i, line in enumerate(tpl["credits"]["lines"]):
            d.text((W // 2 - 60, 40 + i * 30), line, fill=(230, 230, 230, 255))
    tmp = out.with_suffix(".png")
    img.convert("RGB").save(tmp)
    return _ff(["-i", tmp, "-vf", _label(e["name"]), "-frames:v", "1"], out)


def _waveform(e: dict, path: str, out: Path) -> Path:
    from PIL import Image, ImageDraw
    p = Path(path)
    if p.suffix.lower() != ".wav":
        tmpw = out.with_suffix(".wav")
        _ff(["-i", p, "-ac", "1", "-ar", "16000"], tmpw)
        p = tmpw
    with wave.open(str(p)) as w:
        n, ch, sw = w.getnframes(), w.getnchannels(), w.getsampwidth()
        x = np.frombuffer(w.readframes(n), dtype="<i2" if sw == 2 else np.uint8).astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    x = x / (np.abs(x).max() + 1e-9)
    img = Image.new("RGB", (W, H // 2), (16, 20, 32))
    d = ImageDraw.Draw(img)
    cols = np.array_split(x, W) if len(x) >= W else [x]
    for i, c in enumerate(cols):
        a = float(np.abs(c).max()) if len(c) else 0
        d.line([i, H // 4 - a * H // 4, i, H // 4 + a * H // 4], fill=(62, 197, 255))
    d.text((6, 4), e["name"][:50], fill=(255, 255, 255))
    if p != Path(path):
        p.unlink(missing_ok=True)
    tmp = out.with_suffix(".png")
    img.save(tmp)
    return _ff(["-i", tmp, "-frames:v", "1"], out)


def _font(e: dict, path: str, out: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (W, H // 2), (16, 20, 32))
    d = ImageDraw.Draw(img)
    sample = "أبجد هوز" if "arabic" in e["id"] else "The quick brown fox 0123"
    f = ImageFont.truetype(path, 34)
    d.text((14, 20), e["name"], font=ImageFont.truetype(path, 26), fill=(255, 214, 0))
    d.text((14, 66), sample, font=f, fill=(240, 240, 240))
    tmp = out.with_suffix(".png")
    img.save(tmp)
    return _ff(["-i", tmp, "-frames:v", "1"], out)


def preview(asset_id: str, install: bool = False) -> Path:
    """Path of the cached preview JPG (rendered on first request). Non-installed file assets are installed first only
    when `install` is True (previews never download behind the user's back)."""
    from . import entry, installed, status
    from . import install as do_install
    e = entry(asset_id)
    out = _thumbs() / f"{asset_id}.{_key(e)}.jpg"
    if out.exists():
        return out
    ap = e.get("apply") or {}
    if e["kind"] != "builtin" and status(e) != "installed":
        if not install:
            raise FileNotFoundError(f"{asset_id} is not installed; install it to see a preview")
        do_install(asset_id)
    rec = installed().get(asset_id, {})
    path = (rec.get("files") or [None])[0]
    cat = e["category"]
    if "effect" in ap:
        return _effect(e, out)
    if "transition" in ap:
        return _transition(e, out)
    if "keyframes" in ap or "title_anim" in ap:
        return _motion(e, out)
    if "process" in ap or "captions" in ap:
        return _template({"name": e["name"]}, _desc_tpl(e), out)
    if cat == "luts":
        return _lut(e, path, out)
    if cat in ("frames", "overlays") or (cat == "titles" and str(path).endswith(".png")):
        return _overlay(e, path, out)
    if cat in ("templates", "titles"):
        return _template(e, path, out)
    if cat in ("sound_effects", "music"):
        return _waveform(e, path, out)
    if cat == "fonts":
        return _font(e, path, out)
    raise FileNotFoundError(f"no preview for {asset_id}")


def _desc_tpl(e: dict) -> str:
    p = _thumbs() / f"_tpl_{e['id']}.json"
    p.write_text(json.dumps({"titles": [{"text": e["name"], "subtitle": (e.get("description") or "")[:60], "style": "title",
                                          "start": 0, "end": 2}]}), encoding="utf-8")
    return str(p)
