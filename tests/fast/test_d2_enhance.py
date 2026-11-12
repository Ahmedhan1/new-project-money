"""Agent D enhancement processors on real (synthetic) videos: geometry, frame counts, PSNR vs naive upscale, denoise
gain, film look, HDR->SDR tone mapping, timeline apply(), cache, and the hand-written ONNX writer vs a NumPy reference."""
import subprocess

import numpy as np
import pytest
from test_d2_util import moving_objects, texture, write_video

from nexus import contracts, enhance
from nexus.enhance import onnx_build as ob
from nexus.generate import metrics
from nexus.media import ff


def _frames(path, **kw):
    return metrics.read_frames(path, **kw)


@pytest.fixture()
def clip(tmp_path):
    frames, _ = moving_objects(30, 144, 192)
    return write_video(tmp_path / "src.mp4", frames, fps=25)


def test_upscale_lanczos_beats_naive_on_a_downscale_roundtrip(home, tmp_path):
    hi = np.stack([texture(144, 192, s) for s in range(6)])
    ref = write_video(tmp_path / "ref.mp4", hi, crf=4)
    small = tmp_path / "small.mp4"
    ff.run(["-i", ref, "-vf", "scale=96:72:flags=area", "-c:v", "libx264", "-crf", "4", small])
    naive = tmp_path / "naive.mp4"
    ff.run(["-i", small, "-vf", "scale=192:144:flags=neighbor", "-c:v", "libx264", "-crf", "4", naive])
    r = enhance.upscale(small, scale=2, method="lanczos", denoise=0, sharpen=0.3)
    up = _frames(r["path"])
    assert up.shape == (6, 144, 192, 3)
    a, n, g = _frames(ref), _frames(naive), up
    p_up = np.mean([metrics.psnr(a[i], g[i]) for i in range(6)])
    p_nv = np.mean([metrics.psnr(a[i], n[i]) for i in range(6)])
    assert p_up > p_nv + 0.5, (p_up, p_nv)
    assert r["manifest"]["classification"] == "edited-source" and not contracts.validate(r["manifest"])
    again = enhance.upscale(small, scale=2, method="lanczos", denoise=0, sharpen=0.3)
    assert again["cached"] and again["path"] == r["path"]


def test_interpolate_doubles_frames_and_denoise_improves_psnr(home, tmp_path, clip):
    r = enhance.interpolate(clip, fps=50, method="mci")
    info = ff.probe(r["path"])["video"]
    assert info["fps"] == 50 and 56 <= len(_frames(r["path"])) <= 60  # 30 frames -> ~2x (the last frame has no successor)
    clean = np.stack([texture(144, 192, 7)] * 12)
    noisy = np.clip(clean + np.random.default_rng(3).normal(0, 18, clean.shape), 0, 255).astype(np.uint8)
    nv = write_video(tmp_path / "noisy.mp4", noisy, crf=2)
    for method in ("hqdn3d", "nlmeans"):
        d = enhance.denoise(nv, method=method, strength=0.6)
        out = _frames(d["path"])
        before = np.mean([metrics.psnr(clean[i], _frames(nv)[i]) for i in range(12)])
        after = np.mean([metrics.psnr(clean[i], out[i]) for i in range(12)])
        assert after > before + 1.0, (method, before, after)


def test_deblock_sharpen_film_look_keep_geometry_and_change_pixels(home, clip):
    src = _frames(clip)
    for r in (enhance.deblock(clip, strength=0.8), enhance.sharpen(clip, amount=0.7), enhance.sharpen(clip, method="cas"),
              enhance.film_look(clip, grain=0.6, halation=0.8, weave=0.8)):
        out = _frames(r["path"])
        assert out.shape == src.shape, r["params"]
        assert np.abs(out.astype(int) - src).mean() > 0.2, r["params"]
    fl = _frames(enhance.film_look(clip, grain=0.0, halation=1.0, weave=0.0)["path"])
    hot = src.mean(axis=-1) > 200  # halation adds a warm glow around highlights: red rises more than blue there
    gain = (fl.astype(float) - src)[np.roll(hot, 6, axis=2)]
    assert gain[:, 0].mean() > gain[:, 2].mean()


def test_tonemap_only_acts_on_hdr(home, tmp_path, clip):
    r = enhance.tonemap(clip)
    assert r["path"] == str(clip) and "SDR" in r["note"]
    hdr = tmp_path / "pq.mkv"
    ff.run(["-i", clip, "-vf", "zscale=tin=bt709:pin=bt709:min=bt709:t=smpte2084:p=bt2020:m=bt2020nc:npl=1000,format=yuv420p10le", "-c:v", "libx265",
            "-x265-params", "log-level=error", "-color_trc", "smpte2084", "-color_primaries", "bt2020", "-colorspace",
            "bt2020nc", "-an", hdr])
    assert enhance.is_hdr(hdr)["hdr"]
    t = enhance.tonemap(hdr)
    raw = ff.probe_raw(t["path"])["streams"][0]
    assert raw.get("color_transfer") == "bt709" and _frames(t["path"]).shape[0] == 30
    assert 20 < _frames(t["path"]).mean() < 235


