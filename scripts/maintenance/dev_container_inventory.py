"""Disk inventory of the development container (the disk-full incident of the Account 2 demo run).

Every location is listed with its owner, purpose and the decision taken, BEFORE anything is deleted. The same
script produces the before and the after report, so both are measured the same way:

    python scripts/maintenance/dev_container_inventory.py --label before --out docs/results/a2_storage/disk_before.json
    python scripts/maintenance/dev_container_inventory.py --label after  --out docs/results/a2_storage/disk_after.json

This describes the shared cloud development container only, not the owner's computer. On the owner's machine the
same categories are measured by `python -m nexus disk` (nexus/diskman.py), which only ever touches NEXUS_HOME.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRATCH = Path(os.environ.get("NEXUS_SCRATCH", "/tmp/claude-0"))
MEDIA = {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".wav", ".mp3", ".aac", ".flac", ".ogg", ".png", ".jpg", ".jpeg"}

# (path, category, owner, purpose, decision). Decision vocabulary: keep | clean-generated | delete (results recorded)
LOCATIONS: list[tuple[str, str, str, str, str]] = [
    (str(REPO / ".nexus/models"), "model weights", "NEXUS install (scripts/setup.sh)", "Whisper, Piper, LLM, MADLAD, SigLIP, faces - used by the app and heavy tests", "keep"),
    (str(REPO / ".nexus/benchmarks"), "benchmark media", "Account 2 (scripts/benchmarks/media.yaml)", "licensed NASA/Prelinger benchmark set with provenance; heavy tests and demos read it", "keep"),
    (str(REPO / ".nexus/test-home"), "test home", "tests/conftest.py", "NEXUS_HOME of the fast suite; projects/cache accumulate between runs", "clean-generated"),
    (str(REPO / ".nexus/cache"), "render cache", "dev NEXUS_HOME", "segment/overlay cache of the development home", "keep (small, bounded by diskman)"),
    (str(REPO / ".nexus/projects"), "projects", "dev NEXUS_HOME", "development projects", "keep"),
    (str(REPO / ".nexus/assets"), "asset library", "dev NEXUS_HOME", "installed LUTs/assets", "keep"),
    (str(REPO / ".venv"), "python environment", "developer", "virtualenv with requirements.txt", "keep"),
    (str(REPO / ".git"), "git", "repository", "history", "keep"),
    ("/root/.cache/pip", "package cache", "pip", "downloaded wheels; re-downloadable", "delete (pip cache purge)"),
    ("/root/.cache/uv", "package cache", "uv", "uv wheel cache; re-downloadable", "keep (small)"),
    (str(SCRATCH), "agent scratch", "this agent session", "benchmark homes and renders of the Account 2 render benchmark (results in docs/results/a2_render_bench.json)", "delete media + homes"),
    ("/tmp/a2-d", "workstream workdir", "Account 2 Agent D", "generation/enhancement bench + a 928 MB test venv (results in ACCOUNT2_MODEL_BENCHMARKS.md)", "delete (results recorded)"),
    ("/tmp/a2-agent-a", "workstream workdir", "Account 2 Agent A", "renderer bench homes and frame grabs (results in a2_render_bench.json)", "delete (results recorded)"),
    ("/tmp/a2-c", "workstream workdir", "Account 2 Agent C", "audio acceptance workdir (results in a2_audio_acceptance.json)", "delete (results recorded)"),
    ("/tmp/a2-demos", "demo home", "Account 2 integrator", "Demo 1/2/4 projects, final renders, audio review package", "clean-generated (keep finals + review package)"),
    ("/tmp/a2-demo3", "demo home", "Account 2 integrator", "Demo 3 first run (rejected: mask region)", "delete (superseded by a2-demo3b)"),
    ("/tmp/a2-demo3b", "demo home", "Account 2 integrator", "Demo 3 accepted run (docs/results/a2_demos/demo3_advanced.json)", "clean-generated (keep final)"),
    ("/tmp/a2-demo5", "demo home", "Account 2 integrator", "Demo 5 deferred GPU jobs", "keep (small)"),
    ("/tmp/nx-smoke", "smoke home", "earlier sessions", "release-candidate smoke runs", "delete (results recorded)"),
    ("/tmp/nx-ws", "smoke home", "earlier sessions", "workspace UI smoke runs", "delete (results recorded)"),
    ("/tmp/nx-director", "smoke home", "earlier sessions", "director smoke runs", "delete (results recorded)"),
    ("/tmp/a2-full", "test home", "Account 2 integrator", "three-account merge test home", "delete (results in a2_three_account_check.json)"),
    ("/tmp/a2-merge-home", "test home", "Account 2 integrator", "three-account merge test home", "delete (results recorded)"),
    ("/tmp/a2-trial-home", "test home", "Account 2 integrator", "trial home", "delete"),
    ("/tmp/pytest-of-root", "test outputs", "pytest", "tmp_path directories kept from the last 3 runs (rendered test videos)", "delete; pytest now keeps only failed runs' tmp dirs"),
    ("/tmp/explainer75.mp4", "temporary media", "Account 2 integrator", "reference excerpt for Demo 4 (re-cut from the benchmark media)", "delete"),
]


def walk(p: Path) -> dict:
    total = files = media_bytes = 0
    big: list[tuple[int, str]] = []
    if p.is_file():
        st = p.lstat()
        return {"bytes": st.st_size, "files": 1, "media_bytes": st.st_size if p.suffix.lower() in MEDIA else 0, "largest": [[st.st_size, str(p)]]}
    stack = [p]
    while stack:
        d = stack.pop()
        try:
            it = list(os.scandir(d))
        except OSError:
            continue
        for e in it:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                    continue
                s = e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            total += s
            files += 1
            if os.path.splitext(e.name)[1].lower() in MEDIA:
                media_bytes += s
            big.append((s, e.path))
    big.sort(reverse=True)
    return {"bytes": total, "files": files, "media_bytes": media_bytes, "largest": [[s, f] for s, f in big[:5]]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="before")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    du = shutil.disk_usage("/")
    rows = []
    for path, cat, owner, purpose, decision in LOCATIONS:
        p = Path(path)
        r = {"path": path, "category": cat, "owner": owner, "purpose": purpose, "decision": decision, "exists": p.exists()}
        if p.exists():
            r.update(walk(p))
        rows.append(r)
    rep = {"label": a.label, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "machine": "shared cloud development container",
           "disk": {"filesystem_size_gb": round(du.total / 1e9, 1), "free_gb": round(du.free / 1e9, 2),
                    "note": "the container's writable allowance is smaller than the filesystem; 'free' is what is really writable"},
           "total_listed_gb": round(sum(r.get("bytes", 0) for r in rows) / 1e9, 2), "locations": rows}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
    for r in sorted(rows, key=lambda r: -r.get("bytes", 0)):
        print(f"{r.get('bytes', 0) / 1e9:7.2f} GB  {r['category']:<20} {r['path']:<60} {r['decision']}")
    print(f"free: {rep['disk']['free_gb']} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
