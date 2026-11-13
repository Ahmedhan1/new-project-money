"""Release gate: Account 2 QualityReport -> publishing, AI-content disclosure, language review. No network.
QualityReport / GeneratedMediaManifest documents use the shapes Account 2 produces (nexus/qa.py approve(),
nexus/generate/classify.py); on the merged tree Account 2's own validator is used."""
import hashlib
import json

import pytest

from nexus.intelligence.language import review
from nexus.intelligence.publishing import creds, engine, release_gate as rg
from nexus.intelligence.publishing.adapters import gated, youtube as yt
from nexus.intelligence.publishing.adapters.base import FakeTransport, Response

PENDING = "PENDING - automated checks never replace a human look at the final video"


@pytest.fixture()
def clip(home):
    p = home / "final.mp4"
    p.write_bytes(b"\1" * 1000)
    return p


def art(p):
    return {"uri": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size, "media_type": "video/mp4"}


def qr(p, status="PASSED_AUTOMATED_CHECKS", human=PENDING, checks=None, output=None):
    return {"schema": "nexus.QualityReport/1", "output": output or p.name, "status": status, "score": 90.0, "human_approval": human,
            "stages": [{"stage": "technical", "checks": checks or [{"check": "duration", "ok": True, "severity": "major", "detail": "ok"}]}],
            "known_issues": []}


PROV_AI = {"media": [{"classification": "generated-image"}], "audio": [{"kind": "tts"}]}
DECISION = {"by": "owner", "contains_synthetic_media": True, "reason": "generated stills and a TTS voice"}


def rel(p, **kw):
    base = {"quality_report": qr(p), "provenance": PROV_AI, "disclosure_decision": DECISION}
    return {**base, **kw}


# ------------------------------------------------------------------ 1. QualityReport gate
@pytest.mark.parametrize("make, why", [
    (lambda p: None, "no QualityReport"),
    (lambda p: qr(p, status="BLOCKED"), "status is BLOCKED"),
    (lambda p: qr(p, status="NEEDS_FIXES"), "status is NEEDS_FIXES"),
    (lambda p: qr(p, status="LOOKS_FINE"), "not one Account 2 produces"),
    (lambda p: qr(p, checks=[{"check": "black frames", "ok": False, "severity": "critical", "detail": "12 s black"}]), "critical"),
    (lambda p: qr(p, output="another_render.mp4"), "is for output"),
    (lambda p: {"output": p.name, "status": "PASSED_AUTOMATED_CHECKS"}, "invalid"),
])
def test_missing_or_failed_quality_report_never_becomes_publishable(home, clip, make, why):
    m = engine.create("p", art(clip), "youtube", {"title": "t"}, release=rel(clip, quality_report=make(clip)))
    assert m["release_check"]["upload_ok"] is False and any(why in b for b in m["release_check"]["blockers"])
    with pytest.raises(engine.PublishError, match="release gate"):
        engine.enqueue(m["id"])
    assert engine.get(m["id"])["status"] == "draft"                       # stays a draft


def test_missing_output_and_checksum_mismatch_block(home, clip):
    a = art(clip)
    m = engine.create("p", {**a, "sha256": "0" * 64}, "youtube", {"title": "t"}, release=rel(clip))
    assert any("checksum" in b for b in m["release_check"]["blockers"])
    m2 = engine.create("p", {**a, "uri": str(clip.with_name("gone.mp4"))}, "youtube", {"title": "t2"}, release=rel(clip))
    assert any("missing" in b for b in m2["release_check"]["blockers"])
    m3 = engine.create("p", {**a, "uri": "nexus://storage/x.mp4"}, "youtube", {"title": "t3"}, release=rel(clip))
    assert any("not a local file" in b for b in m3["release_check"]["blockers"])


def test_passed_report_allows_private_only_until_human_review(home, clip):
    m = engine.create("p", art(clip), "youtube", {"title": "t"}, visibility="public", release=rel(clip))
    rc = m["release_check"]
    assert rc["upload_ok"] and not rc["public_ok"] and any("human review not completed" in b for b in rc["public_blockers"])
    engine.approve(m["id"])                                                    # owner approval alone is not enough
    with pytest.raises(engine.PublishError, match="public/unlisted"):
        engine.enqueue(m["id"])
    m = engine.attach_release(m["id"], quality_report=qr(clip, human="APPROVED by owner 2026-11-13"))
    assert m["release_check"]["public_ok"] and engine.enqueue(m["id"])["status"] == "queued"
    ev = [h["event"] for h in m["history"]]
    assert ev.count("release_checked") >= 2 and "release_evidence_attached" in ev       # review history kept
    assert m["release_check"]["quality"]["report"]["human_approval"].startswith("APPROVED")


