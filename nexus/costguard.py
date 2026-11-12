"""Zero-spend financial protection layer.

Every operation that touches a tool, model, service or compute provider goes
through `authorize()`. An operation is allowed only when the component is
declared in registry/components.yaml with zero monetary cost and no payment
method. Network egress goes through `check_host()`, which only allows hosts
declared by an allowed component, and always refuses known paid endpoints.
Every decision is written to the ledger; recorded spend is always 0.0 because
anything that could cost money is refused before it runs.
"""
from __future__ import annotations

import functools
import time
from urllib.parse import urlparse

import yaml

from . import config, db

# Hosts / SDKs of services that bill per use or require a card. Never allowed,
# even if someone adds them to the manifest by mistake.
PAID_HOSTS = {
    "api.openai.com", "api.anthropic.com", "api.elevenlabs.io", "api.replicate.com", "api.runwayml.com",
    "api.stability.ai", "api.together.xyz", "api.fal.ai", "fal.run", "api.runpod.ai", "api.lumalabs.ai",
    "api.heygen.com", "api.d-id.com", "api.deepl.com", "translation.googleapis.com",
    "texttospeech.googleapis.com", "generativelanguage.googleapis.com", "api.cohere.ai", "api.mistral.ai",
    "api.groq.com", "api.pika.art", "api.assemblyai.com", "api.deepgram.com", "api.shutterstock.com",
    "api.gettyimages.com", "bedrock-runtime.amazonaws.com", "api.lambdalabs.com", "api.vast.ai",
    "api.modal.com", "api.synthesia.io", "api.klingai.com",
}
PAID_SDK_IMPORTS = {
    "openai", "anthropic", "elevenlabs", "replicate", "runwayml", "stability_sdk", "together", "fal_client",
    "runpod", "cohere", "mistralai", "groq", "deepl", "boto3", "google.cloud", "azure", "assemblyai",
    "deepgram", "modal", "stripe", "lumaai",
}


class CostGuardError(RuntimeError):
    """Raised when an operation could create a financial charge."""


@functools.lru_cache(maxsize=1)
def manifest() -> dict[str, dict]:
    raw = yaml.safe_load((config.REGISTRY_DIR / "components.yaml").read_text(encoding="utf-8"))
    out = {}
    for c in raw["components"]:
        out[c["id"]] = {**raw["defaults"], **c}
    return out


REQUIRED_FIELDS = ["monetary_cost_usd", "free_tier", "account_required", "payment_method_required", "quotas",
                   "hardware", "usage_restrictions", "commercial_use", "data_retention", "availability",
                   "license", "source_url"]


def violations(comp: dict) -> list[str]:
    v = [f"missing declaration: {f}" for f in REQUIRED_FIELDS if f not in comp]
    if comp.get("monetary_cost_usd", 1) != 0:
        v.append(f"monetary cost is {comp.get('monetary_cost_usd')} (must be 0)")
    if comp.get("payment_method_required", True):
        v.append("requires a payment method / billing account")
    for h in comp.get("network_hosts", []):
        if h in PAID_HOSTS:
            v.append(f"talks to paid endpoint {h}")
    return v


def alternatives(capabilities: list[str], exclude: str = "") -> list[str]:
    """Zero-cost, locally available components that provide the same capability."""
    out = []
    for cid, c in manifest().items():
        if cid == exclude or violations(c) or c.get("status") != "deployed":
            continue
        if set(capabilities) & set(c.get("capability", [])):
            out.append(cid)
    # local-first ordering
    return sorted(out, key=lambda i: manifest()[i].get("availability") != "local")


def _log(component: str, operation: str, decision: str, reason: str) -> None:
    db.execute("INSERT INTO ledger(ts,component,operation,decision,reason,cost_usd) VALUES(?,?,?,?,?,0.0)",
               (time.time(), component, operation, decision, reason))


def authorize(component: str, operation: str = "run", capabilities: list[str] | None = None) -> dict:
    """Allow the operation or raise CostGuardError naming free alternatives."""
    comp = manifest().get(component)
    if comp is None:
        reason = "component is not declared in the zero-spend manifest"
        alts = alternatives(capabilities or [])
    else:
        v = violations(comp)
        if not v:
            _log(component, operation, "allow", "zero-cost declared component")
            return comp
        reason = "; ".join(v)
        alts = alternatives(capabilities or comp.get("capability", []), exclude=component)
    _log(component, operation, "block", reason)
    err = CostGuardError(f"BLOCKED {component}/{operation}: {reason}. Free alternatives: {alts or 'none - user must be notified'}")
    err.alternatives = alts  # type: ignore[attr-defined]
    raise err


def run_with_fallback(component: str, capabilities: list[str], runners: dict):
    """Policy from the spec: block -> find free alternative -> retry -> notify.

    `runners` maps component id -> zero-arg callable.
    """
    try:
        authorize(component, "run", capabilities)
        return component, runners[component]()
    except CostGuardError as e:
        for alt in getattr(e, "alternatives", []):
            if alt in runners:
                authorize(alt, "run-as-fallback", capabilities)
                return alt, runners[alt]()
        raise


def check_host(url: str) -> str:
    """Refuse network egress to anything that is not a declared zero-cost host."""
    host = (urlparse(url).hostname or "").lower()
    if host in PAID_HOSTS:
        _log(host, "http", "block", "known paid endpoint")
        raise CostGuardError(f"BLOCKED network call to paid endpoint {host}")
    for cid, c in manifest().items():
        if host in c.get("network_hosts", []) and not violations(c):
            return cid
    _log(host, "http", "block", "host not declared by any zero-cost component")
    raise CostGuardError(f"BLOCKED network call to undeclared host {host}")


def spend_report() -> dict:
    rows = db.query("SELECT decision, COUNT(*) n, COALESCE(SUM(cost_usd),0) usd FROM ledger GROUP BY decision")
    blocked = db.query("SELECT ts,component,operation,reason FROM ledger WHERE decision='block' ORDER BY id DESC LIMIT 20")
    return {
        "actual_spend_usd": sum(r["usd"] for r in rows),
        "decisions": {r["decision"]: r["n"] for r in rows},
        "recent_blocks": blocked,
        "components": {cid: {"violations": violations(c), "status": c.get("status")} for cid, c in manifest().items()},
    }
