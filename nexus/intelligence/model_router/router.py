"""Task routing with privacy classes, quotas, circuit breakers, structured-output validation, caching and an honest
'no provider' result. Never routes to a paid endpoint (the cost guard is consulted for every network call)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ... import config, db
from .. import capability_registry as reg

# task -> what it needs. `cap` is the quality dimension used for ranking; `min_q` the lowest acceptable quality;
# `light` tasks prefer the fastest adequate model instead of the strongest one.
TASKS: dict[str, dict] = {
    "deep_research": {"cap": "reasoning", "min_q": 4, "light": False},
    "topic_ideation": {"cap": "writing", "min_q": 3, "light": True},
    "scriptwriting": {"cap": "writing", "min_q": 4, "light": False},
    "summarization": {"cap": "summarization", "min_q": 3, "light": True},
    "translation": {"cap": "translation", "min_q": 3, "light": False},
    "title_generation": {"cap": "writing", "min_q": 3, "light": True},
    "hook_generation": {"cap": "writing", "min_q": 3, "light": True},
    "audience_classification": {"cap": "json", "min_q": 3, "light": True},
    "content_scoring": {"cap": "reasoning", "min_q": 3, "light": True},
    "reference_interpretation": {"cap": "reasoning", "min_q": 4, "light": False},
    "quality_review": {"cap": "reasoning", "min_q": 4, "light": False},
    "story_planning": {"cap": "writing", "min_q": 4, "light": False},
    "data_extraction": {"cap": "json", "min_q": 3, "light": True},
    "analytics_interpretation": {"cap": "reasoning", "min_q": 4, "light": False},
    "general": {"cap": "reasoning", "min_q": 3, "light": True},
}
PRIVACY_RANK = {"public": 0, "internal": 1, "private": 2}
SPEED_RANK = {"fast": 0, "medium": 1, "slow": 2}
BREAKER_FAILS, BREAKER_COOLDOWN = 3, 300.0


class NoProviderAvailable(RuntimeError):
    def __init__(self, task: str, reasons: list[str]):
        self.task, self.reasons = task, reasons
        super().__init__(f"no provider can run '{task}' now: " + "; ".join(reasons[:8]))


@dataclass
class Result:
    text: str
    data: object = None
    provider: str = ""
    model: str = ""
    latency_s: float = 0.0
    cached: bool = False
    fallbacks: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


_lock = threading.Lock()
_breaker: dict[str, dict] = {}


# ----------------------------------------------------------------- settings / eligibility
def provider_settings(pid: str) -> dict:
    return (config.settings().get("ai_providers") or {}).get(pid, {})


def api_key(pid: str) -> str | None:
    """Env NEXUS_KEY_<ID>, then Account 1's vault when present, then NEXUS_HOME/secrets/<id>.key (never in Git)."""
    env = os.environ.get("NEXUS_KEY_" + pid.upper().replace("-", "_"))
    if env:
        return env
    try:
        from ... import vault  # Account 1 (work/account-1-cloud-platform)
        v = vault.get(f"ai/{pid}")  # type: ignore[attr-defined]
        if v:
            return v
    except Exception:
        pass
    f = config.HOME / "secrets" / f"{pid}.key"
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def _usage_today(pid: str) -> int:
    day = time.strftime("%Y-%m-%d")
    rows = db.query("SELECT COUNT(*) n FROM metrics WHERE name=? AND meta LIKE ?", ("ai_request", f'%"provider": "{pid}"%"day": "{day}"%'))
    return rows[0]["n"] if rows else 0


def _usage_last_minute(pid: str) -> int:
    rows = db.query("SELECT COUNT(*) n FROM metrics WHERE name='ai_request' AND ts>? AND meta LIKE ?", (time.time() - 60, f'%"provider": "{pid}"%'))
    return rows[0]["n"] if rows else 0


def _breaker_open(pid: str) -> bool:
    b = _breaker.get(pid)
    return bool(b and b["fails"] >= BREAKER_FAILS and time.time() - b["since"] < BREAKER_COOLDOWN)


