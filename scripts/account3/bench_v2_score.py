"""Score Kaggle benchmark outputs locally (translation back-checked with the local MADLAD-400 model).

    python scripts/account3/bench_v2_score.py OUT.json results_a.json [results_b.json ...]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> None:
    from nexus.intelligence.model_router import bench, bench_v2
    from nexus.speech import mt
    out, files = sys.argv[1], sys.argv[2:]
    raw = {"models": {}, "runs": []}
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        raw["models"].update(d["models"])
        raw["runs"].append({"file": Path(f).name, "started": d.get("started"), "finished": d.get("finished"), "env": d.get("env")})

    def back(lines, lang):
        try:
            return [" ".join(mt.translate(bench.split_sentences(x), "en")) for x in lines]
        except Exception as e:  # recorded as no back-translation: scored on the remaining checks
            print("back-translation unavailable:", e)
            return None

    scored = bench_v2.score_results(raw, back)
    mt.unload()
    doc = {"schema": "nexus.ModelBenchmarkV2/1", "scored": time.strftime("%Y-%m-%dT%H:%M:%S"), "runs": raw["runs"],
           "items": len(bench_v2.taskset()), "models": scored,
           "not_run": {k: (v.get("pull_log") or "")[-160:] for k, v in raw["models"].items() if not v.get("items")}}
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in scored.items():
        print(k, v["overall"], v["capabilities"], v["mean_latency_s"], v["median_tokens_per_s"])


if __name__ == "__main__":
    main()
