"""Path containment for project exports, project file serving and staged-upload cleanup (replaces string-prefix checks)."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from nexus.pathsafe import PathEscape, contained, is_within


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "proj"
    (root / "renders").mkdir(parents=True)
    (root / "renders" / "final.mp4").write_bytes(b"x")
    sib = tmp_path / "proj-evil"  # sibling whose name shares the root's prefix
    sib.mkdir()
    (sib / "secret.mp4").write_bytes(b"s")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"o")
    return root, sib, outside


def test_valid_relative_path_is_accepted(tree):
    root, _, _ = tree
    assert contained(root, "renders/final.mp4", must_be_file=True) == (root / "renders" / "final.mp4").resolve()


@pytest.mark.parametrize("rel", ["../outside.mp4", "renders/../../outside.mp4", "renders/../renders/final.mp4", "..",
                                 "..\\outside.mp4", "../proj-evil/secret.mp4"])
def test_parent_traversal_rejected(tree, rel):
    with pytest.raises(PathEscape):
        contained(tree[0], rel)


def test_absolute_outside_rejected(tree):
    _, sib, outside = tree
    for rel in (str(outside), str(sib / "secret.mp4"), "/etc/passwd"):
        with pytest.raises(PathEscape):
            contained(tree[0], rel)


def test_sibling_prefix_rejected_by_containment(tree):
    root, sib, _ = tree
    # the old check str(src).startswith(str(root)) accepted this
    assert str((sib / "secret.mp4").resolve()).startswith(str(root.resolve()))
    assert not is_within(sib / "secret.mp4", root)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs admin/developer mode on Windows")
def test_symlink_escape_rejected(tree):
    root, sib, outside = tree
    os.symlink(outside, root / "renders" / "link.mp4")
    os.symlink(sib, root / "renders" / "dirlink")
    with pytest.raises(PathEscape):
        contained(root, "renders/link.mp4")
    with pytest.raises(PathEscape):
        contained(root, "renders/dirlink/secret.mp4")


def test_empty_nul_and_missing_rejected(tree):
    for rel in ("", "  ", "renders/a\x00.mp4"):
        with pytest.raises(PathEscape):
            contained(tree[0], rel)
    with pytest.raises(PathEscape):
        contained(tree[0], "renders/missing.mp4", must_be_file=True)


def _job(params):
    return SimpleNamespace(params=params, stage=lambda name, fn: fn())


@pytest.mark.parametrize("bad", ["../escape.mp4", "/etc/passwd", "../{sib}/secret.mp4", "renders/link.mp4"])
def test_export_job_rejects_escaping_render(home, tmp_path, bad):
    from nexus import project as prj, workflows
    p = prj.Project.create("exp", kind="edit")
    (p.dir / "renders").mkdir(parents=True, exist_ok=True)
    sib = p.dir.parent / (p.dir.name + "-evil")
    sib.mkdir()
    (sib / "secret.mp4").write_bytes(b"s")
    (p.dir.parent / "escape.mp4").write_bytes(b"e")
    if sys.platform != "win32":
        os.symlink(p.dir.parent / "escape.mp4", p.dir / "renders" / "link.mp4")
    with pytest.raises(ValueError, match="render not found"):
        workflows.export_(_job({"project": p.id, "render": bad.format(sib=sib.name)}))
    assert not (p.dir / "exports").exists() or not any((p.dir / "exports").iterdir())


def test_export_job_valid_render_still_exports(home):
    from nexus import project as prj, workflows
    p = prj.Project.create("exp-ok", kind="edit")
    (p.dir / "renders").mkdir(parents=True, exist_ok=True)
    src = p.dir / "renders" / "final.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24:d=1", "-f", "lavfi", "-i",
                    "sine=d=1", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    rep = workflows.export_(_job({"project": p.id, "render": "renders/final.mp4", "codec": "h264", "quality": "fast"}))
    assert rep["output"] and (p.dir / rep["output"]).is_file()


def test_project_file_route_contains_paths(home, monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDED_WORKER", "0")
    monkeypatch.setenv("NEXUS_SCHEDULER_THREAD", "0")
    monkeypatch.delenv("NEXUS_API_TOKEN", raising=False)
    from fastapi.testclient import TestClient

    from nexus import api, project as prj
    p = prj.Project.create("files", kind="edit")
    (p.dir / "renders").mkdir(parents=True, exist_ok=True)
    (p.dir / "renders" / "ok.txt").write_text("ok")
    sib = p.dir.parent / (p.dir.name + "-evil")
    sib.mkdir()
    (sib / "secret.txt").write_text("secret")
    c = TestClient(api.app)
    assert c.get(f"/api/projects/{p.id}/file", params={"path": "renders/ok.txt"}).text == "ok"
    for bad in ("../" + sib.name + "/secret.txt", str(sib / "secret.txt"), "../../../etc/passwd"):
        r = c.get(f"/api/projects/{p.id}/file", params={"path": bad})
        assert r.status_code == 404 and "secret" not in r.text