def eligibility(entry: dict, privacy: str) -> tuple[bool, str]:
    """Why a (provider, model) entry may or may not take a request of this privacy class right now."""
    pid = entry["provider"]
    if PRIVACY_RANK[privacy] > PRIVACY_RANK[entry["privacy"]]:
        return False, f"{pid}: data class '{privacy}' may not leave for a '{entry['hosting']}' provider"
    s = provider_settings(pid)
    if s.get("disabled"):
        return False, f"{pid}: disabled by the owner"
    if entry["hosting"] == "owner_worker" and not s.get("base_url"):
        return False, f"{pid}: no endpoint configured (settings.json ai_providers.{pid}.base_url)"
    if entry["hosting"] == "hosted_free":
        if not s.get("authorized"):
            return False, f"{pid}: not authorised by the owner (settings.json ai_providers.{pid}.authorized)"
        if not api_key(pid):
            return False, f"{pid}: no API key (env NEXUS_KEY_{pid.upper().replace('-', '_')} or vault ai/{pid})"
        suffix = entry.get("free_model_suffix")
        if suffix and not entry["model"].endswith(suffix):
            return False, f"{pid}: {entry['model']} is not a free variant"
        q = entry.get("quotas") or {}
        if q.get("rpd") and _usage_today(pid) >= int(q["rpd"] * 0.9):
            return False, f"{pid}: 90 % of the daily free quota used"
        if q.get("rpm") and _usage_last_minute(pid) >= q["rpm"]:
            return False, f"{pid}: per-minute limit reached"
    if _breaker_open(pid):
        return False, f"{pid}: circuit open after {BREAKER_FAILS} consecutive failures (retry after {int(BREAKER_COOLDOWN)} s)"
    if entry["hosting"] == "in_process" and entry.get("kind") == "llamacpp_inprocess":
        from ...models import catalog
        mdir = config.MODELS / catalog()["models"]["llm-qwen3-4b"]["dir"]
        if not any(mdir.glob("*.gguf")) and os.environ.get("NEXUS_ALLOW_MODEL_DOWNLOAD") != "1":
            return False, f"{pid}: model weights not installed (python -m nexus models fetch)"
    return True, "ok"


def _measured_quality(entry: dict, cap: str) -> float | None:
    rows = db.query("SELECT AVG(value) v, COUNT(*) n FROM metrics WHERE name=? AND meta LIKE ?",
                    (f"ai_quality.{cap}", f'%"model": "{entry["model"]}"%'))
    return rows[0]["v"] if rows and rows[0]["n"] else None


