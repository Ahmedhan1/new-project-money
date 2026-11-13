"""K-18: semantic B-roll. Ranking by image-text similarity with injected embeddings (deterministic), duplicate / self /
variety penalties, local-library candidates inserted as real cutaways, the lexical fallback flag, and - with the SigLIP
files present - a real run on rendered frames."""
import subprocess

import numpy as np

from nexus import broll
from nexus.timeline import ops, render

from test_b2_visual import MODEL, _landscape, _textcard, needs_model

W, H = 320, 180
AXES = ["rocket", "ocean", "forest", "city", "face", "text"]


def vec(*words, noise=0.0):
    v = np.array([1.0 if a in words else 0.0 for a in AXES]) + noise
    return v / (np.linalg.norm(v) + 1e-9)


def embed_text(t):
    return vec(*[a for a in AXES if a in t.lower()], noise=0.01)


def _tl(src, texts):
    clips, subs, t = [], [], 0.0
    for k, txt in enumerate(texts):
        clips.append(ops.video_clip(src, k * 6.0, k * 6.0 + 6.0))
        subs.append({"start": t + 0.2, "end": t + 5.8, "text": txt})
        t += 6.0
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none", "target_lufs": -16,
            "clips": clips, "audio": [], "music": None, "subtitles": subs, "burn_subtitles": False, "markers": []}


TEXTS = ["Welcome to the show about our planet.",
         "The rocket engines lit up and the rocket climbed over the launch tower.",
         "Waves of the ocean carried warm water along the coast for thousands of kilometres.",
         "Another rocket engine test happened later that summer near the coast.",
         "Forest canopies store carbon across the northern regions every single year."]


def _c(i, title, e, kind="image", **kw):
    return {"id": f"nasa:{i}", "source": "NASA Image and Video Library", "kind": kind, "title": title, "description": "",
            "keywords": [], "file_url": f"u{i}", "page": f"p{i}", "license": "public domain", "credit": f"NASA {i}",
            "rights_note": None, "embedding": e, **kw}


def search_factory(cands):
    def search(src, q):
        return cands if src == "nasa" else []
    return search


def test_visual_ranking_beats_words_and_records_reasons():
    # 'Rocket launch tower budget' shares words with the narration but shows a forest; 'frame 7' shows a rocket.
    cands = [_c("words", "Rocket launch tower budget meeting", vec("forest")), _c("pic", "Planet archive frame 7", vec("rocket")),
             _c("sea", "Planet coastline", vec("ocean")), _c("trees", "Boreal forest", vec("forest"))]
    tl = _tl("/x.mp4", TEXTS)
    p = broll.plan(tl, search=search_factory(cands), max_share=1.0, embed=lambda c: c["embedding"], embed_text=embed_text,
                   context="rocket ocean forest planet")
    by_clip = {i["clip"]: i for i in p["inserts"]}
    rocket = by_clip[tl["clips"][1]["id"]]
    assert p["visual"] and rocket["asset"]["id"] == "nasa:pic" and rocket["visual"] > 0.9
    assert any("visual match" in r for r in rocket["reasons"]) and rocket["provenance"]["license"] == "public domain"
    assert by_clip[tl["clips"][2]["id"]]["asset"]["id"] == "nasa:sea"
    assert by_clip[tl["clips"][4]["id"]]["asset"]["id"] in ("nasa:trees", "nasa:words")
    ids = [i["asset"]["id"] for i in p["inserts"]]
    assert len(ids) == len(set(ids))


def test_repeated_imagery_rejected_and_self_similar_rejected():
    cands = [_c("r1", "Rocket A", vec("rocket")), _c("r2", "Rocket B (same picture)", vec("rocket", noise=0.001)),
             _c("talk", "Rocket talk studio", vec("face", "rocket"))]
    tl = _tl("/x.mp4", TEXTS)
    p = broll.plan(tl, search=search_factory(cands), max_share=1.0, embed=lambda c: c["embedding"], embed_text=embed_text,
                   speaker_embedding=vec("face", "rocket"), context="rocket ocean forest planet")
    used = [i["asset"]["id"] for i in p["inserts"]]
    assert used.count("nasa:r1") + used.count("nasa:r2") == 1  # the second rocket clip may not show the same imagery
    assert "nasa:talk" not in used
    rej = [r for d in p["decisions"] for r in d.get("rejected", [])]
    assert any("repeats imagery" in r for r in rej) and any("speaker's own shot" in r for r in rej)


def test_rights_gate_still_applies_in_visual_mode():
    cands = [_c("bad", "Rocket", vec("rocket"), rights_note="description mentions third-party rights"),
             _c("nolic", "Rocket 2", vec("rocket"), license="")]
    p = broll.plan(_tl("/x.mp4", TEXTS), search=search_factory(cands), max_share=1.0, embed=lambda c: c["embedding"],
                   embed_text=embed_text, context="rocket")
    assert not p["inserts"]


