"""Account 3 model router: routing by task/privacy/quota, fallbacks, breaker, schema validation, real HTTP adapter."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nexus import costguard
from nexus.intelligence import contracts
from nexus.intelligence.model_router import router


def _entry(provider, model, hosting="owner_worker", privacy="internal", q=6, speed="medium", kind="openai_compatible", **kw):
    return {"provider": provider, "model": model, "hosting": hosting, "privacy": privacy, "kind": kind, "speed": speed,
            "quality": {c: q for c in ("reasoning", "writing", "json", "translation", "summarization")}, **kw}


@pytest.fixture()
def settings(home):
    def write(d):
        (home / "settings.json").write_text(json.dumps(d))
    router._reset_for_tests()
    return write


class _Srv(BaseHTTPRequestHandler):
    replies: list = []
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Srv.seen.append({"path": self.path, "model": body["model"], "auth": self.headers.get("Authorization"), "fmt": body.get("response_format")})
        code, content = _Srv.replies.pop(0) if _Srv.replies else (200, "ok")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if code == 200:
            self.wfile.write(json.dumps({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 7}}).encode())
        else:
            self.wfile.write(b'{"error":"x"}')

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    s = HTTPServer(("127.0.0.1", 0), _Srv)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    _Srv.replies, _Srv.seen = [], []
    yield f"http://127.0.0.1:{s.server_address[1]}/v1"
    s.shutdown()


def test_privacy_class_decides_who_may_see_the_data(settings):
    settings({"ai_providers": {"groq": {"authorized": True}, "owner-openai-compatible": {"base_url": "http://127.0.0.1:9/v1"}}})
    es = [_entry("groq", "big", hosting="hosted_free", privacy="public", q=9), _entry("owner-openai-compatible", "mid", q=6),
          _entry("local-llamacpp", "small", hosting="in_process", privacy="private", q=4, kind="x")]
    import os
    os.environ["NEXUS_KEY_GROQ"] = "test-key"
    try:
        pub, _ = router.candidates("scriptwriting", "public", es)
        assert [e["provider"] for e in pub] == ["groq", "owner-openai-compatible", "local-llamacpp"]   # strongest first for heavy tasks
        priv, why = router.candidates("scriptwriting", "private", es)
        assert [e["provider"] for e in priv] == ["local-llamacpp"] and any("may not leave" in w for w in why)
        light, _ = router.candidates("title_generation", "public", [*es[:2], {**es[1], "model": "f", "speed": "fast"}])
        assert light[0]["speed"] == "fast"                                                     # light tasks: fastest adequate model
    finally:
        del os.environ["NEXUS_KEY_GROQ"]


def test_hosted_free_needs_authorisation_key_free_model_and_quota(settings, monkeypatch):
    e = _entry("openrouter-free", "qwen/x:free", hosting="hosted_free", privacy="public", free_model_suffix=":free", quotas={"rpd": 10, "rpm": 100})
    settings({})
    assert "not authorised" in router.eligibility(e, "public")[1]
    settings({"ai_providers": {"openrouter-free": {"authorized": True}}})
    assert "no API key" in router.eligibility(e, "public")[1]
    monkeypatch.setenv("NEXUS_KEY_OPENROUTER_FREE", "k")
    assert router.eligibility(e, "public") == (True, "ok")
    assert "not a free variant" in router.eligibility({**e, "model": "qwen/x"}, "public")[1]      # paid variant never routed
    monkeypatch.setattr(router, "_usage_today", lambda pid: 9)
    assert "daily free quota" in router.eligibility(e, "public")[1]


def test_fallback_breaker_and_honest_failure(settings, server, monkeypatch):
    settings({"ai_providers": {"owner-openai-compatible": {"base_url": server}, "second": {"base_url": server}}})
    a, b = _entry("owner-openai-compatible", "m-a", q=8), _entry("second", "m-b", q=5)
    _Srv.replies = [(500, ""), (500, ""), (200, "hello from b")]
    r = router.complete("general", "hi", entries=[a, b], use_cache=False)
    assert r.text == "hello from b" and r.provider == "second" and len(r.fallbacks) == 2
    _Srv.replies = [(500, "")] * 3
    with pytest.raises(router.NoProviderAvailable):
        router.complete("general", "hi again", entries=[a], use_cache=False)
    assert router.eligibility(a, "internal")[1].startswith("owner-openai-compatible: circuit open")    # 3 failures -> breaker
    with pytest.raises(router.NoProviderAvailable) as ex:
        router.complete("general", "x", privacy="private", entries=[a, b])
    assert "may not leave" in str(ex.value)


def test_structured_output_is_validated_and_retried(settings, server):
    settings({"ai_providers": {"owner-openai-compatible": {"base_url": server}}})
    e = _entry("owner-openai-compatible", "m", q=8)
    schema = {"type": "object", "required": ["titles"], "properties": {"titles": {"type": "array", "minItems": 2}}}
    _Srv.replies = [(200, "{}"), (200, 'Sure! {"titles": ["A", "B", "C"]}')]
    r = router.complete("title_generation", "t", schema=schema, entries=[e], use_cache=False)
    assert r.data == {"titles": ["A", "B", "C"]} and _Srv.seen[0]["fmt"] == {"type": "json_object"}
    _Srv.replies = [(200, '{"titles": ["X", "Y"]}')]
    r2 = router.complete("title_generation", "t", schema=schema, entries=[e])
    r3 = router.complete("title_generation", "t", schema=schema, entries=[e])
    assert not r2.cached and r3.cached and r3.data == r2.data                                 # cache hit costs nothing


def test_unknown_task_and_privacy_rejected(settings):
    with pytest.raises(KeyError):
        router.candidates("make_coffee")
    with pytest.raises(ValueError):
        router.complete("general", "x", privacy="secret", entries=[])


def test_cost_guard_free_tier_exception_is_narrow():
    with pytest.raises(costguard.CostGuardError):
        costguard.check_host("https://api.groq.com/openai/v1/chat/completions")                  # still blocked by default
    assert costguard.check_host("https://api.groq.com/openai/v1/chat/completions", free_tier_provider="groq") == "groq"
    with pytest.raises(costguard.CostGuardError):
        costguard.check_host("https://api.openai.com/v1/chat/completions", free_tier_provider="groq")   # cannot borrow the exception
    with pytest.raises(costguard.CostGuardError):
        costguard.check_host("https://api.groq.com/x", free_tier_provider="gemini")


def test_catalogue_entries_are_complete_and_map_to_account1():
    from nexus.intelligence import capability_registry as reg
    for p in reg.catalog()["providers"]:
        assert {"id", "kind", "hosting", "privacy", "commercial_use", "payment_method_required", "quotas", "verified"} <= set(p), p["id"]
        assert p["payment_method_required"] is False
        if p["hosting"] == "hosted_free":
            assert costguard.FREE_TIER_HOSTS.get(p["host"]) == p["id"]
    cap = contracts.make("ProviderCapability", provider="groq", connection_id="c1", tasks=["general"], models=["m"], local=False,
                         commercial_use="per model", hosting="hosted_free", privacy={"max": "public"}, quotas=[], verified="unverified")
    a1 = contracts.to_account1_ai_capability(cap)
    assert a1["contract"] == "AIProviderCapability/v1" and a1["commercial_use"] == "unverified"


def test_contracts_validate():
    ok = contracts.make("ContentOpportunity", id="op1", topic="t", niche="science", language="en", score=0.6, factors={}, evidence=[contracts.evidence("Wikipedia", "https://x")],
                        confidence=0.4, status="proposed")
    assert contracts.validate(ok) == []
    assert "confidence" in " ".join(contracts.validate({**ok, "confidence": 3}))
    assert "status" in " ".join(contracts.validate({**ok, "status": "viral"}))
    assert "major version" in " ".join(contracts.validate({**ok, "schema": "nexus.ContentOpportunity/2"}))
    pub = {"schema": "nexus.PublishingManifest/1", "id": "p", "project_id": "x", "artifact": {}, "platform": "youtube", "metadata": {},
           "visibility": "public", "approval": "none", "status": "draft", "idempotency_key": "k"}
    assert "approved_by_owner" in " ".join(contracts.validate(pub))                           # no hidden public publishing
