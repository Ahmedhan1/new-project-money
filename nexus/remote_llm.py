"""Heavy text generation on the owner's free Kaggle GPU instead of this computer's CPU.

Why: on a CPU-only PC the local 4B model first downloads ~2.5 GB and then needs minutes per script. With Kaggle
connected, `nexus.llm.chat` sends the prompt to Account 1's `batch_llm` worker task (one bounded Kaggle run: model
loaded, prompt answered, process exits; no serving endpoint, no billing) and nothing is downloaded on this computer.

Setting `heavy_compute` (settings.json or the Kaggle card on the System page):
    "auto"   (default) Kaggle when it is connected, otherwise the local model
    "kaggle" always Kaggle; if it is not connected the step fails with a clear message instead of downloading 2.5 GB
    "local"  always this computer
Setting `kaggle_llm_model`: the Hugging Face model the Kaggle run loads (default Qwen/Qwen3-4B, the same model the
local path uses, so prompts tuned for it keep working).
"""
from __future__ import annotations

import hashlib
import json
import time

from . import config, jobs, vault

MODES = ("auto", "kaggle", "local")
DEFAULT_MODEL = "Qwen/Qwen3-4B"
CONNECTION_ID = "kaggle_primary"
LEGACY_KEY_SECRET = "kaggle_primary_legacy_key"   # stored only so vault.redact() also hides a legacy kaggle.json key
WAIT_S = 45 * 60


class KaggleNotReady(RuntimeError):
    """Kaggle was required for a heavy step but is not usable; the message says what to do."""


def mode() -> str:
    m = str(config.settings().get("heavy_compute", "auto")).lower()
    return m if m in MODES else "auto"


def model() -> str:
    return str(config.settings().get("kaggle_llm_model") or DEFAULT_MODEL)


ENGINE = "transformers"   # batch_llm engine on Kaggle ("echo" is the worker's no-model test engine)


def _provider():
    """The dispatcher's Kaggle provider (one connection, 'kaggle_primary'), so status and runs use the same object."""
    from .cloud.compute import dispatcher
    from .cloud.compute.kaggle import KaggleProvider
    return dispatcher.get().providers.get("kaggle") or KaggleProvider()


def ready() -> tuple[bool, str]:
    try:
        return _provider().configured()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def use_kaggle() -> bool:
    """True when the next heavy LLM step should run on Kaggle. Raises KaggleNotReady in mode 'kaggle' when it cannot."""
    m = mode()
    if m == "local":
        return False
    ok, why = ready()
    if not ok and m == "kaggle":
        raise KaggleNotReady(f"Kaggle is selected for heavy steps but is not ready: {why}. "
                             "Open System > Kaggle, paste your Kaggle API token, or switch heavy steps to 'auto'.")
    return ok


def connect(token: str) -> dict:
    """Store the owner's Kaggle API token in the encrypted vault and check it with the official CLI (`config view`)."""
    from .cloud.compute.base import save_conn
    from .cloud.compute.kaggle import CONNECTION_ID as CID, cli_command
    t = (token or "").strip()
    if len(t) < 16:
        raise ValueError("that does not look like a Kaggle API token (kaggle.com > Settings > API > Create New Token)")
    if t.startswith("{"):
        try:
            j = json.loads(t)
        except ValueError:
            raise ValueError("the pasted kaggle.json is not valid JSON") from None
        if not (j.get("username") and j.get("key")):
            raise ValueError("kaggle.json needs 'username' and 'key'")
        vault.put(LEGACY_KEY_SECRET, str(j["key"]), {"kind": "kaggle_legacy_key"})
    if not cli_command():
        raise RuntimeError("the official kaggle CLI is not installed in this NEXUS (pip install kaggle)")
    vault.put(CID, t, {"kind": "kaggle_api_token", "stored": time.time()})
    save_conn(CID, "kaggle", status="ok", auth_failed="", _identity="")
    try:
        user = _provider().username(refresh=True)
    except Exception as e:  # noqa: BLE001 - keep the token so the owner can retry, but say it failed
        save_conn(CID, "kaggle", status="unauthorized", last_error=vault.redact(str(e))[:300])
        raise ValueError(f"Kaggle did not accept the token: {vault.redact(str(e))[:300]}") from None
    return {"connected": True, "username": user}