def test_variety_penalty_prefers_a_different_shot_type():
    loc = [{"id": f"local:l{k}", "source": "lib", "kind": "local_video", "src": "/lib.mp4", "in": 10.0 * k, "out": 10.0 * k + 6,
            "title": "shot", "license": "public domain", "credit": "lib", "rights_note": None, "page": "/lib.mp4",
            "embedding": vec("rocket", noise=0.05 * k), "shot_type": st, "motion_level": "low"}
           for k, st in enumerate(["wide", "wide", "close-up"])]
    # l1 (wide) matches the second rocket line slightly better than l2 (close-up); after a wide cutaway the variety
    # penalty must flip the choice to the close-up. All three are distinct pictures (cosine < DUP_REJECT).
    loc[0]["embedding"] = vec("rocket")
    loc[1]["embedding"] = (vec("rocket") + 0.58 * vec("ocean")) / np.linalg.norm(vec("rocket") + 0.58 * vec("ocean"))
    loc[2]["embedding"] = (vec("rocket") + 0.6 * vec("forest")) / np.linalg.norm(vec("rocket") + 0.6 * vec("forest"))
    tl = _tl("/x.mp4", ["intro words here", TEXTS[1], TEXTS[3]])
    kw = dict(search=lambda s, q: [], max_share=1.0, embed=lambda c: c["embedding"], embed_text=embed_text, sources=())
    p = broll.plan(tl, local=loc, **kw)
    got = [i["asset"]["id"] for i in p["inserts"]]
    assert got == ["local:l0", "local:l2"], [(i["asset"]["id"], i["score"], i["reasons"]) for i in p["inserts"]]
    loc[2]["shot_type"] = "wide"  # same type everywhere: no penalty difference, the better match l1 wins
    assert [i["asset"]["id"] for i in broll.plan(tl, local=loc, **kw)["inserts"]] == ["local:l0", "local:l1"]


def test_lexical_fallback_is_flagged_and_ignores_local_shots():
    loc = [{"id": "local:x", "source": "lib", "kind": "local_video", "src": "/lib.mp4", "in": 0, "out": 6, "title": "shot",
            "license": "pd", "credit": "lib", "rights_note": None, "page": "/lib.mp4", "embedding": None}]
    p = broll.plan(_tl("/x.mp4", TEXTS), search=lambda s, q: [], local=loc, visual_rank=False)
    assert p["visual"] is False and "lexical" in p["method"] and not p["inserts"]


def _make_library(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    _landscape(a)
    _textcard(b)
    lib = tmp_path / "lib.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-t", "4", "-i", str(a), "-loop", "1", "-t", "4", "-i", str(b),
                    "-filter_complex", "[0:v]scale=320:180,setsar=1,fps=10[x];[1:v]scale=320:180,setsar=1,fps=10[y];"
                    "[x][y]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(lib)], check=True)
    return lib


@needs_model
def test_real_model_picks_the_matching_local_shot_and_renders_it(tmp_path, home, monkeypatch):
    monkeypatch.setenv("NEXUS_VISUAL_MODEL_DIR", str(MODEL))
    from nexus.media import visual
    lib = _make_library(tmp_path)
    an = {"path": str(lib), "shots": [{"id": 0, "start": 0.0, "end": 4.0, "speech_share": 0.0},
                                      {"id": 1, "start": 4.0, "end": 8.0, "speech_share": 0.0}]}
    visual.attach(an, visual.analyze_asset(lib, an, fps=2.0))
    loc = broll.local_candidates(str(lib), an, "public domain (test render)", "test library")
    assert len(loc) == 2 and all(c["embedding"] is not None for c in loc)
    talk = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x404040:s={W}x{H}:r=30:d=12", "-f", "lavfi",
                    "-i", "sine=f=300:d=12:r=48000", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
                    str(talk)], check=True)
    tl = _tl(str(talk), ["Hello there and welcome everyone.", "We hiked into the green mountains under a clear blue sky."])
    p = broll.plan(tl, local=loc, sources=(), max_share=1.0)
    assert p["visual"] and len(p["inserts"]) == 1 and p["inserts"][0]["asset"]["id"].endswith("#0"), p["decisions"]
    new, rep = broll.apply(tl, p)
    assert rep["inserted"] == 1 and new["clips"][1]["pip"]["src"] == str(lib)
    out = render.render(new, tmp_path / "o.mp4", tmp_path / "w")
    t = 6.0 + (new["clips"][1]["pip"]["start"] + new["clips"][1]["pip"]["end"]) / 2
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(out["output"]), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    f = np.frombuffer(raw[: W * H * 3], dtype=np.uint8).reshape(H, W, 3).astype(int)
    top, bottom = f[: H // 4].mean((0, 1)), f[-H // 5:].mean((0, 1))
    assert top[2] > top[0] + 40 and bottom[1] > bottom[0] + 40  # blue sky over green ground is on screen, not the grey speaker


def test_model_absent_means_lexical(tmp_path, monkeypatch, home):
    monkeypatch.setenv("NEXUS_VISUAL_MODEL_DIR", str(tmp_path / "none"))
    p = broll.plan(_tl("/x.mp4", TEXTS), search=lambda s, q: [])
    assert p["visual"] is False


