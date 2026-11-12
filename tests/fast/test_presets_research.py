from nexus import create, presets, project
from nexus.timeline import ops


def test_rank_hits_prefers_the_page_about_the_topic():
    hits = [{"title": "Trained immunity", "snippet": "long-term <b>immune</b> memory"},
            {"title": "Vaccine", "snippet": "a preparation that <b>trains</b> the <b>immune system</b>"},
            {"title": "Immune system", "snippet": "network of biological systems"},
            {"title": "Train", "snippet": "rail vehicles"}]
    order = create.rank_hits("How vaccines train the immune system", hits)
    assert order[0] == "Vaccine" and order.index("Train") > order.index("Immune system")


def test_presets_resolve_and_apply():
    assert {"cinematic", "documentary", "social-short"} <= {p["id"] for p in presets.names()}
    st = presets.get("social-short", mood=None, grade="bw")
    assert st["grade"] == "bw" and st["mood"] == "upbeat" and st["size"] == [1080, 1920]
    assert presets.get("does-not-exist")["preset"] == presets.DEFAULT
    tl = project.new_timeline()
    tl["clips"] = [ops.color_clip(2), ops.image_clip("a.jpg", 3, transition="crossfade"), ops.image_clip("b.jpg", 3, transition="crossfade", grade="warm")]
    tl["music"] = {"src": "m.wav", "volume": 0.5}
    out = presets.apply(tl, st)
    assert out["captions"]["style"] == "pop" and out["overlays"][0]["type"] == "progress_bar"
    assert out["clips"][1]["grade"] == "bw" and out["clips"][2]["grade"] == "warm"      # explicit per-clip choice kept
    assert out["clips"][1]["xfade"] == "slideup" and out["music"]["volume"] == 0.3
    assert "captions" not in tl                                                         # input not mutated
