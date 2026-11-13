"""Live batch_llm benchmark on the authorised Kaggle GPU worker (small: 8 prompts, one ~1 GB Apache-2.0 model).

    python scripts/acceptance/batch_llm_live.py [--model Qwen/Qwen2.5-0.5B-Instruct] [--out docs/results/batch_llm_live.json]

Uses the existing `kaggle_primary` vault connection only (no second account, no paid compute). The model is downloaded
inside the Kaggle kernel, never on this machine. The evidence file holds metrics, checksums and the answers, no secrets.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nexus import vault  # noqa: E402
from nexus.cloud.compute import dispatcher as dsp  # noqa: E402
from nexus.contracts import ProductionJobManifest  # noqa: E402

ITEMS = [
    {"id": "sum1", "task": "summarization", "system": "Answer in one sentence.",
     "prompt": "Summarise: The Kaggle notebook ran a batch of prompts on a T4 GPU, wrote the answers to a file and exited.", "max_tokens": 48},
    {"id": "tr_ar", "task": "translation", "system": "Translate to Arabic. Output only the translation.",
     "prompt": "The video is ready for review.", "max_tokens": 32},
    {"id": "tr_fr", "task": "translation", "system": "Translate to French. Output only the translation.",
     "prompt": "Upload the file before Friday.", "max_tokens": 32},
    {"id": "json1", "task": "data_extraction", "system": "Return only JSON.",
     "prompt": 'Extract {"city": str, "year": int} from: "The festival moved to Lyon in 2019."', "max_tokens": 32},
    {"id": "title1", "task": "title_generation", "system": "Give one short video title.",
     "prompt": "A documentary about how bees navigate using the sun.", "max_tokens": 24},
    {"id": "math1", "task": "general", "system": "Answer with a number only.", "prompt": "What is 17 * 23?", "max_tokens": 8},
    {"id": "class1", "task": "audience_classification", "system": "Answer with one word: kids, teens or adults.",
     "prompt": "Video: 'Mortgage refinancing explained step by step'.", "max_tokens": 4},
    {"id": "hook1", "task": "hook_generation", "system": "Write one opening line for a short video.",
     "prompt": "Topic: why the sky is blue.", "max_tokens": 32},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    ap.add_argument("--licence", default="Apache-2.0")
    ap.add_argument("--out", default="docs/results/batch_llm_live.json")
    ap.add_argument("--timeout", type=float, default=3600)
    a = ap.parse_args()
    if "kaggle_primary" not in vault.names():
        print("kaggle_primary is not connected (store the token with nexus.vault.put; never paste it in chat)")
        return 2
    m = ProductionJobManifest(task="batch_llm", owner_account="account3", max_runtime_s=1800, max_retries=1,
                              params={"engine": "transformers", "model": a.model, "model_licence": a.licence, "items": ITEMS,
                                      "part_size": 4, "batch_size": 4, "max_new_tokens": 64, "max_model_gb": 3.0, "temperature": 0},
                              idempotency_key=f"acceptance:batch_llm_live:{a.model}:{time.strftime('%Y%m%d')}")
    r = dsp.submit(m)
    print("submitted", r)
    row = dsp.get().run_until_done(r["job_id"], timeout=a.timeout, interval=15)
    res = json.loads(row["result"]) if row.get("result") else {}
    rows = []
    art = next((x for x in dsp.job_artifacts(row["id"]) if x["name"].startswith("batch_llm_results.")), None)
    if art:
        rows = [json.loads(x) for x in dsp.artifact_path(art).read_text(encoding="utf-8").splitlines() if x.strip()]
    ev = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": "LIVE (real Kaggle GPU, real model)",
          "job_id": row["id"], "status": row["status"], "error": row.get("error"), "provider": res.get("provider"),
          "accelerator": res.get("accelerator"), "duration_s": res.get("duration_s"), "attempts": res.get("attempts"),
          "postprocess": res.get("postprocess"), "model": a.model, "licence": a.licence,
          "metrics": {k: (res.get("metrics") or {}).get(k) for k in ("batch_llm", "items_ok", "items_error", "completion_tokens",
                                                                       "completion_tokens_per_s", "model_load_s", "model_download_bytes",
                                                                       "model_weight_bytes", "peak_vram_allocated_mb", "gpu_util_max", "task_s")},
          "artifacts": [{"name": x["name"], "size": x["size"], "sha256": x["sha256"]} for x in dsp.job_artifacts(row["id"])],
          "answers": rows}
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = re.sub(r'"ref": "[^"/]+/', '"ref": "<kaggle-user>/', json.dumps(ev, indent=1, ensure_ascii=False))  # no account ids
    out.write_text(vault.redact(text), encoding="utf-8")
    print(json.dumps({k: ev[k] for k in ("status", "error", "accelerator", "duration_s", "postprocess")}, indent=1))
    return 0 if row["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
