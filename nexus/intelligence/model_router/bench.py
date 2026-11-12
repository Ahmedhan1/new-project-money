"""Reproducible model benchmark with automatic scoring (no human judgement, no LLM-as-judge).

    NEXUS_HOME=/home/user/new-project-money/.nexus python -m nexus.intelligence.model_router bench \
        [--providers local-llamacpp,owner-openai-compatible] [--no-backtranslate] [--out docs/results/account3/models_bench.json]

Fixed task set (TASKSET); every item has a pure scorer returning 0..1:
  json        structured JSON validity (parse + keys + types)                         -> ai_quality.json
  extract     extraction accuracy against a gold record                               -> ai_quality.json
  reason      three short arithmetic/logic questions with exact answers               -> ai_quality.reasoning
  summary     keyword recall + word limit                                             -> ai_quality.summarization
  translate   de/fr/ar/zh/ru/hi: target-script share (nexus.speech.packs.own_share)
              + chrF similarity of a MADLAD back-translation to the English source   -> ai_quality.translation
  dialect     Egyptian vs Gulf Arabic marker words (own markers present, other absent) -> ai_quality.arabic
  titles      title constraints (count, length, keyword, distinct, no shouting)      -> ai_quality.writing
Also recorded per item: latency, output tokens, tokens/s, failures. Per-capability means are stored as db metrics
`ai_quality.<cap>` (scale 0-10, meta {model, provider, source: "bench"}) which the router uses instead of estimates.
Heavy work runs under jobs.heavy_lock(), one provider at a time; local models are unloaded before the lock is released.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from ... import config, db

SYS = "You are a precise assistant. Follow the format exactly."
LANGS = {"de": "German", "fr": "French", "ar": "Modern Standard Arabic", "zh": "Simplified Chinese", "ru": "Russian", "hi": "Hindi"}
TRANSLATE_SRC = "The museum opens at nine in the morning. Children under twelve enter for free."
EGY = ["دلوقتي", "عايز", "عايزة", "إزاي", "ازاي", "كده", "بتاع", "أوي", "اوي", "ده", "دي", "إيه", "ايه", "مش", "هنروح", "بكرة"]
GULF = ["الحين", "وايد", "شلون", "أبي", "ابي", "ابغى", "أبغى", "زين", "هاذي", "شنو", "عاد", "يالله", "حق", "بروح", "باكر"]

EXTRACT_TEXT = ("On 14 March 2026 Lina Haddad and Marco Rossi signed the lease for the studio in Lyon. "
                "The monthly rent is 1250 euros and the deposit is 2500 euros.")
EXTRACT_GOLD = {"people": ["Lina Haddad", "Marco Rossi"], "date": "2026-03-14", "city": "Lyon", "rent_eur": 1250, "deposit_eur": 2500}
SUMMARY_TEXT = ("Honeybees communicate the location of flowers through a waggle dance. The angle of the dance relative to "
                "vertical encodes the direction of the food source relative to the sun, while the duration of the waggle "
                "run encodes distance. Researchers decoded this behaviour in the twentieth century, and Karl von Frisch "
                "received a Nobel Prize for the work in 1973. Recent studies show that young bees improve their dance "
                "accuracy by watching experienced foragers.")
SUMMARY_KEYS = [("dance", "waggle"), ("direction", "angle"), ("distance",), ("sun",), ("frisch", "nobel")]
REASON_QS = [("A train leaves at 14:40 and the trip takes 95 minutes. At what time does it arrive? Answer as HH:MM.", "16:15"),
             ("Mia has 3 times as many stamps as Leo. Together they have 48. How many stamps does Mia have?", "36"),
             ("If all bloops are razzies and no razzies are lazzies, can a bloop be a lazzie? Answer yes or no.", "no")]


# ----------------------------------------------------------------- scorers (pure, unit-tested)
def parse_json(text: str):
    from .router import _parse
    return _parse(text)


def score_json(text: str) -> float:
    d = parse_json(text)
    if not isinstance(d, dict):
        return 0.0
    checks = [isinstance(d.get("title"), str) and 0 < len(d["title"]) <= 80,
              isinstance(d.get("tags"), list) and len(d["tags"]) == 3 and all(isinstance(t, str) for t in d["tags"]),
              d.get("language") == "en", isinstance(d.get("duration_s"), (int, float)) and not isinstance(d.get("duration_s"), bool)]
    return round(0.2 + 0.8 * sum(checks) / len(checks), 3)


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def score_extract(text: str) -> float:
    d = parse_json(text)
    if not isinstance(d, dict):
        return 0.0
    people = {_norm(p) for p in d.get("people") or [] if isinstance(p, str)}
    num = lambda v: float(re.sub(r"[^\d.]", "", str(v)) or "nan")  # noqa: E731
    checks = [people == {_norm(p) for p in EXTRACT_GOLD["people"]}, _norm(d.get("date", "")) == EXTRACT_GOLD["date"],
              _norm(d.get("city", "")) == "lyon", num(d.get("rent_eur")) == 1250, num(d.get("deposit_eur")) == 2500]
    return round(sum(checks) / len(checks), 3)


def score_reason(text: str, gold: str) -> float:
    d = parse_json(text)
    ans = _norm(d.get("answer", "")) if isinstance(d, dict) else _norm(text)
    if gold == "16:15":
        return 1.0 if re.search(r"\b16[:.]15\b", ans) else 0.0
    if gold.isdigit():
        nums = re.findall(r"\d+", ans)
        return 1.0 if nums and nums[0] == gold else 0.0
    return 1.0 if re.match(rf"^\W*{gold}\b", ans) else 0.0


def score_summary(text: str, max_words: int = 40) -> float:
    low = (text or "").lower()
    recall = sum(any(k in low for k in alts) for alts in SUMMARY_KEYS) / len(SUMMARY_KEYS)
    words = len(low.split())
    if not words:
        return 0.0
    return round(recall * (1.0 if words <= max_words else max(0.0, 1 - (words - max_words) / max_words)), 3)


def chrf(hyp: str, ref: str, n: int = 6, beta: float = 2.0) -> float:
    """Character n-gram F-score (chrF, Popovic 2015) on lower-cased text without spaces; 0..1."""
    h, r = re.sub(r"\s+", "", (hyp or "").lower()), re.sub(r"\s+", "", (ref or "").lower())
    precs, recs = [], []
    for k in range(1, n + 1):
        hg = defaultdict(int)
        rg = defaultdict(int)
        for i in range(len(h) - k + 1):
            hg[h[i:i + k]] += 1
        for i in range(len(r) - k + 1):
            rg[r[i:i + k]] += 1
        if not hg or not rg:
            continue
        m = sum(min(c, rg[g]) for g, c in hg.items())
        precs.append(m / sum(hg.values()))
        recs.append(m / sum(rg.values()))
    if not precs:
        return 0.0
    p, rc = sum(precs) / len(precs), sum(recs) / len(recs)
    return 0.0 if p + rc == 0 else round((1 + beta ** 2) * p * rc / (beta ** 2 * p + rc), 4)


def script_share(text: str, lang: str) -> float:
    from ...speech import packs
    return round(packs.own_share(text or "", lang), 3)


def score_translation(text: str, lang: str, back: str | None) -> dict:
    """0.4 x target-script share (>= 0.9 counts fully) + 0.6 x chrF(back-translation, source). Without a
    back-translation only the script part is scored and `sim` is None (reported, never invented)."""
    share = script_share(text, lang)
    s_part = min(1.0, share / 0.9)
    if not (text or "").strip():
        return {"score": 0.0, "script_share": share, "sim": None}
    if back is None:
        return {"score": round(s_part, 3), "script_share": share, "sim": None}
    sim = chrf(back, TRANSLATE_SRC)
    return {"score": round(0.4 * s_part + 0.6 * min(1.0, sim / 0.6), 3), "script_share": share, "sim": sim}


def markers(text: str, words: list[str]) -> list[str]:
    toks = set(re.findall(r"[؀-ۿ]+", text or ""))
    return [w for w in words if w in toks]


def score_dialect(text: str, want: str) -> dict:
    own, other = (EGY, GULF) if want == "egy" else (GULF, EGY)
    mo, mx = markers(text, own), markers(text, other)
    arabic = script_share(text, "ar") >= 0.8
    score = (0.2 if arabic else 0.0) + (0.5 if mo else 0.0) + (0.3 if not mx and mo else 0.0)
    return {"score": round(score, 3), "own_markers": mo, "other_markers": mx}


def score_titles(text: str, keyword: str = "bees") -> float:
    d = parse_json(text)
    titles = d.get("titles") if isinstance(d, dict) else None
    if not isinstance(titles, list) or not titles:
        return 0.0
    titles = [t for t in titles if isinstance(t, str)]
    checks = [len(titles) == 3, all(0 < len(t) <= 60 for t in titles), all(keyword in t.lower() for t in titles),
              len({t.lower() for t in titles}) == len(titles), not any(t.isupper() or "!!" in t for t in titles)]
    return round(sum(checks) / len(checks), 3)


# ----------------------------------------------------------------- task set
def taskset() -> list[dict]:
    items = [
        {"id": "json", "cap": "json", "schema": True, "max_tokens": 160,
         "prompt": 'Describe this video as JSON with keys "title" (string), "tags" (exactly 3 strings), "language" '
                   '(ISO code) and "duration_s" (number): a 90-second English explainer about how volcanoes form.'},
        {"id": "extract", "cap": "json", "schema": True, "max_tokens": 160,
         "prompt": 'Extract from the text a JSON object with keys "people" (list of full names), "date" (YYYY-MM-DD), '
                   f'"city", "rent_eur" (number), "deposit_eur" (number).\nText: {EXTRACT_TEXT}'},
        *[{"id": f"reason{i}", "cap": "reasoning", "schema": True, "max_tokens": 80, "gold": g,
           "prompt": f'{q}\nReply as JSON: {{"answer": "<answer>"}}'} for i, (q, g) in enumerate(REASON_QS, 1)],
        {"id": "summary", "cap": "summarization", "max_tokens": 120,
         "prompt": f"Summarise the text in at most 40 words, plain prose.\nText: {SUMMARY_TEXT}"},
        *[{"id": f"translate_{lg}", "cap": "translation", "lang": lg, "max_tokens": 160,
           "prompt": f"Translate into {name}. Output only the translation.\nText: {TRANSLATE_SRC}"} for lg, name in LANGS.items()],
        {"id": "dialect_egy", "cap": "arabic", "want": "egy", "max_tokens": 100,
         "prompt": "اكتب جملتين باللهجة المصرية العامية (مش الفصحى) عن الخروج مع الأصحاب يوم الجمعة. اكتب الجملتين فقط."},
        {"id": "dialect_gulf", "cap": "arabic", "want": "gulf", "max_tokens": 100,
         "prompt": "اكتب جملتين باللهجة الخليجية العامية (مو الفصحى) عن الخروج مع الربع يوم الجمعة. اكتب الجملتين فقط."},
        {"id": "titles", "cap": "writing", "schema": True, "max_tokens": 160,
         "prompt": 'Write 3 different YouTube titles for a video about how bees communicate. Each title at most 60 '
                   'characters, must contain the word "bees", no all-caps, no clickbait. Reply as JSON {"titles": [..]}.'},
    ]
    return items


def score_item(it: dict, text: str, back: str | None = None) -> dict:
    i = it["id"]
    if i == "json":
        return {"score": score_json(text)}
    if i == "extract":
        return {"score": score_extract(text)}
    if i.startswith("reason"):
        return {"score": score_reason(text, it["gold"])}
    if i == "summary":
        return {"score": score_summary(text)}
    if i.startswith("translate_"):
        return score_translation(text, it["lang"], back)
    if i.startswith("dialect_"):
        return score_dialect(text, it["want"])
    if i == "titles":
        return {"score": score_titles(text)}
    raise KeyError(i)


def aggregate(rows: list[dict]) -> dict:
    caps: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        caps[r["cap"]].append(r["score"] if not r.get("error") else 0.0)
    lat = [r["latency_s"] for r in rows if not r.get("error")]
    tps = [r["tokens_per_s"] for r in rows if r.get("tokens_per_s")]
    return {"capabilities": {c: round(sum(v) / len(v), 3) for c, v in caps.items()},
            "overall": round(sum(r["score"] if not r.get("error") else 0 for r in rows) / max(len(rows), 1), 3),
            "failures": sum(1 for r in rows if r.get("error")), "items": len(rows),
            "mean_latency_s": round(sum(lat) / len(lat), 2) if lat else None,
            "mean_tokens_per_s": round(sum(tps) / len(tps), 2) if tps else None}


def store_quality(provider: str, model: str, agg: dict, run_id: str) -> None:
    for cap, v in agg["capabilities"].items():
        db.metric(f"ai_quality.{cap}", round(v * 10, 2), model=model, provider=provider, source="bench", run=run_id)


# ----------------------------------------------------------------- running
def _count_tokens(entry: dict, text: str, usage: dict) -> tuple[int, bool]:
    if usage.get("completion_tokens"):
        return int(usage["completion_tokens"]), False
    if entry.get("kind") == "llamacpp_inprocess":
        from ... import llm
        return len(llm.load().tokenize(text.encode("utf-8"), add_bos=False)), False
    from .router import estimate_tokens
    return estimate_tokens(text), True


def run_entry(entry: dict, items: list[dict], log=print) -> list[dict]:
    from ..providers import call
    rows = []
    for it in items:
        schema = {"type": "object"} if it.get("schema") else None
        t0 = time.time()
        try:
            text, usage = call(entry, it["prompt"], SYS, schema, it["max_tokens"], 0.2)
            err = None
        except Exception as e:  # recorded as a failure, scored 0
            text, usage, err = "", {}, f"{type(e).__name__}: {str(e)[:200]}"
        lat = round(time.time() - t0, 2)
        ntok, est = _count_tokens(entry, text, usage) if text else (0, False)
        rows.append({"id": it["id"], "cap": it["cap"], "output": text[:600], "latency_s": lat, "output_tokens": ntok,
                     "tokens_estimated": est, "tokens_per_s": round(ntok / lat, 2) if lat and ntok else None, "error": err})
        log(f"  {entry['model']:16s} {it['id']:14s} {lat:6.1f}s {ntok:4d} tok {'ERR ' + err if err else ''}")
    return rows


def backtranslate(rows_by_model: dict[str, list[dict]]) -> None:
    """Back-translate every translation output into English with MADLAD-400 (one batch, under the heavy lock)."""
    from ... import jobs
    from ...speech import mt
    todo = [(m, r) for m, rows in rows_by_model.items() for r in rows if r["id"].startswith("translate_") and r["output"].strip()]
    if not todo:
        return
    with jobs.heavy_lock():
        try:
            outs = mt.translate([r["output"].replace("\n", " ") for _, r in todo], "en")
        finally:
            mt.unload()
    for (_, r), b in zip(todo, outs):
        r["back_translation"] = b


def main(argv: list[str] | None = None) -> int:
    from ... import jobs, llm
    from .. import capability_registry as reg
    from ..providers import local_server
    ap = argparse.ArgumentParser(prog="model_router bench")
    ap.add_argument("--providers", default="local-llamacpp,owner-openai-compatible")
    ap.add_argument("--server-model", default=str(config.MODELS / "hf" / "qwen3-0.6b-gguf" / "Qwen3-0.6B-Q8_0.gguf"))
    ap.add_argument("--server-alias", default="qwen3-0.6b-q8")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-backtranslate", action="store_true")
    ap.add_argument("--no-store", action="store_true", help="do not write ai_quality.* metrics")
    ap.add_argument("--out", default="docs/results/account3/models_bench.json")
    a = ap.parse_args(argv)
    items = taskset()
    run_id = time.strftime("bench-%Y%m%dT%H%M%S")
    rows_by_model: dict[str, list[dict]] = {}
    entries: dict[str, dict] = {}
    notes = []
    for pid in a.providers.split(","):
        if pid == "local-llamacpp":
            e = next(m for m in reg.models() if m["provider"] == pid)
            print(f"[{pid}] waiting for the heavy lock")
            with jobs.heavy_lock():
                try:
                    llm.load()
                    rows_by_model[e["model"]] = run_entry(e, items)
                finally:
                    llm.unload()
        elif pid == "owner-openai-compatible":
            base = reg.providers()[pid]
            prof = (reg.catalog().get("model_profiles") or {}).get(a.server_alias, {})
            e = {**{k: v for k, v in base.items() if k != "models"}, "provider": pid, **prof, "model": a.server_alias}
            print(f"[{pid}] waiting for the heavy lock")
            with jobs.heavy_lock():
                log = config.LOGS / "bench_llama_server.log"
                config.LOGS.mkdir(parents=True, exist_ok=True)
                with local_server.serve(Path(a.server_model), a.server_alias, a.port, log=log) as url:
                    e["base_url"] = url
                    from ..providers import list_models
                    notes.append(f"{pid}: GET /v1/models -> {list_models(url)}")
                    rows_by_model[e["model"]] = run_entry(e, items)
        else:
            notes.append(f"{pid}: skipped (hosted providers are not benchmarked without the owner's key)")
            continue
        entries[e["model"]] = e
    if not a.no_backtranslate:
        print("[mt] back-translating with MADLAD-400")
        backtranslate(rows_by_model)
    result = {"run": run_id, "date": time.strftime("%Y-%m-%d"), "machine": "4 vCPU, 15 GB RAM, no GPU (shared with other agents)",
              "scoring": __doc__.split("Fixed task set")[1].split("Also recorded")[0].strip(), "notes": notes, "models": {}}
    for model, rows in rows_by_model.items():
        it_by_id = {i["id"]: i for i in items}
        for r in rows:
            r.update(score_item(it_by_id[r["id"]], r["output"], None if a.no_backtranslate else r.get("back_translation", "")))
            if r.get("error"):
                r["score"] = 0.0
        agg = aggregate(rows)
        e = entries[model]
        if not a.no_store:
            store_quality(e["provider"], model, agg, run_id)
        result["models"][model] = {"provider": e["provider"], "hosting": e["hosting"], "licence": e.get("licence"),
                                   "weights": e.get("weights"), "summary": agg, "items": rows}
        print(f"{model}: {json.dumps(agg)}")
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"evidence -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
