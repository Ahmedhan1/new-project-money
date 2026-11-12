"""Multi-provider routing demo with two REAL local providers and no hosted API:
  * local-llamacpp            in-process Qwen3-4B (llama-cpp-python)
  * owner-openai-compatible   llama.cpp's OpenAI-compatible server with Qwen3-0.6B on 127.0.0.1

    NEXUS_HOME=/home/user/new-project-money/.nexus .venv/bin/python scripts/account3/models_demo.py

The owner's settings.json is NOT modified: the demo injects the owner-server endpoint into this process only
(router.provider_settings is wrapped), which is exactly what `ai_providers.owner-openai-compatible` in settings.json
would contain. Evidence: docs/results/account3/models_routing_demo.json
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nexus import config, jobs, llm  # noqa: E402
from nexus.intelligence.model_router import router  # noqa: E402
from nexus.intelligence.providers import list_models, local_server  # noqa: E402

GGUF = config.MODELS / "hf" / "qwen3-0.6b-gguf" / "Qwen3-0.6B-Q8_0.gguf"
OUT = Path(__file__).resolve().parents[2] / "docs" / "results" / "account3" / "models_routing_demo.json"


def main() -> int:
    ev = {"date": time.strftime("%Y-%m-%d %H:%M"), "steps": []}
    orig = router.provider_settings
    url_box: dict = {}

    def settings(pid):
        if pid == "owner-openai-compatible" and url_box:
            return {"base_url": url_box["url"], "models": ["qwen3-0.6b-q8"]}
        return orig(pid)

    router.provider_settings = settings

    def step(name, fn):
        t0 = time.time()
        try:
            r = fn()
            out = {"ok": True, **r}
        except router.NoProviderAvailable as e:
            out = {"ok": False, "no_provider": True, "reasons": e.reasons[:10]}
        out["wall_s"] = round(time.time() - t0, 2)
        ev["steps"].append({"step": name, **out})
        print(name, json.dumps(out, ensure_ascii=False)[:400])

    def res(r):
        return {"provider": r.provider, "model": r.model, "latency_s": r.latency_s, "text": r.text[:300], "data": r.data,
                "fallbacks": r.fallbacks, "usage": r.usage}

    print("waiting for the heavy lock")
    with jobs.heavy_lock():
        try:
            with local_server.serve(GGUF, "qwen3-0.6b-q8", 8766, log=config.LOGS / "demo_llama_server.log") as url:
                url_box["url"] = url
                ev["owner_server"] = {"base_url": url, "models": list_models(url), "weights": str(GGUF.name),
                                      "command": " ".join(local_server.command(GGUF, "qwen3-0.6b-q8", 8766)[1:])}
                step("explain summarization/internal", lambda: router.route_explain("summarization", "internal"))
                step("summarization/internal (real call)", lambda: res(router.complete(
                    "summarization", "Summarise in one sentence: The city library will close for renovation from June to "
                    "August and reopen in September with a new children's wing.", privacy="internal", max_tokens=80, use_cache=False)))
                step("data_extraction/private (must stay in-process)", lambda: res(router.complete(
                    "data_extraction", 'Extract JSON {"name": str, "city": str} from: "Amira Nabil moved to Alexandria."',
                    schema={"type": "object", "required": ["name", "city"]}, privacy="private", max_tokens=60, use_cache=False)))
            # server stopped: the same internal request must fall back to the in-process model (or say why not)
            step("general/internal after the owner server stopped (fallback)", lambda: res(router.complete(
                "general", "Answer with one word: what colour is the sky on a clear day?", privacy="internal",
                max_tokens=20, use_cache=False)))
        finally:
            llm.unload()
    url_box.clear()
    step("status/public without any hosted key (hosted providers excluded)", lambda: {
        "general": router.status("public")["general"]})
    ev["health"] = [h for h in router.health() if h["provider"] in ("local-llamacpp", "owner-openai-compatible")]
    ev["account1_capabilities"] = router.account1_capabilities()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(ev, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print("evidence ->", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
