"""First-party analytics ledger: NEXUS-uploaded videos get production features + experiment arm; others are marked."""
from __future__ import annotations

from nexus.intelligence.growth import experiments, ledger
from nexus.intelligence.publishing import engine


class Up:
    platform = "youtube"

    def __init__(self):
        self.n = 0

    def preflight(self, m, a):
        pass

    def upload(self, m, a, ctx):
        self.n += 1
        return {"remote_id": f"vid{self.n}", "remote_status": "uploaded"}


def _project(title, hook, kinds):
    from nexus import project as prj
    p = prj.Project.create(title, "create")
    p.data["script"] = {"title": title, "scenes": [{"narration": hook}, {"narration": "More."}], "verbatim": True}
    p.data["visual_plan"] = [{"scene": i, "kind": k} for i, k in enumerate(kinds)]
    p.data["style"] = {"lang": "en", "preset": "documentary"}
    p.save()
    return p.id


def test_enrich_joins_platform_rows_to_first_party_facts_and_arms(home, tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\0" * 100)
    ad = {"youtube": Up()}
    ids = []
    for arm, (title, hook) in {"A": ("How tall is the dam?", "How tall is it? Very."), "B": ("The 111 m dam", "It is 111 m tall.")}.items():
        pid = _project(title, hook, ["photo", "fact_graphic"])
        m = engine.create(pid, {"uri": str(f), "sha256": "a" * 64, "bytes": 100, "media_type": "video/mp4"}, "youtube",
                          {"title": title, "experiment": {"id": "exp_t", "arm": arm}})
        engine.enqueue(m["id"])
        ids.append(engine.process(adapters=ad)[0]["tracking"]["remote"]["remote_id"])
    rows = [{"video_id": ids[0], "platform": "youtube", "metrics": {"views": 10}},
            {"video_id": ids[1], "platform": "youtube", "metrics": {"views": 12}},
            {"video_id": "not-ours", "platform": "youtube", "metrics": {"views": 99}}]
    e = {r["video_id"]: r for r in ledger.enrich(rows)}
    assert e[ids[0]]["first_party"] and e[ids[0]]["variant"] == "A" and e[ids[0]]["meta"]["hook_is_question"]
    assert e[ids[1]]["meta"]["title_has_number"] and e[ids[1]]["meta"]["fact_graphic_share"] == 0.5
    assert e["not-ours"]["first_party"] is False and "meta" not in e["not-ours"]
    assert [r["video_id"] for r in ledger.experiment_records("exp_t", rows)] == ids
    assert ledger.coverage(rows) == {"records": 3, "first_party": 2, "other": 1, "note": ledger.coverage(rows)["note"]}
    assert experiments  # evaluate() reads r["variant"] - set here from the arm recorded at publish time
