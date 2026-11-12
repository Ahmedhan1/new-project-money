"""BRL-1: B-roll engine - opportunities, licence/rights gate, relevance, no reuse, de-duplication, full-frame cutaways that
keep the speaker's audio and change the picture. HTTP is mocked; media from Pillow/lavfi."""
import subprocess

import numpy as np
from PIL import Image

from nexus import broll
from nexus.timeline import ops, render

W, H = 320, 180


def _tl(src, texts):
    clips, subs, t = [], [], 0.0
    for k, txt in enumerate(texts):
        clips.append(ops.video_clip(src, k * 5.0, k * 5.0 + 5.0))
        subs.append({"start": t + 0.2, "end": t + 4.8, "text": txt})
        t += 5.0
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none", "target_lufs": -16,
            "clips": clips, "audio": [], "music": None, "subtitles": subs, "burn_subtitles": False, "markers": []}


TEXTS = ["Hello and welcome, today we talk about the mission.",
         "We trained inside a lava cave with headlamps and dehydrated food packages.",
         "I feel so proud of my family and I love them.",
         "The International Space Station orbits the Earth every ninety minutes.",
         "We tested the rover wheels on volcanic rock fields."]


def _cand(i, title, kind="image", keywords=(), desc="", rights=None):
    return {"id": f"nasa:{i}", "source": "NASA Image and Video Library", "kind": kind, "title": title, "description": desc,
            "keywords": list(keywords), "file_url": f"https://images-assets.nasa.gov/image/{i}/{i}~medium.jpg", "page": f"p{i}",
            "license": "public domain", "credit": f"NASA - {title}", "rights_note": rights}


CANDS = {
    "food": [_cand("food1", "Space food packages dehydrated for Gemini", keywords=["food", "dehydrated"])],
    "station": [_cand("iss1", "International Space Station over Earth", keywords=["international space station", "earth", "orbit"])],
    "rover": [_cand("bad1", "Annual budget town hall", keywords=["budget"]),
              _cand("rights1", "Rover wheel test on volcanic rock", keywords=["rover", "wheels", "volcanic"], desc="Courtesy of Acme Corp"),
              _cand("rov2", "Rover wheels tested on volcanic rock field", keywords=["rover", "wheels", "volcanic", "rock"])],
}


def fake_search(src, q):
    if src != "nasa":
        return []
    for k, v in CANDS.items():
        if k in q or any(k in w for w in q.split()):
            return v
    return [c for v in CANDS.values() for c in v]


def test_opportunities_skip_hook_and_emotional_statements(tmp_path):
    tl = _tl("/x.mp4", TEXTS)
    ops_ = {o["index"]: o for o in broll.opportunities(tl)}
    assert not ops_[0]["use"] and "opening" in ops_[0]["why"]
    assert not ops_[2]["use"] and "emotional" in ops_[2]["why"]
    assert ops_[1]["use"] and ops_[3]["use"]


def test_plan_gates_relevance_rights_and_never_reuses():
    tl = _tl("/x.mp4", TEXTS)
    p = broll.plan(tl, search=fake_search, max_share=1.0)
    ids = [i["asset"]["id"] for i in p["inserts"]]
    assert len(ids) == len(set(ids)) and "nasa:bad1" not in ids and "nasa:rights1" not in ids
    assert "nasa:rov2" in ids or any("rover" in (d.get("why") or "") for d in p["decisions"])
    assert all(d["why"] for d in p["decisions"])
    assert any("third-party rights" in r for d in p["decisions"] for r in d.get("rejected", [])) or "nasa:rov2" in ids


def test_apply_inserts_full_frame_cutaways_that_change_pixels_and_keep_audio(home, tmp_path):
    src = tmp_path / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x404040:s={W}x{H}:r=30:d=25", "-f", "lavfi",
                    "-i", "sine=f=300:d=25:r=48000", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)],
                   check=True)
    tl = _tl(str(src), TEXTS)
    p = broll.plan(tl, search=fake_search, max_share=1.0)

    def fetcher(asset):
        f = tmp_path / f"{asset['id'].split(':')[1]}.jpg"
        rng = np.random.default_rng(abs(hash(asset["id"])) % 2**32)
        Image.fromarray((rng.random((360, 640, 3)) * 255).astype("uint8")).save(f)
        return f
    new, rep = broll.apply(tl, p, fetcher=fetcher)
    assert rep["inserted"] >= 2 and new["credits"]
    pips = [c for c in new["clips"] if c.get("pip")]
    assert all(c["pip"]["full"] and c["pip"]["provenance"]["license"] for c in pips)
    st = render.render(new, tmp_path / "o.mp4", tmp_path / "w")
    assert abs(st["duration"] - 25) < 0.2
    k = new["clips"].index(pips[0])
    t = k * 5.0 + (pips[0]["pip"]["start"] + pips[0]["pip"]["end"]) / 2
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(tmp_path / "o.mp4"), "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    f = np.frombuffer(raw[: W * H], dtype=np.uint8)
    assert f.std() > 8  # the noisy cutaway is on screen (the flat grey speaker shot has std ~0)
    vol = subprocess.run(["ffmpeg", "-ss", f"{t:.2f}", "-t", "1", "-i", str(tmp_path / "o.mp4"), "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    assert "mean_volume" in vol and float(vol.split("mean_volume:")[1].split("dB")[0]) > -40  # speaker audio continues


def test_terms_drop_filler_and_keep_entities():
    t = broll.terms("We basically went to the International Space Station because it was really totally amazing")
    assert t[0] == "international space station" and "basically" not in t and "because" not in t and "totally" not in t


def test_footage_in_point_skips_a_static_opening(tmp_path):
    """Archive clips often open on a still shot (rocket on the pad): the cutaway must start where the picture moves."""
    import subprocess
    v = tmp_path / "clip.mp4"
    # 6 s of a static frame, then 6 s of moving test pattern
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=10:d=6",
                    "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=6", "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
                    "-map", "[v]", "-pix_fmt", "yuv420p", str(v)], check=True)
    from nexus import broll
    t = broll.moving_in_point(v, 3.0)
    assert t >= 5.5, t
    assert broll.hd_window({"kind": "video"}, 0, 3) is None          # no HD rendition known: keep the fetched file
