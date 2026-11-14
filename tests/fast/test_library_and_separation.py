"""Free video library (licence filter, credits) and the dub background items (Demucs stem under muted clips)."""
from nexus import library
from nexus.audio import separate


def test_archive_keeps_only_cc_and_public_domain(monkeypatch):
    docs = [{"identifier": "a1", "title": "Free film", "licenseurl": "http://creativecommons.org/licenses/by/4.0/", "creator": "X"},
            {"identifier": "a2", "title": "No licence", "licenseurl": "", "creator": "Y"},
            {"identifier": "a3", "title": "PD film", "licenseurl": "http://creativecommons.org/publicdomain/mark/1.0/"}]
    monkeypatch.setattr(library.net, "get_json", lambda url, *a, **k: {"response": {"docs": docs}})
    out = library.search_archive("moon", 5)
    assert [x["ident"] for x in out] == ["a1", "a3"]
    assert "creativecommons.org/licenses/by/4.0" in out[0]["credit"] and out[0]["page"].endswith("/a1")


def test_commons_skips_non_free_and_non_video(monkeypatch):
    pages = {"1": {"pageid": 1, "title": "File:Moon.webm", "imageinfo": [{"mime": "video/webm", "url": "https://u/m.webm",
              "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": "<a>Ann</a>"}}}]},
             "2": {"pageid": 2, "title": "File:Logo.webm", "imageinfo": [{"mime": "video/webm", "url": "https://u/l.webm",
              "extmetadata": {"LicenseShortName": {"value": "Non-free logo"}}}]},
             "3": {"pageid": 3, "title": "File:Pic.jpg", "imageinfo": [{"mime": "image/jpeg", "url": "https://u/p.jpg",
              "extmetadata": {"LicenseShortName": {"value": "CC0"}}}]}}
    monkeypatch.setattr(library.net, "get_json", lambda url, *a, **k: {"query": {"pages": pages}})
    out = library.search_commons("moon", 5)
    assert [x["title"] for x in out] == ["Moon.webm"] and out[0]["credit"].startswith("Ann")


def test_one_source_failing_does_not_hide_the_others(monkeypatch):
    monkeypatch.setitem(library.SOURCES, "nasa", lambda q, n: (_ for _ in ()).throw(TimeoutError("down")))
    monkeypatch.setitem(library.SOURCES, "commons", lambda q, n: [{"id": "c", "title": "ok"}])
    r = library.search("moon", ["nasa", "commons"])
    assert [x["id"] for x in r["results"]] == ["c"] and "nasa" in r["errors"]


def test_background_items_follow_muted_clips_and_skip_stretched_ones():
    tl = {"clips": [{"id": "a", "type": "video", "src": "/v.mp4", "in": 10.0, "out": 14.0, "mute": True},
                    {"id": "b", "type": "video", "src": "/v.mp4", "in": 30.0, "out": 33.0},
                    {"id": "c", "type": "video", "src": "/v.mp4", "in": 50.0, "out": 52.0, "mute": True, "speed": 0.8}],
          "width": 640, "height": 360}
    items = separate.background_items(tl, {"/v.mp4": "/bg.wav"}, {"a", "c"})
    assert len(items) == 1
    it = items[0]
    assert (it["src"], it["start"], it["in"], it["out"], it["role"]) == ("/bg.wav", 0.0, 10.0, 14.0, "ambience")
