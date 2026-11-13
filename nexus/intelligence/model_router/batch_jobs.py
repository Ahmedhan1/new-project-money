"""Account 3 benchmark items as Account 1 `batch_llm` jobs (ProductionJobManifest/v1).

Contract source: Account 1 `nexus/cloud/worker_kit/tasks/batch_llm.py` and `docs/ACCOUNT1_HANDOFF_TO_ACCOUNT3.md` §7
(branch work/account-1-cloud-platform). The rules mirrored here:
- one job = ONE Hugging Face model (`params.model`, "owner/name") and a finite list of prompts;
- the prompts themselves travel in the job: `params.items` as a LIST of {"id", "task"?, "system"?, "prompt",
  "max_tokens"?} (or a JSONL input artifact; this module uses the inline list), ids unique, at most `max_items`
  (hard cap 5000);
- engines: "transformers" (default), "vllm" (only if already in the Kaggle image), "echo" (deterministic test engine,
  loads no model, results are labelled simulated). There is no "ollama" engine and no `runtime` key: batch_llm is a
  bounded job, not a model server;
- routing is Kaggle only (TaskSpec providers=("kaggle",)); weights larger than `max_model_gb` are refused before download;
- `idempotency_key` = same job; a resubmission returns the existing one.

Nothing here submits anything. `validate_manifest` refuses the old A3 shape (params.models list, items as a count,
runtime "ollama") with a message naming the cause, instead of letting the worker fail later with a TypeError.
"""
from __future__ import annotations

import hashlib
import json
import re

HARD_MAX_ITEMS = 5000          # Account 1 BATCH_LLM_HARD_MAX_ITEMS
HARD_MAX_TOKENS = 4096         # Account 1 BATCH_LLM_HARD_MAX_TOKENS
DEFAULT_PROMPT_CHARS = 16000   # Account 1 default max_prompt_chars
ENGINES = ("transformers", "vllm", "echo")
REPO_RE = re.compile(r"[A-Za-z0-9][\w.-]*/[\w.-]+")   # the same pattern Account 1's worker checks
# fp16 weights must fit the authorised Kaggle worker (2x T4, 15 GB usable each) with room for activations / KV cache.
MAX_FP16_WEIGHT_GB = 24.0
ITEM_KEYS = {"id", "task", "system", "prompt", "max_tokens"}
PARAM_KEYS = {"model", "model_revision", "model_licence", "engine", "output_format", "max_items", "max_new_tokens",
              "max_prompt_chars", "batch_size", "part_size", "max_model_gb", "temperature", "seed", "chat_template_kwargs",
              "time_budget_s", "items", "items_input"}


class BatchManifestError(ValueError):
    """The manifest does not match Account 1's batch_llm contract; the message names the field and the fix."""


def fp16_weight_gb(params_b: float) -> float:
    return round(params_b * 2 * 1.0e9 / 2 ** 30, 1)


def batch_items(items: list[dict]) -> list[dict]:
    """Benchmark items -> batch_llm input rows (prompt text unchanged; JSON-schema constraints cannot be passed to
    batch_llm, so structured items rely on the instruction already in the prompt)."""
    out = []
    for it in items:
        row = {"id": str(it["id"]), "task": it.get("cap") or it.get("task"), "prompt": it["prompt"]}
        if it.get("system"):
            row["system"] = it["system"]
        if it.get("max_tokens"):
            row["max_tokens"] = int(it["max_tokens"])
        out.append(row)
    return out


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def manifest_for_model(run_id: str, model_key: str, items: list[dict], *, spec: dict | None = None, engine: str = "transformers",
                       max_new_tokens: int = 512, part_size: int = 32, batch_size: int = 8, max_runtime_s: float = 7200,
                       max_retries: int = 2) -> dict:
    """One ProductionJobManifest/v1 dict for one model. `items` are batch rows (see batch_items) or benchmark items."""
    from .bench_v2 import MODELS
    spec = spec or MODELS.get(model_key)
    if not spec:
        raise BatchManifestError(f"unknown model {model_key!r}: not in bench_v2.MODELS")
    rows = batch_items(items) if items and "cap" in items[0] else [dict(r) for r in items]
    weight_gb = fp16_weight_gb(float(spec["params_b"]))
    if engine != "echo" and weight_gb > MAX_FP16_WEIGHT_GB:
        raise BatchManifestError(f"{model_key}: ~{weight_gb} GiB of fp16 weights > {MAX_FP16_WEIGHT_GB} GiB that fit the "
                                 "Kaggle 2x T4 worker; batch_llm loads full-precision Hugging Face weights (no GGUF)")
    longest = max((len(r["prompt"]) + len(r.get("system") or "") for r in rows), default=0)
    params = {"model": spec["repo"], "model_licence": spec.get("licence", "unverified"), "engine": engine,
              "items": rows, "max_items": len(rows), "max_new_tokens": min(max_new_tokens, HARD_MAX_TOKENS),
              "max_prompt_chars": max(DEFAULT_PROMPT_CHARS, longest), "part_size": part_size, "batch_size": batch_size,
              "max_model_gb": round(weight_gb + 1.0, 1), "temperature": 0, "seed": 0, "output_format": "jsonl",
              "chat_template_kwargs": {"enable_thinking": bool(spec.get("think"))}}
    m = {"contract": "ProductionJobManifest/v1", "task": "batch_llm", "owner_account": "account3",
         "params": params, "inputs": [], "requires": {"gpu": True}, "priority": 5, "max_runtime_s": max_runtime_s,
         "max_retries": max_retries, "preferred_providers": ["kaggle"],
         "idempotency_key": f"a3:batch:{run_id}:{model_key}:{_digest(params)}"}
    validate_manifest(m)
    return m