def test_findings_are_preserved_in_the_publishing_record(home, clip):
    minor = [{"check": "loudness", "ok": False, "severity": "minor", "detail": "-15.2 LUFS"}]
    m = engine.create("p", art(clip), "youtube", {"title": "t"}, release=rel(clip, quality_report=qr(clip, checks=minor)))
    assert m["release_check"]["upload_ok"]
    assert m["release_check"]["quality"]["report"]["findings"] == [{"stage": "technical", "check": "loudness", "severity": "minor",
                                                                    "detail": "-15.2 LUFS"}]


def test_file_changed_after_enqueue_is_caught_at_upload_time(home, clip):
    m = engine.create("p", art(clip), "youtube", {"title": "t"}, release=rel(clip))
    engine.enqueue(m["id"])
    clip.write_bytes(b"\2" * 1000)
    r = engine.run_one(m["id"], adapters={"youtube": object()})
    assert r["status"] == "failed" and r["tracking"]["blocked"]["code"] == "release_gate"


def test_quality_report_from_account2_validator_when_merged(clip):
    a2 = pytest.importorskip("nexus.contracts.media")
    doc = a2.make("QualityReport", output=clip.name, status="PASSED_AUTOMATED_CHECKS", stages=[], score=90.0, human_approval="PENDING")
    assert rg.quality(art(clip), doc)["upload_ok"]


# ------------------------------------------------------------------ 2. disclosure
@pytest.mark.parametrize("prov, cats", [
    ({"media": [{"classification": "generated-video"}]}, ["fully_ai_generated"]),
    ({"media": [{"classification": "generated-image"}]}, ["ai_generated_imagery"]),
    ({"media": [{"classification": "animated-still"}]}, ["ai_generated_imagery"]),
    ({"media": [{"classification": "edited-source"}]}, ["ai_assisted_editing"]),
    ({"audio": [{"kind": "dub"}]}, ["synthetic_audio"]),
    ({"audio": [{"kind": "voice_clone"}]}, ["synthetic_audio"]),
    ({"audio": [{"kind": "tts"}]}, ["ai_narration"]),
    ({"edits": ["ai_postprocessing"]}, ["human_ai_postprocessing"]),
    (None, ["unknown"]),
    ({"media": [], "audio": []}, ["unknown"]),                               # nothing recorded and nobody attested
    ({"media": [{"classification": "procedural"}], "audio": [{"kind": "human"}]}, ["unknown"]),
    ({"media": [{"classification": "deepfake"}]}, ["unknown"]),
])
def test_provenance_maps_to_disclosure_categories(prov, cats):
    d = rg.disclosure("youtube", prov)
    assert d["categories"] == cats and d["flag"] is True


def test_only_an_owner_attestation_without_ai_records_gives_non_ai():
    d = rg.disclosure("youtube", {"audio": [{"kind": "human"}], "no_ai_attested_by": "owner"})
    assert d["flag"] is False and d["no_ai_attested"] and d["public_ok"]
    bad = rg.disclosure("youtube", {"media": [{"classification": "generated-video"}], "no_ai_attested_by": "owner"})
    assert bad["flag"] is True and not bad["upload_ok"] and "refusing to label AI content as non-AI" in bad["blockers"][0]
    assert rg.disclosure("youtube", {"no_ai_attested_by": "script"})["flag"] is True    # only the owner can attest


def test_youtube_realism_is_an_owner_decision():
    d = rg.disclosure("youtube", PROV_AI)
    assert d["upload_ok"] and not d["public_ok"] and d["flag"] is True
    no_reason = rg.disclosure("youtube", PROV_AI, {"by": "owner", "contains_synthetic_media": False})
    assert not no_reason["upload_ok"]
    stylised = rg.disclosure("youtube", PROV_AI, {"by": "owner", "contains_synthetic_media": False, "reason": "cartoon style, not realistic"})
    assert stylised["flag"] is False and stylised["public_ok"]
    assert not rg.disclosure("youtube", PROV_AI, {"by": "agent", "contains_synthetic_media": True})["upload_ok"]


def test_unknown_provenance_blocks_public_but_not_private_youtube():
    d = rg.disclosure("youtube", None)
    assert d["upload_ok"] and not d["public_ok"] and d["flag"] is True


