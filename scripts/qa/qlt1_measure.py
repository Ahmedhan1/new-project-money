"""QLT-1 before/after measurement (independent QA). Baseline = the integration candidate's code (`git show <base>:...`),
after = this branch. Real inputs only:
- series: the real Test F bible + 2 episodes (docs/results/F_series.json);
- image search: live Wikimedia Commons (free, throttled; rate-limit answers are recorded, never counted as misses);
(The research-ranking experiment is recorded separately: docs/results/final_qa/qlt1_ranking_experiment.json.)

    python scripts/qa/qlt1_measure.py --base 9752d91 --out docs/results/final_qa/qlt1_measure.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

os.environ.setdefault("NEXUS_EMBEDDED_WORKER", "0")
from nexus import assets, net, series  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _old(base: str, path: str, names: list[str]) -> dict:
    src = subprocess.check_output(["git", "show", f"{base}:{path}"], cwd=ROOT, text=True)
    ns: dict = {"re": re}
    keep = []
    for n in names:
        m = re.search(rf"^(def {n}\(.*?)(?=^\S)", src, re.M | re.S) or re.search(rf"^({n} = .*?\n)(?=^\S)", src, re.M | re.S)
        keep.append(m.group(1))
    exec("\n".join(keep), ns)  # noqa: S102 - our own repository source at a pinned commit
    return ns


def _commons(q: str, cache: dict) -> dict:
    if q in cache:
        return cache[q]
    for attempt in range(4):
        try:
            r = assets.search_commons(q)
            cache[q] = {"hits": len(r), "titles": [x.get("title") for x in r[:5]]}
            return cache[q]
        except net.RemoteUnavailable as e:
            wait = int((re.search(r"after (\d+)s", str(e)) or [0, 30])[1]) + 2
            time.sleep(min(wait, 70))
    cache[q] = {"hits": None, "error": "rate limited (not counted)"}
    return cache[q]


def _ladder(q: str, cache: dict) -> dict:
    """What find_image would search: the query ladder, first rung with hits (as in nexus.assets.find_image)."""
    tried = []
    for rung in assets.query_ladder(q):
        r = _commons(rung, cache)
        tried.append(rung)
        if r.get("hits"):
            return {"matched_rung": rung, "rungs_tried": tried, **r}
    return {"matched_rung": None, "rungs_tried": tried, "hits": 0, "titles": []}


def _relevant(titles: list[str], look: str) -> bool:
    lw = {w for w in re.findall(r"[^\W\d_]{4,}", look.lower())}
    return any(lw & set(re.findall(r"[^\W\d_]{4,}", (t or "").lower())) for t in titles)


def series_metrics(base: str, live: bool) -> dict:
    d = json.loads((ROOT / "docs/results/F_series.json").read_text(encoding="utf-8"))
    b = d["bible"]
    old = _old(base, "nexus/series.py", ["_fields"])
    # The stored bible lost "Lunar Reef" to the one-line parser defect; the corrected parser recovers it from the stored text.
    fixed = {**b, "locations": [dict(zip(("name", "look"), series._parts(x, 2)))
                                for l in b["locations"] for x in series._fields(f"LOCATION: {l['name']} | {l['look']}").get("LOCATION", [])]}
    scenes = [s for e in ("episode_1", "episode_2") for s in d[e]["scenes"]]
    names = {w.lower() for c in b["characters"] for w in c["name"].split()} | {w.lower() for l in fixed["locations"] for w in l["name"].split()}
    before_q = [s["location"] for s in scenes]                                  # candidate: visual_query = location name
    after_q = [series.visual_query_for(fixed, series.location_for(fixed, s["location"]), s["location"]) for s in scenes]
    fict = lambda qs: sum(bool(names & set(q.lower().split())) for q in qs)     # noqa: E731
    locs = [l["name"].lower() for l in b["locations"]]
    off_before = [s["location"] for s in scenes if not any(l in s["location"].lower() or s["location"].lower() in l for l in locs)]
    ep = {"scenes": [dict(s) for s in scenes]}
    repairs = series.repair_locations(fixed, ep)
    # Reconstructed bible line in the shape the model wrote (the second LOCATION on the first one's line)
    raw = "LOCATION: Seabrook Bay | A sprawling estuary with fog-draped piers and rusting fishing nets LOCATION: Lunar Reef | glowing reef"
    out = {
        "input": "docs/results/F_series.json (real Test F bible + 2 episodes, 8 scenes)",
        "bible_locations_parsed": {"before": len(old["_fields"](raw).get("LOCATION", [])), "after": len(series._fields(raw).get("LOCATION", []))},
        "queries_with_fictional_names": {"before": fict(before_q), "after": fict(after_q), "of": len(scenes)},
        "queries": {"before": sorted(set(before_q)), "after": sorted(set(after_q))},
        "image_searches_per_2_episodes": {"before": len(scenes), "after": len({s.get("location") for s in ep["scenes"]})},
        "off_bible_scenes": {"before": len(off_before), "after_repair": sum(series.location_for(fixed, s["location"]) is None for s in ep["scenes"]),
                             "repairs": repairs},
        "bible_after_parser_fix": fixed["locations"],
        "note": "Lunar Reef's look text was cut by the model's one-line output; with no look it keeps its name as the query.",
    }
    if live:
        cache: dict = {}
        look = {l["name"]: l["look"] for l in fixed["locations"]}
        rows = []
        for bq, aq in sorted(set(zip(before_q, after_q))):
            loc = series.location_for(fixed, bq)
            lk = look.get(loc["name"], "") if loc else ""
            if not lk:
                continue
            rb, ra = _ladder(bq, cache), _ladder(aq, cache)
            rows.append({"location": loc["name"], "before": {"query": bq, **rb, "relevant": _relevant(rb.get("titles") or [], lk)},
                         "after": {"query": aq, **ra, "relevant": _relevant(ra.get("titles") or [], lk)}})
        out["commons_live"] = {"rows": rows, "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "relevance_proxy": "a returned title shares a 4+ letter word with the location's look text",
                               "relevant": {"before": sum(r["before"]["relevant"] for r in rows), "after": sum(r["after"]["relevant"] for r in rows),
                                            "of": len(rows)}}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    res = {"schema": "nexus.QLT1Measure/1", "base": subprocess.check_output(["git", "rev-parse", a.base], cwd=ROOT, text=True).strip(),
           "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
           "series": series_metrics(a.base, not a.offline)}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: res["series"][k] for k in ("bible_locations_parsed", "queries_with_fictional_names", "image_searches_per_2_episodes")},
                     ensure_ascii=False))
    print(json.dumps(res["series"].get("commons_live", {}).get("relevant"), ensure_ascii=False))


if __name__ == "__main__":
    main()
