"""Visual understanding (VIS-1): image-text embeddings on CPU, per-shot visual facts, visual similarity.

    available() -> bool                      model files present (never downloads by itself)
    ensure_model(allow_download=False)       opt-in download (~215 MB) through the Cost Guard
    embed_texts([str]) / embed_images([rgb]) -> L2-normalised float32 [n, 768]
    frames(path, times) / sample(path, ...)  RGB frames via FFmpeg
    analyze_asset(path, analysis) -> visual  per-shot embedding (cached .npz), scene labels (zero-shot), shot type
                                             (close-up/medium/wide from face size, else zero-shot), motion level, near
                                             duplicates; MediaAnalysis["visual"]
    asset_embedding(path) -> vector          one cached embedding for a B-roll candidate (still or video)
    text_image_scores(texts, emb) / similarity(a, b)

Model: SigLIP base patch16-224 (google/siglip-base-patch16-224, Apache-2.0) in the int8 ONNX export of
Xenova/siglip-base-patch16-224 (vision 99.5 MB + text 111.5 MB), run with onnxruntime on CPU. No torch.
Measured on the shared 4-vCPU build container: docs/results/a2_visual_bench.json.
Without the model every function degrades: `available: False`, callers keep their lexical path and say so.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path

import numpy as np

from .. import config

MODEL_ID = "siglip-base-patch16-224-int8"
MODEL_REPO = "Xenova/siglip-base-patch16-224"
BASE_MODEL = "google/siglip-base-patch16-224"
LICENCE = ("SigLIP base patch16-224 weights: Apache-2.0 (huggingface.co/google/siglip-base-patch16-224 model card, read "
           "2026-11-12); ONNX int8 export by Xenova (same weights, derivative under Apache-2.0); onnxruntime: MIT")
FILES = {"vision_model_quantized.onnx": 99_499_129, "text_model_quantized.onnx": 111_475_220, "tokenizer.json": 2_398_744,
         "preprocessor_config.json": 368}
DOWNLOAD_MB = round(sum(FILES.values()) / 1e6, 1)
SIZE = 224
MAX_TOKENS = 64
PAD_ID = 1
CACHE_VERSION = 1
# Zero-shot vocabularies (prompted as "a photo of {}."). Labels are generic, not tied to one benchmark.
SCENE_LABELS = ("a person talking to the camera", "two people in a conversation", "a group of people at a table",
                "a podcast studio with microphones", "a crowd of people", "a city street", "a building exterior",
                "an indoor room", "an office", "a laboratory", "a factory or machinery", "a classroom or lecture",
                "a landscape", "the ocean or a beach", "mountains", "a forest", "the earth seen from space",
                "outer space with stars", "a rocket or spacecraft", "an aircraft", "a car or road traffic", "a train",
                "animals", "food", "a product on a table", "a computer screen", "a chart or diagram",
                "text or a title card", "an animation or cartoon", "an aerial view", "a night scene", "a sports event")
SHOT_LABELS = {"close-up": "a close-up of a face", "medium": "a medium shot of a person from the waist up",
               "wide": "a wide shot of a scene"}

_LOCK = threading.Lock()
_SESS: dict = {}
_TOK = None
_LABEL_CACHE: dict = {}


# ---------------------------------------------------------------- model
def model_dir() -> Path:
    import os
    env = os.environ.get("NEXUS_VISUAL_MODEL_DIR")  # a shared read-only copy (tests, several NEXUS_HOMEs)
    return Path(env) if env else config.MODELS / "visual" / "siglip-base-patch16-224"


def available() -> bool:
    d = model_dir()
    return all((d / f).exists() and (d / f).stat().st_size >= min(n, 1000) * 0.9 for f, n in FILES.items())


def ensure_model(allow_download: bool = False) -> bool:
    """True when the model is usable. Downloads (DOWNLOAD_MB MB from huggingface.co) only when explicitly allowed."""
    if available():
        return True
    if not allow_download:
        return False
    from .. import costguard, net
    costguard.authorize("siglip-visual", "download")
    for f in FILES:
        sub = "" if f.endswith(".json") else "onnx/"
        net.download(f"https://huggingface.co/{MODEL_REPO}/resolve/main/{sub}{f}", model_dir() / f, timeout=600, retries=2)
    return available()


def info() -> dict:
    return {"model": MODEL_ID, "base": BASE_MODEL, "repo": MODEL_REPO, "licence": LICENCE, "download_mb": DOWNLOAD_MB,
            "available": available(), "runtime": "onnxruntime CPU"}


def _session(name: str):
    import onnxruntime as ort
    with _LOCK:
        if name not in _SESS:
            so = ort.SessionOptions()
            so.intra_op_num_threads = 2  # the CPU is shared; two threads measured close to four on this model
            _SESS[name] = ort.InferenceSession(str(model_dir() / name), so, providers=["CPUExecutionProvider"])
        return _SESS[name]


def _tokenizer():
    global _TOK
    if _TOK is None:
        from tokenizers import Tokenizer
        _TOK = Tokenizer.from_file(str(model_dir() / "tokenizer.json"))
    return _TOK


def _norm(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-9)


def embed_texts(texts: list[str]) -> np.ndarray:
    """L2-normalised SigLIP text embeddings [n, 768]. Lower-cased and padded to 64 tokens (the model's training setup)."""
    if not texts:
        return np.zeros((0, 768), dtype=np.float32)
    tok = _tokenizer()
    ids = np.full((len(texts), MAX_TOKENS), PAD_ID, dtype=np.int64)
    for i, t in enumerate(texts):
        e = tok.encode((t or "").lower()).ids[:MAX_TOKENS]
        if e and e[-1] != 1:
            e = e[:MAX_TOKENS - 1] + [1]
        ids[i, :len(e)] = e
    out = []
    s = _session("text_model_quantized.onnx")
    for k in range(0, len(texts), 16):
        out.append(s.run(["pooler_output"], {"input_ids": ids[k:k + 16]})[0])
    return _norm(np.concatenate(out))


def _prep(rgb: np.ndarray) -> np.ndarray:
    from PIL import Image
    im = Image.fromarray(np.asarray(rgb, dtype=np.uint8))
    if im.size != (SIZE, SIZE):
        im = im.resize((SIZE, SIZE), Image.BICUBIC)  # SiglipImageProcessor: plain resize, no crop
    a = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    return ((a - 0.5) / 0.5).transpose(2, 0, 1)


def embed_images(images: list) -> np.ndarray:
    """L2-normalised SigLIP image embeddings [n, 768] from RGB uint8 arrays (any size) or image paths."""
    if not len(images):
        return np.zeros((0, 768), dtype=np.float32)
    batch = []
    for im in images:
        if isinstance(im, (str, Path)):
            from PIL import Image
            im = np.asarray(Image.open(im).convert("RGB"))
        batch.append(_prep(im))
    x = np.stack(batch).astype(np.float32)
    s = _session("vision_model_quantized.onnx")
    out = [s.run(["pooler_output"], {"pixel_values": x[k:k + 8]})[0] for k in range(0, len(x), 8)]
    return _norm(np.concatenate(out))


def similarity(a, b) -> float:
    a, b = _norm(np.asarray(a)), _norm(np.asarray(b))
    return float(np.dot(a.ravel(), b.ravel()))


def label_embeddings(labels: tuple[str, ...] | list[str], template: str = "a photo of {}.") -> np.ndarray:
    key = (tuple(labels), template)
    if key not in _LABEL_CACHE:
        _LABEL_CACHE[key] = embed_texts([template.format(x) for x in labels])
    return _LABEL_CACHE[key]


def zero_shot(emb: np.ndarray, labels, top: int = 3, template: str = "a photo of {}.") -> list[dict]:
    """Labels ranked by cosine similarity, with a softmax share over the label set (temperature 0.01). The share is
    relative to this label list only; it is not a calibrated probability."""
    L = label_embeddings(tuple(labels), template)
    sims = L @ _norm(emb).ravel()
    p = np.exp((sims - sims.max()) / 0.01)
    p /= p.sum()
    order = np.argsort(-sims)[:top]
    return [{"label": labels[i], "cos": round(float(sims[i]), 4), "share": round(float(p[i]), 3)} for i in order]


# ---------------------------------------------------------------- frames
def _probe_wh(path) -> tuple[int, int, float]:
    from . import ff
    inf = ff.probe(path)
    v = inf.get("video") or {}
    return int(v.get("width") or 0), int(v.get("height") or 0), float(inf.get("duration") or 0.0)


def sample(path, fps: float = 1.0, max_frames: int = 400, size: int = SIZE) -> tuple[list[float], np.ndarray]:
    """RGB frames at `fps` (capped at max_frames over the file) squashed to size x size in one FFmpeg pass."""
    _, _, dur = _probe_wh(path)
    rate = max(0.02, min(fps, max_frames / max(dur, 0.1)))
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-an", "-vf",
                        f"fps={rate:.5f},scale={size}:{size}:flags=bicubic,format=rgb24", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=3600)
    n = len(p.stdout) // (size * size * 3)
    fr = np.frombuffer(p.stdout[:n * size * size * 3], dtype=np.uint8).reshape(n, size, size, 3)
    return [round((k + 0.5) / rate, 3) for k in range(n)], fr


