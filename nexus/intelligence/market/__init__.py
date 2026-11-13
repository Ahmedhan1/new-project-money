"""Market intelligence (agent C): multi-source research, demand/trend signals, ContentOpportunity scoring,
ResearchBrief with cross-checked claims, niche database, trend monitor and the hand-off to Account 2's create pipeline.

    from nexus.intelligence.market import Ctx, analyse, trend_monitor, to_production_brief
    report = analyse(Ctx(), "black hole", "science", lang="en")

See docs/ACCOUNT3_MARKET_INTELLIGENCE.md.
"""
from __future__ import annotations

from .brief import build_brief  # noqa: F401
from .niches import load as load_niches, watchlist  # noqa: F401
from .pipeline import analyse, gather, to_production_brief, trend_monitor  # noqa: F401
from .scoring import build_opportunity  # noqa: F401
from .sources import Ctx, host_declared  # noqa: F401
