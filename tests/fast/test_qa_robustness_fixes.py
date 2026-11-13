"""Regression tests for QA-D2 (damaged nexus.db -> traceback), QA-D9 (doctor said OK for FFmpeg-dependent parts
without FFmpeg). Each fails on the pre-fix code."""
import os
import subprocess
import sys

import pytest


def _damage(home):
    from nexus import config, db
    db.conn()
    db.reset_for_tests()
    for ext in ("-wal", "-shm"):
        p = config.DB_PATH.with_name(config.DB_PATH.name + ext)
        if p.exists():
            p.unlink()
    with open(config.DB_PATH, "r+b") as f:
        f.write(b"\x00garbage, not a database" * 8)


def test_qa_d2_damaged_db_raises_plain_recovery_message(home):
    from nexus import db
    _damage(home)
    with pytest.raises(db.DatabaseDamaged) as e:
        db.conn()
    msg = str(e.value)
    assert "nexus.db" in msg and "nexus restore" in msg and "Traceback" not in msg
    db.reset_for_tests()


def test_qa_d2_preflight_and_cli_report_instead_of_crashing(home):
    from nexus import db, launcher
    _damage(home)
    chk = {c["check"]: c for c in launcher.preflight()}["database"]
    assert not chk["ok"] and "damaged" in chk["detail"] and "restore" in chk["fix"]
    db.reset_for_tests()
    r = subprocess.run([sys.executable, "-m", "nexus", "jobs", "list"], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "NEXUS_HOME": str(home), "NEXUS_EMBEDDED_WORKER": "0"})
    assert r.returncode == 3 and "nexus restore" in r.stderr and "Traceback" not in r.stderr


def test_qa_d9_ffmpeg_dependent_components_unavailable_without_ffmpeg(monkeypatch):
    from nexus import tools
    real = tools.shutil.which
    monkeypatch.setattr(tools.shutil, "which", lambda b, *a, **k: None if b == "ffmpeg" else real(b, *a, **k))
    rows = {r["id"]: r for r in tools.health()}
    assert rows["pillow-motion"]["healthy"] is False and "ffmpeg" in rows["pillow-motion"]["version"]


def test_qa_d3_registry_cache_is_safe_and_invalidates(tmp_path, monkeypatch):
    import threading

    from nexus import assetlib, config
    reg = tmp_path / "asset_library.yaml"
    reg.write_text("assets:\n  - id: a\n    name: A\n", encoding="utf-8")
    monkeypatch.setattr(config, "REGISTRY_DIR", tmp_path)
    assetlib._REG_CACHE.clear()
    first = assetlib._registry()
    first["assets"].append({"id": "mutated"})          # a caller mutating its copy must not change the cache
    assert assetlib._registry() == {"assets": [{"id": "a", "name": "A"}]}
    reg.write_text("assets:\n  - id: b\n    name: Bee\n", encoding="utf-8")   # edit -> picked up
    assert assetlib._registry()["assets"][0]["id"] == "b"
    got, errs = [], []

    def worker():
        try:
            got.append(assetlib._registry())
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and all(g == {"assets": [{"id": "b", "name": "Bee"}]} for g in got)
    assetlib._REG_CACHE.clear()


def test_qa_d3_real_registry_parsed_once_per_version(monkeypatch):
    from nexus import assetlib
    assetlib._REG_CACHE.clear()
    calls = []
    real = assetlib.yaml.load
    monkeypatch.setattr(assetlib.yaml, "load", lambda *a, **k: calls.append(1) or real(*a, **k))
    for _ in range(50):
        assetlib._registry()
    assert len(calls) == 1
    assetlib._REG_CACHE.clear()


def test_qa_d8_corrupt_media_fails_once_with_a_plain_error_and_a_failed_stage(home):
    from nexus import config, jobs
    from nexus.media import ff
    bad = config.HOME / "uploads" / "corrupt.mp4"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\x00not a video" * 100)

    @jobs.handler("qa-d8-probe")
    def _probe(job):
        return job.stage("probe", lambda: ff.probe(bad))
    jid = jobs.submit("qa-d8-probe", {}, max_retries=3)["job"]
    jobs.run_worker(once=True)
    r = jobs.get(jid)
    assert r["status"] == "failed" and r["retries"] == 1                  # not retried: the input cannot change
    assert "Traceback" not in r["error"] and str(config.HOME) not in r["error"] and "<NEXUS_HOME>" in r["error"]
    assert r["stages"]["probe"]["status"] == "failed"                     # not left 'running'
    log = (config.LOGS / "jobs" / f"{jid}.log").read_text(encoding="utf-8")
    assert "Traceback" in log                                             # details kept for the owner
    jobs.HANDLERS.pop("qa-d8-probe", None)


def test_qa_d8_transient_errors_are_still_retried(home):
    from nexus import jobs

    @jobs.handler("qa-d8-flaky")
    def _flaky(job):
        raise ConnectionError("network blip")
    jid = jobs.submit("qa-d8-flaky", {}, max_retries=2)["job"]
    jobs.run_worker(once=True)          # runs until the queue is empty: every allowed retry happens
    r = jobs.get(jid)
    assert r["status"] == "failed" and r["retries"] == 3   # 1 + max_retries attempts, unlike the permanent case
    jobs.HANDLERS.pop("qa-d8-flaky", None)
