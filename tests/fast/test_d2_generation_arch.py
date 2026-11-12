"""Agent D generation architecture: hardware detection, licence classes, download consent, cache/checkpoints, remote
manifests and the classification guard (real videos: a Ken Burns still must never become 'generated-video')."""
import hashlib
import io
import json

import numpy as np
import pytest
from test_d2_util import moving_objects, texture, write_video

from nexus import contracts, generate
from nexus.generate import cache, catalog, classify, downloads, hardware, metrics, remote

CPU = {"cpu_count": 4, "ram_gb": 15.6, "disk_free_gb": 40, "gpus": [], "best_vram_gb": None, "tier": "cpu"}
GPU16 = {**CPU, "gpus": [{"name": "Tesla T4", "vram_gb": 15.0, "vendor": "nvidia"}], "best_vram_gb": 15.0, "tier": "vram_12_16"}


def test_nvidia_smi_parsing_and_requirements():
    g = hardware.nvidia_gpus("NVIDIA GeForce RTX 4090, 24564, 23000\nTesla T4, 15360, 15000\n")
    assert g[0]["vram_gb"] == 24.0 and g[1]["name"] == "Tesla T4"
    assert hardware.tier_for(24.0) == "vram_24" and hardware.tier_for(None) == "cpu"
    ok, why = hardware.satisfies({"gpu": True, "vram_gb": 8.19}, CPU)
    assert not ok and any("GPU" in w for w in why)
    assert hardware.satisfies({"gpu": True, "vram_gb": 8.19}, GPU16)[0]
    assert hardware.vulkan_devices("GPU0:\n  deviceName = llvmpipe (LLVM 17)\n")[0]["cpu_device"]
    real = hardware.detect()  # this machine: no GPU is ever invented
    assert real["tier"] == ("cpu" if not real["gpus"] else real["tier"]) and real["ram_gb"]


def test_licence_classes_and_hardware_choice():
    assert catalog.licence_class(catalog.model("wan21-t2v-1.3b")) == "foss"
    assert catalog.licence_class(catalog.model("hunyuanvideo")) == "excluded"
    assert catalog.licence_class(catalog.model("flux1-dev")) == "research"
    assert catalog.licence_class(catalog.model("sd35-medium")) == "restricted"
    assert catalog.licence_class(catalog.model("depth-anything-v2-large")) == "research"
    with pytest.raises(catalog.LicenceRefused):
        catalog.allowed(catalog.model("flux1-dev"))
    r = catalog.requirements(catalog.model("wan21-t2v-1.3b"), "text_to_video")
    assert r["gpu"] and r["vram_gb"] == 8.19 and r["models"] == ["wan21-t2v-1.3b"]
    assert catalog.choose("text_to_video", CPU) is None  # no generated video on CPU, ever
    assert catalog.choose("text_to_video", GPU16)["id"] == "wan21-t2v-1.3b"
    assert catalog.choose("image", CPU)["id"] == "sd15-lcm"
    assert not catalog.requirements(catalog.model("modnet"), "segment")["gpu"]


def _fake_model(monkeypatch, payload: bytes, size_gb: float | None = None):
    sha = hashlib.sha256(payload).hexdigest()
    fake = {"id": "fake-tiny", "name": "Fake tiny", "tasks": ["segment"], "weights_licence": "Apache-2.0", "commercial_use": "yes",
            "source_url": "https://huggingface.co/x/y", "read_on": "2026-11-12", "cpu": {"feasible": "yes"},
            "fetch": [{"repo": "x/y", "revision": "abc", "files": [{"file": "m.onnx",
                                                                     "bytes": int(size_gb * 2**30) if size_gb else len(payload),
                                                                     "sha256": sha}]}]}
    real = catalog.model
    monkeypatch.setattr(catalog, "model", lambda mid: fake if mid == "fake-tiny" else real(mid))
    return fake