def frames(path, times: list[float], size: int = SIZE) -> np.ndarray:
    """RGB frames at the given times (input seeking, one FFmpeg call per frame; for a few frames only)."""
    out = []
    for t in times:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
                            "-frames:v", "1", "-vf", f"scale={size}:{size}:flags=bicubic,format=rgb24", "-f", "rawvideo", "-"],
                           capture_output=True, timeout=120)
        if len(p.stdout) >= size * size * 3:
            out.append(np.frombuffer(p.stdout[:size * size * 3], dtype=np.uint8).reshape(size, size, 3))
    return np.stack(out) if out else np.zeros((0, size, size, 3), dtype=np.uint8)


# ---------------------------------------------------------------- cache
def _cache_dir() -> Path:
    d = config.CACHE / "visual"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(path, extra: str = "") -> str:
    p = Path(path)
    st = p.stat()
    return hashlib.sha1(f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}|{MODEL_ID}|v{CACHE_VERSION}|{extra}".encode()).hexdigest()[:20]


def asset_embedding(path, n: int = 3) -> np.ndarray | None:
    """Mean embedding of a still or of `n` frames spread over a video (cached per file). None without the model."""
    if not available() or not Path(path).exists():
        return None
    f = _cache_dir() / f"asset_{_key(path, str(n))}.npy"
    if f.exists():
        return np.load(f)
    if Path(path).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"):
        e = embed_images([path])
    else:
        _, _, dur = _probe_wh(path)
        ts = [dur * (k + 0.5) / n for k in range(n)] if dur > 0 else [0.0]
        fr = frames(path, ts)
        if not len(fr):
            return None
        e = embed_images(list(fr))
    v = _norm(e.mean(0))
    np.save(f, v)
    return v