def candidates(task: str, privacy: str = "internal", entries: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    if task not in TASKS:
        raise KeyError(f"unknown AI task '{task}' (known: {', '.join(TASKS)})")
    t = TASKS[task]
    ok, why = [], []
    for e in entries if entries is not None else _entries():
        good, reason = eligibility(e, privacy)
        q = (e.get("quality") or {}).get(t["cap"], 0)
        mq = _measured_quality(e, t["cap"])
        q = mq if mq is not None else q
        if good and q < t["min_q"]:
            good, reason = False, f"{e['provider']}/{e['model']}: quality {q} < {t['min_q']} needed for {task}"
        (ok.append({**e, "_q": q}) if good else why.append(reason))
    if t["light"]:  # adequate + fastest + most private first
        ok.sort(key=lambda e: (SPEED_RANK.get(e.get("speed", "medium"), 1), -PRIVACY_RANK[e["privacy"]], -e["_q"]))
    else:           # strongest first; ties go to the more private / local provider
        ok.sort(key=lambda e: (-e["_q"], -PRIVACY_RANK[e["privacy"]], SPEED_RANK.get(e.get("speed", "medium"), 1)))
    return ok, why


def _entries() -> list[dict]:
    out = reg.models()
    # owner-run servers: models declared in settings (licence must be stated by the owner)
    s = provider_settings("owner-openai-compatible")
    if s.get("base_url"):
        base = reg.providers()["owner-openai-compatible"]
        for m in s.get("models", []):
            out.append({**{k: v for k, v in base.items() if k != "models"}, "provider": base["id"], **m})
    return out


# ----------------------------------------------------------------- calling
def _cache_key(task, prompt, system, schema, model) -> str:
    return hashlib.sha256(json.dumps([task, prompt, system, schema, model], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _cache_get(key: str) -> dict | None:
    f = config.CACHE / "ai" / f"{key}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _cache_put(key: str, val: dict) -> None:
    d = config.CACHE / "ai"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.json").write_text(json.dumps(val, ensure_ascii=False), encoding="utf-8")


def _validate(data, schema: dict | None) -> list[str]:
    if schema is None:
        return []
    errs = []
    if schema.get("type") == "object":
        if not isinstance(data, dict):
            return ["expected an object"]
        for k in schema.get("required", []):
            if k not in data or data[k] in (None, "", [], {}):
                errs.append(f"missing '{k}'")
        for k, sub in (schema.get("properties") or {}).items():
            if k in data and isinstance(sub, dict) and sub.get("type") == "array":
                if not isinstance(data[k], list):
                    errs.append(f"'{k}' must be a list")
                elif "minItems" in sub and len(data[k]) < sub["minItems"]:
                    errs.append(f"'{k}' needs at least {sub['minItems']} items")
    return errs


def _parse(text: str):
    import re
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}|\[.*\]", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _call(entry: dict, prompt: str, system: str, schema: dict | None, max_tokens: int, temperature: float) -> tuple[str, dict]:
    from ..providers import call
    return call(entry, prompt, system, schema, max_tokens, temperature)


def complete(task: str, prompt: str, *, system: str = "You are a precise assistant.", schema: dict | None = None,
             privacy: str = "internal", max_tokens: int = 900, temperature: float = 0.4, use_cache: bool = True,
             retries_per_provider: int = 1, entries: list[dict] | None = None) -> Result:
    """Run one AI task on the best eligible provider, falling back down the ranked list."""
    if privacy not in PRIVACY_RANK:
        raise ValueError("privacy must be public, internal or private")
    ranked, why = candidates(task, privacy, entries)
    if not ranked:
        raise NoProviderAvailable(task, why)
    tried: list[str] = []
    for e in ranked:
        key = _cache_key(task, prompt, system, schema, e["model"])
        if use_cache and (hit := _cache_get(key)):
            return Result(hit["text"], hit.get("data"), e["provider"], e["model"], 0.0, True, tried, hit.get("usage", {}))
        for attempt in range(retries_per_provider + 1):
            t0 = time.time()
            try:
                text, usage = _call(e, prompt, system, schema, max_tokens, min(1.0, temperature + 0.15 * attempt))
                data = _parse(text) if schema is not None else None
                problems = _validate(data, schema)
                if problems:
                    raise ValueError("invalid structured output: " + "; ".join(problems))
            except Exception as ex:  # provider error, timeout, quota, invalid output -> next attempt / provider
                with _lock:
                    b = _breaker.setdefault(e["provider"], {"fails": 0, "since": 0.0})
                    b["fails"] += 1
                    b["since"] = time.time()
                db.metric("ai_failure", 1, provider=e["provider"], model=e["model"], task=task, error=f"{type(ex).__name__}: {str(ex)[:160]}")
                tried.append(f"{e['provider']}/{e['model']}: {type(ex).__name__}: {str(ex)[:120]}")
                continue
            lat = round(time.time() - t0, 3)
            with _lock:
                _breaker.pop(e["provider"], None)
            db.metric("ai_request", lat, provider=e["provider"], model=e["model"], task=task, day=time.strftime("%Y-%m-%d"),
                      tokens=usage.get("total_tokens"))
            if use_cache:
                _cache_put(key, {"text": text, "data": data, "usage": usage})
            return Result(text, data, e["provider"], e["model"], lat, False, tried, usage)
    raise NoProviderAvailable(task, why + tried)


def status(privacy: str = "public") -> dict:
    """Per task: which provider would run it now, and why the others would not."""
    out = {}
    for t in TASKS:
        ok, why = candidates(t, privacy)
        out[t] = {"route": [f"{e['provider']}/{e['model']}" for e in ok], "excluded": why}
    return out


def health() -> list[dict]:
    rows = []
    for pid, p in reg.providers().items():
        m = db.query("SELECT COUNT(*) n, AVG(value) lat FROM metrics WHERE name='ai_request' AND meta LIKE ?", (f'%"provider": "{pid}"%',))[0]
        f = db.query("SELECT COUNT(*) n FROM metrics WHERE name='ai_failure' AND meta LIKE ?", (f'%"provider": "{pid}"%',))[0]
        rows.append({"provider": pid, "hosting": p["hosting"], "requests": m["n"], "mean_latency_s": round(m["lat"] or 0, 2), "failures": f["n"],
                     "circuit": "open" if _breaker_open(pid) else "closed", "today": _usage_today(pid), "quotas": p.get("quotas", {})})
    return rows


def _reset_for_tests() -> None:
    _breaker.clear()


__all__ = ["TASKS", "NoProviderAvailable", "Result", "complete", "candidates", "status", "health", "eligibility", "Path"]