def test_download_needs_explicit_consent_and_verifies_sha(home, monkeypatch):
    payload = b"onnx-bytes" * 100
    _fake_model(monkeypatch, payload)
    p = downloads.plan("fake-tiny")
    assert p["total_bytes"] == len(payload) and p["within_cap"] and not p["installed"] and p["licence"]["class"] == "foss"
    urls = []

    def opener(url):
        urls.append(url)
        return io.BytesIO(payload)
    with pytest.raises(downloads.ConsentRequired):
        downloads.fetch("fake-tiny", opener=opener)  # never without consent
    with pytest.raises(downloads.ConsentRequired, match="consent covers"):
        downloads.fetch("fake-tiny", consent=downloads.consent("fake-tiny", accepted_gb=0.0), opener=opener)
    assert urls == []
    r = downloads.fetch("fake-tiny", consent=downloads.consent("fake-tiny", accepted_gb=0.001), opener=opener)
    assert urls == ["https://huggingface.co/x/y/resolve/abc/m.onnx"] and downloads.installed("fake-tiny")
    assert downloads.path("fake-tiny").read_bytes() == payload and r["bytes"] == len(payload)
    downloads.remove("fake-tiny")
    with pytest.raises(ValueError, match="sha256"):
        downloads.fetch("fake-tiny", consent=downloads.consent("fake-tiny", 0.001), opener=lambda u: io.BytesIO(b"tampered"))
    assert not downloads.installed("fake-tiny")


def test_download_cap_and_real_plans(home):
    big = downloads.plan("wan21-t2v-1.3b")
    assert big["total_gb"] > 1.5 and not big["within_cap"]  # routed to a remote worker, never fetched onto the PC
    with pytest.raises(downloads.ConsentRequired):
        downloads.fetch("wan21-t2v-1.3b", consent=downloads.consent("wan21-t2v-1.3b", 999))
    for mid in ("modnet", "mediapipe-selfie", "realesr-general-x4v3", "depth-anything-v2-small"):
        p = downloads.plan(mid)
        assert p["exact_files"] and p["within_cap"] and all(len(f["sha256"]) == 64 for f in p["files"]), mid


def test_cache_key_lookup_prune_and_checkpoint(home, tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"a" * 5000)
    k1 = cache.key("upscale", {"scale": 2}, [src])
    assert k1 == cache.key("upscale", {"scale": 2}, [src]) and k1 != cache.key("upscale", {"scale": 4}, [src])
    c = cache.MediaCache()
    assert c.get(k1, ".mp4") is None
    out = c.path_for(k1, ".mp4")
    out.write_bytes(b"v" * 2000)
    c.put(k1, out, {"op": "upscale"})
    assert c.get(k1, ".mp4") == out and c.usage()["entries"] == 1
    src.write_bytes(b"b" * 5001)  # edited input -> different key
    assert cache.key("upscale", {"scale": 2}, [src]) != k1
    assert c.prune(0)["removed"] == 1 and c.usage()["entries"] == 0
    ck = cache.Checkpoint(tmp_path / "ck.json")
    ck.mark("chunk-0", {"frames": 10})
    assert cache.Checkpoint(tmp_path / "ck.json").done("chunk-0")


def test_remote_manifest_defers_on_cpu_and_retries_with_checkpoint(home):
    m = remote.job_manifest("text_to_video", "wan21-t2v-1.3b", prompt="waves on a beach", settings={"frames": 33})
    assert not contracts.validate(m) and m["requirements"]["gpu"] and m["requirements"]["vram_gb"] == 8.19
    with pytest.raises(catalog.LicenceRefused):
        remote.job_manifest("text_to_video", "hunyuanvideo", prompt="x")
    req = remote.local_request(m, hw=CPU)
    res = remote.execute(req, hw=CPU)
    assert res["status"] == "deferred" and res["error"]["code"] == "RESOURCE_EXHAUSTED" and res["error"]["retryable"]
    failed = remote.result(req["request_id"], "failed", error={"code": "PROVIDER_LOST", "message": "kaggle", "retryable": True},
                           checkpoint={"done": {"step-10": {}}})
    out = remote.consume(failed, m)
    assert out["retry_manifest"]["checkpoint"] == {"done": {"step-10": {}}} and not contracts.validate(out["retry_manifest"])


