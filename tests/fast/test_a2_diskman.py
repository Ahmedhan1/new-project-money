"""Disk management (nexus/diskman.py): measured usage by category, render preflight, LRU cache eviction and a cleanup
that only ever deletes allowlisted files matched by a rule - never sources, referenced files, finals, secrets, files
of an active job, or anything outside NEXUS_HOME."""
import json
import os
import subprocess
import time
from collections import namedtuple
from pathlib import Path

import pytest

from nexus import config, db, diskman, project as prj
from nexus.timeline import ops, render

DU = namedtuple("DU", "total used free")
OLD = time.time() - 30 * 86400


def _f(p: Path, n: int = 1000, old: bool = False) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(n))
    if old:
        os.utime(p, (OLD, OLD))
    return p


def test_usage_by_category_is_measured(home):
    _f(home / "cache" / "segments" / "seg_a.mp4", 5000)
    _f(home / "cache" / "proxies" / "p.mp4", 3000)
    _f(home / "models" / "m.bin", 7000)
    p = prj.Project.create("u")
    _f(p.dir / "media" / "00_src.mp4", 11000)
    _f(p.dir / "renders" / "final.mp4", 13000)
    c = diskman.usage()["categories"]
    assert c["render_cache"]["bytes"] == 5000 and c["media_cache"]["bytes"] == 3000 and c["models"]["bytes"] == 7000
    assert c["project_sources"]["bytes"] == 11000 and c["renders"]["bytes"] == 13000


def test_status_thresholds(home):
    assert diskman.status()["level"] in ("ok", "warning")
    diskman.save_settings({"warn_free_gb": 1e9})
    assert diskman.status()["level"] == "warning"
    diskman.save_settings({"min_free_gb": 1e9})
    st = diskman.status()
    assert st["level"] == "critical" and "refused" in st["messages"][0]
    with pytest.raises(ValueError):
        diskman.save_settings({"bogus": 1})


def test_preflight_evicts_lru_then_refuses_with_numbers(home, monkeypatch):
    seg = home / "cache" / "segments"
    files = [_f(seg / f"seg_{i}.mp4", 1000) for i in range(5)]
    for i, f in enumerate(files):  # seg_0 is the least recently used
        os.utime(f, (OLD + i, OLD + i))

    def fake_du(_path):
        used = sum(x.stat().st_size for x in seg.glob("*.mp4"))
        return DU(10 ** 9, used, 10_000 - used)  # 5000 free with all 5 cached

    monkeypatch.setattr(diskman.shutil, "disk_usage", fake_du)
    diskman.save_settings({"min_free_gb": 0.0})
    d = diskman.preflight(6500, "test render")         # needs 1500 more: the two oldest entries go
    assert d["ok"] and d["evicted"]["files"] == 2
    assert not files[0].exists() and not files[1].exists() and files[2].exists()
    with pytest.raises(diskman.InsufficientDisk) as e:
        diskman.preflight(10 ** 6, "huge render")
    assert "needs" in str(e.value) and "Nothing was changed" in str(e.value)


def test_cleanup_plan_rules_and_protection(home):
    now = time.time()
    p = prj.Project.create("c")
    src = _f(p.dir / "media" / "00_src.mp4", 100, old=True)                    # source: never
    final = _f(p.dir / "renders" / "main_123.mp4", 100, old=True)              # final: never (archive releases it)
    draft = _f(p.dir / "renders" / "main_draft_123.mp4", 100, old=True)        # stale preview
    music = _f(home / "cache" / "music" / "bed.m4a", 100, old=True)            # referenced by the timeline
    tl = prj.new_timeline()
    tl["music"] = {"src": str(music), "volume": 0.2}
    tl["clips"] = [ops.video_clip(str(src), 0, 1)]
    p.save_timeline(tl, "t")
    part = _f(home / "models" / "x.bin.part", 100, old=True)                  # abandoned partial download
    fresh_part = _f(home / "models" / "y.bin.part", 100)                      # still downloading
    work = _f(p.dir / "work" / "checkpoint_old.json", 100, old=True)          # abandoned render work
    dpiece = _f(home / "cache" / "pieces_draft" / "piece_x.mp4", 100, old=True)
    log = _f(home / "logs" / "jobs" / "job_old.log", 100, old=True)
    secret = _f(home / "secrets" / "vault.key", 100, old=True)
    pl = diskman.plan(now=now)
    got = {Path(i["path"]).name: i["rule"] for i in pl["items"]}
    assert got[part.name] == "incomplete_download" and got[draft.name] == "stale_preview"
    assert got[work.name] == "abandoned_temporary" and got[dpiece.name] == "stale_preview" and got[log.name] == "old_job_log"
    for keep in (src, final, music, fresh_part, secret):
        assert keep.name not in got, keep
    r = diskman.execute(pl["plan_id"])
    assert r["deleted"] == len(pl["items"]) and not r["errors"]
    for keep in (src, final, music, fresh_part, secret):
        assert keep.exists()
    assert not part.exists() and not draft.exists()


def test_execute_refuses_a_changed_plan(home):
    _f(home / "cache" / "uploads" / "a.bin", 100, old=True)
    pl = diskman.plan(["abandoned_temporary"])
    _f(home / "cache" / "uploads" / "b.bin", 100, old=True)
    with pytest.raises(ValueError, match="changed"):
        diskman.execute(pl["plan_id"], ["abandoned_temporary"])
    assert (home / "cache" / "uploads" / "a.bin").exists()


