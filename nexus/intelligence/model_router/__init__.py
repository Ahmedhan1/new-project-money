"""Provider-independent AI calls for every Account 3 task.

    from nexus.intelligence.model_router import complete
    r = complete("title_generation", "…prompt…", schema={...}, privacy="public", language="ar-EG")
    r.text / r.data / r.provider / r.model / r.latency_s / r.fallbacks

Contract for callers (stable; agent B owns the implementation behind it):
  * `task` is a key of TASKS (unknown tasks raise).
  * `privacy` is "public" (may go to an authorised hosted-free provider), "internal" (local / owner-run workers only)
    or "private" (local only). Callers that handle user media, unreleased scripts or client data pass "private".
  * With `schema` the result is validated; invalid output is retried, then the next provider is tried.
  * When nothing eligible is available, NoProviderAvailable is raised with the reasons - callers queue or ask a human.
    Nothing is ever sent to a paid endpoint.
"""
from __future__ import annotations

from .router import (TASKS, NoProviderAvailable, Result, account1_capabilities, candidates, complete, health,  # noqa: F401
                     route_explain, status)
