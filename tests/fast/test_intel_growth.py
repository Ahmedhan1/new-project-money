"""Account 3 growth engine (agent F): statistics, experiments, learning loop, recommendations. No network, no models."""
from __future__ import annotations

import math

import numpy as np
import pytest

from nexus.intelligence import contracts as C
from nexus.intelligence.growth import experiments as X
from nexus.intelligence.growth import learning as L
from nexus.intelligence.growth import normalise as nz
from nexus.intelligence.growth import recommend as R
from nexus.intelligence.growth import stats as st


def rec(vid, date="2026-01-08", published="2026-01-01", variant=None, **metrics):
    d = C.make("AnalyticsRecord", platform="youtube", video_id=vid, date=date, metrics=metrics, source="test fixture (synthetic)",
               imported=0.0, variant=variant)
    d["meta"] = {"published": published}
    return d


# ---------------------------------------------------------------- statistics
def test_distributions_match_reference_values():
    assert abs(st.norm_ppf(0.975) - 1.959963984540054) < 1e-9
    assert abs(st.t_ppf(0.975, 7) - 2.364624251592784) < 1e-6
    # reference values from scipy.stats.ttest_ind(b, a, equal_var=False) (computed once, scipy not needed here)
    a, b = [3.1, 2.9, 3.6, 4.0, 2.2, 3.3, 3.8], [4.1, 3.9, 5.2, 4.4, 3.7, 4.9]
    w = st.welch(a, b)
    assert abs(w["p_value"] - 0.007254942705356839) < 1e-5
    assert abs(w["ci"][0] - 0.36279218690525905) < 1e-4 and abs(w["ci"][1] - 1.8276840035709316) < 1e-4


def test_power_calculation_and_holm():
    assert 8000 < st.n_two_proportions(0.05, 0.06) < 8300          # textbook ~8,150 per arm
    assert st.n_two_means(0.5) == 64                                # 2*(1.96+0.84)^2/0.25 = 62.8 -> 63 + 1
    assert st.holm([0.01, 0.04, None, 0.03]) == [0.03, 0.06, None, 0.06]


def test_bootstrap_is_deterministic_and_covers_effect():
    rng = np.random.default_rng(0)
    a, b = rng.normal(0, 1, 200), rng.normal(1, 1, 200)
    r1, r2 = st.bootstrap_ci(a, b), st.bootstrap_ci(a, b)
    assert r1 == r2 and r1["ci"][0] < 1 < r1["ci"][1] and r1["ci"][0] > 0


def test_three_vs_five_views_is_never_a_winner():
    r = st.verdict(st.two_proportions(3, 10, 5, 10), n_min=100, n=[10, 10])
    assert r["verdict"] == "insufficient_data"
    # even ignoring the guard, 3/10 vs 5/10 is not significant
    r = st.verdict(st.two_proportions(3, 10, 5, 10), n_min=1, n=[10, 10])
    assert r["verdict"] == "inconclusive" and r["ci"][0] < 0 < r["ci"][1]


# ---------------------------------------------------------------- experiments
def _ctr_exp():
    return X.create("question titles raise CTR", "title", [{"id": "A"}, {"id": "B", "params": {"title": "question"}}],
                    "tiktok", "ctr", baseline=0.05, mde=0.2)


def test_experiment_contract_power_and_single_variable():
    e = _ctr_exp()
    assert C.validate(e) == [] and e["status"] == "planned" and e["sample_unit"] == "impressions"
    assert e["min_samples"] == st.n_two_proportions(0.05, 0.06)
    with pytest.raises(X.ExperimentError):
        X.create("two things", "hook", [{"id": "A"}, {"id": "B", "params": {"hook": "q", "music": "off"}}], "youtube", "ctr", baseline=0.05, mde=0.2)
    with pytest.raises(X.ExperimentError):
        X.set_status(e, "concluded")


def test_tiny_experiment_stays_collecting():
    e = _ctr_exp()
    X.plan_assignment(e, ["v1", "v2"])
    recs = [rec(a["upload"], impressions=10, clicks=3 if a["variant"] == "A" else 5) for a in e["assignment"]]
    X.evaluate(e, recs)
    assert e["results"]["verdict"] == "insufficient_data" and e["status"] == "collecting" and e["results"]["winner"] is None
    assert e["results"]["comparisons"][0]["effect"] is not None      # effect + CI always reported


