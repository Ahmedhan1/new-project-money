"""Local LLM (llama.cpp + Qwen3-4B, Apache-2.0). No remote API is ever used."""
from __future__ import annotations

import gc
import json
import re
import threading
import time

from . import costguard, db, models

_llm = None
_lock = threading.Lock()


def load():
    global _llm
    if _llm is None:
        costguard.authorize("local-llm", "load")
        from llama_cpp import Llama
        path = models.ensure_model("llm-qwen3-4b") / "Qwen3-4B-Q4_K_M.gguf"
        _llm = Llama(model_path=str(path), n_ctx=8192, n_threads=4, n_batch=256, verbose=False)
    return _llm


def unload() -> None:
    """Free ~3 GB so that rendering/Blender can use the RAM (memory-aware scheduling)."""
    global _llm
    _llm = None
    gc.collect()


def chat(prompt: str, system: str = "You are a precise assistant.", max_tokens: int = 900, temperature: float = 0.5,
         json_mode: bool = False, schema: dict | None = None) -> str:
    costguard.authorize("local-llm", "generate")
    from . import jobs
    with _lock:
        jobs.note("writing with the local language model (CPU, can take a few minutes)")
        t0 = time.time()
        # With a schema the sampler is grammar-constrained: required keys and array lengths cannot be skipped.
        kw = {"response_format": {"type": "json_object", **({"schema": schema} if schema else {})}} if json_mode or schema else {}
        # /no_think disables Qwen3's reasoning block: 3-5x faster on CPU
        r = load().create_chat_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt + " /no_think"}],
            max_tokens=max_tokens, temperature=temperature, top_p=0.9, repeat_penalty=1.05, **kw)
        txt = r["choices"][0]["message"]["content"] or ""
        n = r["usage"]["completion_tokens"]
        if n > 20:
            db.metric("llm_tok_per_s", n / max(time.time() - t0, 0.01))
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def chat_json(prompt: str, system: str = "You output only valid JSON.", max_tokens: int = 1400, temperature: float = 0.5,
              retries: int = 2, require: str | None = None, schema: dict | None = None):
    """`require`: a key that must be present and non-empty. Small local models sometimes answer `{}` in JSON mode;
    that is treated like invalid JSON and retried (slightly warmer) instead of being passed on as an empty result."""
    last = ""
    for i in range(retries + 1):
        last = chat(prompt, system, max_tokens, min(1.0, temperature + 0.15 * i), json_mode=True, schema=schema)
        data = None
        try:
            data = json.loads(last)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}|\[.*\]", last, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        if data is not None and (require is None or (isinstance(data, dict) and data.get(require))):
            return data
    raise ValueError(f"LLM did not return {'a usable ' + require + ' field' if require else 'valid JSON'}: {last[:200]}")