def test_platform_capability_mapping_is_sourced_and_complete():
    for fam, cap in rg.PLATFORMS.items():
        assert {"field", "representation", "source", "requires_disclosure", "human_decision_required", "adapter_supports",
                "private_mode", "unknown"} <= set(cap)
        assert cap["source"]
    for p in ("youtube", "youtube_shorts", "tiktok", "instagram_reels", "instagram_feed", "facebook", "snapchat_spotlight"):
        assert rg.PLATFORM_FAMILY[p] in rg.PLATFORMS
    assert rg.PLATFORMS["facebook"]["adapter_supports"] is False


def test_youtube_request_carries_contains_synthetic_media(home, clip, monkeypatch):
    monkeypatch.setattr("nexus.costguard.check_host", lambda url, **k: "test")
    for k in ("client_id", "client_secret", "refresh_token"):
        monkeypatch.setenv(creds.env_name(creds.secret_name("youtube", "main", k)), f"test-{k}")
    acct = engine.connect_account("youtube", "main")
    m = engine.create("p", art(clip), "youtube", {"title": "Hi"}, connection_id=acct["id"], release=rel(clip))
    engine.enqueue(m["id"])
    sess = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=Q"
    t = FakeTransport([("POST", "https://oauth2.googleapis.com/token", Response(200, {}, b'{"access_token":"at","expires_in":3599}')),
                       ("POST", yt.UPLOAD_URL, Response(200, {"Location": sess})),
                       ("PUT", sess, Response(201, {}, b'{"id":"v1","status":{"uploadStatus":"uploaded","privacyStatus":"private"}}'))])
    r = engine.process(adapters={"youtube": yt.YouTubeAdapter(t, chunk=2000)})[0]
    assert r["status"] == "uploaded"
    body = json.loads(t.calls[1]["body"])
    assert body["status"] == {**body["status"], "privacyStatus": "private", "containsSyntheticMedia": True}
    attested = engine.create("p", art(clip), "youtube", {"title": "Human"}, release=rel(clip, provenance={"no_ai_attested_by": "owner"}, disclosure_decision=None))
    assert yt.video_body(attested)["status"]["containsSyntheticMedia"] is False
    assert yt.video_body({"metadata": {}, "visibility": "private"})["status"]["containsSyntheticMedia"] is True   # no check -> AI


class Rec:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, body=None, params=None, timeout=120):
        self.calls.append({"url": url, "body": body, "params": params})
        if "init" in url:
            return Response(200, {}, b'{"data":{"upload_url":"https://open-upload.tiktokapis.com/u","publish_id":"p1"}}')
        if url.endswith("/media"):
            return Response(200, {}, b'{"id":"c1"}')
        if "status_code" in json.dumps(params or {}):
            return Response(200, {}, b'{"status_code":"IN_PROGRESS"}')
        return Response(200, {}, b"{}")


def test_tiktok_and_instagram_requests_carry_the_ai_flag(home, clip, monkeypatch):
    for p, adapter in (("tiktok", gated.TikTokAdapter), ("instagram_reels", gated.InstagramAdapter)):
        acct = engine.connect_account(p, "main", external_id="1", app_approved=True)
        monkeypatch.setenv(creds.env_name(acct["secret_ref"]), "tok")
        m = engine.create("p", art(clip), p, {"title": "t", "caption": "c"}, connection_id=acct["id"], release=rel(clip))
        t = Rec()
        adapter(t).upload(m, acct, {})
        if p == "tiktok":
            assert json.loads(t.calls[0]["body"])["post_info"]["is_aigc"] is True
        else:
            assert t.calls[0]["params"]["is_ai_generated"] == "true"


def test_facebook_refuses_ai_content_it_cannot_label(home, clip, monkeypatch):
    m = engine.create("p", art(clip), "facebook", {"title": "t"}, release=rel(clip))
    assert not m["release_check"]["upload_ok"]                               # no private mode + no field: blocked at the gate
    acct = engine.connect_account("facebook", "main", app_approved=True)
    from nexus.intelligence.publishing.adapters.base import AdapterBlocked
    with pytest.raises(AdapterBlocked, match="AI disclosure"):
        gated.FacebookAdapter(Rec()).preflight(m, acct)


# ------------------------------------------------------------------ 3. language review
def lang_rec(lang, dialect=None, items=None, **kw):
    return {"language": lang, "dialect": dialect, "review_items": items or [], **kw}