# ---------------------------------------------------------------- per-shot analysis
MOTION_LEVELS = ((0.012, "static"), (0.045, "low"), (0.09, "medium"))  # mean abs frame diff (0..1) at 1 fps, 224 px


def motion_level(diff: float | None) -> str | None:
    if diff is None:
        return None
    for thr, name in MOTION_LEVELS:
        if diff < thr:
            return name
    return "high"


def shot_type_from_face(face_h: float | None) -> str | None:
    """Close-up / medium / wide from the largest face's height as a share of the frame height (the usual framing
    rules: a face filling a third of the frame or more is a close-up)."""
    if face_h is None:
        return None
    return "close-up" if face_h >= 0.30 else "medium" if face_h >= 0.12 else "wide"


def _face_h(analysis: dict | None, a: float, b: float) -> float | None:
    f = (analysis or {}).get("faces") or {}
    if not f.get("available"):
        return None
    pts = [s["h"] for s in ((f.get("tracks") or [{}])[0].get("samples") or []) if a <= s["t"] < b and s.get("h")]
    return float(np.median(pts)) if pts else 0.0  # face model ran and found no face in this shot: no person in close view


def _shots_of(analysis: dict | None, dur: float) -> list[dict]:
    an = analysis or {}
    sh = an.get("shots") or [{"id": k, "start": float(a), "end": float(b)} for k, (a, b) in enumerate(an.get("scenes") or [])]
    return [dict(s) for s in sh] or [{"id": 0, "start": 0.0, "end": dur}]


