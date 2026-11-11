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
