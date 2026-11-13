"""VIS-1: visual understanding. Fallback without the model (always), and - when the SigLIP int8 files are present on
this machine - real embeddings of real rendered frames: zero-shot scene labels, visual duplicates, shot type, motion."""
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from nexus import config
from nexus.media import visual


def _find_model() -> Path | None:
    cands = [os.environ.get("NEXUS_VISUAL_MODEL_DIR")]
    for base in [config.ROOT, *config.ROOT.parents]:
        cands.append(str(base / ".nexus" / "models" / "visual" / "siglip-base-patch16-224"))
    for c in cands:
        if c and (Path(c) / "vision_model_quantized.onnx").exists() and (Path(c) / "text_model_quantized.onnx").exists():
            return Path(c)
    return None


MODEL = _find_model()
needs_model = pytest.mark.skipif(MODEL is None, reason="SigLIP int8 ONNX files not on this machine (opt-in download)")


@pytest.fixture()
def model(monkeypatch):
    monkeypatch.setenv("NEXUS_VISUAL_MODEL_DIR", str(MODEL))
    return MODEL


def _landscape(path):
    im = Image.new("RGB", (320, 180), (110, 170, 235))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 110, 320, 180], fill=(40, 140, 50))
    d.polygon([(40, 110), (120, 30), (200, 110)], fill=(120, 110, 100))
    d.polygon([(150, 110), (240, 45), (320, 110)], fill=(100, 95, 90))
    d.ellipse([250, 10, 290, 50], fill=(250, 230, 120))
    im.save(path)


def _textcard(path):
    im = Image.new("RGB", (320, 180), (250, 250, 250))
    d = ImageDraw.Draw(im)
    for k, line in enumerate(["CHAPTER ONE", "THE BEGINNING", "OF EVERYTHING"]):
        d.text((40, 40 + 35 * k), line, fill=(0, 0, 0), font_size=26)
    im.save(path)


def _video(tmp_path):
    """3 shots of 2 s: landscape (static), text card (static), landscape again; plus a moving testsrc shot."""
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _landscape(a)
    _textcard(b)
    out = tmp_path / "v.mp4"
    fc = ("[0:v]scale=320:180,setsar=1,fps=10,format=yuv420p[v0];[1:v]scale=320:180,setsar=1,fps=10,format=yuv420p[v1];"
          "[2:v]scale=320:180,setsar=1,fps=10,format=yuv420p[v2];[3:v]scale=320:180,setsar=1,fps=10,format=yuv420p[v3];"
          "[v0][v1][v2][v3]concat=n=4:v=1:a=0[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-t", "2", "-i", str(a), "-loop", "1", "-t", "2", "-i", str(b),
                    "-loop", "1", "-t", "2", "-i", str(a), "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=2",
                    "-filter_complex", fc, "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(out)], check=True)
    return out


SHOTS = {"scenes": [[0, 2], [2, 4], [4, 6], [6, 8]]}


def test_fallback_without_model(tmp_path, monkeypatch, home):
    monkeypatch.setenv("NEXUS_VISUAL_MODEL_DIR", str(tmp_path / "nothing"))
    assert not visual.available()
    assert not visual.ensure_model(allow_download=False)  # never downloads unless asked
    v = visual.analyze_asset(tmp_path / "missing.mp4", SHOTS)
    assert v["available"] is False and "lexical" in v["method"] and v["download_mb"] > 100
    assert visual.asset_embedding(tmp_path / "missing.mp4") is None
    an = visual.enrich({"path": str(tmp_path / "missing.mp4"), "shots": [{"id": 0}]})
    assert an["visual"]["available"] is False and "shot_type" not in an["shots"][0]


def test_rules_shot_type_and_motion():
    assert visual.shot_type_from_face(0.45) == "close-up"
    assert visual.shot_type_from_face(0.2) == "medium"
    assert visual.shot_type_from_face(0.05) == "wide"
    assert visual.shot_type_from_face(None) is None
    assert [visual.motion_level(x) for x in (0.0, 0.02, 0.06, 0.2)] == ["static", "low", "medium", "high"]
    assert "Apache-2.0" in visual.LICENCE and visual.info()["download_mb"] < 300


@needs_model
def test_real_frames_labels_duplicates_motion(tmp_path, model, home):
    v = _video(tmp_path)
    an = {"path": str(v), "scenes": SHOTS["scenes"], "shots": [{"id": k, "start": a, "end": b, "tags": []}
                                                                for k, (a, b) in enumerate(SHOTS["scenes"])]}
    vis = visual.analyze_asset(v, an, fps=2.0)
    assert vis["available"] and vis["frames"] >= 14
    rows = {r["id"]: r for r in vis["shots"]}
    # the landscape shot is recognised as a landscape-like scene, the card as text
    land = [x["label"] for x in rows[0]["scene"]]
    assert {"a landscape", "mountains"} & set(land), land
    assert rows[1]["scene"][0]["label"] in ("text or a title card",), rows[1]["scene"]
    # shot 2 repeats shot 0 -> visual duplicate; the text card and testsrc are not duplicates of it
    assert rows[2].get("dup_of") == 0 and "dup_of" not in rows[1] and "dup_of" not in rows[3]
    assert rows[0]["motion_level"] == "static" and rows[3]["motion_level"] != "static" and rows[3]["motion"] > 5 * rows[0]["motion"]
    # cache: second call is a hit and gives the same answer
    again = visual.analyze_asset(v, an, fps=2.0)
    assert again["cache"]["hit"] and again["shots"] == vis["shots"]
    E = visual.shot_embeddings(vis)
    assert visual.similarity(E[0], E[2]) > visual.similarity(E[0], E[1])
    visual.attach(an, vis)
    assert an["shots"][1]["scene_label"] == "text or a title card" and an["shots"][2]["visual_dup_of"] == 0


@needs_model
def test_text_image_retrieval_on_rendered_stills(tmp_path, model, home):
    a, b = tmp_path / "land.png", tmp_path / "card.png"
    _landscape(a)
    _textcard(b)
    ea, eb = visual.asset_embedding(a), visual.asset_embedding(b)
    s = visual.text_image_scores(["mountains under a blue sky", "a page of printed text"], ea)
    t = visual.text_image_scores(["mountains under a blue sky", "a page of printed text"], eb)
    assert s[0] > s[1] and t[1] > t[0]
    assert np.isclose(np.linalg.norm(ea), 1.0, atol=1e-3)
