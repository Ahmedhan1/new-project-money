"""GrowthRecommendation documents and next experiments from patterns, weak openings and loss segments.

target "account2": creative/production changes, carried in `production` - a dict Account 2 can apply directly:
    director_overrides  -> nexus.director.get_profile(name, overrides)   (keys must exist in registry/direction.yaml)
    preset_overrides    -> nexus.presets.get(name, **overrides)
    retention_fixes     -> fix ids of nexus.growth.retention_report / apply_retention_pass
    instructions        -> text for nexus.director.parse_instructions
target "account3": packaging / scheduling (titles, thumbnails, publish time, localisation), carried in `packaging`.
`text` is always the deterministic sentence built from the numbers; an optional router narrative goes to `narrative`.
"""
from __future__ import annotations

from .. import contracts as C
from . import experiments as X

FEATURE_VARIABLE = (("title_", "title"), ("thumb", "thumbnail"), ("publish_", "publish_time"), ("hook", "hook"),
                    ("duration", "length"), ("length", "length"), ("caption", "caption_style"), ("music", "music"),
                    ("pace", "pacing"), ("opening", "opening"), ("cta", "cta"), ("lang", "localisation"))
PACKAGING = {"title", "thumbnail", "publish_time", "localisation", "cta"}


def _ev(source: str, **facts) -> dict:
    e = C.evidence(source, kind="analytics")
    e.update(facts)
    return e


def variable_for(feature: str) -> str | None:
    f = feature.lower()
    return next((v for p, v in FEATURE_VARIABLE if f.startswith(p) or p in f), None)


def check_production(prod: dict) -> list[str]:
    """Problems that would stop Account 2 from applying a production dict (unknown profile keys / fix ids)."""
    from nexus import director, growth
    errs = []
    keys = set(director.get_profile(None))
    for k in (prod.get("director_overrides") or {}):
        if k not in keys:
            errs.append(f"director override '{k}' is not a profile field")
    known = set(growth.AUTO_FIXES) | {"add_hook_text", "add_hook_visual", "end_on_closing_line", "choose_window", "move_text"}
    errs += [f"unknown retention fix '{f}'" for f in prod.get("retention_fixes") or [] if f not in known]
    return errs


def _rec(kind: str, text: str, evidence: list, confidence: float, target: str, **extra) -> dict:
    d = C.make("GrowthRecommendation", id=C.new_id("rec"), kind=kind, text=text, evidence=evidence,
               confidence=round(max(0.0, min(1.0, confidence)), 3), target=target, status="proposed", experiment_id=extra.pop("experiment_id", None))
    d.update(extra)
    return d


def from_weak_openings(items: list[dict], profile: str | None = None, platform: str = "shorts") -> list[dict]:
    from nexus import growth
    weak = [i for i in items if i["weak"]]
    if not weak:
        return []
    fs = growth.PLATFORM_NORMS.get(platform, growth.PLATFORM_NORMS["shorts"])["first_speech_max"]
    ev = [_ev("retention curve vs channel baseline (leave-one-out median + 2 MAD)", record_id=i["record_id"],
              windows={str(w): v for w, v in i["windows"].items() if v.get("status") == "weak"}) for i in weak]
    worst = max((v["excess"] for i in weak for v in i["windows"].values() if v.get("status") == "weak"), default=0)
    base_n = min(v["baseline_n"] for i in weak for v in i["windows"].values() if "baseline_n" in v)
    text = (f"{len(weak)} of {len(items)} videos lose viewers in the opening faster than this channel's baseline "
            f"(largest excess {worst:.0%} at the flagged window, baseline of {base_n} videos). Open on the strongest self-contained "
            f"line, speech within {fs}s, no fade-in. Test it as a 'hook'/'opening' experiment before making it the default.")
    prod = {"director_profile": profile, "director_overrides": {"opening": "cold_open"},
            "retention_fixes": ["trim_lead", "remove_lead_fade", "add_hook_text"], "instructions": "start on the strongest line; tighter opening",
            "hook_max_s": fs}
    return [_rec("weak_opening", text, ev, 0.3 + 0.05 * min(len(weak), 4), "account2", production=prod)]


