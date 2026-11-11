"""Durable production workflows (handlers for nexus.jobs). Heavy modules are imported inside the handlers
so the worker starts fast and the ledger can be inspected without models installed."""
from __future__ import annotations

from pathlib import Path

from . import jobs


@jobs.handler("create-from-idea")
def create_from_idea(job: jobs.Job) -> dict:
    from . import create
    p = job.params
    out = create.create_video(p["topic"], scenes=p.get("scenes", 5), lang=p.get("lang", "en"), notes=p.get("notes"),
                              size=tuple(p.get("size", (1280, 720))), name=p.get("name"), stage=job.stage, on_project=job.set_project)
    return {"project": out["project"], "status": out["report"]["status"], "issues": out["report"]["known_issues"],
            "title": out["script"]["title"], "output": out["output"]}


def _analysis(p) -> dict:
    import json
    out = {}
    for m in p.data["media"]:
        f = p.dir / "reports" / f"analysis_{m['id']}.json"
        if f.exists():
            out[m["path"]] = json.loads(f.read_text(encoding="utf-8"))
    return out


@jobs.handler("render")
def render(job: jobs.Job) -> dict:
    from . import project as prj, qa
    a = job.params
    p = prj.Project(a["project"])
    tl = p.timeline(a.get("branch", "main"))

    def run():  # the renderer caches finished segments on disk, so a resumed render only encodes what is missing
        stats, report = qa.render_with_qa(p, tl, a["name"], _analysis(p) or None)
        return {"output": str(Path(stats["output"]).relative_to(p.dir)), "status": report["status"], "issues": report["known_issues"],
                "segments_from_cache": stats["segments_from_cache"], "segments": stats["segments"], "seconds": stats["elapsed"]}

    return job.stage("render-qa", run)


@jobs.handler("localize")
def localize_(job: jobs.Job) -> dict:
    from . import localize, project as prj, qa
    a = job.params
    p = prj.Project(a["project"])
    branch, lang = a.get("branch", "main"), a["lang"]

    def translate():
        ltl, review = localize.localize(p, p.timeline(branch), lang, _analysis(p) or None, context=p.name, dub=a.get("dub", True))
        p.save_timeline(ltl, f"{lang} version", branch=lang)
        return {"review": review}

    review = job.stage("translate-dub", translate)["review"]

    def run():
        stats, report = qa.render_with_qa(p, p.timeline(lang), f"{branch}_{lang}", extra_stages=[review], extra={"language": lang})
        return {"output": str(Path(stats["output"]).relative_to(p.dir)), "status": report["status"], "issues": report["known_issues"],
                "lines": review["lines"]}

    return job.stage("render-qa", run)


@jobs.handler("backup")
def backup_(job: jobs.Job) -> dict:
    from . import backup
    a = job.params
    out = job.stage("backup-verify", lambda: backup.run_backup(a.get("tiers"), a.get("to")))
    if a.get("drill") and out["ok"]:
        out["restore_drill"] = job.stage("restore-drill", lambda: backup.restore_drill(a.get("to")))
    if not out["ok"]:
        raise RuntimeError(out["warning"] or "backup failed")
    return {"status": "VERIFIED" if out["complete"] else "PARTIAL", "copies": out["copies"], "warning": out["warning"],
            "issues": [r["destination"] for r in out["results"] if not r["ok"]]}
