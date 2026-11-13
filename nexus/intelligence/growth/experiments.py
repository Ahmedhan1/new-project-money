"""Experiment registry: one variable per experiment, power-calculated minimum sample, explicit lifecycle,
an assignment plan, and evaluation of AnalyticsRecord lists at equal age.

Documents are `ExperimentManifest` (nexus/intelligence/contracts.py). Extra keys (allowed by the contract's
"readers ignore unknown keys" rule): `sample_unit`, `assumptions`, `assignment`, `history`, `compare_at_day`,
`platform_result`.
"""
from __future__ import annotations

import time

from .. import contracts as C
from . import normalise as nz
from . import stats as st

# variable -> what a variant changes; and whether the platform has a native split test for it
VARIABLES: dict[str, dict] = {
    "hook": {"layer": "production", "desc": "first 3 s: line / visual of the opening"},
    "title": {"layer": "packaging", "native_test": "youtube_test_and_compare"},
    "thumbnail": {"layer": "packaging", "native_test": "youtube_test_and_compare"},
    "length": {"layer": "production", "desc": "target duration"},
    "structure": {"layer": "production", "desc": "story order / chapters"},
    "opening": {"layer": "production", "desc": "cold_open | hook_first | title_first"},
    "caption_style": {"layer": "production", "desc": "classic | boxed | pop | karaoke, position"},
    "density": {"layer": "production", "desc": "words per minute / ideas per scene"},
    "pacing": {"layer": "production", "desc": "shot length, pause tolerance"},
    "editing_profile": {"layer": "production", "desc": "director profile name"},
    "music": {"layer": "production", "desc": "music on/off, mood, volume"},
    "publish_time": {"layer": "packaging", "desc": "hour / weekday of publication"},
    "localisation": {"layer": "packaging", "desc": "subtitles vs dub vs separate upload"},
    "cta": {"layer": "packaging", "desc": "call to action wording/placement"},
}

# metric -> (kind, numerator, denominator, sample unit). Proportions are tested per trial (impression / view),
# per-video metrics with Welch t + bootstrap across videos.
METRICS: dict[str, dict] = {
    "ctr": {"kind": "proportion", "num": "clicks", "den": "impressions", "unit": "impressions"},
    "like_rate": {"kind": "proportion", "num": "likes", "den": "views", "unit": "views"},
    "comment_rate": {"kind": "proportion", "num": "comments", "den": "views", "unit": "views"},
    "subscribe_rate": {"kind": "proportion", "num": "subscribers_gained", "den": "views", "unit": "views"},
    "avg_view_pct": {"kind": "mean", "unit": "videos"},
    "avg_view_s": {"kind": "mean", "unit": "videos"},
    "retention_3s": {"kind": "mean", "unit": "videos"},
    "log_views": {"kind": "mean", "unit": "videos"},
    "views_per_day": {"kind": "mean", "unit": "videos"},
    "watch_time_share": {"kind": "platform", "unit": "platform"},
}

TRANSITIONS = {"planned": {"running", "stopped"}, "running": {"collecting", "stopped"},
               "collecting": {"concluded", "inconclusive", "running", "stopped"},
               "inconclusive": {"running", "stopped"}, "concluded": set(), "stopped": set()}


class ExperimentError(ValueError):
    pass


def min_samples(metric: str, baseline: float, mde: float, alpha: float = st.ALPHA, power: float = st.POWER) -> tuple[int, dict]:
    """Per-arm minimum from a power calculation. For proportions `mde` is the relative lift to detect (0.1 = +10 %);
    for means it is Cohen's d. Returns (n, assumptions)."""
    m = METRICS[metric]
    if m["kind"] == "proportion":
        n = st.n_two_proportions(baseline, baseline * (1 + mde), alpha, power)
        how = f"two-proportion z-test, baseline {baseline}, relative lift {mde:+.0%}"
    elif m["kind"] == "mean":
        n = st.n_two_means(mde, alpha, power)
        how = f"Welch t-test, standardised effect d={mde}"
    else:
        n, how = 1, "decided by the platform's own test; NEXUS records the owner-entered result"
    return n, {"alpha": alpha, "power": power, "baseline": baseline, "mde": mde, "method": how, "unit": m["unit"]}


