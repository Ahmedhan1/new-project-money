"""Account 3 growth engine (agent F): experiments, statistics, learning loop, recommendations.

    from nexus.intelligence.growth import experiments, stats, learning, recommend, normalise
    exp = experiments.create("hook B keeps more viewers", "hook", [{"id": "A"}, {"id": "B", "params": {"hook": "question"}}],
                             "youtube", "retention_3s", baseline=0.0, mde=0.5)
    experiments.evaluate(exp, analytics_records)   # -> results with effect, CI, Holm p, verdict; "inconclusive" is normal

Inputs are AnalyticsRecord documents (nexus/intelligence/contracts.py). Nothing here predicts views; observational
patterns are associations and only produce low-confidence recommendations to run an experiment.
See docs/ACCOUNT3_GROWTH_ENGINE.md.
"""
from . import experiments, learning, normalise, recommend, stats  # noqa: F401
