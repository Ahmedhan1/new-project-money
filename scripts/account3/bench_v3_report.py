"""Write docs/ACCOUNT3_MODEL_BENCHMARKS_V3.md from docs/results/account3/models_bench_v3.json.

    python scripts/account3/bench_v3_report.py [SCORED.json OUT.md]
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from nexus.intelligence.model_router import bench_v2, bench_v3
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/results/account3/models_bench_v3.json"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs/ACCOUNT3_MODEL_BENCHMARKS_V3.md"
    d = json.loads(src.read_text(encoding="utf-8"))
    caps = bench_v3.CAPS + ["dialect"]
    n = Counter(it["cap"] for it in bench_v3.taskset())
    full = {k: v for k, v in d["models"].items() if k not in d.get("incomplete", {})}
    rows = []
    invalid = {k: bench_v3.valid_run(m)[1] for k, m in full.items() if not bench_v3.valid_run(m)[0]}
    for k, m in sorted(full.items(), key=lambda x: (x[0] in invalid, -x[1]["overall"])):
        ok, why = bench_v3.valid_run(m)
        spec = bench_v2.MODELS.get(k, {})
        cells = []
        for c in caps:
            v, (lo, hi) = m["capabilities"].get(c), m["ci95"].get(c, (None, None))
            cells.append("—" if v is None else f"{v:.2f} [{lo:.2f}–{hi:.2f}]")
        rows.append(f"| {k} | {spec.get('params_b')} | {m['overall'] if ok else '**invalid** ' + why} | " + " | ".join(cells)
                    + f" | {m['mean_latency_s']} | {m['median_tokens_per_s']} |")
    rt = []
    for t, x in d["routing"].items():
        sep = {True: "yes", False: "**no** (intervals overlap)", None: "—"}[x.get("separable_from_next")]
        nxt = ", ".join(f"{r['model']} {r['score']}" for r in x["ranking"][1:3])
        rt.append(f"| {t} | {x['best']} | {x['score']} | {x['bar']} | {'yes' if x['meets_bar'] else 'no'} | "
                  f"{'**required**' if x['requires_human_review'] else ('critical' if x['critical'] else '')} | {sep} | {nxt} |")
    runs = ", ".join(f"`{r['file']}`" for r in d["runs"])
    inc = "; ".join(f"{k}: {v}" for k, v in d.get("incomplete", {}).items()) or "none"
    doc = f"""# Account 3 — open-weight model benchmark V3 (Kaggle GPUs, {d['items']} items)

**What ran:** {d['items']} auto-scored items, identical for every model, at least 15 per routed capability
({', '.join(f'{c} {n[c]}' for c in bench_v3.CAPS)}). Harness unchanged from V2: Account 1's authorised Kaggle worker
(2× NVIDIA T4), Ollama, GGUF **Q4_K_M**, temperature 0, schema-constrained JSON where an item asks for structure,
context 4 096 tokens (8 192 for long-context items). Free Kaggle GPU quota only; no paid endpoint was touched.
Items and scoring rules: `nexus/intelligence/model_router/bench_v3.py` (docstring lists every rule). Raw outputs: {runs};
scored: `docs/results/account3/models_bench_v3.json`. Incomplete runs (excluded from the table): {inc}.
Invalid runs (shown last, excluded from routing): {"; ".join(f"{k}: {v}" for k, v in invalid.items()) or "none"}.

Reproduce: `python -m nexus.intelligence.model_router.bench_v3 kernel --models A,B --out DIR --slug SLUG`, push with
`scripts/account3/kaggle_cli.py kernels push -p DIR`, then `scripts/account3/bench_v3_score.py OUT raw_*.json` and
`scripts/account3/bench_v3_report.py`. Scoring needs no model, so anyone can re-score the raw outputs.

## Results (mean 0–1 per capability, [95 % interval]; latency = mean s/item)
| Model | Params (B) | Overall | {' | '.join(caps)} | latency s | tok/s |
|---|---|---|{'---|' * len(caps)}---|---|
{chr(10).join(rows)}

## How to read this (limits)
- Intervals are normal approximations over 15–19 items. Two models whose intervals overlap are **not** shown to differ;
  the routing table marks this per task. These are measurements on one fixed item set, not general rankings.
- Every scorer is a rule, not a judge: it checks format, numbers, facts named in the item, and reference overlap
  (chrF for translation). It cannot see fluency or tone. Dialect scores rest on marker lexicons (`EGY`, `GULF`, `MSA`
  in `bench_v3.py`) and are a proxy only — a native reviewer is mandatory before any dialect text is published.
- Context was capped at 4k/8k tokens to fit two T4s; long-context results are for documents of ~2 000–2 800 words.
- V3 re-scores the 36 V2 items with reference-based translation and the V3 dialect lexicon, so V3 numbers are on their
  own scale; compare models within V3, not V3 with V2.

## Task routing (measured; bar = router min quality / 10)
| Task | Best model | Score | Bar | Meets bar | Human review | Best separable from 2nd? | Next best |
|---|---|---|---|---|---|---|---|
{chr(10).join(rt)}

Critical tasks (`router.CRITICAL`) below the bar, or below 0.7, carry `requires_human_review`; dialect adaptation
always does. These models are batch-only (Kaggle forbids persistent serving); interactive work stays local.
"""
    dst.write_text(doc, encoding="utf-8")
    print("ok", len(rows), "models")


if __name__ == "__main__":
    main()
