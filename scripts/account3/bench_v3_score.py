"""Score Bench V3 Kaggle outputs locally (deterministic rules only; no model is needed to score).

    python scripts/account3/bench_v3_score.py docs/results/account3/models_bench_v3.json raw_a.json [raw_b.json ...]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> None:
    from nexus.intelligence.model_router import bench_v3
    out, files = sys.argv[1], sys.argv[2:]
    raw = {"models": {}, "runs": []}
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        raw["models"].update(d["models"])
        raw["runs"].append({"file": Path(f).name, "started": d.get("started"), "finished": d.get("finished"), "env": d.get("env")})
    scored = bench_v3.score_results(raw)
    expected = len(bench_v3.taskset())
    doc = {"schema": "nexus.ModelBenchmarkV3/1", "scored": time.strftime("%Y-%m-%dT%H:%M:%S"), "runs": raw["runs"], "items": expected,
           "models": scored, "routing": bench_v3.routing({k: v for k, v in scored.items() if len(v["rows"]) == expected}),
           "incomplete": {k: f"{len(v['rows'])}/{expected} items" for k, v in scored.items() if len(v["rows"]) != expected},
           "not_run": {k: (v.get("pull_log") or "")[-160:] for k, v in raw["models"].items() if not v.get("items")}}
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in scored.items():
        print(k, len(v["rows"]), v["overall"], v["mean_latency_s"])


if __name__ == "__main__":
    main()