def test_classification_guard_refuses_animated_still_as_generated_video(home, tmp_path):
    from PIL import Image
    Image.fromarray(texture(360, 480, 3)).save(tmp_path / "still.png")
    kb = generate.animate_still(tmp_path / "still.png", tmp_path / "kb.mp4", seconds=2.0, size=(320, 240))
    sm = metrics.still_motion(metrics.read_frames(kb["path"], width=192))
    assert sm["verdict"] == "global-transform-of-one-still", sm
    # a worker claims a real video model made it: the frames still show one still under a zoom -> animated-still
    lie = {"model": "wan21-t2v-1.3b", "task": "text_to_video"}
    assert classify.classify(lie, path=kb["path"])["classification"] == "animated-still"
    m = remote.job_manifest("text_to_video", "wan21-t2v-1.3b", prompt="a forest")
    res = remote.result("r1", "done", outputs=[contracts.artifact(kb["path"], role="output")], metrics={"elapsed_s": 1.0})
    got = remote.consume(res, m)["generated"][0]
    assert got["classification"] == "animated-still"
    forged = dict(got, classification="generated-video")
    with pytest.raises(classify.ClassificationError):
        classify.guard(forged)
    # provenance of the animated still itself is honoured without even looking at frames
    assert classify.classify(kb["provenance"])["classification"] == "animated-still"


def test_independent_motion_from_a_video_model_is_generated_video(home, tmp_path):
    frames, _ = moving_objects(40)
    v = write_video(tmp_path / "gen.mp4", frames)
    sm = metrics.still_motion(metrics.read_frames(v, width=192))
    assert sm["verdict"] == "independent-motion", sm
    gm = classify.manifest("text_to_video", {"model": "wan21-t2v-1.3b"}, v, compute={"gpu": "test"})
    assert gm["classification"] == "generated-video" and gm["licence"]["class"] == "foss"
    classify.guard(gm)
    with pytest.raises(classify.ClassificationError):  # same frames, no model / simulated -> never generated-video
        classify.classify({"model": "wan21-t2v-1.3b", "simulated": True}, path=v)
    with pytest.raises(classify.ClassificationError):
        classify.classify({"task": "text_to_video"}, path=v)
    t = metrics.temporal(metrics.read_frames(v))
    assert t["frames"] == 40 and 0.5 < t["ssim_f2f_mean"] <= 1.0 and t["warp_error"] < 0.1
    # the metric orders consistency correctly: frames shuffled in time are far less consistent
    rnd = np.random.default_rng(0).permutation(len(frames))
    assert metrics.temporal(frames[rnd])["warp_error"] > t["warp_error"]


def test_metrics_psnr_ssim_basics():
    a = texture(64, 64, 1)
    assert metrics.psnr(a, a) == 99.0 and metrics.ssim(a, a) == pytest.approx(1.0, abs=1e-6)
    noisy = np.clip(a + np.random.default_rng(1).normal(0, 20, a.shape), 0, 255).astype(np.uint8)
    assert metrics.psnr(a, noisy) < 30 and metrics.ssim(a, noisy) < 0.95
    assert json.dumps(catalog.table("upscale"))


def test_gpu_bench_metrics_mode_and_request_deferral_on_cpu(home, tmp_path):
    """The GPU benchmark's measurement path runs on CPU on a real clip; its generation path refuses without CUDA."""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("gpu_video_bench", generate.config.ROOT / "scripts" / "gpu_video_bench.py")
    bench = importlib.util.module_from_spec(spec)
    sys.modules["gpu_video_bench"] = bench
    spec.loader.exec_module(bench)
    frames, _ = moving_objects(24)
    v = write_video(tmp_path / "clip.mp4", frames)
    assert bench.main(["--metrics-only", str(v), "--out", str(tmp_path / "o")]) == 0
    rep = json.loads((tmp_path / "o" / "metrics_only.json").read_text())
    assert rep["frames"] == 24 and rep["still_motion"]["verdict"] == "independent-motion" and "warp_error" in rep
    assert rep["classification"].startswith("refused")  # no model named -> never generated-video
    m = remote.job_manifest("text_to_video", "wan21-t2v-1.3b", prompt="waves")
    rq = tmp_path / "req.json"
    rq.write_text(json.dumps(remote.local_request(m, hw=CPU)))
    res = bench.run_request(str(rq), str(tmp_path / "o"))
    assert res["status"] == "deferred" and res["error"]["retryable"] and not contracts.validate(res)
