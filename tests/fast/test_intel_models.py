"""Account 3 model ecosystem: zero-spend catalogue gates, token budgets, owner limits, health percentiles, route_explain,
Account 1 capability export, benchmark scorers, OpenAI-compatible adapter details, CLI. No network, no models."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nexus import db
from nexus.intelligence.model_router import bench, router
from nexus.intelligence.providers import strip_reasoning


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
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Srv.seen.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": "<think>hmm</think>\nanswer"}}],
                                     "usage": {"total_tokens": 40, "completion_tokens": 3}}).encode())

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    s = HTTPServer(("127.0.0.1", 0), _Srv)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    _Srv.seen = []
    yield f"http://127.0.0.1:{s.server_address[1]}/v1"
    s.shutdown()


# ----------------------------------------------------------------- zero spend + budgets
def test_card_or_no_free_quota_is_never_routed_even_when_authorised(settings, monkeypatch):
    settings({"ai_providers": {"cerebras": {"authorized": True}, "huggingface-free": {"authorized": True}}})
    monkeypatch.setenv("NEXUS_KEY_CEREBRAS", "k")
    monkeypatch.setenv("NEXUS_KEY_HUGGINGFACE_FREE", "k")
    card = _entry("cerebras", "m", hosting="hosted_free", privacy="public", payment_method_required=True)
    noquota = _entry("huggingface-free", "m", hosting="hosted_free", privacy="public", free_quota="none")
    for e in (card, noquota):
        ok, why = router.eligibility(e, "public")
        assert not ok and "zero spend" in why
    with pytest.raises(router.NoProviderAvailable):
        router.complete("general", "x", privacy="public", entries=[card, noquota])


def test_token_budget_context_quota_and_owner_limits(settings, monkeypatch):
    settings({"ai_providers": {"owner-openai-compatible": {"base_url": "http://127.0.0.1:9/v1", "daily_tokens": 1000},
                               "groq": {"authorized": True}}})
    monkeypatch.setenv("NEXUS_KEY_GROQ", "k")
    small = _entry("owner-openai-compatible", "tiny", context=512)
    assert "do not fit" in router.eligibility(small, "internal", need_tokens=900)[1]
    assert router.eligibility(small, "internal", need_tokens=400) == (True, "ok")
    db.metric("ai_request", 1.0, provider="owner-openai-compatible", model="tiny", day=__import__("time").strftime("%Y-%m-%d"), tokens=800)
    assert "owner daily token limit" in router.eligibility(small, "internal", need_tokens=300)[1]
    g = _entry("groq", "m", hosting="hosted_free", privacy="public", quotas={"tpm": 8000, "tpd": 10000})
    assert "per-minute token limit" in router.eligibility(g, "public", need_tokens=9000)[1]
    db.metric("ai_request", 1.0, provider="groq", model="m", day=__import__("time").strftime("%Y-%m-%d"), tokens=8900)
    assert "daily free token quota" in router.eligibility(g, "public", need_tokens=200)[1]


def test_estimate_tokens_is_conservative_for_dense_scripts():
    assert router.estimate_tokens("") == 0
    en, zh, ar = router.estimate_tokens("hello world " * 20), router.estimate_tokens("你好世界" * 30), router.estimate_tokens("مرحبا بالعالم " * 20)
    assert en < ar and zh >= 120 and en >= 240 / 4


# ----------------------------------------------------------------- owner server, adapter, health, explain
def test_owner_server_model_gets_catalogue_profile_and_no_think_suffix(settings, server):
    settings({"ai_providers": {"owner-openai-compatible": {"base_url": server, "models": ["qwen3-0.6b-q8"]}}})
    ex = router.route_explain("general", "internal")
    assert ex["chosen"]["provider"] == "owner-openai-compatible" and ex["chosen"]["licence"] == "Apache-2.0"
    assert ex["chosen"]["quality_source"] == "estimate" and "gates" in ex
    r = router.complete("general", "hi", privacy="internal", use_cache=False)
    assert r.text == "answer" and r.provider == "owner-openai-compatible"                     # <think> stripped
    assert _Srv.seen[-1]["messages"][1]["content"].endswith(" /no_think")
    h = {x["provider"]: x for x in router.health()}["owner-openai-compatible"]
    assert h["requests"] == 1 and h["p50_latency_s"] is not None and h["tokens_today"] == 40 and h["circuit"] == "closed"
    assert {x["provider"]: x for x in router.health()}["cerebras"]["zero_spend_blocked"] is True
    priv = router.route_explain("general", "private")                                        # never leaves the machine
    assert all(c["hosting"] == "in_process" for c in priv["ranked"])
    assert any("may not leave" in w for w in priv["excluded"])


def test_measured_bench_quality_overrides_estimates(settings, server):
    settings({"ai_providers": {"owner-openai-compatible": {"base_url": server, "models": ["qwen3-0.6b-q8"]}}})
    assert router.route_explain("general", "internal")["chosen"]["model"] == "qwen3-0.6b-q8"
    bench.store_quality("owner-openai-compatible", "qwen3-0.6b-q8", {"capabilities": {"reasoning": 0.2}}, "t")
    ex = router.route_explain("general", "internal")
    assert ex["chosen"] is None or ex["chosen"]["model"] != "qwen3-0.6b-q8"
    assert any("(measured)" in w for w in ex["excluded"])


def test_schema_grammar_only_for_servers_that_declare_it(settings, server):
    from nexus.intelligence.providers import openai_chat
    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    base = _entry("owner-openai-compatible", "m", base_url=server)
    openai_chat(base, "p", "s", schema, 10, 0.1)
    openai_chat({**base, "json_schema": "llamacpp"}, "p", "s", schema, 10, 0.1)
    assert "schema" not in _Srv.seen[0]["response_format"] and _Srv.seen[1]["response_format"]["schema"] == schema


def test_strip_reasoning():
    assert strip_reasoning("<think>a\nb</think> ok") == "ok"
    assert strip_reasoning("<think>never closed") == ""


# ----------------------------------------------------------------- Account 1 alignment
def test_account1_capabilities_shape(settings):
    settings({"ai_providers": {"owner-openai-compatible": {"base_url": "http://127.0.0.1:9/v1", "models": ["qwen3-0.6b-q8"]}}})
    caps = {c["provider"]: c for c in router.account1_capabilities()}
    assert "cerebras" not in caps and "huggingface-free" not in caps                           # zero-spend blocked
    fields = {"contract", "provider", "connection_id", "tasks", "models", "local", "commercial_use", "quotas"}
    for c in caps.values():
        assert set(c) == fields and c["contract"] == "AIProviderCapability/v1"
        assert c["commercial_use"] in ("yes", "no", "unverified")
        for q in c["quotas"]:
            assert q["contract"] == "QuotaStatus/v1" and q["source"] == "documented" and q["resource"] == "api_requests"
    assert caps["owner-openai-compatible"]["local"] and caps["owner-openai-compatible"]["models"] == ["qwen3-0.6b-q8"]
    assert caps["owner-openai-compatible"]["commercial_use"] == "yes" and "general" in caps["owner-openai-compatible"]["tasks"]
    assert caps["openrouter-free"]["commercial_use"] == "unverified"


def test_cli_status_and_explain(settings, capsys):
    from nexus.intelligence.model_router.__main__ import main
    settings({})
    assert main(["status", "--privacy", "private"]) == 0
    out = capsys.readouterr().out
    assert "scriptwriting" in out and "excluded" in out
    assert main(["explain", "title_generation", "--privacy", "public"]) == 0
    assert json.loads(capsys.readouterr().out)["task"] == "title_generation"


# ----------------------------------------------------------------- benchmark scorers
def test_bench_scorers():
    assert bench.score_json('{"title": "Volcanoes", "tags": ["a", "b", "c"], "language": "en", "duration_s": 90}') == 1.0
    assert bench.score_json("not json") == 0.0
    assert bench.score_json('{"title": "x", "tags": ["a"], "language": "de"}') < 0.5
    gold = json.dumps({"people": ["Marco Rossi", "Lina Haddad"], "date": "2026-03-14", "city": "Lyon", "rent_eur": "1,250 EUR", "deposit_eur": 2500})
    assert bench.score_extract(gold) == 1.0 and bench.score_extract('{"people": []}') == 0.0
    assert bench.score_reason('{"answer": "16:15"}', "16:15") == 1.0 and bench.score_reason('{"answer": "16:05"}', "16:15") == 0.0
    assert bench.score_reason('{"answer": "36 stamps"}', "36") == 1.0 and bench.score_reason('{"answer": "No."}', "no") == 1.0
    assert bench.score_reason('{"answer": "not sure, no"}', "no") == 0.0
    good = "Bees use a waggle dance whose angle to the sun gives direction and whose duration gives distance; von Frisch won a Nobel Prize."
    assert bench.score_summary(good) == 1.0 and bench.score_summary("Bees dance.") < 0.5
    assert bench.score_summary(good + " filler" * 80) < 0.5
    assert bench.score_titles('{"titles": ["How bees talk", "The secret language of bees", "Why bees dance"]}') == 1.0
    assert bench.score_titles('{"titles": ["HOW BEES TALK!!", "x"]}') < 0.5


def test_bench_translation_and_dialect_scoring():
    assert bench.chrf(bench.TRANSLATE_SRC, bench.TRANSLATE_SRC) == 1.0 and bench.chrf("abc", "xyz") == 0.0
    ar = bench.score_translation("يفتح المتحف في التاسعة صباحا.", "ar", bench.TRANSLATE_SRC)
    assert ar["script_share"] == 1.0 and ar["score"] == 1.0
    wrong = bench.score_translation("The museum opens at nine.", "zh", None)                      # English instead of Chinese
    assert wrong["score"] == 0.0 and wrong["sim"] is None
    assert bench.score_translation("", "de", None)["score"] == 0.0
    egy = bench.score_dialect("أنا عايز أروح السينما دلوقتي مع صحابي", "egy")
    assert egy["score"] == 1.0 and "دلوقتي" in egy["own_markers"]
    gulf_as_egy = bench.score_dialect("أبي أروح الحين مع الربع", "egy")
    assert gulf_as_egy["score"] == 0.2 and gulf_as_egy["other_markers"]
    assert bench.score_dialect("أبي أروح الحين مع الربع", "gulf")["score"] == 1.0


def test_bench_taskset_is_fixed_and_fully_scorable():
    items = bench.taskset()
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)) == 15
    assert {i["cap"] for i in items} == {"json", "reasoning", "summarization", "translation", "arabic", "writing"}
    for it in items:
        assert 0.0 <= bench.score_item(it, "", "")["score"] <= 1.0
    rows = [{"cap": "json", "score": 1.0, "latency_s": 2.0, "tokens_per_s": 5.0}, {"cap": "json", "score": 0.5, "error": "x", "latency_s": 1}]
    agg = bench.aggregate(rows)
    assert agg["capabilities"]["json"] == 0.5 and agg["failures"] == 1 and agg["mean_tokens_per_s"] == 5.0
