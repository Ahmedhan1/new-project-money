"""Real-ESRGAN general-x4v3 (SRVGGNetCompact, BSD-3) on CPU through onnxruntime.

The 4.9 MB safetensors (registry id `realesr-general-x4v3`, pinned sha256) is turned into an ONNX graph locally
(onnx_build) - no torch, no third-party ONNX export. Architecture (xinntao/Real-ESRGAN, archs/srvgg_arch.py):
    body.0 conv3x3(3->64), body.1 PReLU, 32 x [conv3x3(64->64), PReLU], body.66 conv3x3(64->48)
    out = PixelShuffle(4)(body(x)) + nearest_upsample(x, 4)
Input/output: float32 NCHW RGB in [0, 1].
"""
from __future__ import annotations

import functools
from pathlib import Path

import numpy as np

from . import onnx_build as ob

MODEL_ID = "realesr-general-x4v3"
SCALE = 4


def build_onnx(weights: dict[str, np.ndarray]) -> bytes:
    convs = sorted({int(k.split(".")[1]) for k in weights if k.endswith(".weight") and weights[k].ndim == 4})
    prelus = sorted({int(k.split(".")[1]) for k in weights if k.endswith(".weight") and weights[k].ndim == 1})
    nodes, inits = [], []
    cur = "input"
    for i in sorted(convs + prelus):
        w = weights[f"body.{i}.weight"]
        if i in convs:
            inits += [ob.tensor(f"w{i}", w), ob.tensor(f"b{i}", weights[f"body.{i}.bias"])]
            nodes.append(ob.node("Conv", [cur, f"w{i}", f"b{i}"], [f"c{i}"], kernel_shape=[3, 3], pads=[1, 1, 1, 1]))
            cur = f"c{i}"
        else:
            inits.append(ob.tensor(f"p{i}", w.reshape(-1, 1, 1)))
            nodes.append(ob.node("PRelu", [cur, f"p{i}"], [f"a{i}"]))
            cur = f"a{i}"
    inits.append(ob.tensor("scales", np.array([1, 1, SCALE, SCALE], np.float32)))
    nodes.append(ob.node("DepthToSpace", [cur], ["ps"], blocksize=SCALE, mode="CRD"))
    nodes.append(ob.node("Resize", ["input", "", "scales"], ["base"], mode="nearest"))
    nodes.append(ob.node("Add", ["ps", "base"], ["output"]))
    return ob.model(nodes, inits, [ob.value_info("input", [1, 3, "h", "w"])],
                    [ob.value_info("output", [1, 3, "H", "W"])], name="srvgg_x4v3")


def onnx_path(weights_path: str | Path) -> Path:
    p = Path(weights_path)
    out = p.with_name(p.stem + ".nexus.onnx")
    if not out.exists() or out.stat().st_mtime < p.stat().st_mtime:
        out.write_bytes(build_onnx(ob.read_safetensors(p)))
    return out


@functools.lru_cache(maxsize=2)
def session(weights_path: str, threads: int = 4):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    return ort.InferenceSession(str(onnx_path(weights_path)), so, providers=["CPUExecutionProvider"])


def upscale_rgb(img: np.ndarray, weights_path: str | Path, tile: int = 0, threads: int = 4) -> np.ndarray:
    """uint8 HxWx3 RGB -> uint8 (4H)x(4W)x3. `tile` > 0 processes overlapping tiles to bound RAM on large frames."""
    s = session(str(weights_path), threads)
    x = img.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    if not tile or max(img.shape[:2]) <= tile:
        y = s.run(["output"], {"input": x})[0]
    else:
        pad = 36  # >= receptive field radius of 34 stacked 3x3 convs: tiles are seamless
        h, w = img.shape[:2]
        y = np.zeros((1, 3, h * SCALE, w * SCALE), np.float32)
        for t in range(0, h, tile):
            for left in range(0, w, tile):
                t0, l0 = max(0, t - pad), max(0, left - pad)
                t1, l1 = min(h, t + tile + pad), min(w, left + tile + pad)
                o = s.run(["output"], {"input": x[:, :, t0:t1, l0:l1]})[0]
                ot, ol = (t - t0) * SCALE, (left - l0) * SCALE
                hh, ww = min(tile, h - t) * SCALE, min(tile, w - left) * SCALE
                y[:, :, t * SCALE:t * SCALE + hh, left * SCALE:left * SCALE + ww] = o[:, :, ot:ot + hh, ol:ol + ww]
    return (np.clip(y[0].transpose(1, 2, 0), 0, 1) * 255 + 0.5).astype(np.uint8)
