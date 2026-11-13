"""Task routing with privacy classes, quotas, token budgets, circuit breakers, structured-output validation, caching and
an honest 'no provider' result. Never routes to a paid endpoint (the cost guard is consulted for every network call)
nor to a service that needs a payment method or has no free quota."""
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
# Tasks whose output can mislead an audience or the owner (facts, scripts, translations, QA verdicts, analytics). Below
# the bar their result is not a draft to skim but REQUIRES a human review before use (Result.requires_human_review).
CRITICAL = {"deep_research", "scriptwriting", "translation", "quality_review", "analytics_interpretation", "reference_interpretation",
            "story_planning"}
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
    @property
    def requires_human_review(self) -> bool:
        """True when a CRITICAL task ran below its quality bar: a person must review before the output is used."""
        return bool(self.below_bar and self.below_bar.get("requires_human_review"))

    # set when no eligible model met the task's quality bar and the best one below it was used (strict=False);
    # callers must show it ("draft quality - review before use"), never present the output as meeting the bar
    below_bar: dict | None = None


_lock = threading.Lock()
_breaker: dict[str, dict] = {}


# ----------------------------------------------------------------- settings / eligibility
def provider_settings(pid: str) -> dict:
    return (config.settings().get("ai_providers") or {}).get(pid, {})


def api_key(pid: str) -> str | None:
    """Env NEXUS_KEY_<ID>, then Account 1's vault ("ai.<id>") when present, then NEXUS_HOME/secrets/<id>.key (never in Git)."""
    env = os.environ.get("NEXUS_KEY_" + pid.upper().replace("-", "_"))
    if env:
        return env
    try:
        from ... import vault  # Account 1 (work/account-1-cloud-platform)
        v = vault.get(f"ai.{pid}")  # type: ignore[attr-defined]  # vault names: [a-z0-9_.-]
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


def _usage_tokens_today(pid: str) -> int:
    day = time.strftime("%Y-%m-%d")
    rows = db.query("SELECT SUM(CAST(json_extract(meta, '$.tokens') AS INTEGER)) n FROM metrics WHERE name='ai_request' AND meta LIKE ?",
                    (f'%"provider": "{pid}"%"day": "{day}"%',))
    return int(rows[0]["n"] or 0) if rows else 0


def _usage_last_minute(pid: str) -> int:
    rows = db.query("SELECT COUNT(*) n FROM metrics WHERE name='ai_request' AND ts>? AND meta LIKE ?", (time.time() - 60, f'%"provider": "{pid}"%'))
    return rows[0]["n"] if rows else 0


def estimate_tokens(text: str) -> int:
    """Token estimate without a tokenizer, deliberately on the high side so budgets are never exceeded:
    ~3.5 chars/token for Latin/Cyrillic, 1 token per CJK character, 2.5 chars/token for Arabic/Devanagari."""
    if not text:
        return 0
    cjk = sum(1 for c in text if "぀" <= c <= "鿿" or "가" <= c <= "힯")
    dense = sum(1 for c in text if "؀" <= c <= "ۿ" or "ऀ" <= c <= "ॿ")
    return int(cjk + dense / 2.5 + (len(text) - cjk - dense) / 3.5) + 8


def _breaker_open(pid: str) -> bool:
    b = _breaker.get(pid)
    return bool(b and b["fails"] >= BREAKER_FAILS and time.time() - b["since"] < BREAKER_COOLDOWN)


def _pid(entry: dict) -> str:
    return str(entry.get("provider", entry.get("id")))


def zero_spend_block(entry: dict) -> str | None:
    """Static zero-spend gate, FAIL CLOSED: a provider is usable only when its payment-method requirement is explicitly
    declared `false` (the YAML boolean). Missing, null, "unverified", malformed or true values all refuse; so does a
    provider without any free quota."""
    pm = entry.get("payment_method_required", None)
    if pm is not False:
        if pm is True:
            return f"{_pid(entry)}: requires a payment method - never used (zero spend)"
        return (f"{_pid(entry)}: payment-method requirement is {pm!r}, not verified false - never used (zero spend, fail closed)")
    if entry.get("free_quota") == "none":
        return f"{_pid(entry)}: no free quota exists - never used (zero spend)"
    return None


