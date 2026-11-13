"""Reproducible model benchmark (wrapper). Run from the repo root:

    NEXUS_HOME=/home/user/new-project-money/.nexus .venv/bin/python scripts/account3/models_bench.py

Same as `python -m nexus.intelligence.model_router bench`; see nexus/intelligence/model_router/bench.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nexus.intelligence.model_router import bench  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(bench.main(sys.argv[1:]))
