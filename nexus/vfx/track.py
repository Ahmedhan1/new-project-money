"""Subject/mask tracking on CPU (OpenCV, Apache-2.0): pyramidal Lucas-Kanade optical flow on corner features inside the
box (median shift + scale from point spread), with normalised-cross-correlation template re-detection when too few points
survive. CSRT/KCF live in opencv-contrib, which is not installed; this tracker needs only opencv core.

    track_box(src, box=(x, y, w, h) in 0..1, start=0, duration=None, fps=None) -> {"samples": [{"t","x","y","w","h","ok"}], ...}
    mask_keyframes(track, t0=0) -> {"x": [[t, v, "linear"]], "y": .., "w": .., "h": ..}   (docs/TIMELINE.md keyframe triples)
    follow_keyframes(track, out_w, out_h, src_w, src_h) -> {"x": [...], "y": [...]}      (renderer `keyframes` crop follow)
    render_tracked_mask(src, out, track, effect="blur"|"pixelate"|"black"|"dim", region="inside")   media-to-media fallback
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..enhance import framepipe
from ..generate import metrics
from ..media import ff


def _clip_box(b, w, h):
    x, y, bw, bh = b
    bw, bh = max(4.0, min(bw, w)), max(4.0, min(bh, h))
    return [min(max(0.0, x), w - bw), min(max(0.0, y), h - bh), bw, bh]


def track_box(src: str | Path, box: tuple[float, float, float, float], *, start: float = 0.0, duration: float | None = None,
              fps: float | None = None, width: int = 640) -> dict:
    import cv2
    v = ff.probe(src)["video"]
    rate = fps or v["fps"] or 25.0
    n = int(round(duration * rate)) if duration is not None else None
    frames = metrics.read_frames(src, n=n, start=start, fps=fps, width=min(width, v["width"]) // 2 * 2)
    gray = [cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in frames]
    h, w = gray[0].shape
    b = _clip_box([box[0] * w, box[1] * h, box[2] * w, box[3] * h], w, h)
    tmpl = gray[0][int(b[1]):int(b[1] + b[3]), int(b[0]):int(b[0] + b[2])].copy()
    lk = {"winSize": (21, 21), "maxLevel": 3, "criteria": (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01)}

    def feats(g, bb):
        m = np.zeros_like(g)
        m[int(bb[1]):int(bb[1] + bb[3]), int(bb[0]):int(bb[0] + bb[2])] = 255
        return cv2.goodFeaturesToTrack(g, maxCorners=80, qualityLevel=0.01, minDistance=3, mask=m, blockSize=5)
    pts = feats(gray[0], b)
    samples = [{"t": 0.0, "x": b[0] / w, "y": b[1] / h, "w": b[2] / w, "h": b[3] / h, "ok": True, "method": "init"}]
    redetects = 0
    for i in range(1, len(gray)):
        ok, method = False, "flow"
        if pts is not None and len(pts) >= 6:
            nxt, st, _ = cv2.calcOpticalFlowPyrLK(gray[i - 1], gray[i], pts, None, **lk)
            back, st2, _ = cv2.calcOpticalFlowPyrLK(gray[i], gray[i - 1], nxt, None, **lk)
            good = (st.ravel() == 1) & (st2.ravel() == 1) & (np.linalg.norm((back - pts).reshape(-1, 2), axis=1) < 1.0)
            if good.sum() >= 6:
                p0, p1 = pts.reshape(-1, 2)[good], nxt.reshape(-1, 2)[good]
                d = np.median(p1 - p0, axis=0)
                # scale = median ratio of pairwise point distances (robust to a few bad points), damped by half and
                # limited to +-2 % per frame so feature churn cannot inflate the box
                ii, jj = np.triu_indices(len(p0), 1)
                d0 = np.linalg.norm(p0[ii] - p0[jj], axis=1)
                d1 = np.linalg.norm(p1[ii] - p1[jj], axis=1)
                ok_d = d0 > 4
                s = float(np.median(d1[ok_d] / d0[ok_d])) if ok_d.sum() >= 5 else 1.0
                s = float(np.clip(1 + 0.5 * (s - 1), 0.98, 1.02))
                cx, cy = b[0] + b[2] / 2 + d[0], b[1] + b[3] / 2 + d[1]
                b = _clip_box([cx - b[2] * s / 2, cy - b[3] * s / 2, b[2] * s, b[3] * s], w, h)
                pts = p1.reshape(-1, 1, 2)
                ok = True
        if not ok:  # re-detect with the first-frame template near the last position
            method = "template"
            redetects += 1
            res = cv2.matchTemplate(gray[i], tmpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx > 0.5:
                b = _clip_box([loc[0], loc[1], tmpl.shape[1], tmpl.shape[0]], w, h)
                ok = True
        if i % 10 == 0 or pts is None or len(pts) < 20:  # refresh features inside the current box
            pts = feats(gray[i], b)
        samples.append({"t": round(i / rate, 4), "x": b[0] / w, "y": b[1] / h, "w": b[2] / w, "h": b[3] / h, "ok": ok,
                        "method": method})
    for s in samples:
        for k in ("x", "y", "w", "h"):
            s[k] = round(float(s[k]), 5)
    return {"src": str(src), "start": start, "fps": rate, "frames": len(gray), "samples": samples, "redetections": redetects,
            "lost": sum(1 for s in samples if not s["ok"])}


def _thin(samples, key_fn, step: float, tol: float) -> list:
    out, last = [], None
    for i, s in enumerate(samples):
        v = key_fn(s)
        final = i == len(samples) - 1
        if out and not final and (s["t"] - out[-1][0] < step) and last is not None and abs(v - last) < tol:
            continue
        out.append([round(s["t"], 3), round(float(v), 4), "linear"])
        last = v
    return out


def mask_keyframes(track: dict, step: float = 0.2, tol: float = 0.004) -> dict:
    """Keyframes for a moving `mask` (x, y, w, h fractions; clip-local seconds from the track's start)."""
    s = track["samples"]
    return {k: _thin(s, lambda q, k=k: q[k], step, tol) for k in ("x", "y", "w", "h")}


def follow_keyframes(track: dict, out_w: int, out_h: int, src_w: int, src_h: int, step: float = 0.3) -> dict:
    """x/y crop keyframes (renderer `keyframes`, fit cover) that keep the tracked box centred - same convention as
    nexus.media.faces.follow_keyframes (0..1 position of the crop window)."""
    from ..media.faces import crop_fraction
    sc = max(out_w / src_w, out_h / src_h)
    iw, ih = src_w * sc, src_h * sc
    s = track["samples"]
    return {"x": [[a, round(crop_fraction(v, iw, out_w), 4), "ease"] for a, v, _ in _thin(s, lambda q: q["x"] + q["w"] / 2, step, 0.01)],
            "y": [[a, round(crop_fraction(v, ih, out_h), 4), "ease"] for a, v, _ in _thin(s, lambda q: q["y"] + q["h"] / 2, step, 0.01)]}


def render_tracked_mask(src: str | Path, out: str | Path, track: dict, *, effect: str = "blur", region: str = "inside",
                        amount: float = 0.6, feather: float = 6.0) -> dict:
    """Burn a moving mask effect into a new file (works today, without a renderer change)."""
    import cv2
    samples = track["samples"]

    def fn(frame, i):
        s = samples[min(i, len(samples) - 1)]
        h, w = frame.shape[:2]
        m = np.zeros((h, w), np.float32)
        x0, y0 = int(s["x"] * w), int(s["y"] * h)
        m[y0:y0 + int(s["h"] * h), x0:x0 + int(s["w"] * w)] = 1.0
        if region == "outside":
            m = 1 - m
        if feather:
            m = cv2.GaussianBlur(m, (0, 0), feather)
        if effect == "blur":
            fx = cv2.GaussianBlur(frame, (0, 0), 6 + 20 * amount)
        elif effect == "pixelate":
            k = max(2, int(8 + 24 * amount))
            fx = cv2.resize(cv2.resize(frame, (max(1, w // k), max(1, h // k)), interpolation=cv2.INTER_AREA), (w, h),
                            interpolation=cv2.INTER_NEAREST)
        elif effect == "dim":
            fx = (frame * (1 - 0.8 * amount)).astype(np.uint8)
        else:
            fx = np.zeros_like(frame)
        m3 = m[..., None]
        return (frame * (1 - m3) + fx * m3 + 0.5).astype(np.uint8)
    import hashlib
    import json
    jk = hashlib.sha256(json.dumps([samples, effect, region, amount, feather]).encode()).hexdigest()[:16]
    return framepipe.process_video(src, out, fn, start=float(track.get("start", 0)),
                                   duration=len(samples) / float(track["fps"]), job_key=jk)