def test_large_clear_effect_concludes_with_winner_and_equal_age():
    e = _ctr_exp()
    recs = [rec(f"a{i}", variant="A", impressions=5000, clicks=250) for i in range(4)]
    recs += [rec(f"b{i}", variant="B", impressions=5000, clicks=350) for i in range(4)]
    # an old snapshot (60 days) with a huge count must not be mixed into the day-7 comparison
    recs.append(rec("a0", date="2026-03-02", variant="A", impressions=10**6, clicks=10**4))
    X.evaluate(e, recs)
    c = e["results"]["comparisons"][0]
    assert e["results"]["verdict"] == "winner" and e["results"]["winner"] == "B" and e["status"] == "concluded"
    assert c["n"] == [20000, 20000] and c["ci"][0] > 0 and len(c["record_ids"]) == 8


def test_multiple_arms_are_holm_corrected_and_null_is_inconclusive():
    e = X.create("captions", "caption_style", [{"id": "A"}, {"id": "B", "params": {"caption_style": "pop"}},
                                                {"id": "C", "params": {"caption_style": "boxed"}}], "youtube", "avg_view_pct", baseline=0.0, mde=0.8)
    rng = np.random.default_rng(3)
    recs = [rec(f"{v}{i}", variant=v, avg_view_pct=float(x)) for v in "ABC" for i, x in enumerate(rng.normal(45, 8, 40))]
    X.evaluate(e, recs)
    cs = e["results"]["comparisons"]
    assert all(c["p_adjusted"] >= c["p_value"] for c in cs)
    assert e["results"]["verdict"] == "inconclusive" and e["status"] == "inconclusive"
    assert all("bootstrap_median" in c for c in cs)


def test_assignment_balanced_and_native_platform_test():
    e = X.create("hook", "hook", [{"id": "A"}, {"id": "B", "params": {"hook": "q"}}], "youtube", "retention_3s", baseline=0.0, mde=0.5)
    plan = X.plan_assignment(e, [f"u{i}" for i in range(10)])
    assert sorted(p["variant"] for p in plan).count("A") == 5
    t = X.create("thumb", "thumbnail", [{"id": "A"}, {"id": "B", "params": {"thumbnail": "face"}}], "youtube", "watch_time_share",
                 baseline=0.5, mde=0.1)
    plan = X.plan_assignment(t, ["u1"])
    assert plan[0]["method"] == "youtube_test_and_compare" and plan[0]["variants"] == ["A", "B"]
    X.record_platform_result(t, "preferred", {"A": 0.47, "B": 0.53}, winner="B")
    assert t["status"] == "inconclusive" and t["results"]["verdict"] == "inconclusive" and C.validate(t) == []


# ---------------------------------------------------------------- normalisation
def test_metric_aliases_and_loader(tmp_path):
    r = rec("x", ctr="4.5", average_view_duration=12, duration_s=40, views=1000, like_count=50)
    assert nz.metric(r, "ctr") == 0.045 and nz.metric(r, "avg_view_pct") == 0.3 and nz.metric(r, "like_rate") == 0.05
    assert nz.metric(r, "views_per_day") == 1000 / 7 and nz.metric(r, "unknown_metric") is None
    p = tmp_path / "e.csv"
    p.write_text("video_id,date,published,views,impressions,niche\nv1,2026-02-01,2026-01-25,120,3000,science\n,,,,,\n", encoding="utf-8")
    rows = nz.load_csv(p, "youtube")
    assert len(rows) == 1 and rows[0]["metrics"] == {"views": 120.0, "impressions": 3000.0} and nz.meta(rows[0], "niche") == "science"
    assert C.validate(rows[0]) == [] and nz.age_days(rows[0]) == 7


# ---------------------------------------------------------------- learning loop
def _curve(drop3, total=40.0, steep=None):
    pts, y = [[0.0, 1.0]], 1.0
    for t in range(1, int(total) + 1):
        rate = drop3 / 3 if t <= 3 else 0.006
        if steep and steep[0] < t <= steep[1]:
            rate = 0.03
        y = max(0.0, y - rate)
        pts.append([float(t), round(y, 4)])
    return pts