LOCAL_HOSTING = ("in_process", "owner_worker")
QUOTA_KEYS = ("rpm", "rpd", "tpm", "tpd")


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def hosted_free_facts(entry: dict) -> tuple[dict | None, str]:
    """Re-derive a hosted_free entry's zero-spend facts from the CATALOGUE, never from the caller's dict: a crafted entry
    (or a bare provider name) cannot claim `payment_method_required: false`, a free model or bigger quotas.
    Returns (facts, "ok") or (None, refusal). Facts = catalogue provider + the effective (strictest) quotas."""
    pid = _pid(entry)
    cat = reg.providers().get(pid)
    if not cat:
        return None, f"{pid}: not a catalogued provider - hosted calls refused (zero spend, fail closed)"
    if cat.get("hosting") != "hosted_free":
        return None, f"{pid}: catalogued as '{cat.get('hosting')}', not hosted_free - refused"
    # the catalogue must say false; an entry that carries its own value (eligibility() already requires it) must agree
    for src in (cat, *([entry] if "payment_method_required" in entry or "free_quota" in entry else [])):
        if blocked := zero_spend_block({**src, "provider": pid}):
            return None, blocked
    if not str(cat.get("verified") or "").startswith("20"):
        return None, f"{pid}: catalogue facts not verified against first-party docs (verified: {cat.get('verified')!r}) - refused"
    model = entry.get("model")
    listed = {m.get("model") for m in cat.get("models") or []}
    suffix = cat.get("free_model_suffix")
    if suffix:
        if not str(model).endswith(suffix):
            return None, f"{pid}: {model} is not a free variant"
    elif model not in listed:
        return None, f"{pid}: model {model!r} is not a catalogued free model of this provider - refused"
    quotas = {}
    cq, eq = cat.get("quotas") or {}, entry.get("quotas") or {}
    for k in QUOTA_KEYS:
        vals = [v for v in (_num(cq.get(k)), _num(eq.get(k))) if v is not None]
        if vals:
            quotas[k] = min(vals)  # an entry may tighten a documented quota, never loosen it
    s = provider_settings(pid)
    if not quotas and not _num(s.get("daily_requests")):
        return None, (f"{pid}: no documented numeric quota - set settings.json ai_providers.{pid}.daily_requests "
                      "before it can be used (quotas must be enforceable)")
    return {**cat, "quotas": quotas}, "ok"


def hosted_call_allowed(pid: str, model: str | None) -> tuple[bool, str]:
    """Used by the cost guard's free-tier exception: the full static + owner gate for one (provider, model) call,
    computed from the catalogue and settings only (the caller supplies names, nothing else)."""
    if not model:
        return False, f"{pid}: no model named - free-tier exception refused"
    facts, why = hosted_free_facts({"provider": pid, "model": model})
    if not facts:
        return False, why
    return eligibility({**facts, "provider": pid, "model": model}, "public")


def eligibility(entry: dict, privacy: str, need_tokens: int = 0) -> tuple[bool, str]:
    """Why a (provider, model) entry may or may not take a request of this privacy class (and size) right now."""
    pid = entry["provider"]
    if PRIVACY_RANK[privacy] > PRIVACY_RANK[entry["privacy"]]:
        return False, f"{pid}: data class '{privacy}' may not leave for a '{entry['hosting']}' provider"
    cat_hosting = reg.providers().get(pid, {}).get("hosting")
    local = entry.get("hosting") in LOCAL_HOSTING and cat_hosting in (None, *LOCAL_HOSTING)
    if local:  # this machine / the owner's own server: nothing can bill; only an explicit card/no-quota flag refuses
        if entry.get("payment_method_required") is True:
            return False, f"{pid}: requires a payment method - never used (zero spend)"
        if entry.get("free_quota") == "none":
            return False, f"{pid}: no free quota exists - never used (zero spend)"
    elif blocked := zero_spend_block(entry):  # anything else (hosted, unknown hosting) must be verified false
        return False, blocked
    s = provider_settings(pid)
    if s.get("disabled"):
        return False, f"{pid}: disabled by the owner"
    if entry["hosting"] == "owner_worker" and not s.get("base_url"):
        return False, f"{pid}: no endpoint configured (settings.json ai_providers.{pid}.base_url)"
    if not local:
        facts, why = hosted_free_facts(entry)
        if not facts:
            return False, why
        if s.get("authorized") is not True:
            return False, f"{pid}: not authorised by the owner (settings.json ai_providers.{pid}.authorized)"
        if not api_key(pid):
            return False, f"{pid}: no API key (env NEXUS_KEY_{pid.upper().replace('-', '_')} or vault ai.{pid})"
        q = facts["quotas"]
        if q.get("rpd") and _usage_today(pid) >= int(q["rpd"] * 0.9):
            return False, f"{pid}: 90 % of the daily free quota used"
        if q.get("rpm") and _usage_last_minute(pid) >= q["rpm"]:
            return False, f"{pid}: per-minute limit reached"
        if q.get("tpd") and _usage_tokens_today(pid) + need_tokens > int(q["tpd"] * 0.9):
            return False, f"{pid}: 90 % of the daily free token quota would be used"
        if q.get("tpm") and need_tokens > int(q["tpm"]):
            return False, f"{pid}: request (~{need_tokens} tokens) exceeds the per-minute token limit {q['tpm']}"
    # owner-set daily limits apply to every provider kind and are never relaxed by documented quotas
    if s.get("daily_requests") and _usage_today(pid) >= int(s["daily_requests"]):
        return False, f"{pid}: owner daily request limit {s['daily_requests']} reached"
    if s.get("daily_tokens") and _usage_tokens_today(pid) + need_tokens > int(s["daily_tokens"]):
        return False, f"{pid}: owner daily token limit {s['daily_tokens']} would be exceeded"
    if need_tokens and entry.get("context") and need_tokens > int(entry["context"]):
        return False, f"{pid}/{entry['model']}: ~{need_tokens} tokens do not fit its {entry['context']}-token context"
    if _breaker_open(pid):
        return False, f"{pid}: circuit open after {BREAKER_FAILS} consecutive failures (retry after {int(BREAKER_COOLDOWN)} s)"
    if entry["hosting"] == "in_process" and entry.get("kind") == "llamacpp_inprocess":
        from ...models import catalog
        mdir = config.MODELS / catalog()["models"]["llm-qwen3-4b"]["dir"]
        if not any(mdir.glob("*.gguf")) and os.environ.get("NEXUS_ALLOW_MODEL_DOWNLOAD") != "1":
            return False, f"{pid}: model weights not installed (python -m nexus models fetch)"
    return True, "ok"


