"""QA-D10: the clean-machine e2e must assert the export's real verification report, not keys export() never returns."""
import ast
from pathlib import Path


def test_qa_d10_windows_e2e_asserts_verify_ok_true():
    src = (Path(__file__).resolve().parents[2] / "scripts" / "acceptance" / "windows_e2e.py").read_text(encoding="utf-8")
    ast.parse(src)
    assert 'ver.get("ok") is True' in src and 'res.get("verify")' in src
    assert "ver is not False" not in src


def test_export_returns_verify_ok(home, tmp_path):
    import subprocess

    from nexus import export
    src = tmp_path / "in.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24:d=1", "-f", "lavfi", "-i", "sine=d=1",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    rep = export.export(src, tmp_path / "out.mp4", codec="h264", quality="fast")
    assert rep["verify"]["ok"] is True and rep["ok"] is True   # the keys the e2e step now asserts
