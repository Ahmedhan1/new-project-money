"""Versioned NEXUS worker kit: the sources that run on remote workers (Kaggle kernel, owner GPU PC, Colab, local CPU).

    nexus_worker.py   the worker (standalone; built-in tasks probe, torch_bench, ffmpeg_transcode, chunked_transcode)
    tasks/*.py        extra tasks from Accounts 2/3, appended to the worker when a script is built (see tasks/__init__.py)
    pull_worker.py    the pull client the owner runs on their own GPU PC / in Colab (user_gpu and colab providers)

Job manifest transport (decision): the job bundle is EMBEDDED as base64 JSON in the single script pushed with the
official `kaggle kernels push` (kernel_type=script). This is the simplest robust official route: one push call, no
dataset versioning race, nothing public. Inputs up to INLINE_MAX bytes travel inline; larger inputs must be https URLs
the worker downloads itself (Kaggle kernels have internet when enable_internet is true). The bundle never holds secrets.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = HERE / "nexus_worker.py"
PULL_WORKER = HERE / "pull_worker.py"
INLINE_MAX = 4 * 2 ** 20  # bytes of inline input per bundle


def worker_version() -> str:
    m = re.search(r'^WORKER_VERSION = "([^"]+)"', WORKER.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "unknown"


def extra_task_files() -> list[Path]:
    return sorted(p for p in (HERE / "tasks").glob("*.py") if not p.name.startswith("_"))


def build_script(bundle: dict | None = None) -> str:
    """The single-file worker: core + extra task files, with `bundle` embedded (None = no embedded bundle)."""
    src = WORKER.read_text(encoding="utf-8")
    extra = "\n\n".join(f"# ---- task file {p.name}\n" + p.read_text(encoding="utf-8") for p in extra_task_files())
    src = src.replace("# NEXUS_EXTRA_TASKS\n", (extra + "\n\n" if extra else "") + "# NEXUS_EXTRA_TASKS\n", 1)
    if bundle is not None:
        b64 = base64.b64encode(json.dumps(bundle, separators=(",", ":")).encode()).decode()
        src = src.replace('_EMBEDDED_BUNDLE_B64 = ""', f'_EMBEDDED_BUNDLE_B64 = "{b64}"', 1)
    return src


def registered_worker_tasks() -> list[str]:
    names = re.findall(r'^@task\("([a-z][a-z0-9_]+)"\)', build_script(), re.M)
    return sorted(set(names))
