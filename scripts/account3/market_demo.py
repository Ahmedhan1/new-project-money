"""Market intelligence demo (real network, real model router). Writes docs/results/account3/market_*.json.

    NEXUS_HOME=/home/user/new-project-money/.nexus .venv/bin/python scripts/account3/market_demo.py [--no-ai] [--record DIR]

Runs the full pipeline for two real topics (English science/tech; Arabic-language demand), the trend monitor on a
small watchlist, and checks the niche database categories. Every HTTP call goes through nexus.net (Cost Guard);
adapters whose hosts are not declared are reported as skipped. `--record DIR` saves every JSON response (used to build
the small fast-test fixtures in tests/fast/fixtures_market/).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nexus.intelligence.market import Ctx, analyse, niches, trend_monitor  # noqa: E402
from nexus.intelligence.market import brief as brief_mod  # noqa: E402

OUT = ROOT / "docs" / "results" / "account3"
TOPICS = [("James Webb Space Telescope", "science", "en", "market_report_jwst_en.json"),
          ("الذكاء الاصطناعي", "arabic_content", "ar", "market_report_ai_ar.json")]
WATCH = [{"topic": "Black hole", "niche": "science"}, {"topic": "Large language model", "niche": "technology"},
         {"topic": "Ramadan", "niche": "arabic_content"}, {"topic": "Solid-state battery", "niche": "technology"},
         {"topic": "Petra", "niche": "travel"}, {"topic": "رمضان", "niche": "arabic_content", "lang": "ar"},
         {"topic": "Shrinkflation", "niche": "consumer_awareness"}]


def recording(ctx: Ctx, folder: Path) -> Ctx:
    folder.mkdir(parents=True, exist_ok=True)
    inner = ctx.fetch_json

    def fetch(url, params=None):
        data = inner(url, params)
        key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:12]
        (folder / f"{key}.json").write_text(json.dumps({"url": url, "params": params, "response": data}, ensure_ascii=False), encoding="utf-8")
        return data
    ctx.fetch_json = fetch
    return ctx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--record")
    ap.add_argument("--topic", choices=["en", "ar"], help="run only this report")
    ap.add_argument("--only", choices=["reports", "trends", "niches"], default=None)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    ai = None if a.no_ai else brief_mod._router_ai
    if a.only in (None, "reports"):
        for topic, niche, lang, fname in [t for t in TOPICS if not a.topic or t[2] == a.topic]:
            ctx = Ctx()
            if a.record:
                recording(ctx, Path(a.record) / fname.removesuffix(".json"))
            t0 = time.time()
            rep = analyse(ctx, topic, niche, lang, ai=ai)
            rep["runtime_s"] = round(time.time() - t0, 1)
            (OUT / fname).write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=sorted), encoding="utf-8")
            o = rep["opportunity"]
            print(fname, "score", o["score"], "confidence", o["confidence"], "unknown", o["unknown_factors"],
                  "claims", len(rep["brief"]["claims"]), "verified", sum(c["status"] == "verified" for c in rep["brief"]["claims"]),
                  rep["brief"]["method"])
    if a.only in (None, "trends"):
        ctx = Ctx()
        tm = trend_monitor(ctx, WATCH)
        tm["http_calls"] = len(ctx.calls)
        (OUT / "market_trends.json").write_text(json.dumps(tm, ensure_ascii=False, indent=1), encoding="utf-8")
        print("trends", [(r["topic"], r["status"], r.get("ratio")) for r in tm["topics"]])
    if a.only in (None, "niches"):
        ctx = Ctx()
        db = niches.load()
        chk = [niches.check_categories(ctx, n) for n in db["niches"]]
        (OUT / "market_niches_check.json").write_text(json.dumps({"date": time.strftime("%Y-%m-%d"), "niches": len(db["niches"]),
                                                                  "categories": chk}, ensure_ascii=False, indent=1), encoding="utf-8")
        print("niche categories missing:", [(c["niche"], [k for k, v in c.get("exists", {}).items() if not v]) for c in chk])


if __name__ == "__main__":
    main()
