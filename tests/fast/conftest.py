"""Fast tests: no models, no network, no GPU. They run in CI on Linux and Windows in seconds."""
import os

import pytest

# No background threads in the test process: importing nexus.api would otherwise start the embedded job worker (and the
# scheduler loop), which then claims jobs that a test hands to its own worker subprocess. That made
# test_worker_killed_mid_job_resumes_without_repeating_stages fail on CI, depending on test order. Tests that need a
# worker start one explicitly.
os.environ.setdefault("NEXUS_EMBEDDED_WORKER", "0")
os.environ.setdefault("NEXUS_SCHEDULER_THREAD", "0")

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
    opt = session.config.option
    # only a complete run is a result: never record a filtered (-k/-m) or partially deselected run
    if tr is None or session.config.args != ["tests/fast"] or getattr(opt, "keyword", "") or getattr(opt, "markexpr", "") or tr.stats.get("deselected"):
        return
    n = {k: len([r for r in tr.stats.get(k, []) if getattr(r, "when", "call") == "call" or k != "passed"]) for k in ("passed", "failed", "error", "skipped")}
    out = Path(__file__).resolve().parents[2] / "docs" / "results" / "fast_tests.json"
    out.write_text(json.dumps({"summary": f"{n['passed']} passed, {n['failed'] + n['error']} failed, {n['skipped']} skipped",
                               "ok": exitstatus == 0, "date": time.strftime("%Y-%m-%d"), "platform": platform.system(),
                               "python": platform.python_version()}, indent=1) + "\n")


@pytest.fixture()
def verified_free_provider(monkeypatch):
    """A catalogue with one extra hosted_free provider whose zero-spend facts are verified (payment method false, dated
    verification, numeric quotas, listed models). Real providers stay as catalogued. No network is ever touched."""
    from nexus import costguard
    from nexus.intelligence import capability_registry as reg
    real = reg.providers
    fake = {"id": "test-free", "name": "verified free test provider", "kind": "openai_compatible", "hosting": "hosted_free",
            "base_url": "https://free.test.invalid/v1", "host": "free.test.invalid", "privacy": "public", "commercial_use": "yes",
            "payment_method_required": False, "verified": "2026-11-13 (test fixture)",
            "quotas": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000},
            "models": [{"model": m} for m in ("big", "m", "free-model")]}
    monkeypatch.setattr(reg, "providers", lambda: {**real(), "test-free": fake})
    monkeypatch.setitem(costguard.FREE_TIER_HOSTS, "free.test.invalid", "test-free")
    return fake
