"""Account 3 / E: capability matrix, publishing engine, YouTube adapter (recorded responses), packaging, analytics import.
No network, no models."""
import json
from pathlib import Path

import pytest

from nexus.intelligence import contracts as C
from nexus.intelligence.publishing import analytics, capabilities, creds, engine, packaging
from nexus.intelligence.publishing.adapters import youtube as yt
from nexus.intelligence.publishing.adapters.base import AdapterBlocked, FakeTransport, Response, TransientError

FIX = Path(__file__).parent / "fixtures_publishing"


@pytest.fixture()
def clip(home):
    p = home / "clip.mp4"
    p.write_bytes(b"\0" * 1000)
    return p


def _art(p):
    return {"uri": str(p), "sha256": "a" * 64, "bytes": p.stat().st_size, "media_type": "video/mp4"}


def _yt_env(monkeypatch, label="main"):
    for k in ("client_id", "client_secret", "refresh_token"):
        monkeypatch.setenv(creds.env_name(creds.secret_name("youtube", label, k)), f"test-{k}")


# ------------------------------------------------------------------ matrix
def test_matrix_covers_seven_destinations_with_sources():
    m = capabilities.matrix()
    assert set(m) == set(capabilities.PLATFORMS) and len(m) == 7
    for p, row in m.items():
        assert row["default_visibility"] in ("private", "draft")
        for k, node in row["api"].items():
            for leaf in ([node] if "kind" in node else node.values()):
                if isinstance(leaf, dict) and leaf.get("kind") == "documented":
                    assert leaf["source"].startswith("https://") and leaf["verified"]
    assert m["tiktok"]["api"]["upload"]["unverified_app_visibility"]["value"].startswith("private only")
    assert m["youtube"]["api"]["quota"]["value"]["videos_insert"].startswith("1 unit")
    assert m["snapchat_spotlight"]["adapter_status"] == "manual_export_only"


def test_registry_diff_lists_corrections():
    d = capabilities.registry_diff()
    keys = {(x["platform"], x["field"]) for x in d}
    assert ("facebook", "analytics_api") in keys and ("youtube", "analytics_api") in keys


# ------------------------------------------------------------------ engine
def test_manifest_defaults_private_and_idempotent(home, clip):
    md = {"title": "T", "description": "D"}
    a = engine.create("p1", _art(clip), "youtube", md)
    assert a["visibility"] == "private" and a["status"] == "draft" and not C.validate(a)
    b = engine.create("p1", _art(clip), "youtube", dict(md))
    assert b["id"] == a["id"] and b["deduplicated"]
    c = engine.create("p1", _art(clip), "youtube", {**md, "title": "T2"})
    assert c["id"] != a["id"]
    assert engine.create("p1", _art(clip), "instagram_reels", md)["visibility"] == "draft"


def test_public_requires_owner_approval(home, clip):
    m = engine.create("p1", _art(clip), "youtube", {"title": "x"}, visibility="public")
    assert m["visibility"] == "private" and m["status"] == "awaiting_approval"
    with pytest.raises(engine.PublishError):
        engine.enqueue(m["id"])
    with pytest.raises(engine.PublishError):
        engine.approve(m["id"], approval="yes")
    m = engine.approve(m["id"])
    assert m["visibility"] == "public" and m["approval"] == "approved_by_owner"
    assert engine.enqueue(m["id"])["status"] == "queued"
    bad = dict(m, visibility="public", approval="pending")
    assert C.validate(bad)


def test_account_refuses_token_as_reference(home):
    with pytest.raises(engine.PublishError):
        engine.connect_account("youtube", "main", secret_ref="ya29" + ".a0" + "x" * 30)  # built at runtime: no token-shaped literal in Git
    a = engine.connect_account("youtube", "main")
    assert a["secret_ref"] == "pub.youtube.main.token" and "token" not in json.dumps({k: v for k, v in a.items() if k != "secret_ref"})


def test_missing_credentials_blocks_with_owner_steps(home, clip, monkeypatch):
    acct = engine.connect_account("youtube", "main")
    m = engine.create("p1", _art(clip), "youtube", {"title": "x"}, connection_id=acct["id"])
    engine.enqueue(m["id"])
    out = engine.process()
    assert out[0]["status"] == "failed"
    blk = out[0]["tracking"]["blocked"]
    assert blk["code"] == "credentials_missing" and any("console.cloud.google.com" in s for s in blk["owner_action"])
    assert engine.process() == []  # blocked jobs are not retried automatically


