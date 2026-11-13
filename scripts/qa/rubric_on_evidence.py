"""Apply the QLT-1 reviewer rubric to stored real renders (Account 3 Demo 10 runs 1-4). The stored evidence has scene
texts and visual kinds, but no image sources or per-check audio results, so those dimensions are reported unknown -
never guessed.

    python scripts/qa/rubric_on_evidence.py --out docs/results/final_qa/rubric_demo10.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nexus.intelligence.quality import rubric

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = []
    for n in (1, 2, 3, 4):
        f = ROOT / f"docs/results/account3/demo10_run{n}_e2e.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        sc = d["steps"].get("strategy", {}).get("scenes") or []
        script = {"scenes": [{"narration": s if isinstance(s, str) else s.get("narration", ""), "on_screen_text": ""} for s in sc]}
        plan = [{"query": "stored", **p} for p in d["steps"].get("render", {}).get("visual_plan") or []]
        r = rubric.score(script, plan, None)
        rows.append({"run": n, "evidence": str(f.relative_to(ROOT)), "overall": r["overall"], "verdict": r["verdict"], "unknown": r["unknown"],
                     "dimensions": {k: v for k, v in r["dimensions"].items()},
                     "visual_kinds": [p.get("kind") for p in plan]})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"schema": "nexus.RubricOnEvidence/1", "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    for r in rows:
        print(r["run"], r["overall"], r["verdict"], {k: v["score"] for k, v in r["dimensions"].items()}, r["visual_kinds"])


if __name__ == "__main__":
    main()
