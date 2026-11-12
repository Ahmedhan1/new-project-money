"""Shared synthetic-media helpers for the Agent D (generation / enhance / VFX) tests, plus a sanity test of them.
Everything is generated here (NumPy -> FFmpeg), no downloads."""
import subprocess
from pathlib import Path

import numpy as np


def write_video(path: Path, frames: np.ndarray, fps: int = 25, crf: int = 12) -> Path:
    n, h, w, _ = frames.shape
    p = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                        "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt",
                        "yuv420p", str(path)], input=np.ascontiguousarray(frames, dtype=np.uint8).tobytes(), capture_output=True)
    assert p.returncode == 0, p.stderr.decode()[-400:]
    return path


def texture(h: int, w: int, seed: int = 0) -> np.ndarray:
    """Natural-ish image: smooth colour gradients + blobs + fine noise (gives ECC/flow something to lock on)."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([120 + 80 * np.sin(x / 37.0 + seed), 110 + 70 * np.cos(y / 29.0), 100 + 60 * np.sin((x + y) / 53.0)], -1)
    for _ in range(25):
        cx, cy, r = rng.uniform(0, w), rng.uniform(0, h), rng.uniform(6, 30)
        col = rng.uniform(0, 255, 3)
        m = ((x - cx) ** 2 + (y - cy) ** 2) < r * r
        img[m] = 0.5 * img[m] + 0.5 * col
    img += rng.normal(0, 6, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def moving_objects(n: int = 40, h: int = 144, w: int = 192, seed: int = 1) -> tuple[np.ndarray, list[tuple]]:
    """Static textured background + a bright square moving right and a dark disc moving left (independent motion).
    Returns frames and the square's true box per frame (x, y, w, h) in pixels."""
    bg = texture(h, w, seed)
    frames, boxes = [], []
    yy, xx = np.mgrid[0:h, 0:w]
    for i in range(n):
        f = bg.copy()
        sx, sy = 10 + i * 3, 40 + int(10 * np.sin(i / 6))
        f[sy:sy + 28, sx:sx + 28] = (250, 220, 40)
        f[sy + 6:sy + 22, sx + 6:sx + 22] = (200, 40, 40)
        cx, cy = w - 20 - i * 2, 100
        f[((xx - cx) ** 2 + (yy - cy) ** 2) < 14 ** 2] = (20, 30, 90)
        frames.append(f)
        boxes.append((sx, sy, 28, 28))
    return np.stack(frames), boxes


def test_helpers_make_real_video(tmp_path):
    frames, boxes = moving_objects(10)
    p = write_video(tmp_path / "m.mp4", frames)
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                          "stream=nb_read_frames,width,height", "-of", "csv=p=0", str(p)], capture_output=True, text=True).stdout
    w, h, n = (int(x) for x in out.strip().split(","))
    assert (w, h, n) == (192, 144, 10) and boxes[-1][0] > boxes[0][0]
