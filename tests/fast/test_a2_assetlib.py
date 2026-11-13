"""Asset library (nexus/assetlib): manifest + licence validation, generated installs with SHA-256 records, resumable
pinned downloads (local HTTP server with Range support), safe pack extraction, duplicates, import/export, uninstall
protection, missing-asset repair, the LRU quota, and that LUTs, transitions, overlays, sounds, finishing looks and title
motion presets really change the rendered output."""
import http.server
import json
import subprocess
import threading
import zipfile
from pathlib import Path

import numpy as np
import pytest

from nexus import assetlib, config, costguard, project as prj
from nexus.fx import transitions
from nexus.timeline import ops, render

W, H = 160, 90


def _tl(clips, **kw):
    return {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
            "target_lufs": -16.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": True,
            "markers": [], **kw}


def _src(path, colour="testsrc2", d=3):
    inp = f"testsrc2=s={W}x{H}:r=30:d={d}" if colour == "testsrc2" else f"color={colour}:s={W}x{H}:r=30:d={d}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", inp, "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={d}",
                    "-t", str(d), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)
    return str(path)


def _frame(video, t):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(H, W, 3).astype(float)


def _render(tl, tmp, name="o.mp4"):
    out = Path(tmp) / name
    render.render(tl, out, Path(tmp) / f"work_{name}", workers=1)
    return out


# ---------------------------------------------------------------- manifest + licences
def test_manifest_is_valid_and_every_asset_has_licence_metadata(home):
    cat = assetlib.catalog()
    ids = [a["id"] for a in cat]
    assert len(ids) == len(set(ids)) and len(ids) >= 170
    for a in cat:
        assert a["category"] in assetlib.CATEGORY_DIRS, a["id"]
        assert a["license_ok"], (a["id"], a["license_note"])
        for f in ("name", "license", "author", "source_url", "attribution", "description"):
            assert a.get(f), (a["id"], f)
        assert a["commercial_use"] is True
    trans = {a["id"].split(".", 1)[1] for a in cat if a["category"] == "transitions"}
    assert set(transitions.NAMES) <= trans and set(transitions.CUSTOM) <= trans and len(trans) == 76
    for e in assetlib._entries():
        if e["kind"] in ("download", "pack"):
            assert len(e["sha256"]) == 64 and e["url"].startswith("https://")
            host = e["url"].split("/")[2]
            assert any(host in c.get("network_hosts", []) for c in costguard.manifest().values()), host
        if e["kind"] == "generated":
            from nexus.assetlib import generators
            assert e["gen"] in generators.GENERATORS
    for o in assetlib.optional_future():
        assert o["why"]


def test_licence_gate_refuses_unlicensed_or_non_commercial(home, tmp_path, monkeypatch):
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG fake")
    with pytest.raises(assetlib.LicenseRefused):
        assetlib.import_file(f, category="overlays", name="x", license="CC-BY-NC-4.0", author="a", source_url="u",
                             commercial_use=True, attribution="a")
    with pytest.raises(assetlib.LicenseRefused):
        assetlib.import_file(f, category="overlays", name="x", license="CC0-1.0", author="", source_url="u",
                             commercial_use=True, attribution="none")
    with pytest.raises(assetlib.LicenseRefused):  # CC-BY needs an attribution text
        assetlib.import_file(f, category="overlays", name="x", license="CC-BY-4.0", author="a", source_url="u",
                             commercial_use=True, attribution="none required")
    bad = {"id": "x.nc", "name": "x", "category": "luts", "kind": "download", "license": "CC-BY-NC-4.0", "author": "a",
           "source_url": "u", "commercial_use": True, "attribution": "a", "url": "https://x", "sha256": "0" * 64}
    monkeypatch.setattr(assetlib, "_entries", lambda: [bad])
    with pytest.raises(assetlib.LicenseRefused):
        assetlib.install("x.nc")


