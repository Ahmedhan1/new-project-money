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
                              size=tuple(p.get("size", (1280, 720))), name=p.get("name"), stage=job.stage, on_project=job.set_project,
                              preset=p.get("preset"))
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
        stats, report = qa.render_with_qa(p, tl, a["name"], _analysis(p) or None, draft=bool(a.get("draft")))
        return {"output": str(Path(stats["output"]).relative_to(p.dir)), "status": report["status"], "issues": report["known_issues"],
                "segments_from_cache": stats["segments_from_cache"], "segments": stats["segments"], "seconds": stats["elapsed"],
                "draft": bool(a.get("draft")), "render_notes": stats.get("notes", [])}

    r = job.stage("render-qa", run)
    if a.get("draft"):
        return r  # drafts are previews: no quality verdict
    r["quality"] = job.stage("quality-score", lambda: _quality(p, tl, r["output"], a.get("platform")))
    return r


def _quality(p, tl: dict, output: str, platform: str | None = None) -> dict:
    """Weighted quality score of a finished render, stored next to the QA report (nexus/quality.py)."""
    import json
    from . import quality
    an = _analysis(p)
    rep = quality.score(p.dir / output, tl=tl, analysis=next(iter(an.values())) if len(an) == 1 else None,
                        lang=tl.get("lang"), platform=platform)
    (p.dir / "reports" / f"{Path(output).stem}.quality.json").write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    return quality.summary(rep)


@jobs.handler("direct")
def direct_(job: jobs.Job) -> dict:
    """Edit-my-video: analysis -> director plan (every kept/dropped span with a reason) -> editable timeline."""
    import json
    from . import director, project as prj
    a = job.params
    p = prj.Project(a["project"])
    brief = {k: a[k] for k in ("profile", "target_seconds", "instructions", "title", "platforms", "languages", "shorts") if a.get(k) is not None}
    size = tuple(a.get("size") or (1920, 1080))

    def plan():
        an = _analysis(p)
        if not an:
            raise ValueError("no finished analysis in this project yet: upload a video and wait for the analysis job")
        out = director.direct(list(an), an, brief, size=size)
        (p.dir / "reports" / "director_plan.json").write_text(json.dumps(out["plan"], ensure_ascii=False, default=str), encoding="utf-8")
        (p.dir / "reports" / "director_edl.edl").write_text(out["edl"]["cmx3600"], encoding="utf-8")
        (p.dir / "work" / "director_timeline.json").write_text(json.dumps(out["master"], ensure_ascii=False), encoding="utf-8")
        s = out["plan"]["summary"]
        return {"summary": s, "notes": out["notes"], "retention": out.get("retention"), "profile": out["plan"]["profile"]}

    r = job.stage("plan", plan)

    def build():
        tl = json.loads((p.dir / "work" / "director_timeline.json").read_text(encoding="utf-8"))
        vid = p.save_timeline(tl, f"director: {r['profile']} -> {r['summary']['programme_seconds']:.0f}s", a.get("branch", "main"))
        return {"version": vid}

    return {**r, **job.stage("build-timeline", build)}


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


@jobs.handler("series-bible")
def series_bible(job: jobs.Job) -> dict:
    from . import series
    return job.stage("write-bible", lambda: series.create_series(job.params["concept"], job.params.get("episodes", 3)))


@jobs.handler("series-episode")
def series_episode(job: jobs.Job) -> dict:
    """Two expensive steps. The first also appends the episode to the series canon, so it must never run twice
    for one job - the stage checkpoint guarantees that when a worker dies during production."""
    from . import series

    def write():
        b, ep, review = series.next_episode(job.params["series"])
        return {"bible": b, "episode": ep, "review": review}

    w = job.stage("write-episode", write)

    def produce():
        out = series.produce_episode(w["bible"], w["episode"], w["review"])
        return {"project": out["project"], "status": out["report"]["status"], "issues": out["report"]["known_issues"]}

    r = job.stage("produce-episode", produce)
    job.set_project(r["project"])
    return {"episode": w["episode"], **r}


@jobs.handler("film-plan")
def film_plan(job: jobs.Job) -> dict:
    from . import series
    return job.stage("plan", lambda: series.film_plan(job.params["concept"]))


@jobs.handler("analyze")
def analyze_(job: jobs.Job) -> dict:
    import json
    from . import project as prj
    from .media import analyze
    p = prj.Project(job.params["project"])
    m = next(x for x in p.data["media"] if x["id"] == job.params["media"])

    def run():
        an = analyze.analyze(m["path"])
        (p.dir / "reports" / f"analysis_{m['id']}.json").write_text(json.dumps(an, ensure_ascii=False), encoding="utf-8")
        return {"issues": an["issues"], "scenes": len(an["scenes"]), "duration": an["probe"]["duration"]}

    return job.stage("analyze", run)


@jobs.handler("remix")
def remix_(job: jobs.Job) -> dict:
    from . import project as prj, remix
    p = prj.Project(job.params["project"])

    def run():
        lib = _analysis(p)
        if len(lib) < 2:
            raise ValueError("upload at least two analysed videos into this project first")
        versions = remix.make_versions(p, lib, job.params["brief"])
        return {"plan": prj.Project(p.id).data["remix"]["plan"], "comparison": remix.compare(versions)}

    return job.stage("plan-and-render", run)


@jobs.handler("package")
def package_(job: jobs.Job) -> dict:
    from . import project as prj, publish
    a = job.params
    p = prj.Project(a["project"])
    return job.stage("package", lambda: publish.package(p, p.timeline(a.get("branch", "main")), a["platform"], a["title"],
                                                         a.get("description", ""), a.get("lang", "en")))


@jobs.handler("animate")
def animate_(job: jobs.Job) -> dict:
    from . import project as prj
    from .animation import engines, mograph
    a = job.params
    p = prj.Project(a["project"])
    out, kind, title, data = p.dir / a["output"], a["kind"], a.get("title", ""), a.get("data", [])

    def run():
        if kind == "bar_chart":
            mograph.bar_chart(out, title, [(str(x), float(y)) for x, y in data])
        elif kind == "kinetic":
            mograph.kinetic_text(out, [str(x) for x in data])
        elif kind == "timeline":
            mograph.timeline_animation(out, title, [(str(x), str(y)) for x, y in data])
        elif kind == "explainer":
            engines.manim_explainer(out, title, [{"label": str(x), "detail": str(y)} for x, y in data])
        elif kind == "title3d":
            engines.blender_title(out, title or "NEXUS")
        else:
            raise LookupError(f"unknown animation kind {kind}")
        return {"project": p.id, "output": a["output"]}

    return job.stage("render", run)


@jobs.handler("evolution-check")
def evolution_check(job: jobs.Job) -> dict:
    from . import tools
    return job.stage("check", tools.evolution_check)


@jobs.handler("shorts")
def shorts_(job: jobs.Job) -> dict:
    """Long video -> ranked vertical shorts, each saved on its own branch (short-1..n). Rendering is a separate job."""
    from . import project as prj, shorts
    p = prj.Project(job.params["project"])

    def run():
        lib = _analysis(p)
        if not lib:
            raise ValueError("this project has no analysed video yet")
        path = job.params.get("media") or next(iter(lib))
        media = next(m for m in p.data["media"] if m["path"] == path)
        rows = shorts.make_shorts(p, media, lib[path], n=job.params.get("n", 3), min_s=job.params.get("min_s", 18),
                                  max_s=job.params.get("max_s", 58), sfx=True)
        return {"project": p.id, "shorts": rows}

    return job.stage("find-and-build", run)