def test_egyptian_output_without_review_stays_draft_and_private(home, clip):
    m = engine.create("p", art(clip), "youtube", {"title": "t", "language": "ar-EG"}, visibility="public",
                      release=rel(clip, quality_report=qr(clip, human="APPROVED"), language=lang_rec("ar", "EG")))
    lc = m["release_check"]["language"]
    assert lc["tag"] == "ar-EG" and lc["review_required"] and lc["reviewer_approval"] == "not_submitted"
    assert lc["state"] == "draft_needs_native_review" and m["release_check"]["upload_ok"] and not m["release_check"]["public_ok"]
    engine.approve(m["id"])
    with pytest.raises(engine.PublishError, match="native review"):
        engine.enqueue(m["id"])


def test_gulf_review_does_not_clear_egyptian_output(home, clip):
    ids = review.submit("ar-Gulf", [{"source": "It is tall.", "target": "طويل واجد"}])
    review.record(ids[0], "approve", "reviewer-gulf")
    r = rg.language(lang_rec("ar", "EG", ids), "ar-EG")
    assert not r["public_ok"] and any("not interchangeable" in b for b in r["public_blockers"])


def test_approved_native_review_for_the_same_variety_clears_language(home, clip):
    ids = review.submit("ar-EG", [{"source": "It is tall.", "target": "طويل قوي"}, {"source": "Look.", "target": "بص"}])
    for i in ids:
        review.record(i, "approve", "reviewer-eg")
    r = rg.language(lang_rec("ar", "EG", ids), "ar-EG")
    assert r["public_ok"] and r["reviewer_approval"] == "approved" and r["state"] == "ready"
    pend = review.submit("ar-EG", [{"source": "x", "target": "y"}])
    assert rg.language(lang_rec("ar", "EG", ids + pend), "ar-EG")["reviewer_approval"] == "pending"
    review.record(pend[0], "reject", "reviewer-eg")
    assert rg.language(lang_rec("ar", "EG", ids + pend), "ar-EG")["reviewer_approval"] == "rejected"


def test_no_fake_approval_from_unknown_review_ids(home):
    r = rg.language(lang_rec("hi", None, ["rev_doesnotexist"]), "hi")
    assert not r["upload_ok"] and r["reviewer_approval"] == "invalid"


@pytest.mark.parametrize("rec, ok_upload, ok_public", [
    (lang_rec("zh", translation_qa={"ok": False, "detail": "number mismatch"}), True, False),
    (lang_rec("de", ocr_glyph={"ok": False, "detail": "tofu"}), True, False),
    (lang_rec("de", voice={"licence_ok": False}), False, False),
    (lang_rec("de", voice={"licence_ok": True, "consent_problems": ["consent expired"]}), False, False),
    (lang_rec("ar", "Levant"), False, False),
    (lang_rec("en"), True, True),
])
def test_language_record_checks(home, rec, ok_upload, ok_public):
    r = rg.language(rec)
    assert (r["upload_ok"], r["public_ok"]) == (ok_upload, ok_public)


def test_localized_metadata_without_a_language_record_is_draft_only():
    assert not rg.language(None, "hi")["public_ok"] and rg.language(None, "en")["public_ok"]


# ------------------------------------------------------------------ API
def test_api_create_attach_and_check(home, clip):
    from fastapi.testclient import TestClient

    from nexus.api import app
    c = TestClient(app)
    r = c.post("/api/a3/publish", json={"project_id": "p", "file": str(clip), "platform": "youtube", "metadata": {"title": "api"},
                                        "visibility": "public", "release": {"quality_report": qr(clip), "provenance": PROV_AI}})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    assert r.json()["artifact"]["sha256"] == hashlib.sha256(clip.read_bytes()).hexdigest()
    chk = c.get(f"/api/a3/publish/{mid}/release").json()
    assert chk["upload_ok"] and not chk["public_ok"]
    r = c.post(f"/api/a3/publish/{mid}/release", json={"quality_report": qr(clip, human="APPROVED"), "disclosure_decision": DECISION})
    assert r.status_code == 200 and r.json()["release_check"]["public_ok"]


def test_no_platform_is_advertised_as_live_verified():
    from nexus.intelligence.publishing import capabilities
    r = capabilities.readiness()
    assert set(r) == set(capabilities.PLATFORMS)
    assert not any(x["live_verified"] for x in r.values())
    assert r["snapchat_spotlight"]["primary"] == "manual_export_only"
    assert all(r[p]["primary"] == "requires_owner_authorization" for p in ("youtube", "tiktok", "instagram_reels", "facebook"))