# ---------------------------------------------------------------- generated installs, integrity, duplicates
def test_generated_install_verify_tamper_and_duplicates(home, tmp_path):
    r = assetlib.install("lut.teal_orange")
    p = Path(r["path"])
    assert p.exists() and p.parent == config.ASSETS / "luts" and assetlib.sha256_file(p) == r["sha256"]
    assert assetlib.install("lut.teal_orange")["installed_at"] == r["installed_at"]       # idempotent
    from nexus.fx import lut
    assert lut.validate(p)["ok"]
    assert assetlib.verify("lut.teal_orange")["ok"]
    p.write_text(p.read_text() + "\n# edited")
    v = assetlib.verify("lut.teal_orange")
    assert not v["ok"] and "changed" in v["detail"]
    u = assetlib.import_file(p, category="luts", name="copy", license="CC0-1.0", author="me", source_url="local",
                             commercial_use=True, attribution="none")
    again = assetlib.import_file(p, category="luts", name="copy2", license="CC0-1.0", author="me", source_url="local",
                                 commercial_use=True, attribution="none")
    assert again["duplicate"] and again["id"] == u["id"]


# ---------------------------------------------------------------- downloads (resume, checksum) and packs
class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        f = Path(self.directory) / self.path.lstrip("/")
        data = f.read_bytes()
        rng = self.headers.get("Range")
        self.server.ranges.append(rng)
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    d = tmp_path / "srv"
    d.mkdir()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: _RangeHandler(*a, directory=str(d), **k))
    srv.ranges = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(costguard, "check_host", lambda url: "test")
    yield d, f"http://127.0.0.1:{srv.server_address[1]}", srv
    srv.shutdown()


def test_download_resumes_with_range_and_rejects_wrong_checksum(home, server):
    d, base, srv = server
    data = np.random.default_rng(1).bytes(300_000)
    (d / "font.ttf").write_bytes(data)
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    dest = config.ASSETS / "fonts" / "f.ttf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.with_name("f.ttf.part").write_bytes(data[:120_000])                     # an interrupted earlier attempt
    assetlib.download(f"{base}/font.ttf", dest, sha, len(data))
    assert dest.read_bytes() == data and srv.ranges[-1] == "bytes=120000-"
    assert not dest.with_name("f.ttf.part").exists()
    with pytest.raises(assetlib.AssetError, match="checksum"):
        assetlib.download(f"{base}/font.ttf", config.ASSETS / "fonts" / "g.ttf", "0" * 64, len(data))
    assert not (config.ASSETS / "fonts" / "g.ttf").exists() and not (config.ASSETS / "fonts" / "g.ttf.part").exists()


def test_pack_install_extracts_only_matching_members_safely(home, server, monkeypatch):
    d, base, _ = server
    z = d / "pack.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.ogg", b"OggS-a")
        zf.writestr("sub/b.ogg", b"OggS-b")
        zf.writestr("../../evil.ogg", b"OggS-evil")
        zf.writestr("source/a.xpf", b"project")
    import hashlib
    e = {"id": "sfxpack.test", "name": "t", "category": "sound_effects", "kind": "pack", "url": f"{base}/pack.zip",
         "sha256": hashlib.sha256(z.read_bytes()).hexdigest(), "size": z.stat().st_size, "members": "*.ogg", "license": "CC0-1.0",
         "author": "t", "source_url": "t", "commercial_use": True, "attribution": "none"}
    monkeypatch.setattr(assetlib, "_entries", lambda: [e])
    r = assetlib.install("sfxpack.test")
    names = sorted(Path(f).name for f in r["files"])
    assert names == ["a.ogg", "b.ogg", "evil.ogg"]
    for f in r["files"]:
        assert Path(f).parent == config.ASSETS / "sound_effects" / "sfxpack.test"   # flattened: nothing escaped
    assert not (config.ASSETS / "tmp" / "sfxpack.test.zip").exists()


