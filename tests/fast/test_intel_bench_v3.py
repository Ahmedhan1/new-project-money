"""Bench V3 item set and scorers: >= 15 items per routed capability, deterministic rules, oracle answers score 1.0 and
typical failures score low. No model and no network."""
import json
from collections import Counter

import pytest

from nexus.intelligence.model_router import bench_v3 as b

ITEMS = {it["id"]: it for it in b.taskset()}


def test_every_routed_capability_has_at_least_15_items_and_ids_are_unique():
    t = b.taskset()
    assert len({i["id"] for i in t}) == len(t)
    n = Counter(i["cap"] for i in t)
    assert set(b.CAPS) <= set(n) and all(n[c] >= 15 for c in b.CAPS), n
    for task, caps in b.TASK_CAPS.items():
        for c in caps:
            assert c == "dialect" or n[c] >= 15, (task, c)


def test_v2_prompts_are_unchanged_so_v2_subsets_stay_comparable():
    from nexus.intelligence.model_router import bench_v2
    for it in bench_v2.taskset():
        assert ITEMS[it["id"]]["prompt"] == it["prompt"]


def test_long_context_fits_the_context_window():
    from nexus.intelligence.model_router.router import estimate_tokens
    for it in ITEMS.values():
        if it["cap"] == "long_context":
            assert estimate_tokens(it["prompt"]) + it["max_tokens"] < it.get("num_ctx", 4096)


def _ans(a):
    return json.dumps({"answer": a})


@pytest.mark.parametrize("iid,good,bad", [
    ("reason3_1", "Saturday", "Friday"), ("reason3_6", "no", "yes"), ("reason3_5", "12:15", "12:45"),
    ("longctx3_1_3", "94 euros", "99"), ("longctx3_2_2", "two days", "3"), ("longctx3_2_4", "3,870 m", "2817"),
    ("longctx3_3_4", "They moor free of charge", "14 crowns"), ("ground3_qa6", "11.2 km", "12 km"), ("ground3_qa4", "S3", "S1"),
])
def test_answer_scoring(iid, good, bad):
    assert b.score_v3(ITEMS[iid], _ans(good))["score"] == 1.0
    assert b.score_v3(ITEMS[iid], _ans(bad))["score"] == 0.0


def test_unanswerable_and_grounded_writing():
    na = ITEMS["ground3_na3"]
    assert b.score_v3(na, _ans("not stated"))["score"] == 1.0 and b.score_v3(na, _ans("200 million euros"))["score"] == 0.0
    w = ITEMS["ground3_w3"]
    assert b.score_v3(w, "The 11.2 km Corrin tunnel opened in December 2020.")["score"] == 1.0
    assert b.score_v3(w, "The shocking 12 km Corrin tunnel opened in 2019!")["score"] <= 0.4


def test_conflict_scoring():
    dis = ITEMS["conflict3"]                                        # 8,400 vs 9,100
    assert b.score_v3(dis, json.dumps({"agree": False, "values": ["8,400", "9,100"]}))["score"] == 1.0
    assert b.score_v3(dis, json.dumps({"agree": True, "values": ["8400"]}))["score"] == 0.25
    ag = ITEMS["conflict15"]                                        # 5 km == 5,000 m
    assert b.score_v3(ag, json.dumps({"agree": True, "values": ["5 km"]}))["score"] == 1.0
    assert b.score_v3(ag, json.dumps({"agree": False, "values": ["5 km", "5000 m"]}))["score"] == 0.5
    assert b.score_v3(ITEMS["conflict11"], json.dumps({"agree": False, "values": ["three", "four"]}))["score"] == 1.0
    assert b.score_v3(dis, "no json")["score"] == 0.0