def from_loss_segments(loss: dict, profile: str | None = None) -> list[dict]:
    from nexus import director
    out = []
    for s in loss.get("segments") or []:
        flags = {f["check"] for f in s.get("editorial_flags") or []}
        prof = director.get_profile(profile)
        prod: dict = {"director_profile": profile, "director_overrides": {}, "retention_fixes": []}
        why = []
        if flags & {"longest static stretch", "average shot length", "pattern interrupts"} or s.get("clips") and len(s["clips"]) == 1 and s["t1"] - s["t0"] > 4:
            lo, hi = prof["shot_len"]
            prod["director_overrides"]["shot_len"] = [round(lo * 0.75, 2), round(hi * 0.75, 2)]
            prod["retention_fixes"].append("add_punch_ins")
            prod["instructions"] = "faster"
            why.append("static/long shot")
        if "dead air" in flags or s.get("speech_share", 1) < 0.5:
            prod["director_overrides"]["pause_tolerance"] = round(min(prof["pause_tolerance"], 0.5), 2)
            prod["retention_fixes"].append("tighten_pauses")
            why.append(f"low speech share {s.get('speech_share')}")
        if not why:
            continue
        ev = [_ev("retention curve loss localisation", record_id=loss["record_id"], t0=s["t0"], t1=s["t1"], loss=s["loss"],
                  rate_vs_median=s["rate_vs_median"], clips=[c["index"] for c in s.get("clips") or []], flags=sorted(flags))]
        text = (f"Viewers leave {s['rate_vs_median']}x faster than this video's median rate at {s['t0']}-{s['t1']}s "
                f"({s['loss']:.0%} of the audience), where the edit has: {', '.join(why)}. One video: treat as a lead, not a rule.")
        out.append(_rec("retention_loss", text, ev, 0.25, "account2", production=prod))
    return out


def from_patterns(patterns: list[dict], dataset_label: str) -> list[dict]:
    """Associations become low-confidence recommendations to TEST (never to adopt); inconclusive ones are reported too."""
    out = []
    for p in patterns:
        if p["verdict"] not in ("association", "winner"):
            continue
        var = variable_for(p["feature"])
        target = "account3" if var in PACKAGING or var is None else "account2"
        ci = p["bootstrap_median"]["ci"]
        ev = [_ev(dataset_label, feature=p["feature"], segment=p["segment"], metric=p["metric"], n=p["n"], mean_diff=p["effect"],
                  ci95=p["ci"], median_diff=p["bootstrap_median"]["effect"], median_ci95=ci, p_adjusted=p["p_adjusted"],
                  record_ids_sample=p["record_ids"]["b"][:5])]
        text = (f"In {_seg(p['segment'])}, videos with '{p['feature']}' differ on {p['metric']} by {p['effect']:+.3g} (mean; 95% CI "
                f"{p['ci'][0]:+.3g}..{p['ci'][1]:+.3g}; median diff {p['bootstrap_median']['effect']:+.3g}, CI {ci[0]:+.3g}..{ci[1]:+.3g}; "
                f"n={p['n'][1]} vs {p['n'][0]}; Holm-adjusted p={p['p_adjusted']:.3g}). Association only - "
                f"test '{var or p['feature']}' in a controlled experiment before acting.")
        conf = 0.35 if p["kind"] == "observational" else 0.7
        if not p.get("robust_to_outliers", True):
            text += " The median-difference CI includes 0, so the mean difference may be driven by a few outliers."
            conf -= 0.1
        out.append(_rec("pattern", text, ev, conf, target, variable=var,
                        packaging={"feature": p["feature"]} if target == "account3" else None))
    return out


def _seg(seg: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in seg.items() if v is not None) or "all records"