def manifests(run_id: str, model_keys: list[str], items: list[dict], **kw) -> list[dict]:
    """One job per model (batch_llm takes exactly one model per job)."""
    if not model_keys:
        raise BatchManifestError("no models: batch_llm needs one job per model")
    return [manifest_for_model(run_id, k, items, **kw) for k in model_keys]


def validate_manifest(m: dict) -> dict:
    """Check a manifest against Account 1's batch_llm rules; raise BatchManifestError naming the problem.
    When Account 1's pydantic contract is importable (merged tree) it is validated with it as well."""
    if m.get("contract") != "ProductionJobManifest/v1":
        raise BatchManifestError("contract must be 'ProductionJobManifest/v1'")
    if m.get("task") != "batch_llm":
        raise BatchManifestError("task must be 'batch_llm'")
    if "runtime" in m or "runtime" in (m.get("params") or {}):
        raise BatchManifestError("'runtime' is not a batch_llm field (Ollama runs a server; batch_llm is a bounded job): "
                                 "use params.engine 'transformers' | 'vllm' | 'echo'")
    p = m.get("params")
    if not isinstance(p, dict):
        raise BatchManifestError("params must be an object")
    if "models" in p:
        raise BatchManifestError("params.models is not supported: batch_llm takes ONE params.model per job; submit one job per model")
    unknown = set(p) - PARAM_KEYS
    if unknown:
        raise BatchManifestError(f"unsupported batch_llm params: {sorted(unknown)}")
    if p.get("engine", "transformers") not in ENGINES:
        raise BatchManifestError(f"params.engine {p.get('engine')!r} is not a batch_llm engine {ENGINES}")
    if p.get("engine", "transformers") != "echo" and not (isinstance(p.get("model"), str) and REPO_RE.fullmatch(p["model"])):
        raise BatchManifestError("params.model must be a Hugging Face repo id 'owner/name'")
    items = p.get("items")
    if items is None:
        if not m.get("inputs"):
            raise BatchManifestError("batch_llm needs params.items (a list of prompts) or a JSONL input artifact in inputs")
    else:
        if not isinstance(items, list):
            raise BatchManifestError(f"params.items must be a LIST of prompt objects, not {type(items).__name__} "
                                     "(a count is not accepted: send the prompts themselves)")
        if not items:
            raise BatchManifestError("params.items is empty")
        seen = set()
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("id"), (str, int)) or not isinstance(it.get("prompt"), str):
                raise BatchManifestError("every item needs an 'id' (str/int) and a 'prompt' (str)")
            if set(it) - ITEM_KEYS:
                raise BatchManifestError(f"item {it['id']!r} has unsupported keys {sorted(set(it) - ITEM_KEYS)}")
            if str(it["id"]) in seen:
                raise BatchManifestError(f"duplicate item id {it['id']!r}")
            seen.add(str(it["id"]))
        cap = min(int(p.get("max_items", 500)), HARD_MAX_ITEMS)
        if len(items) > cap:
            raise BatchManifestError(f"{len(items)} items > max_items {cap}: split the batch into several jobs")
        longest = max(len(it["prompt"]) + len(it.get("system") or "") for it in items)
        if longest > int(p.get("max_prompt_chars", DEFAULT_PROMPT_CHARS)):
            raise BatchManifestError(f"a prompt has {longest} chars > max_prompt_chars; raise max_prompt_chars or shorten it")
    if int(p.get("max_new_tokens", 256)) > HARD_MAX_TOKENS:
        raise BatchManifestError(f"max_new_tokens > hard cap {HARD_MAX_TOKENS}")
    if not m.get("idempotency_key"):
        raise BatchManifestError("idempotency_key is required (same key = same job; resubmission returns the existing one)")
    if [x for x in m.get("preferred_providers") or [] if x != "kaggle"]:
        raise BatchManifestError("batch_llm runs on the authorised Kaggle worker only (preferred_providers ['kaggle'])")
    try:
        from nexus.contracts import ProductionJobManifest  # Account 1 contract (present on the merged tree only)
    except ImportError:
        return m
    try:
        ProductionJobManifest.model_validate(m)
    except Exception as e:  # noqa: BLE001 - pydantic ValidationError, re-raised with our type
        raise BatchManifestError(f"Account 1 ProductionJobManifest rejects it: {e}") from None
    return m


def results_to_bench_raw(model_key: str, rows: list[dict], spec: dict | None = None, *, simulated: bool) -> dict:
    """batch_llm_results rows -> the bench raw format (bench_v2/bench_v3.score_results). Latency and token counts come
    from the batch rows; `simulated` (echo engine) is carried so simulated runs are never mistaken for model results."""
    from .bench_v2 import MODELS
    items = []
    for r in rows:
        u = r.get("usage") or {}
        items.append({"id": r["id"], "text": r.get("text") or "", "error": r.get("error"), "latency_s": r.get("latency_s"),
                      "eval_count": u.get("completion_tokens"), "eval_s": r.get("latency_s")})
    return {"source": "account1.batch_llm", "simulated": simulated,
            "models": {model_key: {"spec": spec or MODELS.get(model_key, {}), "pull_ok": True, "items": items}}}
