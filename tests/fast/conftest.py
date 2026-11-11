"""Fast tests: no models, no network, no GPU. They run in CI on Linux and Windows in seconds."""
import pytest

from nexus import config, db


@pytest.fixture()
def home(tmp_path, monkeypatch):
    old = config.HOME
    h = tmp_path / "home"
    db.reset_for_tests()
    config.set_home(h)
    config.ensure_dirs()
    monkeypatch.setenv("NEXUS_HOME", str(h))
    monkeypatch.delenv("NEXUS_BACKUP_PASSPHRASE", raising=False)
    yield h
    db.reset_for_tests()
    config.set_home(old)


def pytest_sessionfinish(session, exitstatus):
    """Record the fast-suite result where `nexus checkpoint` picks it up for NEXUS_STATE.json (full runs only)."""
    import json
    import platform
    import time
    from pathlib import Path
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr is None or session.config.args != ["tests/fast"]:
        return
    n = {k: len([r for r in tr.stats.get(k, []) if getattr(r, "when", "call") == "call" or k != "passed"]) for k in ("passed", "failed", "error", "skipped")}
    out = Path(__file__).resolve().parents[2] / "docs" / "results" / "fast_tests.json"
    out.write_text(json.dumps({"summary": f"{n['passed']} passed, {n['failed'] + n['error']} failed, {n['skipped']} skipped",
                               "ok": exitstatus == 0, "date": time.strftime("%Y-%m-%d"), "platform": platform.system(),
                               "python": platform.python_version()}, indent=1) + "\n")
