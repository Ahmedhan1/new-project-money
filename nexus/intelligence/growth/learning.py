"""Learning loop: patterns with uncertainty, weak openings, retention-loss localisation.

All findings here are OBSERVATIONAL (comparisons between videos that differ in many ways). A pattern can suggest an
experiment; only an experiment (experiments.evaluate) can support a causal claim about one variable.
"""
from __future__ import annotations

import statistics
from typing import Callable

from . import normalise as nz
from . import stats as st

MIN_GROUP = 30          # videos per side before an observational pattern is reported as anything but "insufficient_data"
OPENING_WINDOWS = (3, 5, 15)


def compare(a_rows: list[dict], b_rows: list[dict], metric: str | Callable[[dict], float | None], min_n: int = MIN_GROUP,
            alpha: float = st.ALPHA) -> dict:
    """b vs a on a per-video metric: Welch t (mean diff, CI, p) + bootstrap CI of the median difference."""
    f = metric if callable(metric) else (lambda r, m=metric: nz.metric(r, m))
    va = [(x, r) for r in a_rows if (x := f(r)) is not None]
    vb = [(x, r) for r in b_rows if (x := f(r)) is not None]
    res = st.welch([x for x, _ in va], [x for x, _ in vb], alpha)
    res["bootstrap_median"] = st.bootstrap_ci([x for x, _ in va], [x for x, _ in vb], "median", alpha=alpha)
    res["record_ids"] = {"a": [nz.rid(r) for _, r in va][:15], "b": [nz.rid(r) for _, r in vb][:15], "truncated_at": 15}
    res["n"] = [len(va), len(vb)]
    res["_min_n"] = min_n
    return res


def mine_patterns(records: list[dict], features: dict[str, Callable[[dict], bool | None]], metric: str | Callable,
                  segment_keys: tuple[str, ...] = ("platform", "niche"), min_n: int = MIN_GROUP, alpha: float = st.ALPHA,
                  metric_name: str | None = None) -> list[dict]:
    """For every segment and binary feature: compare videos with vs without the feature on `metric`.
    Holm correction over ALL tests run (segments x features). Every result keeps n, effect, CI, p, adjusted p, verdict."""
    out = []
    for seg, rows in nz.segment(records, segment_keys).items():
        for fname, fn in features.items():
            yes, no = [r for r in rows if fn(r) is True], [r for r in rows if fn(r) is False]
            if not yes or not no:
                continue
            res = compare(no, yes, metric, min_n, alpha)
            res.update(feature=fname, segment=dict(zip(segment_keys, seg)), metric=metric_name or (metric if isinstance(metric, str) else "custom"),
                       direction="with feature minus without")
            out.append(res)
    adj = st.holm([r.get("p_value") for r in out])
    final = []
    for r, p in zip(out, adj):
        n_min = r.pop("_min_n")
        v = st.verdict(r, n_min, r["n"], alpha, p)
        v["kind"] = "observational"
        v["tests_corrected_for"] = sum(x is not None for x in adj)
        if v["verdict"] == "winner":
            v["verdict"] = "association"   # observational: never a "winner"
            v["why"] += "; association only - confounders not controlled"
        final.append(v)
    return final


# ---------------------------------------------------------------- openings
def opening_drops(r: dict, windows=OPENING_WINDOWS) -> dict[int, float | None]:
    return {w: (None if (x := nz.retention_at(r, w)) is None else round(1 - x, 4)) for w in windows}


def weak_openings(records: list[dict], windows=OPENING_WINDOWS, k: float = 2.0, min_abs: float = 0.05, min_baseline: int = 5) -> list[dict]:
    """Flag videos whose relative audience drop in the first 3/5/15 s is well above the channel's own baseline.
    Baseline = median of the OTHER videos (leave-one-out); threshold = median + max(k * 1.4826 * MAD, min_abs).
    With fewer than `min_baseline` other videos the result is 'no_baseline' (not a flag)."""
    rows = nz.latest(records)
    drops = {nz.rid(r): opening_drops(r, windows) for r in rows}
    out = []
    for r in rows:
        me = drops[nz.rid(r)]
        item = {"record_id": nz.rid(r), "video_id": r["video_id"], "drops": me, "windows": {}, "weak": False}
        for w in windows:
            others = [d[w] for i, d in drops.items() if i != nz.rid(r) and d[w] is not None]
            if me[w] is None:
                continue
            if len(others) < min_baseline:
                item["windows"][w] = {"status": "no_baseline", "baseline_n": len(others)}
                continue
            med = statistics.median(others)
            mad = statistics.median(abs(x - med) for x in others) * 1.4826
            thr = med + max(k * mad, min_abs)
            weak = me[w] > thr
            item["windows"][w] = {"status": "weak" if weak else "ok", "drop": me[w], "baseline_median": round(med, 4),
                                  "threshold": round(thr, 4), "baseline_n": len(others), "excess": round(me[w] - med, 4)}
            item["weak"] |= weak
        out.append(item)
    return out


