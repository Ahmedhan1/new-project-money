"""Dialogue / background separation with Demucs (htdemucs, two stems), for dubbing that keeps the original music and
effects under the new voice. Before this, a dubbed clip was simply muted, so its music and ambience disappeared too.

Demucs (github.com/adefossez/demucs, facebookresearch/demucs) is MIT-licensed. GPU only in NEXUS (a CUDA GPU in the
cloud studio); NEXUS_DEMUCS=0 switches it off, NEXUS_DEMUCS_CPU=1 allows CPU (slow, for tests). The result is cached per
source file (SHA-256 of its path + size + mtime) next to the project's audio.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODEL = "htdemucs"
LICENSE = "Demucs (MIT) htdemucs two-stem separation"


def available() -> bool:
    if os.environ.get("NEXUS_DEMUCS", "1") == "0":
        return False
    try:
        import importlib.util
        if importlib.util.find_spec("demucs") is None:
            return False
        if os.environ.get("NEXUS_DEMUCS_CPU") == "1":
            return True
        import torch
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def _key(src: Path) -> str:
    st = src.stat()
    return hashlib.sha256(f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def background(src: str | Path, workdir: Path, timeout: int = 3600) -> Path:
    """Path of a WAV with everything except the voice (music, effects, ambience) for the whole source file."""
    src, workdir = Path(src), Path(workdir)
    out = workdir / f"background_{_key(src)}.wav"
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = workdir / f"demucs_{_key(src)}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    wav = tmp / "input.wav"  # demucs reads audio files; extract the soundtrack first (any video container works then)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100", str(wav)],
                   check=True, timeout=600)
    dev = "cuda" if os.environ.get("NEXUS_DEMUCS_CPU") != "1" else "cpu"
    r = subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", MODEL, "-d", dev, "-o", str(tmp), str(wav)],
                       capture_output=True, text=True, timeout=timeout)
    stem = tmp / MODEL / "input" / "no_vocals.wav"
    if r.returncode != 0 or not stem.exists():
        raise RuntimeError(f"demucs failed ({r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stem), out)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def background_items(tl: dict, bg_by_src: dict[str, str], muted_ids: set[str]) -> list[dict]:
    """Timeline audio items that put each muted clip's own background back at the clip's programme position."""
    from ..timeline.render import clip_starts
    items = []
    for c, start in zip(tl["clips"], clip_starts(tl)):
        if c.get("id") not in muted_ids or c.get("type") != "video" or c.get("src") not in bg_by_src:
            continue
        sp = float(c.get("speed", 1.0)) or 1.0
        if abs(sp - 1.0) > 0.02:  # a stretched clip: the background would drift; leave that clip quiet (said in the review)
            continue
        items.append({"src": bg_by_src[c["src"]], "start": round(float(start), 3), "in": round(float(c["in"]), 3),
                      "out": round(float(c["out"]), 3), "role": "ambience", "volume": 1.0, "fade_in": 0.05, "fade_out": 0.05,
                      "clip": c.get("id"), "note": "original music/effects under the dub (Demucs)"})
    return items