def create(hypothesis: str, variable: str, variants: list[dict], platform: str, metric: str, *, baseline: float,
           mde: float, compare_at_day: int = 7, alpha: float = st.ALPHA, power: float = st.POWER, exp_id: str | None = None) -> dict:
    """A planned experiment. `variants` = [{"id": "A", "desc": ..., "params": {...}}, ...]; the first is the control."""
    if variable not in VARIABLES:
        raise ExperimentError(f"unknown variable '{variable}' (one of {', '.join(VARIABLES)})")
    if metric not in METRICS:
        raise ExperimentError(f"unknown metric '{metric}' (one of {', '.join(METRICS)})")
    if len(variants) < 2 or len({v["id"] for v in variants}) != len(variants):
        raise ExperimentError("need at least two variants with distinct ids")
    changed = {k for v in variants for k in (v.get("params") or {})}
    if len(changed) > 1:
        raise ExperimentError(f"one variable per experiment: variants change {sorted(changed)}")
    n, assumptions = min_samples(metric, baseline, mde, alpha, power)
    doc = C.make("ExperimentManifest", id=exp_id or C.new_id("exp"), hypothesis=hypothesis, variable=variable, variants=variants,
                 platform=platform, metric=metric, min_samples=n, status="planned", results=None, confidence=None,
                 limitations=["correlation within one channel is not causation outside it",
                              "platform distribution changes over time; results expire"])
    doc.update(sample_unit=assumptions["unit"], assumptions=assumptions, compare_at_day=compare_at_day, assignment=[],
               history=[{"t": time.time(), "status": "planned"}])
    return doc


def set_status(exp: dict, status: str, note: str = "") -> dict:
    if status not in TRANSITIONS.get(exp["status"], set()):
        raise ExperimentError(f"illegal transition {exp['status']} -> {status}")
    exp["status"] = status
    exp.setdefault("history", []).append({"t": time.time(), "status": status, "note": note})
    return C.check(exp)


def plan_assignment(exp: dict, uploads: list[str], seed: int = 0) -> list[dict]:
    """Which upload carries which variant. Packaging tests on YouTube use the native Test & compare on ONE upload
    (all variants attached to it). Otherwise uploads are balanced across variants in a seeded shuffled block order
    so that variant is not confounded with publication order."""
    native = VARIABLES[exp["variable"]].get("native_test")
    if native and exp["platform"] == "youtube":
        if not uploads:
            raise ExperimentError("need the upload that carries the native test")
        plan = [{"upload": uploads[0], "variants": [v["id"] for v in exp["variants"]], "method": native,
                 "note": "set up in YouTube Studio by the owner; result is entered with record_platform_result"}]
    else:
        import random
        rng = random.Random(f"{exp['id']}:{seed}")
        ids = [v["id"] for v in exp["variants"]]
        plan, block = [], []
        for u in uploads:
            if not block:
                block = ids[:]
                rng.shuffle(block)
            plan.append({"upload": u, "variant": block.pop(0), "method": "per_upload"})
    exp["assignment"] = plan
    return plan


def record_platform_result(exp: dict, outcome: str, shares: dict[str, float] | None = None, entered_by: str = "owner",
                           winner: str | None = None) -> dict:
    """Store a YouTube Test & compare outcome exactly as the owner reads it in Studio ("winner" | "preferred" | "inconclusive").
    NEXUS does not re-derive significance from watch-time shares it cannot see the sample size of."""
    outcome = outcome.lower()
    if outcome not in {"winner", "preferred", "inconclusive"}:
        raise ExperimentError("outcome must be winner | preferred | inconclusive (YouTube's own labels)")
    if outcome in {"winner", "preferred"} and winner not in {v["id"] for v in exp["variants"]}:
        raise ExperimentError("a winner/preferred outcome needs the variant id")
    exp["platform_result"] = {"outcome": outcome, "variant": winner, "watch_time_share": shares or {}, "entered_by": entered_by,
                              "entered": time.time(), "source": "YouTube Studio Test & compare (owner-entered)"}
    if exp["status"] in {"planned", "running"}:
        exp["status"] = "collecting"
    exp["results"] = {"verdict": "winner" if outcome == "winner" else "inconclusive", "platform": exp["platform_result"]}
    exp["confidence"] = {"winner": 0.8, "preferred": 0.5, "inconclusive": 0.2}[outcome]
    return set_status(exp, "concluded" if outcome == "winner" else "inconclusive", f"platform outcome {outcome}")


