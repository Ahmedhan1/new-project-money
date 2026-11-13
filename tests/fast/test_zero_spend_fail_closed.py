"""Zero-spend gate fails CLOSED. A hosted provider is usable only when its catalogue entry says
`payment_method_required: false` (the boolean), is verified, the model is a catalogued free model, the owner authorised
it, a key exists and an enforceable quota is left. Every test traps the network: any socket use fails the test."""
import json
import socket
import urllib.request

import pytest

from nexus import costguard
from nexus.intelligence import capability_registry as reg
from nexus.intelligence.model_router import router
from nexus.intelligence.providers import openai_chat


@pytest.fixture()
def settings(home):
    def write(d):
        (home / "settings.json").write_text(json.dumps(d))
    router._reset_for_tests()
    return write


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)


def _catalogued(pid):
    return [e for e in reg.models() if e["provider"] == pid]


@pytest.mark.parametrize("pid,env", [("groq", "NEXUS_KEY_GROQ"), ("mistral-free", "NEXUS_KEY_MISTRAL_FREE")])
def test_unverified_payment_requirement_is_refused_even_when_authorised_with_a_key(settings, monkeypatch, pid, env):
    assert reg.providers()[pid]["payment_method_required"] == "unverified"        # the catalogue fact being guarded
    settings({"ai_providers": {pid: {"authorized": True, "daily_requests": 10}}})
    monkeypatch.setenv(env, "dummy-not-a-real-key")
    ents = _catalogued(pid)
    assert ents
    for e in ents:
        ok, why = router.eligibility(e, "public")
        assert not ok and "not verified false" in why and "fail closed" in why
        # a crafted entry claiming the opposite cannot override the catalogue
        ok, why = router.eligibility({**e, "payment_method_required": False}, "public")
        assert not ok and "'unverified'" in why
        with pytest.raises(costguard.CostGuardError, match="free-tier exception refused"):
            costguard.check_host(reg.providers()[pid]["base_url"] + "/chat/completions", free_tier_provider=pid, model=e["model"])
        with pytest.raises(costguard.CostGuardError):                              # the adapter refuses before any socket
            openai_chat({**e, "payment_method_required": False}, "hi", "s", None, 10, 0.0)
    with pytest.raises(router.NoProviderAvailable):
        router.complete("general", "x", privacy="public", entries=ents, use_cache=False)
    st = router.status("public")
    assert not any(r.startswith(pid + "/") for t in st.values() for r in t["route"])
    assert any(w.startswith(pid + ":") and "fail closed" in w for w in st["general"]["excluded"])


@pytest.mark.parametrize("value", [None, "unverified", "false", "no", 0, "", [], {}, "True", True])
def test_missing_or_malformed_payment_declarations_are_refused(settings, monkeypatch, verified_free_provider, value):
    settings({"ai_providers": {"test-free": {"authorized": True}}})
    monkeypatch.setenv("NEXUS_KEY_TEST_FREE", "dummy")
    cat = {k: v for k, v in verified_free_provider.items() if k != "payment_method_required"}
    if value is not None:
        cat["payment_method_required"] = value
    real = reg.providers
    monkeypatch.setattr(reg, "providers", lambda: {**real(), "test-free": cat})
    e = {**{k: v for k, v in cat.items() if k != "models"}, "provider": "test-free", "model": "m"}
    ok, why = router.eligibility(e, "public")
    assert not ok and "zero spend" in why
    with pytest.raises(costguard.CostGuardError):
        costguard.check_host("https://free.test.invalid/v1/chat/completions", free_tier_provider="test-free", model="m")


def test_missing_from_entry_is_refused_even_if_catalogue_is_fine(settings, monkeypatch, verified_free_provider):
    settings({"ai_providers": {"test-free": {"authorized": True}}})
    monkeypatch.setenv("NEXUS_KEY_TEST_FREE", "dummy")
    bare = {"provider": "test-free", "model": "m", "hosting": "hosted_free", "privacy": "public", "kind": "openai_compatible"}
    assert "fail closed" in router.eligibility(bare, "public")[1]
    assert router.eligibility({**bare, "payment_method_required": False}, "public") == (True, "ok")


def test_unverified_catalogue_facts_or_unlisted_model_or_unknown_provider_are_refused(settings, monkeypatch, verified_free_provider):
    settings({"ai_providers": {"test-free": {"authorized": True}, "ghost": {"authorized": True}}})
    monkeypatch.setenv("NEXUS_KEY_TEST_FREE", "dummy")
    monkeypatch.setenv("NEXUS_KEY_GHOST", "dummy")
    base = {"hosting": "hosted_free", "privacy": "public", "kind": "openai_compatible", "payment_method_required": False}
    assert "not a catalogued free model" in router.eligibility({**base, "provider": "test-free", "model": "premium"}, "public")[1]
    assert "not a catalogued provider" in router.eligibility({**base, "provider": "ghost", "model": "m"}, "public")[1]
    # an unknown hosting value is not treated as local
    assert "not a catalogued provider" in router.eligibility({**base, "provider": "ghost", "model": "m", "hosting": "cloud"}, "public")[1]
    real = reg.providers
    monkeypatch.setattr(reg, "providers", lambda: {**real(), "test-free": {**verified_free_provider, "verified": "unverified"}})
    assert "not verified against first-party docs" in router.eligibility({**base, "provider": "test-free", "model": "m"}, "public")[1]