def analyze_asset(path, analysis: dict | None = None, fps: float = 1.0, max_frames: int = 400, dup_cos: float = 0.93) -> dict:
    """Per-shot visual facts for one asset. Embeddings go to the cache (.npz next to a JSON summary); the returned dict
    is small enough for MediaAnalysis["visual"]. Without the model: {"available": False, ...} and nothing else."""
    if not available():
        return {"available": False, "model": MODEL_ID, "method": "none: visual model not installed (lexical fallback)",
                "download_mb": DOWNLOAD_MB}
    _, _, dur = _probe_wh(path)
    k = _key(path, f"{fps}|{max_frames}|{json.dumps([(s['start'], s['end']) for s in _shots_of(analysis, dur)])[:4000]}")
    jf, nf = _cache_dir() / f"shots_{k}.json", _cache_dir() / f"shots_{k}.npz"
    if jf.exists() and nf.exists():
        out = json.loads(jf.read_text(encoding="utf-8"))
        out["cache"] = {"json": str(jf), "npz": str(nf), "hit": True}
        return out
    import time
    t0 = time.time()
    times, fr = sample(path, fps=fps, max_frames=max_frames)
    t1 = time.time()
    embs = embed_images(list(fr)) if len(fr) else np.zeros((0, 768), np.float32)
    t2 = time.time()
    grey = fr.astype(np.float32).mean(-1) / 255.0 if len(fr) else None
    shots = _shots_of(analysis, dur)
    rows, shot_embs = [], []
    for s in shots:
        a, b = float(s["start"]), float(s["end"])
        idx = [i for i, t in enumerate(times) if a <= t < b] or ([min(range(len(times)), key=lambda i: abs(times[i] - (a + b) / 2))]
                                                                 if times else [])
        if not idx:
            continue
        e = _norm(embs[idx].mean(0))
        shot_embs.append(e)
        diffs = [float(np.abs(grey[i] - grey[i - 1]).mean()) for i in idx[1:]] if grey is not None else []
        mot = round(float(np.mean(diffs)), 4) if diffs else None
        fh = _face_h(analysis, a, b)
        st_face = shot_type_from_face(fh)
        zs = zero_shot(e, list(SHOT_LABELS.values()), top=3)
        st_zs = next(k for k, v in SHOT_LABELS.items() if v == zs[0]["label"])
        rows.append({"id": s.get("id", len(rows)), "start": round(a, 3), "end": round(b, 3), "frames": len(idx),
                     "scene": zero_shot(e, SCENE_LABELS, top=3),
                     "shot_type": st_face or st_zs, "shot_type_source": "face size" if st_face else "zero-shot",
                     **({"face_h": round(fh, 3)} if fh is not None else {}),
                     "motion": mot, "motion_level": motion_level(mot)})
    E = np.stack(shot_embs) if shot_embs else np.zeros((0, 768), np.float32)
    S = E @ E.T if len(E) else np.zeros((0, 0))
    dup_groups: list[list[int]] = []
    seen: set = set()
    for i in range(len(rows)):
        if i in seen:
            continue
        g = [j for j in range(i, len(rows)) if j not in seen and S[i, j] >= dup_cos]
        if len(g) > 1:
            dup_groups.append([rows[j]["id"] for j in g])
            seen |= set(g)
            for j in g[1:]:
                rows[j]["dup_of"] = rows[g[0]]["id"]
    out = {"available": True, "model": MODEL_ID, "licence": LICENCE, "method":
           f"SigLIP int8 ONNX image embeddings of frames sampled at {len(times) / max(dur, 0.1):.2f} fps; scene labels "
           f"zero-shot over {len(SCENE_LABELS)} generic prompts; shot type from face height when face tracks exist",
           "frames": len(times), "shots": rows, "duplicates": dup_groups, "dup_cos": dup_cos,
           "variety": round(float(1.0 - S[np.triu_indices(len(E), 1)].mean()) if len(E) > 1 else 0.0, 4),
           "timing": {"decode_s": round(t1 - t0, 2), "embed_s": round(t2 - t1, 2),
                      "per_frame_ms": round(1000 * (t2 - t1) / max(1, len(fr)), 1)}}
    np.savez_compressed(nf, shots=E.astype(np.float16), frames=embs.astype(np.float16), times=np.array(times, np.float32),
                        ids=np.array([r["id"] for r in rows]))
    jf.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    out["cache"] = {"json": str(jf), "npz": str(nf), "hit": False}
    return out


