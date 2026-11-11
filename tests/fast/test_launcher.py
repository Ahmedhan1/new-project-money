"""Acceptance test F (the part that can run without Windows): one command starts worker + API, the health
check passes, a previously created project is served, and stop shuts everything down."""
import json
import shutil
import socket
import urllib.request

import pytest

from nexus import config, jobs, launcher, project as prj


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_preflight_reports_each_requirement_with_a_fix(home):
    checks = {c["check"]: c for c in launcher.preflight()}
    assert {"python", "python packages", "ffmpeg", "disk space", "database"} <= set(checks)
    assert checks["python"]["ok"] and checks["database"]["ok"] and all("fix" in c for c in checks.values())


def test_settings_port_is_configurable(home, monkeypatch):
    assert config.settings()["port"] == config.DEFAULT_PORT
    (home / "settings.json").write_text(json.dumps({"port": 9001}))
    assert config.settings()["port"] == 9001
    monkeypatch.setenv("NEXUS_PORT", "9002")
    assert config.settings()["port"] == 9002


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_start_health_previous_project_stop(home):
    p = prj.Project.create("Project from before the restart", "edit")      # state left by an earlier run
    orphan = jobs.submit("no-such-kind")["job"]
    jobs.claim("worker-that-died")                                           # ... including a job that was mid-flight
    port = _free_port()
    said = []
    try:
        assert launcher.start(port, open_browser=False, say=said.append) == 0, "\n".join(map(str, said))
        st = launcher.service_status()
        assert st["api"] and st["worker"] and st["healthy"]
        url = f"http://127.0.0.1:{port}"
        health = json.loads(urllib.request.urlopen(url + "/api/health", timeout=5).read())
        assert health["ok"] and health["version"] == config.VERSION
        assert "NEXUS" in urllib.request.urlopen(url + "/", timeout=5).read().decode()
        projects = json.loads(urllib.request.urlopen(url + "/api/projects", timeout=5).read())
        assert any(x["id"] == p.id for x in projects)
        assert any("recovered jobs" in str(s) for s in said)                 # the orphan was picked up at startup
        assert jobs.get(orphan)["status"] in ("queued", "failed")            # ...and handed to the new worker, not left 'running'
        assert launcher.start(port, open_browser=False, say=said.append) == 0 and "already running" in said[-1]
    finally:
        launcher.stop(say=said.append)
    assert not launcher.service_status()["api"] and not launcher.service_status()["worker"]


def test_start_fails_clearly_when_port_is_taken(home):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        said = []
        assert launcher.start(s.getsockname()[1], open_browser=False, say=said.append) == 3
        assert "already used" in said[-1] and "NEXUS_PORT" in said[-1]
