"""Account 2 producers -> Account 3 publishing, on the merged tree only (skipped on the Account 3 branch alone).
The QualityReport comes from Account 2's own nexus.qa.approve(); the GeneratedMediaManifest from Account 2's own
nexus.generate.classify.manifest(). No network, no model."""
import hashlib

import pytest

qa = pytest.importorskip("nexus.qa", reason="Account 2 QA: merged integration tree only")
if not hasattr(qa, "approve"):
    pytest.skip("Account 2 nexus.qa.approve not present", allow_module_level=True)
classify = pytest.importorskip("nexus.generate.classify", reason="Account 2 classifier: merged integration tree only")

from nexus.intelligence.publishing import engine  # noqa: E402


def _check(name, ok, severity, detail="d"):
    return {"check": name, "ok": ok, "severity": severity, "detail": detail}


@pytest.fixture()
def out(home, tmp_path):
    p = tmp_path / "final.mp4"
    p.write_bytes(b"\5" * 2048)
    return p


def _art(p):
    return {"uri": p.resolve().as_uri(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}


def test_account2_blocked_report_cannot_be_published(out, tmp_path):
    rep = qa.approve(tmp_path, "final", [{"stage": "technical", "checks": [_check("black frames", False, "critical")]}])
    assert rep["status"] == "BLOCKED"
    m = engine.create("p", _art(out), "youtube", {"title": "a2"}, release={"quality_report": rep, "provenance": {"audio": [{"kind": "tts"}]}})
    assert not m["release_check"]["upload_ok"]
    with pytest.raises(engine.PublishError, match="BLOCKED"):
        engine.enqueue(m["id"])


def test_account2_passed_report_allows_private_and_keeps_findings(out, tmp_path):
    rep = qa.approve(tmp_path, "final", [{"stage": "audio", "checks": [_check("loudness", False, "minor", "-15.3 LUFS")]}])
    assert rep["status"] == "PASSED_AUTOMATED_CHECKS" and rep["human_approval"].startswith("PENDING")
    gm = classify.manifest("image", {"model": "bk-sdm-tiny", "task": "image"}, out, compute={"device": "cpu"}, path_check=False)
    m = engine.create("p", _art(out), "youtube", {"title": "a2 ok"}, visibility="public",
                      release={"quality_report": rep, "provenance": {"media": [gm]}})
    rc = m["release_check"]
    assert rc["upload_ok"] and not rc["public_ok"]                                  # human approval still PENDING
    assert rc["disclosure"]["categories"] == ["ai_generated_imagery"] and rc["disclosure"]["flag"] is True
    assert rc["quality"]["report"]["findings"][0]["check"] == "loudness"
    assert rc["quality"]["report"]["known_issues"]


def test_account2_artifact_helper_uri_is_resolved(out, tmp_path):
    from nexus import contracts
    ref = contracts.artifact(out, role="master", media_type="video/mp4")
    rep = qa.approve(tmp_path, "final", [])
    m = engine.create("p", ref, "youtube", {"title": "a2 ref"}, release={"quality_report": rep, "provenance": {"audio": [{"kind": "tts"}]}})
    assert m["release_check"]["upload_ok"], m["release_check"]["blockers"]
