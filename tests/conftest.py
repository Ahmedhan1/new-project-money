import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
# Tests use their own database/projects but share the downloaded model cache.
os.environ.setdefault("NEXUS_HOME", str(ROOT / ".nexus" / "test-home"))

import pytest  # noqa: E402

from nexus import config  # noqa: E402

config.MODELS = ROOT / ".nexus" / "models"
config.ensure_dirs()
RESULTS = ROOT / "docs" / "results"
RESULTS.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def fx():
    d = config.HOME / "fixtures"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def results():
    return RESULTS