MIN_MEASURED_ITEMS = 5  # a benchmark score replaces the estimate only when it rests on at least this many items


def _measured(entry: dict, cap: str) -> tuple[float | None, int]:
    """Latest measured score and its sample size (the newest benchmark run supersedes older ones)."""
    rows = db.query("SELECT value v, CAST(COALESCE(json_extract(meta, '$.n'), 0) AS INTEGER) n FROM metrics WHERE name=? AND meta LIKE ? "
                    "ORDER BY ts DESC, id DESC LIMIT 1", (f"ai_quality.{cap}", f'%"model": "{entry["model"]}"%'))
    return (rows[0]["v"], rows[0]["n"]) if rows else (None, 0)


def _measured_quality(entry: dict, cap: str) -> float | None:
    v, n = _measured(entry, cap)
    return v if v is not None and n >= MIN_MEASURED_ITEMS else None


def candidates(task: str, privacy: str = "internal", entries: list[dict] | None = None,
               need_tokens: int = 0, below_bar: list | None = None) -> tuple[list[dict], list[str]]:
    """Eligible models meeting the task's quality bar, best first. Eligible models BELOW the bar are appended to
    `below_bar` (if a list is passed) so a non-strict caller can still run as a labelled draft."""
    if task not in TASKS:
        raise KeyError(f"unknown AI task '{task}' (known: {', '.join(TASKS)})")
    t = TASKS[task]
    ok, why = [], []
    for e in entries if entries is not None else _entries():
        good, reason = eligibility(e, privacy, need_tokens)
        q = (e.get("quality") or {}).get(t["cap"], 0)
        mv, mn = _measured(e, t["cap"])
        if mv is not None and mn >= MIN_MEASURED_ITEMS:
            q, qsrc = round(mv, 2), f"measured n={mn}"
        else:
            qsrc = (e.get("quality") or {}).get("source", "estimate")
            if mv is not None:
                qsrc += f" (bench {round(mv, 2)} on n={mn} < {MIN_MEASURED_ITEMS} items: not used)"
        if good and q < t["min_q"]:
            if below_bar is not None and q > 0:
                below_bar.append({**e, "_q": q, "_q_source": qsrc})
            good, reason = False, f"{e['provider']}/{e['model']}: quality {q} ({qsrc}) < {t['min_q']} needed for {task}"
        (ok.append({**e, "_q": q, "_q_source": qsrc}) if good else why.append(reason))
    if t["light"]:  # adequate + fastest + most private first
        ok.sort(key=lambda e: (SPEED_RANK.get(e.get("speed", "medium"), 1), -PRIVACY_RANK[e["privacy"]], -e["_q"]))
    else:           # strongest first; ties go to the more private / local provider
        ok.sort(key=lambda e: (-e["_q"], -PRIVACY_RANK[e["privacy"]], SPEED_RANK.get(e.get("speed", "medium"), 1)))
    return ok, why