def test_extract_and_checks():
    e = ITEMS["extract3_1"]
    assert b.score_v3(e, json.dumps(e["gold"]))["score"] == 1.0
    assert b.score_v3(e, json.dumps({**e["gold"], "amount_eur": 460, "date": "2026-03-02"}))["score"] == round(4 / 6, 3)
    assert b.score_v3(ITEMS["extract3_4"], json.dumps({"chair": "Dr. Samira Nassar", "treasurer": "Leo Brandt", "secretary": "Yuki Tan",
                                                        "date": "2025-05-09"}))["score"] == 1.0
    assert b.score_v3(ITEMS["extract3_6"], json.dumps(ITEMS["extract3_6"]["gold"], ensure_ascii=False))["score"] == 1.0
    j = ITEMS["json3_4"]
    assert b.score_v3(j, json.dumps({"sentiment": "mixed", "topics": ["story", "audio"]}))["score"] == 1.0
    assert b.score_v3(j, json.dumps({"sentiment": "positive", "topics": ["story"]}))["score"] == 0.0
    assert b.score_v3(ITEMS["json3_6"], '{"hours": 2, "minutes": 15}')["score"] == 1.0
    assert b.score_v3(ITEMS["json3_5"], '{"is_question": "yes", "language": "es"}')["score"] == 0.5    # a string is not a boolean


def test_summary_translation_and_dialect():
    s = ITEMS["summary3_1"]
    good = "Mangroves on tropical coasts slow waves and erosion, shelter young fish, store much carbon, and are being replanted after clearing."
    assert b.score_v3(s, good)["score"] == 1.0
    assert b.score_v3(s, "Mangroves cover 75 percent of coasts.")["score"] < 0.5                  # invented number, low coverage
    tr = ITEMS["translate3_3_es"]
    assert b.score_v3(tr, tr["ref"])["score"] == 1.0
    assert b.score_v3(tr, tr["src"])["score"] == 0.0                                             # copying the English source
    assert b.score_v3(ITEMS["translate_zh"], b.TRANSLATION_REFS["zh"])["score"] == 1.0
    eg = ITEMS["dialect3_egy_t1"]
    assert b.score_v3(eg, "أنا عايز أروح السوق دلوقتي.")["score"] == 1.0
    assert b.score_v3(eg, "أريد أن أذهب إلى السوق الآن.")["score"] == 0.2                          # Modern Standard Arabic
    gu = ITEMS["dialect3_gulf_t1"]
    assert b.score_v3(gu, "أبي أروح السوق الحين.")["score"] == 1.0
    assert b.score_v3(gu, "أنا عايز أروح السوق دلوقتي.")["score"] == 0.2                          # Egyptian is not Gulf
    assert b.score_v3(gu, "")["score"] == 0.0


def test_writing_strategy_and_instructions():
    t = ITEMS["titles3_1"]
    assert b.score_v3(t, json.dumps({"titles": ["Why the sea is salty", "Where the sea gets its salt", "The salty sea explained"]}))["score"] == 1.0
    assert b.score_v3(t, json.dumps({"titles": ["SHOCKING SEA SECRET", "Top 10 sea facts"]}))["score"] <= 0.5
    h = ITEMS["hook3_1"]
    assert b.score_v3(h, "Honey from ancient Egyptian tombs was still edible after about 3,000 years.")["score"] == 1.0
    assert b.score_v3(h, "Honey lasts 10,000 years. Shocking!")["score"] < 0.7
    st = ITEMS["strategy3_2"]
    good = {"hook": "What happened to the Nile flood after 1970?", "beats": ["Before the dam", "Building it", "Lake Nasser today"],
            "visuals": ["aerial shot of the dam", "map of Lake Nasser", "farm fields along the Nile"]}
    assert b.score_v3(st, json.dumps(good))["score"] == 1.0
    assert b.score_v3(st, json.dumps({**good, "hook": "The dam cost 1 billion dollars"}))["score"] < 1.0
    c = ITEMS["instr3_1"]
    assert b.score_v3(c, "1. Red\n2. Orange\n3. Yellow\n4. Green")["score"] == 1.0
    assert b.score_v3(c, "- Red\n- Orange\n- Yellow")["score"] == 0.0
    assert b.score_v3(ITEMS["instr3_3"], "OCEAN OCEAN OCEAN")["score"] == 1.0
    assert b.score_v3(ITEMS["instr3_11"], "apple, banana, cherry, mango, pear")["score"] == 1.0
    assert b.score_v3(ITEMS["instr3_11"], "Pear, apple, banana")["score"] < 0.5
    assert b.score_v3(ITEMS["instr3_10"], "The Moon orbits Earth. It has no air.")["score"] == 1.0


