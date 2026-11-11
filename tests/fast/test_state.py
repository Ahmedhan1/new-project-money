"""Acceptance test B - interrupted agent session: everything needed to resume lives in Git-tracked files."""
import json
import subprocess

import pytest

from nexus import state


def test_task_registry_and_state_file_are_valid():
    tasks = state.load_tasks()
    assert state.validate_tasks(tasks) == []
    nxt = state.next_tasks(tasks)
    assert nxt and all(t["status"] in state.OPEN for t in nxt)
    by = {t["id"]: t for t in tasks}
    for t in nxt:                                         # never proposes work whose dependencies are still open
        assert all(by[d]["status"] not in ("planned", "in_progress", "blocked") for d in t.get("deps", []))
    s = state.build_state()
    assert state.validate_state(s) == []
    committed = json.loads(state.STATE_FILE.read_text(encoding="utf-8"))
    assert state.validate_state(committed) == [], "NEXUS_STATE.json in the repo is invalid - run `python -m nexus checkpoint`"
    handoff = state.HANDOFF_FILE.read_text(encoding="utf-8")
    assert state.AUTO_BEGIN in handoff and len(handoff.split()) < 1500, "keep NEXUS_HANDOFF.md under 1,500 words"
    assert "Read NEXUS_HANDOFF.md and NEXUS_STATE.json before making any changes" in (state.ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_verified_needs_evidence_and_deps_must_exist():
    bad = [{"id": "X-1", "priority": 1, "title": "t", "status": "verified", "acceptance": "a", "next": "n"},
           {"id": "X-2", "priority": 1, "title": "t", "status": "done", "acceptance": "a", "next": "n", "deps": ["NOPE"]}]
    errs = " ".join(state.validate_tasks(bad))
    assert "requires evidence" in errs and "bad status" in errs and "unknown dependency NOPE" in errs


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    monkeypatch.setattr(state, "ROOT", r)
    monkeypatch.setattr(state, "STATE_FILE", r / "NEXUS_STATE.json")
    monkeypatch.setattr(state, "HANDOFF_FILE", r / "NEXUS_HANDOFF.md")
    for a in (["init", "-q", "-b", "work"], ["config", "user.email", "t@example.org"], ["config", "user.name", "t"],
              ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", str(r), *a], check=True)
    (r / "NEXUS_HANDOFF.md").write_text("# Handoff\n\nhand-written part\n")
    (r / "app.py").write_text("print('hi')\n")
    return r


def _log(r):
    return subprocess.run(["git", "-C", str(r), "log", "--oneline"], capture_output=True, text=True).stdout.splitlines()


def test_checkpoint_commits_state_and_survives_no_network(repo):
    out = state.checkpoint("first checkpoint")
    assert out["ok"] and out["committed"] and not out["pushed"] and "no git remote" in out["push"]["reason"]
    assert len(_log(repo)) == 1
    s = json.loads((repo / "NEXUS_STATE.json").read_text())
    assert state.validate_state(s) == [] and s["next_tasks"]
    h = (repo / "NEXUS_HANDOFF.md").read_text()
    assert "hand-written part" in h and state.AUTO_BEGIN in h and s["next_tasks"][0]["id"] in h
    # a brand-new session: the brief is built from files only
    brief = state.resume_brief()
    assert s["next_tasks"][0]["id"] in brief and "NEXUS_HANDOFF.md" in brief
    # second checkpoint rewrites the auto block instead of appending another
    (repo / "app.py").write_text("print('v2')\n")
    assert state.checkpoint("second")["committed"]
    assert (repo / "NEXUS_HANDOFF.md").read_text().count(state.AUTO_BEGIN) == 1


def test_checkpoint_refuses_secrets_media_and_large_files(repo):
    assert state.checkpoint("base")["committed"]
    token = "ghp_" + "a1B2c3D4e5" * 4
    (repo / "config.py").write_text(f'GITHUB = "{token}"\n')
    (repo / "final.mp4").write_bytes(b"\x00" * 100)
    (repo / "model.gguf").write_bytes(b"\x00" * 100)
    (repo / "notes.txt").write_text("x" * (state.MAX_FILE_BYTES + 1))
    (repo / ".env").write_text("A=1\n")
    (repo / "frame.png").write_bytes(b"\x89PNG\r\n" + bytes(range(256)))     # small evidence image: allowed
    out = state.checkpoint("should not happen")
    assert not out["ok"] and not out["committed"]
    joined = " ".join(out["blocked"])
    for needle in ("config.py: looks like a GitHub token", "final.mp4", "model.gguf", "notes.txt", ".env"):
        assert needle in joined, needle
    assert "frame.png" not in joined and len(_log(repo)) == 1                # nothing was committed
    for f in ("config.py", "final.mp4", "model.gguf", "notes.txt", ".env"):
        (repo / f).unlink()
    assert state.checkpoint("clean again")["committed"]


def test_push_never_forces_over_someone_elses_work(repo, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
    assert state.checkpoint("one")["pushed"]
    other = tmp_path / "other"                                               # another agent pushes first
    subprocess.run(["git", "clone", "-q", "-b", "work", str(remote), str(other)], check=True)
    (other / "theirs.txt").write_text("their work\n")
    for a in (["add", "-A"], ["-c", "user.email=o@example.org", "-c", "user.name=o", "-c", "commit.gpgsign=false", "commit", "-qm", "theirs"], ["push", "-q"]):
        subprocess.run(["git", "-C", str(other), *a], check=True)
    (repo / "app.py").write_text("print('mine')\n")
    out = state.checkpoint("two")
    assert out["committed"] and not out["pushed"] and "never force-pushes" in out["push"]["reason"]
    theirs = subprocess.run(["git", "-C", str(remote), "log", "--oneline", "work"], capture_output=True, text=True).stdout
    assert "theirs" in theirs                                                # their commit is still on the remote