def test_catalogued_hosted_provider_cannot_be_relabelled_local(settings, monkeypatch):
    settings({"ai_providers": {"groq": {"authorized": True, "base_url": "https://api.groq.com/openai/v1"}}})
    monkeypatch.setenv("NEXUS_KEY_GROQ", "dummy")
    e = {**_catalogued("groq")[0], "hosting": "owner_worker", "payment_method_required": False}
    assert not router.eligibility(e, "public")[0]


def test_known_free_providers_pass_with_authorisation_key_and_quota(settings, monkeypatch):
    # OpenRouter ':free' variants: verified false, numeric documented quotas
    or_ = _catalogued("openrouter-free")[0]
    assert or_["payment_method_required"] is False
    settings({"ai_providers": {"openrouter-free": {"authorized": True}}})
    assert "no API key" in router.eligibility(or_, "public")[1]
    monkeypatch.setenv("NEXUS_KEY_OPENROUTER_FREE", "dummy")
    assert router.eligibility(or_, "public") == (True, "ok")
    assert router.hosted_call_allowed("openrouter-free", or_["model"]) == (True, "ok")
    assert costguard.check_host("https://openrouter.ai/api/v1/chat/completions", free_tier_provider="openrouter-free",
                                model=or_["model"]) == "openrouter-free"
    assert "not a free variant" in router.eligibility({**or_, "model": or_["model"].replace(":free", "")}, "public")[1]
    monkeypatch.setattr(router, "_usage_today", lambda pid: 45)                    # 90 % of rpd=50
    assert "daily free quota" in router.eligibility(or_, "public")[1]
    with pytest.raises(costguard.CostGuardError, match="daily free quota"):
        costguard.check_host("https://openrouter.ai/api/v1/x", free_tier_provider="openrouter-free", model=or_["model"])


def test_authorisation_must_be_the_boolean_true(settings, monkeypatch):
    or_ = _catalogued("openrouter-free")[0]
    monkeypatch.setenv("NEXUS_KEY_OPENROUTER_FREE", "dummy")
    for v in ("yes", "true", 1, None):
        settings({"ai_providers": {"openrouter-free": {"authorized": v}}})
        assert "not authorised" in router.eligibility(or_, "public")[1]


def test_free_provider_without_published_quota_needs_an_owner_limit(settings, monkeypatch):
    gm = _catalogued("gemini")[0]
    assert gm["payment_method_required"] is False and not any(k in gm["quotas"] for k in router.QUOTA_KEYS)
    monkeypatch.setenv("NEXUS_KEY_GEMINI", "dummy")
    settings({"ai_providers": {"gemini": {"authorized": True}}})
    assert "no documented numeric quota" in router.eligibility(gm, "public")[1]
    settings({"ai_providers": {"gemini": {"authorized": True, "daily_requests": 20}}})
    assert router.eligibility(gm, "public") == (True, "ok")


def test_entry_cannot_loosen_documented_quotas(settings, monkeypatch, verified_free_provider):
    settings({"ai_providers": {"test-free": {"authorized": True}}})
    monkeypatch.setenv("NEXUS_KEY_TEST_FREE", "dummy")
    e = {"provider": "test-free", "model": "m", "hosting": "hosted_free", "privacy": "public", "kind": "openai_compatible",
         "payment_method_required": False, "quotas": {"tpm": 10 ** 9, "rpd": 10 ** 9}}
    assert "per-minute token limit 8000" in router.eligibility(e, "public", need_tokens=9000)[1]


def test_paid_and_card_requiring_endpoints_are_refused_directly(settings, monkeypatch):
    settings({"ai_providers": {"cerebras": {"authorized": True, "daily_requests": 5},
                               "huggingface-free": {"authorized": True, "daily_requests": 5}}})
    monkeypatch.setenv("NEXUS_KEY_CEREBRAS", "dummy")
    monkeypatch.setenv("NEXUS_KEY_HUGGINGFACE_FREE", "dummy")
    for url in ("https://api.openai.com/v1/chat/completions", "https://api.anthropic.com/v1/messages", "https://api.groq.com/openai/v1/x"):
        with pytest.raises(costguard.CostGuardError):
            costguard.check_host(url)
    with pytest.raises(costguard.CostGuardError, match="requires a payment method"):
        costguard.check_host("https://api.cerebras.ai/v1/chat/completions", free_tier_provider="cerebras", model="qwen-3.8-27b")
    with pytest.raises(costguard.CostGuardError, match="no free quota"):
        costguard.check_host("https://router.huggingface.co/v1/x", free_tier_provider="huggingface-free", model="any")
    with pytest.raises(costguard.CostGuardError, match="not the free-tier endpoint"):
        costguard.check_host("https://api.openai.com/v1/x", free_tier_provider="openrouter-free", model="a:free")


def test_gate_error_refuses(monkeypatch):
    monkeypatch.setattr(router, "hosted_call_allowed", lambda *a: 1 / 0)
    with pytest.raises(costguard.CostGuardError, match="gate unavailable"):
        costguard.check_host("https://openrouter.ai/api/v1/x", free_tier_provider="openrouter-free", model="a:free")


def test_catalogue_keeps_groq_and_mistral_unverified_and_unroutable():
    for pid in ("groq", "mistral-free"):
        assert router.zero_spend_block(reg.providers()[pid]) is not None
    assert {x["provider"]: x for x in router.health()}["groq"]["zero_spend_blocked"] is True
    assert "groq" not in {c["provider"] for c in router.account1_capabilities()}
