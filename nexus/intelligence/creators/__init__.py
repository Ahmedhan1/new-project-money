"""CreatorReferenceIntelligence (Account 3, agent A): lawfully accessed references -> structural profiles,
abstract style profiles and comparisons. Patterns, never copies. See docs/ACCOUNT3_CREATOR_INTELLIGENCE.md."""
from __future__ import annotations

from .compare import DISCLAIMER, compare, style_from, to_account2_reference_style  # noqa: F401
from .profile import RIGHTS_OK, RightsError, analyze_reference, build_profile  # noqa: F401

__all__ = ["analyze_reference", "build_profile", "compare", "style_from", "to_account2_reference_style", "RIGHTS_OK",
           "RightsError", "DISCLAIMER"]