def test_gated_adapters_refuse_without_approved_app(home, clip):
    for p in ("tiktok", "instagram_reels", "facebook", "snapchat_spotlight"):
        acct = engine.connect_account(p, "main")
        m = engine.create("p1", _art(clip), p, {"title": "x"}, connection_id=acct["id"])
        engine.enqueue(m["id"])
    codes = {m["platform"]: m["tracking"]["blocked"]["code"] for m in engine.process()}
    assert codes == {"tiktok": "requires_approved_app", "instagram_reels": "requires_approved_app",
                     "facebook": "requires_approved_app", "snapchat_spotlight": "manual_only"}


def test_instagram_needs_owner_approval_even_with_app(home, clip, monkeypatch):
    acct = engine.connect_account("instagram_reels", "main", external_id="1784", app_approved=True)
    monkeypatch.setenv(creds.env_name(acct["secret_ref"]), "tok")
    m = engine.create("p1", _art(clip), "instagram_reels", {"caption": "x"}, connection_id=acct["id"])
    engine.enqueue(m["id"])
    assert engine.process()[0]["tracking"]["blocked"]["code"] == "needs_owner_approval"


class Flaky:
    platform = "youtube"

    def __init__(self, fails):
        self.fails = fails

    def preflight(self, m, a):
        pass

    def upload(self, m, a, ctx):
        if self.fails:
            self.fails -= 1
            raise TransientError("HTTP 503", retry_after=None)
        return {"remote_id": "v1", "remote_status": "uploaded"}


def test_retry_with_backoff_then_success(home, clip):
    m = engine.create("p1", _art(clip), "youtube", {"title": "x"})
    engine.enqueue(m["id"])
    ad = {"youtube": Flaky(1)}
    r = engine.process(now=1000.0, adapters=ad)[0]
    assert r["status"] == "queued" and r["tracking"]["due"] == 1000.0 + engine.BACKOFF_BASE_S
    assert engine.process(now=1001.0, adapters=ad) == []  # not due yet
    r = engine.process(now=1031.0, adapters=ad)[0]
    assert r["status"] == "uploaded" and [h["event"] for h in r["history"]][-1] == "uploaded"
    assert engine.backoff(10) == engine.BACKOFF_CAP_S and engine.backoff(1, retry_after=90) == 90


def test_schedule_respected(home, clip):
    m = engine.create("p1", _art(clip), "youtube", {"title": "x"}, schedule="2030-01-01T00:00:00Z")
    engine.enqueue(m["id"])
    assert engine.process(now=1.0e9, adapters={"youtube": Flaky(0)}) == []


def test_job_handler_registered(home):
    from nexus import jobs
    from nexus.intelligence.publishing import jobs_handler
    assert jobs_handler.KIND in jobs.HANDLERS


# ------------------------------------------------------------------ YouTube adapter with recorded responses
def _token():
    return ("POST", "https://oauth2.googleapis.com/token", Response(200, {}, b'{"access_token":"at","expires_in":3599}'))


def test_youtube_resumable_upload_private(home, clip, monkeypatch):
    monkeypatch.setattr("nexus.costguard.check_host", lambda url, **k: "test")
    _yt_env(monkeypatch)
    acct = engine.connect_account("youtube", "main")
    m = engine.create("p1", _art(clip), "youtube", {"title": "Hello", "description": "d", "tags": ["a"]}, connection_id=acct["id"])
    engine.enqueue(m["id"])
    sess = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=XYZ"
    t = FakeTransport([_token(),
                       ("POST", yt.UPLOAD_URL, Response(200, {"Location": sess})),
                       ("PUT", sess, Response(308, {"Range": "bytes=0-399"})),
                       ("PUT", sess, Response(201, {}, b'{"id":"vid123","status":{"uploadStatus":"uploaded","privacyStatus":"private"}}'))])
    r = engine.process(adapters={"youtube": yt.YouTubeAdapter(t, chunk=400)})[0]
    assert r["status"] == "uploaded" and r["tracking"]["remote"]["remote_id"] == "vid123"
    assert r["tracking"]["remote"]["privacy"] == "private"
    init = t.calls[1]
    assert "uploadType=resumable" in init["url"] and init["headers"]["X-Upload-Content-Length"] == "1000"
    assert t.calls[3]["headers"]["Content-Range"] == "bytes 400-799/1000"
    assert yt.quota_status()["videos_insert_calls"] == 1
    assert yt.video_body(r)["status"]["privacyStatus"] == "private"


