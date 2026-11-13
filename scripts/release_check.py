"""Release check: verify a fresh clone of a commit, not this working tree.

    python scripts/release_check.py [--ref HEAD] [--keep]

1. `git clone` this repository into a temporary folder and check out the ref (only committed files exist there).
2. Repository hygiene: no tracked file > 2 MB, no media/model/database files, no obvious secrets.
3. `ruff check` and the fast test suite inside the clone, with a fresh NEXUS_HOME.
Writes docs/results/release_check.json in THIS tree. Uses the current Python environment (dependency installation is
covered by CI and by NEXUS_SETUP on Windows).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIG = 2 * 1024 * 1024
BANNED_EXT = {".mp4", ".mov", ".mkv", ".wav", ".mp3", ".onnx", ".safetensors", ".bin", ".gguf", ".pt", ".ckpt", ".db", ".sqlite"}
SECRET = re.compile(r"(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{32,}|-----BEGIN (RSA |EC )?PRIVATE KEY-----|xox[bp]-[A-Za-z0-9-]{20,})")


def run(cmd, cwd, env=None, timeout=3600):
    t = time.time()
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)[-4000:], round(time.time() - t, 1)


def main() -> int:
    ref = sys.argv[sys.argv.index("--ref") + 1] if "--ref" in sys.argv else "HEAD"
    sha = subprocess.run(["git", "rev-parse", ref], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="nexus_release_"))
    clone = tmp / "repo"
    report: dict = {"ref": ref, "commit": sha, "started_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "python": sys.version.split()[0], "checks": {}}
    try:
        rc, out, s = run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(clone)], tmp)
        rc2, out2, _ = run(["git", "checkout", "-q", sha], clone)
        report["checks"]["clone"] = {"ok": rc == 0 and rc2 == 0, "seconds": s, "log": (out + out2)[-500:]}
        files = subprocess.run(["git", "ls-files", "-z"], cwd=clone, capture_output=True, text=True).stdout.split("\0")
        files = [f for f in files if f]
        big = [f for f in files if (clone / f).is_file() and (clone / f).stat().st_size > BIG]
        banned = [f for f in files if Path(f).suffix.lower() in BANNED_EXT]
        secrets = []
        for f in files:
            p = clone / f
            if p.is_file() and p.stat().st_size < 1_000_000:
                try:
                    if SECRET.search(p.read_text(encoding="utf-8", errors="ignore")):
                        secrets.append(f)
                except OSError:
                    pass
        report["checks"]["hygiene"] = {"ok": not (big or banned or secrets), "tracked_files": len(files),
                                       "over_2mb": big, "media_or_model_files": banned, "possible_secrets": secrets}
        env = {**os.environ, "NEXUS_HOME": str(tmp / "home"), "NEXUS_EMBEDDED_WORKER": "0", "PYTHONPATH": str(clone)}
        rc, out, s = run([sys.executable, "-m", "ruff", "check", "."], clone, env)
        report["checks"]["ruff"] = {"ok": rc == 0, "seconds": s, "log": out[-800:]}
        rc, out, s = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/fast"], clone, env, timeout=3600)
        tail = [ln for ln in out.splitlines() if re.search(r"\d+ (passed|failed)", ln)]
        report["checks"]["fast_tests"] = {"ok": rc == 0, "seconds": s, "summary": tail[-1] if tail else out[-300:]}
    finally:
        if "--keep" not in sys.argv:
            shutil.rmtree(tmp, ignore_errors=True)
    report["ok"] = all(c.get("ok") for c in report["checks"].values())
    out = ROOT / "docs" / "results" / "release_check.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: (v.get("ok"), v.get("summary") or v.get("seconds")) for k, v in report["checks"].items()}, indent=1))
    print("RELEASE CHECK:", "PASS" if report["ok"] else "FAIL", sha[:7])
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
