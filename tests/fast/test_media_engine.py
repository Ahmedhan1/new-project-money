"""MED-1: encoder detection, size-targeted export, export verification, proxies and Windows-safe filter paths.
lavfi media only; no models, no network."""
import os
import subprocess

import pytest

from nexus import export
from nexus.media import encoders, ff, proxy


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    d = tmp_path_factory.mktemp("me")
    f = d / "src.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4", "-f", "lavfi",
                    "-i", "sine=f=330:d=4:r=48000", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-c:a", "aac",
                    "-shortest", str(f)], check=True)
    return f


def test_detect_lists_only_encoders_that_really_produced_a_file(tmp_path):
    rep = encoders.detect_report(refresh=True, cache_file=tmp_path / "enc.json")
    found = encoders.detect(cache_file=tmp_path / "enc.json")
    assert "libx264" in found["h264"]
    res = {r["encoder"]: r for r in rep["results"]}
    listed = set(sum((found[c] for c in ("h264", "hevc", "av1")), []))
    assert listed and all(res[e]["ok"] is True for e in listed)
    assert all(not r["ok"] and r.get("why") for e, r in res.items() if e not in listed)  # a failed probe says why


@pytest.mark.parametrize("quality", ["draft", "balanced"])
def test_video_args_encode_for_every_detected_software_codec(src, tmp_path, quality):
    found = encoders.detect()
    for codec in ("h264", "hevc", "av1"):
        sw = [e for e in found[codec] if e in encoders.SOFTWARE]
        if not sw:
            continue
        args = encoders.encoder_args(sw[0], quality)
        out = tmp_path / f"{codec}_{quality}.mp4"
        ff.run(["-i", src, "-t", "1", *args, "-an", out], op="test-encode")
        assert ff.probe(out)["video"] is not None


def test_export_meets_a_size_limit_and_refuses_an_impossible_one(src, tmp_path):
    limit = 250_000
    r = export.export(src, tmp_path / "sized.mp4", codec="h264", quality="balanced", max_bytes=limit)
    assert r["ok"] and os.path.getsize(tmp_path / "sized.mp4") <= limit
    bad = export.export(src, tmp_path / "tiny.mp4", codec="h264", max_bytes=5_000)
    assert not bad["ok"] and "floor" in str(bad.get("reason", "")).lower()
    assert not (tmp_path / "tiny.mp4").exists()


def test_verify_passes_good_and_fails_truncated_or_missing_audio(src, tmp_path):
    good = export.verify(src, {"codec": "h264", "audio": True})
    assert good["ok"], [c for c in good["checks"] if not c["ok"]]
    b = src.read_bytes()
    (tmp_path / "trunc.mp4").write_bytes(b[: len(b) // 2])
    assert not export.verify(tmp_path / "trunc.mp4")["ok"]
    ff.run(["-i", src, "-an", "-c:v", "copy", tmp_path / "silent.mp4"], op="test-strip-audio")
    silent = export.verify(tmp_path / "silent.mp4", {"audio": True})
    assert not silent["ok"]


def test_proxy_is_cached_and_never_touches_the_source(src, tmp_path):
    before = (src.stat().st_size, src.stat().st_mtime, src.read_bytes()[:4096])
    p1 = proxy.ensure(src, 180, cache_dir=tmp_path / "px")
    mtime = p1.stat().st_mtime
    p2 = proxy.ensure(src, 180, cache_dir=tmp_path / "px")
    assert p1 == p2 and p2.stat().st_mtime == mtime                      # second call encodes nothing
    assert ff.probe(p1)["video"]["height"] == 180
    assert (src.stat().st_size, src.stat().st_mtime, src.read_bytes()[:4096]) == before
    assert proxy.original_for(p1)["source"] == str(src)


def test_interrupted_proxy_leaves_no_file_under_the_final_name(src, tmp_path, monkeypatch):
    real = ff.run

    def boom(args, op="ffmpeg", timeout=3600, **kw):
        raise ff.FFError("simulated crash mid-encode")
    monkeypatch.setattr(ff, "run", boom)
    with pytest.raises((ff.FFError, RuntimeError)):
        proxy.build(src, 120, cache_dir=tmp_path / "px2")
    monkeypatch.setattr(ff, "run", real)
    assert not proxy.proxy_path(src, 120, cache_dir=tmp_path / "px2").exists()


def test_inspect_flags_rotation(tmp_path, src):
    rot = tmp_path / "rot.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(src), "-c", "copy", str(rot)], check=True)
    meta = proxy.inspect(rot)
    assert meta["video"]["rotation"] in (90, -90, 270) and any("rotation" in w.lower() for w in meta["warnings"])
    assert (meta["video"]["display_width"], meta["video"]["display_height"]) == (360, 640)


def test_windows_paths_are_escaped_for_filter_arguments():
    from nexus.fx import post
    esc = post.esc_path(r"C:\Users\me\My Videos\it's.ass")
    assert esc == "C\\:/Users/me/My Videos/it\\'s.ass"