def test_youtube_resumes_saved_session_after_5xx(home, clip, monkeypatch):
    monkeypatch.setattr("nexus.costguard.check_host", lambda url, **k: "test")
    _yt_env(monkeypatch)
    acct = engine.connect_account("youtube", "main")
    m = engine.create("p1", _art(clip), "youtube", {"title": "Hello"}, connection_id=acct["id"])
    engine.enqueue(m["id"])
    sess = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=S"
    t1 = FakeTransport([_token(), ("POST", yt.UPLOAD_URL, Response(200, {"Location": sess})), ("PUT", sess, Response(503))])
    r = engine.process(now=100.0, adapters={"youtube": yt.YouTubeAdapter(t1, chunk=1000)})[0]
    assert r["status"] == "queued" and r["tracking"]["upload_session"]["uri"] == sess
    t2 = FakeTransport([_token(), ("PUT", sess, Response(308, {"Range": "bytes=0-499"})),
                        ("PUT", sess, Response(201, {}, b'{"id":"v9","status":{"uploadStatus":"uploaded","privacyStatus":"private"}}'))])
    r = engine.process(now=200.0, adapters={"youtube": yt.YouTubeAdapter(t2, chunk=1000)})[0]
    assert r["status"] == "uploaded" and t2.calls[1]["headers"]["Content-Range"] == "bytes */1000"
    assert t2.calls[2]["headers"]["Content-Range"] == "bytes 500-999/1000"
    assert not any(c["method"] == "POST" and c["url"].startswith(yt.UPLOAD_URL) for c in t2.calls)  # no duplicate insert


def test_transport_blocks_undeclared_host(home):
    from nexus.intelligence.publishing.adapters.base import Transport
    with pytest.raises(AdapterBlocked) as e:
        Transport().request("GET", "https://open.tiktokapis.com/v2/post/publish/video/init/")   # TikTok host is not declared
    assert e.value.code == "host_not_declared"
    from nexus import costguard
    costguard.manifest.cache_clear()
    assert costguard.check_host("https://youtubeanalytics.googleapis.com/v2/reports") == "youtube-data-api"  # declared by the lead


def test_oauth_url_uses_loopback_pkce():
    from nexus.intelligence.publishing import youtube_oauth as o
    v, c = o.pkce()
    u = o.auth_url("cid", "http://127.0.0.1:5555", c, "st")
    assert "code_challenge_method=S256" in u and "127.0.0.1" in u and "youtube.upload" in u and v != c


# ------------------------------------------------------------------ packaging
TRANSCRIPT = ("Solar panels lose efficiency in heat. Solar panels work best when cool, so installers leave an air gap. "
              "Cleaning solar panels twice a year keeps output steady.")


def test_packaging_deterministic_within_limits(home, monkeypatch):
    from nexus.intelligence import model_router
    def no_provider(*a, **k):
        raise model_router.NoProviderAvailable("title_generation", ["test: none"])
    monkeypatch.setattr(model_router, "complete", no_provider)
    for p in capabilities.PLATFORMS:
        d = packaging.package(p, TRANSCRIPT, title="Why solar panels hate heat <test>", duration_s=50)
        assert not C.validate(d) and d["ok"], (p, d["checks"])
        assert d["text_source"].startswith("deterministic")
        assert len(d["tags"]) <= 5
    yt_doc = packaging.package("youtube", TRANSCRIPT, title="a<b>", use_router=False)
    assert "<" not in yt_doc["title"]
    tt = packaging.package("tiktok", "x " * 3000, title="t", use_router=False)
    assert packaging.utf16_len(tt["caption"]) <= 2200
    assert packaging.caption_placement("tiktok")["band"] == [0.15, 0.7]


