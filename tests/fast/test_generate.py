"""GEN-0: generation registry, honest capability detection, licence gate, provenance labels and the claim guard.
No models, no network: a fake 'generation environment' stands in for torch/diffusers."""
import json
import subprocess

import pytest

from nexus import generate
from nexus.timeline import ops

STATES = {"yes", "no", "restricted", "unverified"}


def test_registry_validates_every_model_has_licence_evidence():
    reg = generate.registry()
    assert reg["read_on"] and reg["models"]
    for m in reg["models"]:
        assert m["commercial_use"] in STATES, m["id"]
        assert m.get("weights_licence") and str(m.get("source_url", "")).startswith("https://"), m["id"]
        assert m.get("read_on"), m["id"]
    tiers = reg["tiers"]
    assert tiers["cpu"]["video"] is None  # no generated video is promised on CPU
    for c in reg["compute"]:
        if c.get("card_required") is True or c.get("billing_activation_required") is True:
            assert str(c.get("allowed", c.get("verdict", ""))).lower() in ("false", "no", "excluded", "blocked") or \
                "exclud" in json.dumps(c).lower(), c["id"]  # anything needing a card is excluded by policy


def test_without_a_generation_environment_nothing_claims_to_generate(home, monkeypatch):
    monkeypatch.setenv("NEXUS_GEN_PYTHONPATH", "/nonexistent")
    monkeypatch.setattr(generate, "_probe_env", lambda python, pp: {"torch": False, "diffusers": False})
    cap = generate.capabilities()
    assert not cap["image"]["available"] and "torch" in cap["image"]["why"] and cap["image"]["enable"]
    assert not cap["text_to_video"]["available"] and "animated_still" in cap["text_to_video"]["fallback"]
    with pytest.raises(generate.Unavailable) as e:
        generate.image("a lighthouse", home / "x.png")
    assert e.value.enable
    with pytest.raises(generate.Unavailable):
        generate.video("a lighthouse at dusk")


def test_noncommercial_weights_are_refused_by_default(home):
    nc = next(m for m in generate.registry()["models"] if m["commercial_use"] == "no")
    with pytest.raises(generate.Unavailable, match="commercial"):
        generate.image("x", home / "x.png", model_id=nc["id"])


def test_fake_provider_full_success_path_records_provenance(home, monkeypatch):
    monkeypatch.setattr(generate, "_probe_env", lambda python, pp: {"torch": "2.x+cpu", "diffusers": True, "cuda": False})
    monkeypatch.setattr(generate, "_weights_present", lambda m, h: True)

    def fake_generate(self, prompts, workdir, allow_download=False, model_id=None):
        from PIL import Image
        for p in prompts:
            Image.new("RGB", (p["w"], p["h"]), (90, 120, 160)).save(p["out"])
        return {"ok": True, "load_seconds": 1.0, "images": [{"out": p["out"], "seconds": 0.5} for p in prompts], "peak_rss_gb": 1.0}
    monkeypatch.setattr(generate.LocalDiffusers, "generate", fake_generate)
    assert generate.capabilities()["image"]["available"]
    r = generate.image("a lighthouse on a rocky coast", home / "g.png", size=(520, 300), seed=7)
    pv = r["provenance"]
    assert pv["kind"] == "generated" and pv["model"] == "sd15-lcm" and pv["seed"] == 7 and pv["size"] == [520, 296]
    assert pv["licence"] and pv["commercial_use"] == "yes"
    anim = generate.animate_still(r["path"], home / "a.mp4", seconds=1.0, size=(320, 180), source_provenance=pv)
    assert anim["provenance"]["kind"] == "animated_still" and anim["provenance"]["still"]["kind"] == "generated"
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", anim["path"]],
                         capture_output=True, text=True).stdout
    assert abs(float(out) - 1.0) < 0.1


def test_label_shares_and_claim_guard():
    still = {"kind": "animated_still", "still": {"kind": "generated", "model": "sd15-lcm"}}
    tl = {"clips": [ops.image_clip("/a.png", 6.0, provenance=still), ops.video_clip("/f.mp4", 0, 3.0),
                    ops.color_clip(1.0)]}
    lab = generate.label(tl)
    assert lab["share"]["animated_still"] == 0.6 and lab["share"]["footage"] == 0.3 and lab["share"]["generated"] == 0
    assert lab["generated_stills"] == 1 and "animated stills" in lab["describe_as"]
    with pytest.raises(ValueError, match="no generated video"):
        generate.guard_claim("Watch our AI-generated video about the Moon", tl)
    generate.guard_claim("Animated illustrations (AI-generated stills) about the Moon", tl)


def test_remote_folder_round_trip_and_simulation_can_never_be_generated(home, tmp_path):
    folder = tmp_path / "gpu"
    (folder / "inbox").mkdir(parents=True)
    job = generate.RemoteFolder.submit(folder, "text_to_video", {"prompt": "waves", "seconds": 4})
    assert json.loads(job.read_text())["task"] == "text_to_video"
    assert generate.RemoteFolder.collect(folder, job) is None  # nothing back yet
    (folder / "inbox" / (job.stem + ".result.json")).write_text(json.dumps({"path": "x.mp4", "provenance": {"simulated": True}}))
    with pytest.raises(ValueError, match="cannot be labelled generated"):
        generate.RemoteFolder.collect(folder, job)
    (folder / "inbox" / (job.stem + ".result.json")).write_text(json.dumps({"path": "x.mp4", "provenance": {"model": "wan21-t2v-1.3b"}}))
    assert generate.RemoteFolder.collect(folder, job)["provenance"]["model"] == "wan21-t2v-1.3b"