def test_apply_rebases_clip_and_keeps_undo_history(home, clip):
    c = {"id": "c1", "type": "video", "src": str(clip), "in": 0.4, "out": 1.0, "grade": "warm"}
    n = enhance.apply(c, "sharpen", amount=0.5)
    assert n["src"] != c["src"] and n["in"] == 0.0 and n["out"] == 0.6 and n["grade"] == "warm"
    assert n["enhanced_from"][0] == {"src": str(clip), "in": 0.4, "out": 1.0, "op": "sharpen", "params": {"amount": 0.5}}
    assert abs(ff.probe(n["src"])["duration"] - 0.6) < 0.1
    em = enhance.effect_manifest()
    assert not contracts.validate(em) and {e["type"] for e in em["effects"]} >= {"enhance.upscale", "enhance.film_look"}


def test_onnx_writer_matches_numpy_reference_srvgg():
    """Tiny SRVGG-shaped net (2 convs + PReLU, x4 pixel shuffle + nearest residual) built by onnx_build and run with
    onnxruntime equals a direct NumPy implementation."""
    import onnxruntime as ort

    from nexus.enhance import esrgan
    rng = np.random.default_rng(0)
    w = {"body.0.weight": rng.normal(0, 0.2, (8, 3, 3, 3)).astype(np.float32), "body.0.bias": rng.normal(0, .1, 8).astype(np.float32),
         "body.1.weight": np.full(8, 0.25, np.float32),
         "body.2.weight": rng.normal(0, 0.2, (48, 8, 3, 3)).astype(np.float32), "body.2.bias": rng.normal(0, .1, 48).astype(np.float32)}
    sess = ort.InferenceSession(esrgan.build_onnx(w), providers=["CPUExecutionProvider"])
    x = rng.random((1, 3, 5, 6)).astype(np.float32)
    y = sess.run(["output"], {"input": x})[0]

    def conv(a, k, b):
        p = np.pad(a, ((0, 0), (1, 1), (1, 1)))
        out = np.zeros((k.shape[0],) + a.shape[1:], np.float64)
        for i in range(3):
            for j in range(3):
                out += np.einsum("oc,chw->ohw", k[:, :, i, j], p[:, i:i + a.shape[1], j:j + a.shape[2]])
        return out + b[:, None, None]
    h = conv(x[0], w["body.0.weight"], w["body.0.bias"])
    h = np.where(h > 0, h, 0.25 * h)
    h = conv(h, w["body.2.weight"], w["body.2.bias"])
    C, H, W = 3, 5, 6  # PixelShuffle (CRD): out[c, h*4+i, w*4+j] = in[c*16 + i*4 + j, h, w]
    ps = h.reshape(C, 4, 4, H, W).transpose(0, 3, 1, 4, 2).reshape(C, H * 4, W * 4)
    ref = ps + np.repeat(np.repeat(x[0], 4, 1), 4, 2)
    assert y.shape == (1, 3, 20, 24) and np.abs(y[0] - ref).max() < 1e-4
    assert ob.read_safetensors  # API present


def test_safetensors_reader_roundtrip(tmp_path):
    import json
    import struct
    a = np.arange(6, dtype=np.float32).reshape(2, 3)
    b = np.array([1.5, -2], np.float16)
    hdr = {"a": {"dtype": "F32", "shape": [2, 3], "data_offsets": [0, 24]}, "b": {"dtype": "F16", "shape": [2], "data_offsets": [24, 28]}}
    hj = json.dumps(hdr).encode()
    (tmp_path / "t.safetensors").write_bytes(struct.pack("<Q", len(hj)) + hj + a.tobytes() + b.tobytes())
    r = ob.read_safetensors(tmp_path / "t.safetensors")
    assert np.array_equal(r["a"], a) and np.allclose(r["b"], [1.5, -2])


def test_esrgan_refuses_without_downloaded_weights(home, clip):
    from nexus.generate import downloads
    with pytest.raises(downloads.ConsentRequired):
        enhance.upscale(clip, scale=2, method="esrgan")
    assert subprocess.run(["ffprobe", "-v", "error", str(clip)]).returncode == 0


def test_framepipe_resumes_chunks_but_never_reuses_stale_ones(home, tmp_path):
    """Regression: a re-run over a changed input (or with other params) must not pick up chunks of the old job."""
    from nexus.enhance import framepipe
    a = write_video(tmp_path / "a.mp4", np.stack([texture(64, 96, 1)] * 20), fps=10)
    calls = []

    def inv(f, i):
        calls.append(i)
        return 255 - f
    r1 = framepipe.process_video(a, tmp_path / "o.mp4", inv, chunk_s=1.0, job_key="invert")
    assert r1["frames"] == 20 and r1["chunks"] == 2 and len(calls) == 20
    calls.clear()
    framepipe.process_video(a, tmp_path / "o.mp4", inv, chunk_s=1.0, job_key="invert")
    assert calls == []  # finished chunks are reused (resume)
    b = write_video(tmp_path / "a.mp4", np.stack([texture(64, 96, 9)] * 20), fps=10)  # same path, new content
    framepipe.process_video(b, tmp_path / "o.mp4", inv, chunk_s=1.0, job_key="invert")
    assert len(calls) == 20
    got = metrics.read_frames(tmp_path / "o.mp4")[5].astype(int)
    assert np.abs(got - (255 - texture(64, 96, 9).astype(int))).mean() < 6
