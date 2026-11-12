"""Account 3 growth engine demo (agent F).

Part 1 - REAL PUBLIC DATASET (not the owner's channel): "YouTube Video Metadata", T. Nawani, Mendeley Data V1,
DOI 10.17632/4gf2nhbr9d.1, CC BY 4.0. Title features vs age-normalised views within search category and format.
Part 2 - SYNTHETIC FIXTURES (clearly labelled): experiment lifecycle, weak openings, retention-loss localisation,
because no channel analytics or retention curves exist in this environment.

    NEXUS_HOME=/home/user/new-project-money/.nexus python scripts/account3/growth_demo.py [--csv PATH] [--llm]

Writes docs/results/account3/growth_demo.json. Download the CSV first (5.7 MB) into .nexus/a3_data/mendeley/data.csv:
https://data.mendeley.com/public-files/datasets/4gf2nhbr9d/files/7b029d0e-8421-4301-a600-4fbe8f281dfd/file_downloaded
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nexus.intelligence import contracts as C  # noqa: E402
from nexus.intelligence.growth import experiments as X  # noqa: E402
from nexus.intelligence.growth import learning as L  # noqa: E402
from nexus.intelligence.growth import normalise as nz  # noqa: E402
from nexus.intelligence.growth import recommend as R  # noqa: E402

DATASET = {"name": "YouTube Video Metadata", "author": "Tisha Nawani", "publisher": "Mendeley Data", "version": "V1",
           "published": "2026-05-12", "doi": "10.17632/4gf2nhbr9d.1", "url": "https://data.mendeley.com/datasets/4gf2nhbr9d/1",
           "licence": "CC BY 4.0 (http://creativecommons.org/licenses/by/4.0), read from the Mendeley public API data_licence field",
           "sha256": "56c938bc9c69a368929c320f54e3d32ec06b916945c0b74abdb57719929abc8e",
           "changes": "derived title features and views/day; no rows redistributed"}
LABEL = "public dataset, not the owner's channel"
SNAPSHOT = "2026-05-11"   # ASSUMPTION: collection date is not stated; newest publish 2026-05-10, dataset published 2026-05-12
AGE_BAND = (3, 30)        # compare only videos 3-30 days old at the assumed snapshot
SYN = "synthetic fixture (not real data)"

TITLE_FEATURES = {
    "title_question": lambda r: "?" in (nz.meta(r, "title") or ""),
    "title_number": lambda r: bool(re.search(r"\d", nz.meta(r, "title") or "")),
    "title_exclamation": lambda r: "!" in (nz.meta(r, "title") or ""),
    "title_allcaps_word": lambda r: bool(re.search(r"\b[A-Z]{4,}\b", nz.meta(r, "title") or "")),
    "title_long_over_60_chars": lambda r: len(nz.meta(r, "title") or "") > 60,
}


def log_views_per_day(r):
    v = nz.metric(r, "views_per_day")
    return math.log10(1 + v) if v is not None else None


def load_public(path: Path) -> tuple[list[dict], dict]:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if sha != DATASET["sha256"]:
        raise SystemExit(f"dataset hash mismatch ({sha}); refusing to label results with the cited dataset")
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    snap = dt.date.fromisoformat(SNAPSHOT)
    out, dropped = [], {"age_out_of_band": 0, "zero_or_missing_views": 0, "zero_duration": 0}
    for row in rows:
        pub = dt.date.fromisoformat(row["published_at"][:10])
        age = (snap - pub).days
        views, dur = float(row["view_count"] or 0), float(row["duration_seconds"] or 0)
        if not AGE_BAND[0] <= age <= AGE_BAND[1]:
            dropped["age_out_of_band"] += 1
            continue
        if views <= 0:
            dropped["zero_or_missing_views"] += 1
            continue
        if dur <= 0:
            dropped["zero_duration"] += 1
            continue
        doc = C.make("AnalyticsRecord", platform="youtube", video_id=row["video_id"], date=SNAPSHOT,
                     metrics={"views": views, "likes": float(row["like_count"] or 0), "comments": float(row["comment_count"] or 0),
                              "duration_s": dur, "age_days": float(age)},
                     source=f"{LABEL}: Mendeley 10.17632/4gf2nhbr9d.1 (CC BY 4.0)", imported=time.time())
        doc["meta"] = {"published": row["published_at"][:10], "niche": row["query"], "language": row["default_language"] or None,
                       "format": "short_le_60s" if dur <= 60 else "long_gt_60s", "title": row["title"]}
        out.append(doc)
    return out, {"rows": len(rows), "used": len(out), "dropped": dropped}


def part1(path: Path) -> dict:
    recs, counts = load_public(path)
    overall = L.mine_patterns(recs, TITLE_FEATURES, log_views_per_day, ("platform",), metric_name="log10(1+views/day)")
    seg = L.mine_patterns(recs, TITLE_FEATURES, log_views_per_day, ("niche", "format"), metric_name="log10(1+views/day)")
    likes = L.mine_patterns(recs, TITLE_FEATURES, "like_rate", ("platform",), metric_name="like_rate (likes/views)")

    def slim(p):
        keep = ("feature", "segment", "metric", "n", "means", "effect", "ci", "cohens_d", "p_value", "p_adjusted", "verdict", "why",
                "tests_corrected_for", "robust_to_outliers")
        d = {k: p.get(k) for k in keep}
        d["median_diff"], d["median_ci"] = p["bootstrap_median"]["effect"], p["bootstrap_median"]["ci"]
        d["record_ids_sample"] = p["record_ids"]["b"][:3]
        return d

    allp = overall + seg + likes
    recs_out = [R.narrate(r) for r in R.from_patterns(allp, f"{LABEL}: Mendeley 10.17632/4gf2nhbr9d.1")]
    exps = R.next_experiments(overall + seg, platform="youtube", max_n=3)
    verdicts = {}
    for p in seg:
        verdicts[p["verdict"]] = verdicts.get(p["verdict"], 0) + 1
    return {"label": LABEL, "dataset": DATASET, "assumptions": {"snapshot_date": SNAPSHOT, "age_band_days": AGE_BAND,
            "why": "one snapshot per video; views are normalised by age (views/day) and compared only within a 3-30 day age band"},
            "counts": counts, "overall": [slim(p) for p in overall], "like_rate": [slim(p) for p in likes],
            "segmented_verdicts": verdicts, "segmented": [slim(p) for p in seg if p["verdict"] != "insufficient_data"],
            "segmented_insufficient": [{"feature": p["feature"], "segment": p["segment"], "n": p["n"]} for p in seg if p["verdict"] == "insufficient_data"],
            "recommendations": recs_out, "next_experiments": exps,
            "caveats": ["public cross-section sampled by search query, NOT the owner's channel and NOT trending data",
                        "collection date not stated: ages assume a 2026-05-11 snapshot (+-1-2 days changes views/day of young videos)",
                        "observational: channel size, topic, thumbnail and promotion are not controlled - associations, not causes",
                        "views/day over the first days is front-loaded; the 3-30 day band reduces but does not remove age effects",
                        "Holm correction across every test in a family; segment tests have small groups and mostly report insufficient_data"]}


# ---------------------------------------------------------------- synthetic fixtures (labelled)
def _rec(vid, variant=None, published="2026-01-01", date="2026-01-08", **m):
    d = C.make("AnalyticsRecord", platform="youtube", video_id=vid, date=date, metrics=m, source=SYN, imported=time.time(), variant=variant)
    d["meta"] = {"published": published}
    return d


def _curve(drop3, total=28, steep=None):
    pts, y = [[0.0, 1.0]], 1.0
    for t in range(1, total + 1):
        rate = drop3 / 3 if t <= 3 else 0.006
        if steep and steep[0] < t <= steep[1]:
            rate = 0.03
        y = max(0.0, y - rate)
        pts.append([float(t), round(y, 4)])
    return pts


def part2() -> dict:
    tiny = X.create("question titles raise CTR", "title", [{"id": "A"}, {"id": "B", "params": {"title": "question"}}], "tiktok", "ctr",
                    baseline=0.05, mde=0.2)
    X.evaluate(tiny, [_rec("t1", "A", impressions=10, clicks=3), _rec("t2", "B", impressions=10, clicks=5)])
    big = X.create("question titles raise CTR", "title", [{"id": "A"}, {"id": "B", "params": {"title": "question"}}], "tiktok", "ctr",
                   baseline=0.05, mde=0.2)
    X.evaluate(big, [_rec(f"a{i}", "A", impressions=5000, clicks=250) for i in range(4)] + [_rec(f"b{i}", "B", impressions=5000, clicks=350) for i in range(4)])
    thumb = X.create("face thumbnail gets more watch-time share", "thumbnail", [{"id": "A"}, {"id": "B", "params": {"thumbnail": "face"}}],
                     "youtube", "watch_time_share", baseline=0.5, mde=0.1)
    X.plan_assignment(thumb, ["upload_1"])
    X.record_platform_result(thumb, "preferred", {"A": 0.47, "B": 0.53}, winner="B")
    curves = [_rec(f"v{i}", retention_curve=_curve(0.2 + 0.01 * (i % 3))) for i in range(8)] + [_rec("weak", retention_curve=_curve(0.45, steep=(10, 18)))]
    weak = L.weak_openings(curves)
    tl = {"width": 1080, "height": 1920, "fps": 30, "audio": [], "overlays": [],
          "clips": [{"type": "video", "src": "/a.mp4", "in": 0.0, "out": 4.0}, {"type": "video", "src": "/b.mp4", "in": 0.0, "out": 18.0},
                    {"type": "video", "src": "/c.mp4", "in": 0.0, "out": 6.0}],
          "subtitles": [{"start": 0.3, "end": 3.9, "text": "a"}, {"start": 4.2, "end": 8.0, "text": "b"}, {"start": 22.5, "end": 27.5, "text": "c."}]}
    loss = L.loss_segments(curves[-1], tl)
    recs = R.from_weak_openings(weak, profile="commercial") + R.from_loss_segments(loss, profile="documentary") + R.from_experiment(tiny) + R.from_experiment(big)
    for r in recs:
        r["label"] = SYN
    return {"label": SYN, "experiments": {"tiny_3_vs_5_clicks": tiny, "large_clear_effect": big, "youtube_test_and_compare": thumb},
            "weak_openings": [w for w in weak if w["weak"]], "loss_localisation": loss, "recommendations": recs,
            "production_dict_check": {r["id"]: R.check_production(r["production"]) for r in recs if r.get("production")}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT.parents[2] / ".nexus/a3_data/mendeley/data.csv") if "worktrees" in str(ROOT) else
                    str(ROOT / ".nexus/a3_data/mendeley/data.csv"))
    ap.add_argument("--llm", action="store_true", help="also phrase one recommendation through the model router (heavy lock)")
    ap.add_argument("--out", default=str(ROOT / "docs/results/account3/growth_demo.json"))
    a = ap.parse_args()
    t0 = time.time()
    path = Path(a.csv)
    p1 = part1(path) if path.exists() else {"label": LABEL, "status": "not run: dataset file missing", "expected_path": str(path)}
    p2 = part2()
    if a.llm and p1.get("recommendations"):
        from nexus import jobs, llm
        with jobs.heavy_lock():
            try:
                R.narrate(p1["recommendations"][0], use_llm=True, privacy="internal")
            finally:
                llm.unload()
    out = {"schema": "nexus.account3.growth_demo/1", "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "runtime_s": None, "part1_public_dataset": p1, "part2_synthetic": p2}
    out["runtime_s"] = round(time.time() - t0, 2)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    s = p1.get("overall") or []
    print(json.dumps({"out": a.out, "used": p1.get("counts"), "overall": [(p["feature"], p["n"], p["effect"], p["ci"], p["p_adjusted"], p["verdict"]) for p in s],
                      "segmented_verdicts": p1.get("segmented_verdicts"), "recs": len(p1.get("recommendations") or []),
                      "next_experiments": [e["hypothesis"] for e in p1.get("next_experiments") or []],
                      "phrasing": [r.get("phrasing") for r in (p1.get("recommendations") or [])[:1]]}, indent=1, default=str))


if __name__ == "__main__":
    main()