def _entries() -> list[dict]:
    out = reg.models()
    # owner-run servers: models declared in settings; facts come from `model_profiles` when the id is known there,
    # otherwise the owner must state licence/quality in settings (unknown quality = 0 = never chosen)
    s = provider_settings("owner-openai-compatible")
    if s.get("base_url"):
        base = reg.providers()["owner-openai-compatible"]
        profiles = reg.catalog().get("model_profiles") or {}
        for m in s.get("models", []):
            m = {"model": m} if isinstance(m, str) else m
            prof = profiles.get(m.get("profile") or m["model"], {})
            out.append({**{k: v for k, v in base.items() if k != "models"}, "provider": base["id"], **prof, **m})
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
             retries_per_provider: int = 1, entries: list[dict] | None = None, strict: bool = False) -> Result:
    """Run one AI task on the best eligible provider, falling back down the ranked list.

    When no eligible model reaches the task's quality bar: strict=True raises NoProviderAvailable; the default runs the
    best eligible model below the bar and sets Result.below_bar so the output is shown as a draft for human review."""
    if privacy not in PRIVACY_RANK:
        raise ValueError("privacy must be public, internal or private")
    need = estimate_tokens(system) + estimate_tokens(prompt) + max_tokens
    low: list[dict] = []
    ranked, why = candidates(task, privacy, entries, need_tokens=need, below_bar=low)
    bar = None
    if not ranked:
        if strict or not low:
            raise NoProviderAvailable(task, why)
        ranked = sorted(low, key=lambda e: (-e["_q"], -PRIVACY_RANK[e["privacy"]]))
        bar = {"required": TASKS[task]["min_q"], "reasons": why[:6], "requires_human_review": task in CRITICAL}
    tried: list[str] = []
    for e in ranked:
        key = _cache_key(task, prompt, system, schema, e["model"])
        if use_cache and (hit := _cache_get(key)):
            return Result(hit["text"], hit.get("data"), e["provider"], e["model"], 0.0, True, tried, hit.get("usage", {}),
                          bar and {**bar, "used_quality": e["_q"], "quality_source": e["_q_source"]})
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
            tokens = usage.get("total_tokens") or (need - max_tokens + estimate_tokens(text))
            db.metric("ai_request", lat, provider=e["provider"], model=e["model"], task=task, day=time.strftime("%Y-%m-%d"),
                      tokens=int(tokens), tokens_estimated=not usage.get("total_tokens"))
            if use_cache:
                _cache_put(key, {"text": text, "data": data, "usage": usage})
            return Result(text, data, e["provider"], e["model"], lat, False, tried, usage,
                          bar and {**bar, "used_quality": e["_q"], "quality_source": e["_q_source"]})
    raise NoProviderAvailable(task, why + tried)


# ----------------------------------------------------------------- introspection (UI, CLI, Account 1)
def status(privacy: str = "public") -> dict:
    """Per task: which provider would run it now, and why the others would not."""
    out = {}
    for t in TASKS:
        ok, why = candidates(t, privacy)
        out[t] = {"route": [f"{e['provider']}/{e['model']}" for e in ok], "excluded": why}
    return out


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 3)


def health(window: int = 50) -> list[dict]:
    """Per provider: counts, response-time percentiles over the last `window` successes, last success/failure, today's
    requests and tokens next to documented and owner-set limits, circuit state, and whether zero-spend blocks it."""
    rows = []
    for pid, p in reg.providers().items():
        like = f'%"provider": "{pid}"%'
        m = db.query("SELECT COUNT(*) n, AVG(value) lat, MAX(ts) last FROM metrics WHERE name='ai_request' AND meta LIKE ?", (like,))[0]
        lats = [r["value"] for r in db.query("SELECT value FROM metrics WHERE name='ai_request' AND meta LIKE ? ORDER BY ts DESC LIMIT ?",
                                             (like, window))]
        f = db.query("SELECT COUNT(*) n, MAX(ts) last FROM metrics WHERE name='ai_failure' AND meta LIKE ?", (like,))[0]
        err = db.query("SELECT meta FROM metrics WHERE name='ai_failure' AND meta LIKE ? ORDER BY ts DESC LIMIT 1", (like,))
        s = provider_settings(pid)
        rows.append({"provider": pid, "hosting": p["hosting"], "requests": m["n"], "mean_latency_s": round(m["lat"] or 0, 2),
                     "p50_latency_s": _pct(lats, 0.5), "p95_latency_s": _pct(lats, 0.95), "last_success": m["last"],
                     "failures": f["n"], "last_failure": f["last"],
                     "last_error": json.loads(err[0]["meta"]).get("error") if err else None,
                     "circuit": "open" if _breaker_open(pid) else "closed", "today": _usage_today(pid),
                     "tokens_today": _usage_tokens_today(pid), "quotas": p.get("quotas", {}),
                     "owner_limits": {k: s[k] for k in ("daily_requests", "daily_tokens") if k in s},
                     "zero_spend_blocked": zero_spend_block(p) is not None})
    return rows