def test_weak_opening_against_channel_baseline():
    recs = [rec(f"v{i}", retention_curve=_curve(0.20 + 0.01 * (i % 3))) for i in range(8)]
    recs.append(rec("weak", retention_curve=_curve(0.45)))
    out = {o["video_id"]: o for o in L.weak_openings(recs)}
    assert out["weak"]["weak"] and out["weak"]["windows"][3]["status"] == "weak"
    assert not any(o["weak"] for k, o in out.items() if k != "weak")
    few = L.weak_openings(recs[:3])
    assert all(w["status"] == "no_baseline" for o in few for w in o["windows"].values())
    recs_out = R.from_weak_openings(L.weak_openings(recs), profile="commercial")
    assert len(recs_out) == 1 and recs_out[0]["target"] == "account2" and C.validate(recs_out[0]) == []
    assert R.check_production(recs_out[0]["production"]) == [] and recs_out[0]["evidence"][0]["record_id"].startswith("youtube:weak")


def _timeline():
    clips = [{"type": "video", "src": "/a.mp4", "in": 0.0, "out": 4.0}, {"type": "video", "src": "/b.mp4", "in": 0.0, "out": 18.0},
             {"type": "video", "src": "/c.mp4", "in": 0.0, "out": 6.0}]
    subs = [{"start": 0.3, "end": 3.9, "text": "a"}, {"start": 4.2, "end": 8.0, "text": "b"}, {"start": 22.5, "end": 27.5, "text": "c."}]
    return {"width": 1080, "height": 1920, "fps": 30, "clips": clips, "subtitles": subs, "audio": [], "overlays": []}


def test_loss_localised_to_static_clip_and_mapped_to_account2():
    r = rec("v", retention_curve=_curve(0.2, total=28, steep=(10, 18)))
    loss = L.loss_segments(r, _timeline())
    seg = max(loss["segments"], key=lambda s: s["loss"])
    assert 9 <= seg["t0"] <= 11 and 17 <= seg["t1"] <= 19
    assert [c["index"] for c in seg["clips"]] == [1]
    assert any(f["check"] == "longest static stretch" for f in seg["editorial_flags"]) and seg["speech_share"] < 0.5
    recs = R.from_loss_segments(loss, profile="documentary")
    prod = recs[0]["production"]
    assert prod["director_overrides"]["shot_len"] == [3.0, 9.0] and "add_punch_ins" in prod["retention_fixes"]
    assert R.check_production(prod) == [] and C.validate(recs[0]) == []


def test_patterns_are_associations_with_guards_and_correction():
    rng = np.random.default_rng(1)
    recs = []
    for i in range(240):
        q = i % 2 == 0
        recs.append(rec(f"p{i}", views=float(math.expm1(rng.normal(9.0 + (0.6 if q else 0.0), 1.0)))))
        recs[-1]["meta"].update(niche="science" if i < 200 else "tiny", has_q=q)
    feats = {"title_question": lambda r: nz.meta(r, "has_q")}
    pats = L.mine_patterns(recs, feats, "log_views", ("niche",))
    by = {p["segment"]["niche"]: p for p in pats}
    assert by["science"]["verdict"] == "association" and by["science"]["ci"][0] > 0 and by["science"]["tests_corrected_for"] == 2
    assert by["tiny"]["verdict"] == "insufficient_data"                     # 20 vs 20 < MIN_GROUP
    assert not any(p["verdict"] == "winner" for p in pats)                  # observational never "wins"
    recs_out = R.from_patterns(pats, "synthetic fixture")
    assert len(recs_out) == 1 and recs_out[0]["target"] == "account3" and recs_out[0]["confidence"] <= 0.4
    exps = R.next_experiments(pats)
    assert exps and exps[0]["variable"] == "title" and exps[0]["metric"] == "watch_time_share" and C.validate(exps[0]) == []


def test_experiment_recommendation_and_router_fallback(monkeypatch):
    e = _ctr_exp()
    X.evaluate(e, [rec("a", variant="A", impressions=10, clicks=3), rec("b", variant="B", impressions=10, clicks=5)])
    r = R.from_experiment(e)[0]
    assert "no variant is declared better" in r["text"] and r["confidence"] <= 0.1
    from nexus.intelligence import model_router as mr

    def boom(*a, **k):
        raise mr.NoProviderAvailable("analytics_interpretation", ["test: nothing eligible"])
    monkeypatch.setattr(mr, "complete", boom)
    out = R.narrate(r, use_llm=True)
    assert out["phrasing"].startswith("deterministic (no provider") and "narrative" not in out and out["text"]
