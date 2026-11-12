"""Provider adapters. Two protocols cover every runtime/API in the catalogue:
  * llamacpp_inprocess  - nexus.llm (llama-cpp-python, Qwen3-4B) in this process
  * openai_compatible   - POST {base_url}/chat/completions: Ollama, vLLM, SGLang, llama.cpp server, Groq, Cerebras,
                          Gemini (OpenAI-compatible endpoint), OpenRouter, Mistral, Hugging Face router
Every network call passes the cost guard first."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from ... import config, costguard


class ProviderError(RuntimeError):
    pass


def call(entry: dict, prompt: str, system: str, schema: dict | None, max_tokens: int, temperature: float) -> tuple[str, dict]:
    kind = entry.get("kind")
    if kind == "llamacpp_inprocess":
        from ... import llm
        text = llm.chat(prompt, system, max_tokens=max_tokens, temperature=temperature, json_mode=schema is not None, schema=schema)
        return text, {}
    if kind == "openai_compatible":
        return openai_chat(entry, prompt, system, schema, max_tokens, temperature)
    raise ProviderError(f"no adapter for provider kind '{kind}'")


def _base_url(entry: dict) -> str:
    from ..model_router.router import provider_settings
    return (provider_settings(entry["provider"]).get("base_url") or entry.get("base_url") or "").rstrip("/")


def openai_chat(entry: dict, prompt: str, system: str, schema: dict | None, max_tokens: int, temperature: float,
                timeout: float = 120) -> tuple[str, dict]:
    from ..model_router.router import api_key
    url = _base_url(entry) + "/chat/completions"
    if entry["hosting"] == "hosted_free":
        costguard.check_host(url, free_tier_provider=entry["provider"])
    elif not url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        costguard.check_host(url)  # an owner worker on another machine must be a declared host
    # prompt_suffix: per-model switch from the catalogue, e.g. Qwen3's " /no_think" (reasoning off: 3-5x faster on CPU)
    user = prompt + (entry.get("prompt_suffix") or "")
    body = {"model": entry["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": temperature}
    if schema is not None:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json", "User-Agent": config.USER_AGENT}
    key = api_key(entry["provider"])
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        if e.code == 402:
            raise ProviderError(f"{entry['provider']}: HTTP 402 payment required - refusing (zero spend)") from e
        raise ProviderError(f"{entry['provider']}: HTTP {e.code} {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"{entry['provider']}: unreachable ({e})") from e
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(f"{entry['provider']}: malformed response") from e
    return strip_reasoning(text), data.get("usage") or {}


def strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks (Qwen3/DeepSeek reasoning traces) - callers want the answer only."""
    return re.sub(r"<think>.*?(</think>|$)", "", text or "", flags=re.S).strip()


def list_models(base_url: str, key: str | None = None, timeout: float = 15) -> list[str]:
    """GET /v1/models on an owner-run server (discovery)."""
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return [m["id"] for m in json.loads(r.read().decode("utf-8")).get("data", [])]