def evaluate(exp: dict, records: list[dict], alpha: float = st.ALPHA, conclude: bool = True) -> dict:
    """Compare every variant against the control on records at equal age (`compare_at_day` days since publish).
    Proportion metrics: pooled per arm, two-proportion z-test. Mean metrics: Welch t + bootstrap median CI.
    Holm correction across the k-1 comparisons. Writes `results`, `confidence` and (if conclude) the status."""
    metric, kind = exp["metric"], METRICS[exp["metric"]]["kind"]
    if kind == "platform":
        raise ExperimentError("platform-decided metric: use record_platform_result")
    by_upload = {a["upload"]: a.get("variant") for a in exp.get("assignment") or []}
    snaps = nz.at_age(records, exp.get("compare_at_day", 7))
    arms: dict[str, list[dict]] = {v["id"]: [] for v in exp["variants"]}
    for r in snaps:
        vid = r.get("variant") or by_upload.get(r["video_id"])
        if vid in arms:
            arms[vid].append(r)
    control = exp["variants"][0]["id"]
    comps, used = [], {}
    for v in exp["variants"][1:]:
        a, b = arms[control], arms[v["id"]]
        if kind == "proportion":
            m = METRICS[metric]
            xa, na = sum(nz.metric(r, m["num"]) or 0 for r in a), sum(nz.metric(r, m["den"]) or 0 for r in a)
            xb, nb = sum(nz.metric(r, m["num"]) or 0 for r in b), sum(nz.metric(r, m["den"]) or 0 for r in b)
            res, n = st.two_proportions(int(xa), int(na), int(xb), int(nb), alpha), [int(na), int(nb)]
        else:
            va = [x for x in (nz.metric(r, metric) for r in a) if x is not None]
            vb = [x for x in (nz.metric(r, metric) for r in b) if x is not None]
            res = st.welch(va, vb, alpha)
            res["bootstrap_median"] = st.bootstrap_ci(va, vb, "median", alpha=alpha)
            n = [len(va), len(vb)]
        res.update(control=control, variant=v["id"], record_ids=[nz.rid(r) for r in a + b])
        comps.append((res, n))
        used[v["id"]] = n
    adj = st.holm([c[0].get("p_value") for c in comps])
    out = [st.verdict(res, exp["min_samples"], n, alpha, p) for (res, n), p in zip(comps, adj)]
    winners = [c for c in out if c["verdict"] == "winner"]
    overall = "winner" if winners else "insufficient_data" if all(c["verdict"] == "insufficient_data" for c in out) else "inconclusive"
    better = [c for c in winners if c["effect"] > 0]
    best = max(better, key=lambda c: c["effect"])["variant"] if better else control if winners else None
    exp["results"] = {"verdict": overall, "comparisons": out, "compare_at_day": exp.get("compare_at_day", 7), "alpha": alpha,
                      "correction": "holm", "evaluated": time.time(), "sample_unit": exp.get("sample_unit"), "winner": best,
                      "note": "winner = best arm among significant comparisons vs control; with >2 arms, variants are not tested against each other"}
    used_rows = [r for arm in arms.values() for r in arm]
    origins = sorted({r.get("data_origin", "unlabelled") for r in used_rows})
    simulated = "simulated_test_data" in origins
    exp["results"].update(data_origin=origins, real_data=bool(used_rows) and not simulated and "unlabelled" not in origins)
    if simulated:
        exp["results"]["note"] += "; SIMULATED test data in the sample - not a real result, the status is not concluded"
    exp["confidence"] = round(1 - min((c["p_adjusted"] for c in winners), default=1.0), 3) if winners else 0.0
    if conclude and not simulated:
        if exp["status"] == "planned":
            set_status(exp, "running")
        if exp["status"] == "running":
            set_status(exp, "collecting")
        if overall == "winner" and exp["status"] == "collecting":
            set_status(exp, "concluded", "winner at corrected alpha")
        elif overall == "inconclusive" and exp["status"] == "collecting":
            set_status(exp, "inconclusive", "minimum sample reached, no reliable difference")
        # insufficient_data: stays "collecting"
    return C.check(exp)
