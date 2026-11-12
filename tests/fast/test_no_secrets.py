"""No secret in the repository: token scan of tracked files, .gitignore coverage of credential files, and a positive
control proving the scanner actually finds tokens (so a silently broken scanner cannot pass)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus import vault

ROOT = Path(__file__).resolve().parents[2]
GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None or not (ROOT / ".git").exists(), reason="needs a git checkout")

# Paths that must be ignored. CORE is enforced; CREDENTIALS are recommended lines for the lead's .gitignore.
CORE = [".nexus/nexus.db", ".nexus/secrets/vault.key", ".nexus/secrets/drive_a.secret", ".env", ".env.local"]
CREDENTIALS = ["drive_a.secret", "kaggle.json", "client_secret_123.apps.googleusercontent.com.json",
               "scripts/client_secret.json"]


def _ignored(rel: str) -> bool:
    return subprocess.run([GIT, "check-ignore", "-q", "--no-index", rel], cwd=ROOT).returncode == 0


def test_tracked_files_contain_no_tokens():
    hits = vault.scan_repo(ROOT, history=False)
    assert hits == [], f"token-shaped strings in tracked files (rotate them, then remove): {hits}"


def test_no_credential_files_tracked():
    files = subprocess.run([GIT, "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    bad = [f for f in files if Path(f).name in {"kaggle.json", "vault.key", "token.json", "credentials.json", ".env"}
           or Path(f).suffix == ".secret" or Path(f).name.startswith("client_secret")]
    assert bad == []


def test_gitignore_core():
    missing = [p for p in CORE if not _ignored(p)]
    assert missing == [], f".gitignore must cover {missing}"


def test_gitignore_credentials():
    missing = [p for p in CREDENTIALS if not _ignored(p)]
    if missing:  # .gitignore is the lead's file; reported, not silently passed
        pytest.xfail(f".gitignore should add: *.secret, kaggle.json, client_secret*.json (not covered: {missing})")


def test_scanner_positive_control(tmp_path):
    """A fake token committed to a scratch repo is found, in the worktree and in history, and never echoed in full."""
    repo = tmp_path / "r"
    repo.mkdir()
    fake = "KGAT_" + "0123456789abcdef" * 2          # built at runtime: never present in this file as one string
    gh = "ghp_" + "Z" * 36
    (repo / "notebook.ipynb").write_text(f'{{"cells": ["token = \'{fake}\'"]}}', encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run([GIT, *cmd], cwd=repo, check=True, env=env)
    (repo / "notebook.ipynb").write_text("{}", encoding="utf-8")
    (repo / "conf.py").write_text(f"TOKEN = '{gh}'\n", encoding="utf-8")
    for cmd in (["add", "."], ["commit", "-qm", "y"]):
        subprocess.run([GIT, *cmd], cwd=repo, check=True, env=env)
    now = vault.scan_repo(repo, history=False)
    assert [h["file"] for h in now] == ["conf.py"]
    hist = vault.scan_repo(repo, history=True)
    assert any(h["where"] == "history" for h in hist) and len(hist) >= 3
    for h in hist:
        assert fake not in str(h) and gh not in str(h)