def status() -> dict:
    ok, why = ready()
    out = {"mode": mode(), "model": model(), "ready": ok, "detail": why}
    if ok:
        try:
            p = _provider()
            out["username"] = p.username()
            q = p.quota()
            out["gpu_hours_left_week"] = round(q.remaining / 3600, 1)
        except Exception as e:  # noqa: BLE001
            out["detail"] = vault.redact(str(e))[:300]
    return out


def _schema_hint(schema: dict | None) -> str:
    if not schema:
        return ""
    return ("\n\nAnswer with ONE JSON object only, no prose and no code fences. It must match this JSON Schema exactly "
            "(all required keys, the exact array lengths):\n" + json.dumps(schema, ensure_ascii=False))


def chat(prompt: str, system: str, max_tokens: int, temperature: float, json_mode: bool = False,
         schema: dict | None = None) -> str:
    """One prompt on the Kaggle GPU. Returns the model's text. Progress is reported on the running NEXUS job."""
    from .cloud.compute import dispatcher, poller
    from .contracts import ProductionJobManifest
    text = prompt + _schema_hint(schema) + ("\nAnswer with valid JSON only." if json_mode and not schema else "")
    items = [{"id": "a", "system": system, "prompt": text, "max_tokens": int(max_tokens)}]
    params = {"model": model(), "model_licence": "Apache-2.0" if model() == DEFAULT_MODEL else "unverified",
              "engine": ENGINE, "items": items, "max_items": 1, "max_new_tokens": min(int(max_tokens), 4096),
              "max_prompt_chars": max(16000, len(text) + len(system) + 100), "batch_size": 1, "part_size": 1,
              "max_model_gb": 12.0, "temperature": float(temperature), "seed": int(time.time()) % 100000,
              "output_format": "jsonl", "chat_template_kwargs": {"enable_thinking": False}}
    key = "llm:" + hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    m = ProductionJobManifest(task="batch_llm", owner_account="account3", params=params,
                              requires={"gpu": True, "providers": ["kaggle"]}, priority=2, max_runtime_s=2400,
                              max_retries=1, preferred_providers=["kaggle"], idempotency_key=key)
    d = dispatcher.get()
    jid = dispatcher.submit(m)["job_id"]
    jobs.note(f"Kaggle GPU: job sent ({jid})")
    tick_here = not poller.running()   # the API process polls by itself; a separate worker process must tick
    t0 = time.time()
    while True:
        if tick_here:
            d.tick()
        row = dispatcher.status(jid) or {}
        st = row.get("status", "?")
        mins = (time.time() - t0) / 60
        msg = {"queued": "waiting for a Kaggle GPU", "running": "running on the Kaggle GPU (start-up and model load take a few minutes)",
               "quota_exhausted": "Kaggle weekly GPU quota reached - waiting"}.get(st, st)
        jobs.note(f"Kaggle GPU: {msg} - {mins:.0f} min so far")
        if st in ("succeeded", "done", "completed"):
            break
        if st in ("failed", "cancelled", "lost"):
            raise RuntimeError(f"the Kaggle run failed: {vault.redact(str(row.get('error') or st))[:400]}")
        if time.time() - t0 > WAIT_S:
            raise TimeoutError(f"Kaggle did not finish within {WAIT_S // 60} min (job {jid}); it keeps its result if it finishes later")
        time.sleep(3)
    arts = [a for a in dispatcher.job_artifacts(jid) if a["name"].startswith("batch_llm_results")]
    p = dispatcher.artifact_path(arts[-1]) if arts else None
    if p is None:
        raise RuntimeError(f"the Kaggle run finished but its results file is missing (job {jid})")
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    r = next((x for x in rows if str(x.get("id")) == "a"), None)
    if not r or r.get("error"):
        raise RuntimeError(f"the Kaggle model returned an error: {(r or {}).get('error', 'no answer')}")
    jobs.note("Kaggle GPU: answer received")
    return str(r.get("text") or "")
