"""Account 3 HTTP API, mounted under /api/a3 by nexus/api.py (one include line), plus the workspace UI script
(/ui/intelligence.js, loaded by index.html). Each section degrades to a clear 503 when its work stream is not
installed, instead of failing the whole app."""
from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/a3", tags=["account3"])
UI_JS = Path(__file__).resolve().parent.parent / "ui" / "intelligence.js"


def _mod(name: str):
    try:
        return importlib.import_module(f"nexus.intelligence.{name}")
    except ImportError as e:
        raise HTTPException(503, f"Account 3 module '{name}' is not installed in this build ({e})")


def ui_js() -> Response:
    return Response(UI_JS.read_text(encoding="utf-8"), media_type="application/javascript")


# ----------------------------------------------------------------- AI models / provider router
@router.get("/ai/providers")
def ai_providers():
    from .capability_registry import catalog
    from .model_router import router as r
    out = []
    for p in catalog()["providers"]:
        for m in p.get("models") or [{"model": "(discovered at runtime)"}]:
            e = {**{k: v for k, v in p.items() if k != "models"}, "provider": p["id"], **m}
            ok, why = r.eligibility(e, "public") if m.get("model") != "(discovered at runtime)" else (False, "configure base_url + models")
            out.append({"provider": p["id"], "name": p.get("name"), "model": m.get("model"), "hosting": p["hosting"], "privacy": p["privacy"],
                        "licence": m.get("licence", p.get("licence_runtime")), "commercial_use": p.get("commercial_use"),
                        "quotas": p.get("quotas", {}), "verified": p.get("verified"), "quality": m.get("quality", {}),
                        "available": ok, "why": why})
    return {"models": out, "health": r.health()}


@router.get("/ai/routes")
def ai_routes(privacy: str = "public"):
    from .model_router import router as r
    if privacy not in r.PRIVACY_RANK:
        raise HTTPException(422, "privacy must be public, internal or private")
    return {"privacy": privacy, "tasks": r.status(privacy)}


class AskReq(BaseModel):
    task: str = "general"
    prompt: str
    privacy: str = "internal"


@router.post("/ai/try")
def ai_try(q: AskReq):
    """Run one prompt through the router (UI 'try it'). Never cached, never sent to a provider the privacy class forbids."""
    from .model_router import router as r
    try:
        res = r.complete(q.task, q.prompt, privacy=q.privacy, max_tokens=300, use_cache=False)
    except r.NoProviderAvailable as e:
        raise HTTPException(503, str(e))
    except KeyError as e:
        raise HTTPException(422, str(e))
    return {"text": res.text, "provider": res.provider, "model": res.model, "latency_s": res.latency_s, "fallbacks": res.fallbacks,
            "below_bar": res.below_bar}