# ---------------------------------------------------------------- import / export, uninstall protection, missing, quota
def test_export_import_roundtrip_and_tamper_detection(home, tmp_path):
    assetlib.install("frame.viewfinder")
    out = assetlib.export_pack(["frame.viewfinder", "effect.vhs"], tmp_path / "p.zip")
    assert out["assets"] == 2
    old = config.HOME
    other = tmp_path / "home2"
    config.set_home(other)
    config.ensure_dirs()
    try:
        r = assetlib.import_pack(tmp_path / "p.zip")
        assert len(r["added"]) == 1 and any("built-in" in s["why"] for s in r["skipped"])
        u = assetlib.entry(r["added"][0]["id"])
        assert u["license"] == "CC0-1.0" and Path(u["path"]).exists()
        bad = tmp_path / "bad.zip"
        with zipfile.ZipFile(tmp_path / "p.zip") as zi, zipfile.ZipFile(bad, "w") as zo:
            for i in zi.infolist():
                b = zi.read(i)
                zo.writestr(i, b + b"x" if i.filename.endswith(".png") else b)
        with pytest.raises(assetlib.AssetError, match="SHA-256"):
            assetlib.import_pack(bad)
    finally:
        config.set_home(old)


def test_uninstall_refused_while_used_missing_detected_and_repaired(home, tmp_path):
    p = prj.Project.create("a")
    tl = _tl([ops.video_clip(_src(tmp_path / "s.mp4"), 0, 2)])
    p.save_timeline(tl, "t")
    assetlib.apply(p.id, "lut.cinematic", "timeline")
    with pytest.raises(assetlib.AssetError, match="used by"):
        assetlib.uninstall("lut.cinematic")
    f = Path(assetlib.installed()["lut.cinematic"]["path"])
    f.unlink()
    m = assetlib.missing()
    assert len(m) == 1 and m[0]["asset_id"] == "lut.cinematic" and m[0]["projects"] == [p.id]
    assert assetlib.repair_missing()["fixed"] == ["lut.cinematic"] and f.exists()


def test_quota_evicts_lru_but_keeps_favourites_and_used(home, tmp_path):
    for a in ("sfx.whoosh", "sfx.impact", "sfx.riser"):
        assetlib.install(a)
    assetlib.set_favorite("sfx.impact")
    p = prj.Project.create("q")
    p.save_timeline(_tl([ops.video_clip(_src(tmp_path / "s.mp4"), 0, 2)]), "t")
    assetlib.apply(p.id, "sfx.riser", "timeline")
    r = assetlib.enforce_quota(0)
    assert r["evicted"] == ["sfx.whoosh"]
    assert set(assetlib.installed()) == {"sfx.impact", "sfx.riser"}