def test_safe_delete_refuses_outside_allowlist_symlinks_and_protected(home, tmp_path):
    outside = _f(tmp_path / "documents" / "letter.txt")
    with pytest.raises(diskman.UnsafePath, match="allowlist"):
        diskman.safe_delete(outside)
    p = prj.Project.create("s")
    src = _f(p.dir / "media" / "00_a.mp4")
    with pytest.raises(diskman.UnsafePath):
        diskman.safe_delete(src)
    db_file = config.DB_PATH
    with pytest.raises(diskman.UnsafePath):
        diskman.safe_delete(db_file)
    target = _f(home / "cache" / "segments" / "seg_t.mp4")
    link = home / "cache" / "segments" / "seg_link.mp4"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(diskman.UnsafePath, match="symbolic"):
        diskman.safe_delete(link)
    # Windows-style separators resolve to the same allowlisted file
    assert diskman.safe_delete(str(target).replace(os.sep, "/")) == 1000


def test_active_render_inputs_and_fresh_cache_are_protected(home):
    p = prj.Project.create("active")
    work_file = _f(p.dir / "work" / "seg_tmp.json", 100, old=True)
    used = _f(home / "cache" / "segments" / "seg_used.mp4", 100)               # touched by the running render
    stale = _f(home / "cache" / "segments" / "seg_stale.mp4", 100, old=True)
    db.execute("INSERT INTO jobs(id,project_id,kind,status,created,started,params) VALUES(?,?,?,?,?,?,?)",
               ("job_r", p.id, "render", "running", time.time() - 100, time.time() - 100, "{}"))
    diskman.save_settings({"cache_quota_gb": 0.0})
    pl = diskman.plan()
    names = {Path(i["path"]).name for i in pl["items"]}
    assert stale.name in names and used.name not in names and work_file.name not in names
    assert any("still running" in s["refused"] for s in pl["protected_skipped"])


def test_intermediates_released_only_after_verified_archive(home):
    a, b = prj.Project.create("a"), prj.Project.create("b")
    seg = home / "cache" / "segments"
    only_a, shared = _f(seg / "seg_onlya.mp4"), _f(seg / "seg_shared.mp4")
    for pr, stems in ((a, ["seg_onlya", "seg_shared"]), (b, ["seg_shared"])):
        (pr.dir / "work").mkdir(exist_ok=True)
        (pr.dir / "work" / "checkpoint_main.json").write_text(json.dumps(
            {"output": str(pr.dir / "renders" / "main.mp4"), "stage": "done", "segments_done": stems, "pieces_done": []}))
    assert diskman.plan(["intermediate_after_archive"])["items"] == []
    db.put_doc("a2_archive", {"id": "arc_x", "project_id": a.id, "kind": "master", "state": "verified", "size": 1,
                              "local_path": str(a.dir / "renders" / "main.mp4"), "source_path": str(a.dir / "renders" / "main.mp4")})
    names = {Path(i["path"]).name for i in diskman.plan(["intermediate_after_archive"])["items"]}
    assert names == {only_a.name}                                              # shared with project b: kept
    assert shared.exists()


def test_log_rotation_keeps_generations(home):
    lg = home / "logs" / "server.log"
    for _ in range(5):
        _f(lg, 3 * 2 ** 20)
        diskman.rotate_log(lg, max_mb=1, keep=2)
    assert not lg.exists() and lg.with_name("server.log.1").exists() and lg.with_name("server.log.2").exists()
    assert not lg.with_name("server.log.3").exists()


def test_render_estimate_covers_a_real_render(home, tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=6", "-f", "lavfi", "-i",
                    "sine=f=440:d=6:r=48000", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)], check=True)
    tl = {"version": 1, "width": 640, "height": 360, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
          "target_lufs": -16.0, "clips": [ops.video_clip(str(src), 0, 3), ops.video_clip(str(src), 3, 6)], "audio": [],
          "music": None, "subtitles": [], "burn_subtitles": True, "markers": []}
    est = diskman.estimate_render(tl)
    before = diskman._walk(home)[0]
    out = home / "projects" / "x" / "renders" / "o.mp4"
    render.render(tl, out, tmp_path / "work", workers=1)
    grown = diskman._walk(home)[0] - before
    assert est["duration_s"] == pytest.approx(6.0, abs=0.2)
    assert est["required_bytes"] >= grown > 0, (est, grown)


def test_working_storage_on_another_directory(home, tmp_path, monkeypatch):
    work = tmp_path / "fastdisk" / "nexus-work"
    monkeypatch.setenv("NEXUS_WORK_DIR", str(work))
    config.set_home(home)
    config.ensure_dirs()
    try:
        assert config.CACHE == work and work.exists()
        f = _f(work / "segments" / "seg_old.mp4", 500, old=True)
        u = diskman.usage()
        assert u["work_dir"] == str(work) and u["categories"]["render_cache"]["bytes"] == 500
        diskman.save_settings({"cache_quota_gb": 0.0})
        p = diskman.plan(["cache_lru"])
        assert [Path(i["path"]).name for i in p["items"]] == [f.name]
        diskman.execute(p["plan_id"], ["cache_lru"])
        assert not f.exists()
    finally:
        monkeypatch.delenv("NEXUS_WORK_DIR")
        config.set_home(home)