def test_v2_items_keep_v2_scorers():
    from nexus.intelligence.model_router import bench_v2
    gh = ITEMS["grounded_hook"]
    assert b.score_v3(gh, "The 310 m footbridge, opened in 2019, rises 48 m above the river.")["score"] == 1.0
    assert b.score_v3(gh, "not stated")["score"] == 0.0 and b.score_v3(gh, "The 52 m footbridge.")["score"] < 1.0
    it = ITEMS["synth"]
    out = json.dumps({"claims": [{"text": "opened in 2019", "sources": ["S1", "S2"]}], "conflicts": ["height 48 m vs 52 m"]})
    assert b.score_v3(it, out) == bench_v2.score_v2(it, out)


def test_results_statistics_and_routing_always_review_dialect():
    rows = lambda v: [{"id": i, "text": v} for i in ITEMS]   # noqa: E731
    raw = {"models": {"m1": {"spec": {}, "items": rows(_ans("not stated"))}, "m2": {"spec": {}, "items": rows("")}}}
    sc = b.score_results(raw)
    assert sc["m1"]["n"]["dialect"] == 30 and all(sc["m1"]["n"][c] >= 15 for c in b.CAPS)
    lo, hi = sc["m1"]["ci95"]["grounding"]
    assert 0 <= lo <= sc["m1"]["capabilities"]["grounding"] <= hi <= 1
    r = b.routing(sc)
    assert r["dialect_adaptation"]["requires_human_review"] is True
    assert "conflicts" in r["deep_research"]["capabilities"] and "separable_from_next" in r["general"]
    assert b.ci95([1.0]) == (0.0, 1.0)


def test_kernel_carries_identical_prompts_for_every_model():
    src = b.kernel_source(["qwen3-4b", "nemotron-nano-9b-v2"])
    items = json.loads(eval(src.split("ITEMS = json.loads(")[1].split(")\nMODELS")[0]))   # the literal embedded in the job
    assert [i["id"] for i in items] == list(ITEMS) and all(i["prompt"] == ITEMS[i["id"]]["prompt"] for i in items)


@pytest.mark.parametrize("junk", ["", "x", '{"answer": "not stated"}', "I cannot help with that."])
def test_degenerate_outputs_earn_almost_nothing(junk):
    """Absence-only checks (no invented numbers, no forbidden word, within a length) must not reward an empty answer."""
    by = {}
    for it in ITEMS.values():
        by.setdefault(it["cap"], []).append(b.score_v3(it, junk)["score"])
    for cap, xs in by.items():
        if cap == "grounding":                         # 4 of 15 items correctly expect "not stated"
            assert sum(xs) / len(xs) <= 0.29, (cap, junk)
        else:
            assert sum(xs) / len(xs) <= 0.12, (cap, junk, sum(xs) / len(xs))


def test_visible_reasoning_in_a_non_thinking_run_invalidates_it():
    from nexus.intelligence.model_router import bench_v3 as b3
    leak = {"spec": {"think": False}, "rows": [{"text": "Okay, so I need to translate this..."}] * 4 + [{"text": "Bonjour."}] * 6}
    ok, why = b3.valid_run(leak)
    assert not ok and "visible reasoning" in why
    assert b3.valid_run({**leak, "spec": {"think": True}})[0]           # thinking models are judged on their stripped answer
    assert b3.valid_run({"spec": {}, "rows": [{"text": "Bonjour."}] * 10})[0]
    scored = {"good": {"capabilities": {c: 0.5 for c in b3.CAPS + ["dialect"]}, "ci95": {}, "rows": [{"text": "x"}], "spec": {}},
              "leaky": {**leak, "capabilities": {c: 0.9 for c in b3.CAPS + ["dialect"]}, "ci95": {}}}
    assert {x["best"] for x in b3.routing(scored).values()} == {"good"}