# ---------------------------------------------------------------- rendering: the assets really change the output
def test_lut_overlay_and_sound_change_the_render(home, tmp_path):
    src = _src(tmp_path / "s.mp4")
    p = prj.Project.create("r")
    p.save_timeline(_tl([ops.video_clip(src, 0, 2)]), "t")
    base = _render(prj.Project(p.id).timeline(), tmp_path, "base.mp4")
    assetlib.apply(p.id, "lut.black_white", "timeline")
    bw = _frame(_render(prj.Project(p.id).timeline(), tmp_path, "bw.mp4"), 1.0)
    assert np.abs(bw[..., 0] - bw[..., 2]).mean() < 4 and np.abs(_frame(base, 1.0)[..., 0] - _frame(base, 1.0)[..., 2]).mean() > 20
    assetlib.apply(p.id, "frame.cinematic_239", "timeline")
    fr = _frame(_render(prj.Project(p.id).timeline(), tmp_path, "lb.mp4"), 1.0)
    assert fr[:8].mean() < 20 and fr[-8:].mean() < 20 and fr[H // 2].mean() > 40   # bars top and bottom, picture between
    assetlib.apply(p.id, "sfx.cinematic_hit", "timeline", at=0.5)
    tl = prj.Project(p.id).timeline()
    assert tl["audio"][-1]["src"].endswith(".wav") and tl["audio"][-1]["start"] == 0.5
    out = _render(tl, tmp_path, "sfx.mp4")
    a = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"], capture_output=True, check=True).stdout
    x = np.abs(np.frombuffer(a, "<i2").astype(float))
    assert x[int(0.6 * 8000):int(1.0 * 8000)].mean() > 10 * (x[:int(0.4 * 8000)].mean() + 1)


@pytest.mark.parametrize("name", ["film_burn", "shape_heart", "spin", "luma_clouds", "whip_pan_left"])
def test_extended_transitions_render_through_the_piece_pipeline(home, tmp_path, name):
    a, b = _src(tmp_path / "a.mp4", "red"), _src(tmp_path / "b.mp4", "blue")
    c2 = ops.video_clip(b, 0, 2)
    c2.update(transition="crossfade", xfade=name, transition_dur=1.0)
    out = _render(_tl([ops.video_clip(a, 0, 2), c2]), tmp_path)
    mid, start, end = _frame(out, 1.5), _frame(out, 0.3), _frame(out, 2.7)
    assert start[..., 0].mean() > 150 and end[..., 2].mean() > 150
    assert np.abs(mid - start).mean() > 10 and np.abs(mid - end).mean() > 10      # a real in-between picture


def test_transition_falls_back_when_ffmpeg_lacks_custom_expressions(monkeypatch):
    monkeypatch.setattr(transitions, "_custom_ok", False)
    assert transitions.filter_for("shape_star", 1, 0).startswith("xfade=transition=circleopen")
    assert transitions.filter_for("wipeleft", 1, 0).startswith("xfade=transition=wipeleft")
    assert transitions.resolve("burn") == "film_burn" and transitions.is_transition("heart")


@pytest.mark.parametrize("fx", ["glow", "bloom", "light_leak", "vhs", "chromatic_aberration", "lens_distortion", "motion_blur",
                                "film_burn", "sharpen", "denoise", "finish"])
def test_finishing_looks_render_and_change_pixels(home, tmp_path, fx):
    src = _src(tmp_path / "s.mp4")
    tl = _tl([])
    plain, _ = render.render_segment(ops.video_clip(src, 0, 2), tl, config.CACHE / "segments")
    e = {"type": fx, "start": 0.2, "duration": 1.6} if fx == "film_burn" else {"type": fx}
    seg, _ = render.render_segment(ops.video_clip(src, 0, 2, effects=[e]), tl, config.CACHE / "segments")
    assert np.abs(_frame(seg, 1.0) - _frame(plain, 1.0)).mean() > 0.8


@pytest.mark.parametrize("anim", ["pop", "spring", "bounce", "zoom"])
def test_title_motion_presets_animate(home, tmp_path, anim):
    c = ops.color_clip(2.0, "0x000000")
    c["titles"] = [{"text": "NEXUS", "style": "title", "start": 0.0, "end": 2.0, "anim": anim}]
    seg, _ = render.render_segment(c, _tl([]), config.CACHE / "segments")
    f1, f2 = _frame(seg, 0.12), _frame(seg, 1.2)
    assert f2.mean() > 1.0 and np.abs(f1 - f2).mean() > 0.3       # the title moves/scales into its resting place


def test_motion_preset_and_template_apply_to_clip(home, tmp_path):
    p = prj.Project.create("m")
    c = ops.video_clip(_src(tmp_path / "s.mp4"), 0, 2)
    p.save_timeline(_tl([c]), "t")
    assetlib.apply(p.id, "motion.ken_burns_in", "clip", c["id"])
    k = prj.Project(p.id).timeline()["clips"][0]["keyframes"]["scale"]
    assert k[0][:2] == [0, 1.0] and k[-1][1] == 1.15 and k[-1][0] == pytest.approx(2.0)
    assetlib.apply(p.id, "template.end_card", "timeline")
    tl = prj.Project(p.id).timeline()
    assert tl["clips"][-1]["type"] == "color" and tl["clips"][-1]["titles"][0]["anim"] == "pop"
    with pytest.raises(assetlib.AssetError, match="no on-screen title"):
        assetlib.apply(p.id, "motion.title_spring", "clip", c["id"])
    hist = prj.Project(p.id).history()["main"]
    assert len(hist["versions"]) == 3                                            # each apply is an undoable version


def test_library_api_and_workspace_apply(home, tmp_path):
    from fastapi.testclient import TestClient

    from nexus import api
    cl = TestClient(api.app)
    lib = cl.get("/api/library?category=luts").json()
    assert lib["summary"]["assets"] >= 170 and all(a["category"] == "luts" for a in lib["assets"])
    assert cl.post("/api/library/install", json={"asset_id": "lut.vintage"}).json()["installed"]["sha256"]
    assert cl.get("/api/library/preview/lut.vintage").headers["content-type"] == "image/jpeg"
    assert cl.get("/api/library/preview/effect.vhs").status_code == 200
    assert cl.get("/api/library/preview/font.inter").status_code == 404                 # previews never download
    p = prj.Project.create("w")
    c = ops.video_clip(_src(tmp_path / "s.mp4"), 0, 2)
    p.save_timeline(_tl([c]), "t")
    r = cl.post(f"/api/projects/{p.id}/assets/apply", json={"asset_id": "effect.light_leak", "target": "clip", "clip": c["id"]})
    assert r.status_code == 200, r.text
    assert prj.Project(p.id).timeline()["clips"][0]["effects"][-1]["type"] == "light_leak"
    assert cl.post(f"/api/projects/{p.id}/assets/apply", json={"asset_id": "effect.vhs", "target": "clip"}).status_code == 400
    assert cl.post("/api/library/favorite", json={"asset_id": "effect.vhs"}).json()["favorites"] == ["effect.vhs"]
    d = cl.get("/api/disk").json()
    assert "render_cache" in d["usage"]["categories"] and d["status"]["level"] in ("ok", "warning", "critical")
    plan = cl.post("/api/disk/cleanup", json={}).json()
    assert plan["dry_run"] and "plan_id" in plan
    assert cl.post("/api/disk/cleanup", json={"plan_id": "0" * 16}).status_code == 409
    assert "summary" in cl.get("/api/archive").json()
    assert json.loads(json.dumps(cl.get("/api/fx/catalog").json()))["transitions"].count("film_burn") == 1


def test_previews_survive_windows_style_paths(tmp_path, monkeypatch):
    """Regression (first Windows clean-machine run, 2026-11-13: GET /api/library/preview/lut.cinematic -> 500): the
    font path (C:\\Windows\\Fonts\\...) and the LUT path went into FFmpeg filters unescaped. Paths here carry the
    characters that break a filter on every OS (drive-style colon, quote, spaces, non-ASCII); the backslash -> slash part
    of ff.filter_path only applies to real Windows paths and is covered by the Windows CI run."""
    import shutil

    from nexus import db
    weird = tmp_path / "D:" / "NEXUS dâta's 数据"
    font_dir = tmp_path / "C:" / "Windows" / "Fonts it's"
    font_dir.mkdir(parents=True)
    font = font_dir / "segoeuib.ttf"
    shutil.copyfile(config.FONT_LATIN, font)
    old = config.HOME
    db.reset_for_tests()
    config.set_home(weird)
    config.ensure_dirs()
    monkeypatch.setattr(config, "FONT_LATIN", str(font))
    try:
        assetlib.install("lut.cinematic")
        for aid in ("lut.cinematic", "effect.vhs", "transition.shape_star"):
            p = assetlib.preview(aid)
            assert p.exists() and p.stat().st_size > 1000, aid
    finally:
        db.reset_for_tests()
        config.set_home(old)