# ---------------------------------------------------------------- retention-loss localisation
def _curve_seconds(r: dict) -> list[tuple[float, float]]:
    c = (r.get("metrics") or {}).get("retention_curve")
    if not c:
        return []
    if isinstance(c, dict):
        d = nz.metric(r, "duration_s")
        pts = [(float(x) * d, float(y)) for x, y in c.get("ratio") or []] if d else []
    else:
        pts = [(float(x), float(y)) for x, y in c]
    pts.sort()
    base = pts[0][1] if pts and pts[0][1] else 1.0
    return [(t, y / base) for t, y in pts]


def loss_segments(r: dict, timeline: dict | None = None, factor: float = 2.0, min_len: float = 1.0, platform: str = "shorts") -> dict:
    """Where the audience leaves faster than this video's own typical rate (loss/second > factor x median), mapped to
    timeline clips and to the editorial checks of nexus.growth.retention_report that overlap the same seconds."""
    pts = _curve_seconds(r)
    if len(pts) < 4:
        return {"record_id": nz.rid(r), "segments": [], "note": "no retention curve with >= 4 points"}
    rates = [((t0, t1), max(0.0, (y0 - y1) / (t1 - t0))) for (t0, y0), (t1, y1) in zip(pts, pts[1:]) if t1 > t0]
    med = statistics.median(x for _, x in rates) or 1e-9
    hot: list[list[float]] = []
    prev_hot = False
    for (t0, t1), x in rates:
        if x > factor * med:
            if prev_hot:
                hot[-1][1] = t1
                hot[-1][2] += x * (t1 - t0)
            else:
                hot.append([t0, t1, x * (t1 - t0)])
        prev_hot = x > factor * med
    segs = [{"t0": round(a, 2), "t1": round(b, 2), "loss": round(loss, 4), "rate_vs_median": round(loss / (b - a) / med, 2)}
            for a, b, loss in hot if b - a >= min_len or loss >= 0.03]
    rep = None
    if timeline:
        from nexus import growth
        from nexus.timeline.render import clip_duration, clip_starts
        rep = growth.retention_report(timeline, None, platform)
        starts = clip_starts(timeline)
        clips = [(s, s + clip_duration(c), i, c) for i, (s, c) in enumerate(zip(starts, timeline["clips"]))]
        spans = rep["data"]["speech_spans"]
        for s in segs:
            s["clips"] = [{"index": i, "type": c["type"], "src": c.get("src"), "start": round(a, 2), "end": round(b, 2)}
                          for a, b, i, c in clips if a < s["t1"] and b > s["t0"]]
            speech = sum(max(0.0, min(b, s["t1"]) - max(a, s["t0"])) for a, b in spans)
            s["speech_share"] = round(speech / max(s["t1"] - s["t0"], 1e-9), 2)
            s["editorial_flags"] = [{"check": c["check"], "detail": c["detail"], "fix": c["fix"]} for c in rep["checks"]
                                    if not c["ok"] and _overlaps(c, s)]
    return {"record_id": nz.rid(r), "median_loss_per_s": round(med, 5), "segments": segs,
            "retention_report_score": rep["score"] if rep else None,
            "note": "localisation is descriptive: a drop next to a clip does not prove the clip caused it"}


def _overlaps(check: dict, seg: dict) -> bool:
    """Checks whose detail names a time range ('... at a-bs') overlap the segment; hook/opening checks overlap t<5s."""
    import re
    m = re.search(r"at (\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)s", check.get("detail") or "")
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return a < seg["t1"] and b > seg["t0"]
    name = check.get("check", "")
    if name.startswith(("hook within", "picture in the first")):
        return seg["t0"] < 5
    return name in ("dead air", "pattern interrupts", "average shot length")