def next_experiments(patterns: list[dict], platform: str = "youtube", max_n: int = 5) -> list[dict]:
    """Turn the strongest observational differences (association, or inconclusive with |d| >= 0.1) into planned experiments
    with a power-calculated sample size for the observed effect."""
    cands = [p for p in patterns if p.get("cohens_d") is not None and variable_for(p["feature"])
             and (p["verdict"] == "association" or (p["verdict"] == "inconclusive" and abs(p["cohens_d"]) >= 0.1))]
    cands.sort(key=lambda p: (p["verdict"] != "association", -abs(p["cohens_d"])))
    out, seen = [], set()
    for p in cands:
        var = variable_for(p["feature"])
        if var in seen:
            continue
        seen.add(var)
        if var in ("title", "thumbnail") and platform == "youtube":
            exp = X.create(f"'{p['feature']}' changes watch-time share (observed association d={p['cohens_d']:+.2f} in public/observational data)",
                           var, [{"id": "A", "desc": "current style"}, {"id": "B", "desc": f"with {p['feature']}", "params": {var: p["feature"]}}],
                           platform, "watch_time_share", baseline=0.5, mde=0.1)
        else:
            d = max(0.1, min(abs(p["cohens_d"]), 0.8))
            exp = X.create(f"'{p['feature']}' changes {p['metric']} (observed d={p['cohens_d']:+.2f}, observational)", var,
                           [{"id": "A", "desc": "control"}, {"id": "B", "desc": f"with {p['feature']}", "params": {var: p["feature"]}}],
                           platform, p["metric"] if p["metric"] in X.METRICS else "avg_view_pct", baseline=0.0, mde=d)
        exp["source_pattern"] = {"feature": p["feature"], "segment": p["segment"], "cohens_d": p["cohens_d"], "p_adjusted": p["p_adjusted"]}
        out.append(exp)
        if len(out) >= max_n:
            break
    return out


def _origin(res: dict) -> dict:
    """Where the experiment's numbers came from (D-19): copied into the evidence so a reader sees real vs simulated."""
    return {"data_origin": res.get("data_origin") or ["unlabelled"], "real_data": bool(res.get("real_data"))}


def from_experiment(exp: dict) -> list[dict]:
    res = exp.get("results") or {}
    target = "account3" if exp["variable"] in PACKAGING else "account2"
    if res.get("verdict") != "winner":
        text = (f"Experiment {exp['id']} ({exp['variable']}): {res.get('verdict', 'not evaluated')} - no variant is declared better. "
                + "; ".join(f"{c['variant']} vs {c['control']}: effect {c['effect']} CI {c['ci']} ({c['why']})" for c in res.get("comparisons") or []))
        return [_rec("experiment_result", text, [_ev("experiment evaluation", experiment_id=exp["id"], verdict=res.get("verdict"), **_origin(res))], 0.1,
                     target, experiment_id=exp["id"])]
    win = res["winner"]
    params = next((v.get("params") or {} for v in exp["variants"] if v["id"] == win), {})
    c = max((c for c in res["comparisons"] if c["verdict"] == "winner"), key=lambda c: abs(c["effect"]))
    text = (f"Experiment {exp['id']}: variant {win} wins on {exp['metric']} (effect {c['effect']:+.4g}, 95% CI {c['ci']}, Holm p={c['p_adjusted']:.3g}, "
            f"n={c['n']} {exp.get('sample_unit')}). Adopt it as the default for {exp['platform']} and re-test in 8-12 weeks.")
    extra = {"production": {"director_overrides": params}} if target == "account2" else {"packaging": params}
    return [_rec("experiment_result", text, [_ev("experiment evaluation", experiment_id=exp["id"], comparison=c["variant"], effect=c["effect"],
                                                  ci95=c["ci"], p_adjusted=c["p_adjusted"], n=c["n"], **_origin(res))],
                 min(0.9, exp.get("confidence") or 0.5), target, experiment_id=exp["id"], **extra)]


def narrate(rec: dict, use_llm: bool = False, privacy: str = "internal") -> dict:
    """Optional plain-language narrative via the model router (task analytics_interpretation). Numbers in `text` and
    `evidence` stay authoritative; when no provider is eligible the deterministic text is used and that is recorded."""
    rec["phrasing"] = "deterministic"
    if not use_llm:
        return rec
    try:
        from nexus.intelligence.model_router import NoProviderAvailable, complete
        prompt = ("Rewrite for a video creator in two plain sentences. Do not add any number, claim or promise that is not in the text.\n\n"
                  + rec["text"])
        try:
            r = complete("analytics_interpretation", prompt, privacy=privacy, max_tokens=160, temperature=0.2)
            rec["narrative"], rec["phrasing"] = r.text.strip(), f"router:{r.provider}/{r.model}"
        except NoProviderAvailable as e:
            rec["phrasing"] = f"deterministic (no provider: {'; '.join(e.reasons[:2])})"
    except Exception as e:  # router absent/misconfigured: never fail the recommendation
        rec["phrasing"] = f"deterministic (router error: {type(e).__name__})"
    return rec