def test_packaging_router_hashtags_must_be_said(home, monkeypatch):
    from nexus.intelligence import model_router
    from nexus.intelligence.model_router import Result
    monkeypatch.setattr(model_router, "complete", lambda *a, **k: Result(text="", data={
        "title": "Solar panels and heat", "description": "Why heat matters.", "hashtags": ["#SolarPanels", "#Viral", "#FYP"],
        "thumbnail_text": "HEAT KILLS OUTPUT NOW TODAY", "cta": "Follow"}, provider="fake", model="m"))
    d = packaging.package("youtube_shorts", TRANSCRIPT)
    assert d["text_source"] == "router:fake/m" and d["tags"] == ["#SolarPanels"]
    assert set(d["dropped_hashtags"]) == {"#Viral", "#FYP"} and len(d["thumbnail"]["text"].split()) == 4


# ------------------------------------------------------------------ analytics
def test_youtube_studio_csv_import(home):
    r = analytics.import_csv(FIX / "youtube_studio_table.csv", "youtube_studio", period="2026-10-01..2026-10-31")
    assert r["imported"] == 2 and r["skipped"][0]["reason"] == "total row"
    a = {x["video_id"]: x for x in r["records"]}
    m = a["dQw4w9WgXcQ"]["metrics"]
    assert m["views"] == 1000 and m["avg_view_duration_s"] == 119 and abs(m["watch_minutes"] - 1992.0) < 1e-6
    assert m["impressions_ctr_pct"] == 4.5 and "avg_view_pct" not in a["abcDEF12345"]["metrics"]  # empty stays absent
    assert "impressions_ctr_pct" not in a["abcDEF12345"]["metrics"]
    assert a["dQw4w9WgXcQ"]["date"] == "2026-10-01..2026-10-31" and not C.validate(a["dQw4w9WgXcQ"])
    analytics.import_csv(FIX / "youtube_studio_table.csv", "youtube_studio", period="2026-10-01..2026-10-31")
    assert len(analytics.records("youtube")) == 2  # upsert, no duplicates


def test_csv_without_ids_is_skipped_not_invented(home):
    r = analytics.import_csv(FIX / "tiktok_studio.csv", "tiktok_studio")
    assert r["imported"] == 0 and len(r["skipped"]) == 2


def test_youtube_analytics_api_parsing(home, monkeypatch):
    monkeypatch.setattr("nexus.costguard.check_host", lambda url, **k: "test")
    _yt_env(monkeypatch)
    acct = engine.connect_account("youtube", "main")
    t = FakeTransport([_token(), ("GET", analytics.YT_REPORTS, Response(200, {}, (FIX / "yt_analytics_daily.json").read_bytes())),
                       ("GET", analytics.YT_REPORTS, Response(200, {}, (FIX / "yt_analytics_retention.json").read_bytes()))])
    ad = yt.YouTubeAdapter(t)
    recs = analytics.youtube_daily("vid1", "2026-10-01", "2026-10-02", acct, adapter=ad)
    assert [r["date"] for r in recs] == ["2026-10-01", "2026-10-02"] and recs[0]["metrics"]["avg_view_duration_s"] == 120
    assert "filters=video%3D%3Dvid1" in t.calls[1]["url"]
    ret = analytics.youtube_retention("vid1", "2026-10-01", "2026-10-02", acct, adapter=ad)
    assert ret["metrics"]["retention_curve"][1] == [0.5, 0.42, 0.48]
    assert "elapsedVideoTimeRatio" in t.calls[2]["url"]


def test_youtube_analytics_without_credentials_blocks(home):
    with pytest.raises(AdapterBlocked) as e:
        analytics.youtube_daily("v", "2026-10-01", "2026-10-02", None, transport=FakeTransport([]))
    assert e.value.code == "account_not_connected"


def test_weak_hashtags_dropped_and_description_fits(home):
    d = packaging.package("snapchat_spotlight", TRANSCRIPT + " When when when.", title="Solar heat", use_router=False)
    assert d["ok"] and len(d["description"]) <= 160
    assert "#When" not in d["tags"]
    assert creds.env_name("pub.youtube.main.refresh_token") == "NEXUS_PUB_YOUTUBE_MAIN_REFRESH_TOKEN"


def test_upload_job_is_registered_and_light(home):
    from nexus import jobs, workflows  # noqa: F401
    assert "publish-upload" in jobs.HANDLERS and "publish-upload" in jobs.LIGHT          # uploads never wait for renders
    assert "render" not in jobs.LIGHT and "create-from-idea" not in jobs.LIGHT