def route_explain(task: str, privacy: str = "internal", prompt_chars: int = 2000, max_tokens: int = 900) -> dict:
    """The routing decision in words for the UI: who would run `task` now, in which order, and why others would not."""
    if privacy not in PRIVACY_RANK:
        raise ValueError("privacy must be public, internal or private")
    need = int(prompt_chars / 3.5) + 8 + max_tokens
    ok, why = candidates(task, privacy, need_tokens=need)
    t = TASKS[task]
    ranked = [{"provider": e["provider"], "model": e["model"], "hosting": e["hosting"], "privacy_max": e["privacy"],
               "quality": e["_q"], "quality_source": e["_q_source"], "speed": e.get("speed", "medium"),
               "licence": e.get("licence"), "local": e["hosting"] in ("in_process", "owner_worker")} for e in ok]
    return {"task": task, "privacy": privacy, "capability": t["cap"], "min_quality": t["min_q"],
            "ordering": "fastest adequate model first (light task)" if t["light"] else
                        "strongest model first; ties go to the more private provider",
            "estimated_tokens": need, "chosen": ranked[0] if ranked else None, "ranked": ranked, "excluded": why,
            "answer": f"{task} runs on {ranked[0]['provider']}/{ranked[0]['model']}" if ranked else
                      f"no provider can run {task} with '{privacy}' data now - queue it or ask a human",
            "gates": ["privacy class", "zero spend (no card, free quota only, cost guard)", "owner authorisation + key (hosted)",
                      "documented and owner-set quotas", "context / token budget", "circuit breaker", "minimum quality"]}


def account1_capabilities(connection_prefix: str = "a3") -> list[dict]:
    """AIProviderCapability/v1 documents for Account 1's scheduler, one per provider the router could ever use.
    `tasks` = tasks whose quality bar at least one of its models meets; runtime gates (keys, quotas, breaker) stay here."""
    from .. import contracts
    ents = _entries()
    out = []
    for pid, p in reg.providers().items():
        if zero_spend_block(p):
            continue
        mine = [e for e in ents if e["provider"] == pid]
        tasks = sorted({t for t, d in TASKS.items() for e in mine if (e.get("quality") or {}).get(d["cap"], 0) >= d["min_q"]})
        q, quotas = p.get("quotas") or {}, []
        for key, period in (("rpd", "daily"), ("rpm", "per-minute")):
            if q.get(key):
                quotas.append({"contract": "QuotaStatus/v1", "provider": pid, "connection_id": f"{connection_prefix}:{pid}",
                               "resource": "api_requests", "limit": float(q[key]),
                               "used": float(_usage_today(pid)) if key == "rpd" else None, "period": period,
                               "source": "documented", "measured_at": time.time(), "exhausted": False})
        uses = {e.get("commercial_use") or ("yes" if e.get("licence") in ("Apache-2.0", "MIT") else "unverified") for e in mine}
        commercial = "no" if "no" in uses else ("yes" if uses == {"yes"} else "unverified")
        cap = contracts.make("ProviderCapability", provider=pid, connection_id=f"{connection_prefix}:{pid}", tasks=tasks,
                             models=[e["model"] for e in mine], local=p["hosting"] in ("in_process", "owner_worker"),
                             commercial_use=commercial, hosting=p["hosting"], privacy={"max": p["privacy"]}, quotas=quotas,
                             verified=str(p.get("verified", "unverified")))
        out.append(contracts.to_account1_ai_capability(cap))
    return out


def _reset_for_tests() -> None:
    _breaker.clear()


__all__ = ["TASKS", "NoProviderAvailable", "Result", "complete", "candidates", "status", "health", "eligibility",
           "route_explain", "account1_capabilities", "estimate_tokens", "Path"]