def shot_embeddings(visual: dict) -> dict[int, np.ndarray]:
    """{shot id: embedding} from an analyze_asset result (reads its cached .npz)."""
    nf = Path(((visual or {}).get("cache") or {}).get("npz") or "")
    if not nf.exists():
        return {}
    z = np.load(nf)
    return {int(i): _norm(z["shots"][k].astype(np.float32)) for k, i in enumerate(z["ids"])}


def text_image_scores(texts: list[str], emb: np.ndarray) -> list[float]:
    """Cosine similarity of each text to one image embedding."""
    if not texts:
        return []
    return [round(float(x), 4) for x in embed_texts(texts) @ _norm(emb).ravel()]


def attach(an: dict, vis: dict) -> dict:
    """Store `vis` as an["visual"] and copy the per-shot facts (shot_type, scene label, motion level, visual duplicate)
    onto the matching an["shots"] entries (additive)."""
    an["visual"] = vis
    if not vis.get("available"):
        return an
    by_id = {r["id"]: r for r in vis.get("shots") or []}
    for sh in an.get("shots") or []:
        r = by_id.get(sh.get("id"))
        if not r:
            continue
        sh["shot_type"] = r["shot_type"]
        sh["scene_label"] = r["scene"][0]["label"] if r.get("scene") else None
        sh["motion_level"] = r.get("motion_level")
        if "dup_of" in r:
            sh["visual_dup_of"] = r["dup_of"]
        sh["tags"] = sorted(set(sh.get("tags") or []) | {r["shot_type"]} | ({sh["scene_label"]} if sh["scene_label"] else set()))
    u = an.get("understanding")
    if isinstance(u, dict):
        u["notes"] = [n for n in u.get("notes") or [] if not n.startswith("no vision model")] + [
            f"visual model {MODEL_ID}: shot type, scene label (zero-shot), motion level and visual duplicates per shot"]
    return an


def enrich(an: dict, path=None) -> dict:
    """analyze_asset + attach, best effort: a failure is recorded, never raised."""
    path = path or an.get("path")
    try:
        vis = analyze_asset(path, an) if path and Path(path).exists() else {"available": False, "method": "no file"}
    except Exception as e:  # noqa: BLE001 - the base analysis stands
        vis = {"available": False, "method": f"failed: {type(e).__name__}: {e}"[:200]}
    return attach(an, vis)
