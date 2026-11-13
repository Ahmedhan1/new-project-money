"""First-party analytics ledger: joins the owner's platform analytics to what NEXUS itself knows about each video.

Platform analytics (YouTube Analytics API / Studio CSV, imported by publishing.analytics) know a video id and numbers.
NEXUS knows, first hand, how that video was made and published: the project, its script, hook, scene visuals,
language, preset, the publishing manifest and the experiment arm it carried. `enrich()` attaches those facts to every
analytics record whose video id NEXUS uploaded, so `learning.mine_patterns` and `experiments.evaluate` work on
first-party features instead of guesses. Records NEXUS did not publish are kept but marked `first_party: False`.
No third-party tracking, nothing leaves the machine."""
from __future__ import annotations

import re

from ..publishing import store


def publications() -> dict[str, dict]:
    """remote video id -> {manifest, project, platform, visibility, uploaded_at, experiment}."""
    out = {}
    for m in store.list_manifests():
        remote = ((m.get("tracking") or {}).get("remote") or {})
        rid = remote.get("remote_id")
        if not rid:
            continue
        up = next((h.get("at") for h in reversed(m.get("history") or []) if h.get("event") == "uploaded"), None)
        out[str(rid)] = {"manifest": m["id"], "project": m["project_id"], "platform": m["platform"], "visibility": m.get("visibility"),
                         "uploaded_at": up, "experiment": (m.get("metadata") or {}).get("experiment")}
    return out


def production_features(project_id: str) -> dict:
    """Facts NEXUS recorded while producing the video (empty when the project is gone)."""
    try:
        from ... import project as prj
        data = prj.Project(project_id).data
    except Exception:
        return {}
    sc = data.get("script") or {}
    scenes = sc.get("scenes") or []
    plan = data.get("visual_plan") or []
    kinds = [p.get("kind") for p in plan]
    hook = scenes[0].get("narration", "") if scenes else ""
    title = sc.get("title", "")
    n = len(kinds) or 1
    return {"title_len": len(title), "title_has_number": bool(re.search(r"\d", title)), "hook_is_question": "?" in hook.split(".")[0],
            "hook_has_number": bool(re.search(r"\d", hook)), "scenes": len(scenes),
            "fact_graphic_share": round(kinds.count("fact_graphic") / n, 3), "title_card_share": round(kinds.count("title_card_fallback") / n, 3),
            "language": (data.get("style") or {}).get("lang"), "preset": (data.get("style") or {}).get("preset"),
            "verbatim_script": bool(sc.get("verbatim")), "a3_script": (data.get("a3") or {}).get("script_id")}


def enrich(records: list[dict]) -> list[dict]:
    pubs = publications()
    cache: dict[str, dict] = {}
    out = []
    for r in records:
        p = pubs.get(str(r.get("video_id")))
        r = dict(r)
        if not p:
            r["first_party"] = False
            out.append(r)
            continue
        if p["project"] not in cache:
            cache[p["project"]] = production_features(p["project"])
        r["meta"] = {**(r.get("meta") or {}), **cache[p["project"]], "manifest": p["manifest"], "project": p["project"]}
        exp = p.get("experiment") or {}
        if exp.get("arm") and not r.get("variant"):
            r["variant"] = exp["arm"]
            r["experiment"] = exp.get("id")
        r["first_party"] = True
        out.append(r)
    return out


def experiment_records(exp_id: str, records: list[dict]) -> list[dict]:
    """Analytics rows belonging to one experiment (by the arm recorded at publish time)."""
    return [r for r in enrich(records) if r.get("experiment") == exp_id]


def coverage(records: list[dict]) -> dict:
    e = enrich(records)
    fp = sum(1 for r in e if r["first_party"])
    return {"records": len(e), "first_party": fp, "other": len(e) - fp,
            "note": "first-party = uploaded by NEXUS; only these carry production features and experiment arms"}
