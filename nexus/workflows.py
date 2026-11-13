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
                              preset=p.get("preset"), script=p.get("script"))
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

    def preflight():  # refuse before encoding anything when the render cannot finish in the free space
        from . import diskman
        est = diskman.estimate_render(tl, bool(a.get("draft")))
        return {**diskman.preflight(est["required_bytes"], f"render {a['name']}"), "estimate": est}

    job.stage("disk-preflight", preflight)

    def run():  # the renderer caches finished segments on disk, so a resumed render only encodes what is missing
        stats, report = qa.render_with_qa(p, tl, a["name"], _analysis(p) or None, draft=bool(a.get("draft")))
        return {"output": str(Path(stats["output"]).relative_to(p.dir)), "status": report["status"], "issues": report["known_issues"],
                "segments_from_cache": stats["segments_from_cache"], "segments": stats["segments"], "seconds": stats["elapsed"],
                "draft": bool(a.get("draft")), "render_notes": stats.get("notes", [])}

    r = job.stage("render-qa", run)
    if a.get("draft"):
        return r  # drafts are previews: no quality verdict
    r["quality"] = job.stage("quality-score", lambda: _quality(p, tl, r["output"], a.get("platform")))
    if a.get("archive") or (a.get("archive") is None and _auto_archive()):
        # daily output: archive the verified final externally (the local copy is released only after verification)
        r["archive"] = {"job": jobs.submit("archive", {"project": p.id, "render": r["output"], "kind": a.get("kind", "master")}, p.id)["job"]}
    return r


def _auto_archive() -> bool:
    """Auto-archive only when the owner configured a destination (Account 1 storage rules or archive settings)."""
    from . import archive, config
    s = archive.settings()
    return bool(s.get("auto_archive")) and (bool(s.get("connection_id")) or (config.HOME / "storage_rules.json").exists())


def _quality(p, tl: dict, output: str, platform: str | None = None) -> dict:
    """Weighted quality score of a finished render, stored next to the QA report (nexus/quality.py)."""
    import json
    from . import quality
    # The source analysis describes the SOURCE, not this programme: an edit is meant to cover only part of it, so it is
    # not passed (it would turn every condensed edit into a "missing dialogue" localization failure).
    rep = quality.score(p.dir / output, tl=tl, analysis=None, lang=tl.get("lang"), platform=platform)
    (p.dir / "reports" / f"{Path(output).stem}.quality.json").write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
    return quality.summary(rep)


@jobs.handler("direct")
def direct_(job: jobs.Job) -> dict:
    """Edit-my-video: analysis -> director plan (every kept/dropped span with a reason) -> editable timeline."""
    import json
    from . import director, project as prj
    a = job.params
    p = prj.Project(a["project"])
    brief = {k: a[k] for k in ("profile", "target_seconds", "instructions", "title", "platforms", "languages", "shorts", "overrides",
                               "profile_overrides", "end_text") if a.get(k) is not None}
    if a.get("reference_profile_id"):  # structure (numbers) of a lawfully supplied reference, never its content
        from . import reference
        ref = json.loads((p.dir / "reports" / "references" / f"{a['reference_profile_id']}.json").read_text(encoding="utf-8"))
        ov = reference.to_overrides(ref)
        brief["profile_overrides"] = director._merge(brief.get("profile_overrides") or {}, ov["profile_overrides"])
        brief.setdefault("instructions", None)
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
        spk_report = None
        try:  # match speakers recorded at different levels (interviewer vs guest mic); needs diarisation turns
            from .audio import post as apost
            tl, spk_report = apost.apply_speaker_gains(tl, _analysis(p))
        except Exception as e:  # noqa: BLE001 - level matching is an improvement, never a reason to fail the edit
            spk_report = {"error": f"{type(e).__name__}: {e}"[:200]}
        vid = p.save_timeline(tl, f"director: {r['profile']} -> {r['summary']['programme_seconds']:.0f}s", a.get("branch", "main"))
        return {"version": vid, "speaker_levels": spk_report}

    r = {**r, **job.stage("build-timeline", build)}
    if not a.get("broll"):
        return r

    def add_broll():  # licensed cutaways over the speaker's own audio (nexus/broll.py); network sources can be down
        from . import broll
        tl = p.timeline(a.get("branch", "main"))
        local = []  # the user's own other files (declared rights) as cutaway candidates, ranked visually when SigLIP is present
        used = {c.get("src") for c in tl["clips"]}
        ans = _analysis(p)
        for m in p.data["media"]:
            an = ans.get(m["path"])
            basis = (m.get("rights") or {}).get("basis") if isinstance(m.get("rights"), dict) else None
            if an and basis in ("owned", "licensed", "public-domain", "generated") and m["path"] not in used:
                local += broll.local_candidates(m["path"], an, basis, (m.get("rights") or {}).get("holder") or m["name"])
        pl = broll.plan(tl, max_share=float(a.get("broll_share", 0.35)), local=local or None, subject=a.get("broll_subject") or ())
        new, rep = broll.apply(tl, pl)
        if rep["inserted"]:
            p.save_timeline(new, f"B-roll: {rep['inserted']} licensed cutaways", a.get("branch", "main"))
        (p.dir / "reports" / "broll_plan.json").write_text(json.dumps(rep, ensure_ascii=False, default=str), encoding="utf-8")
        return {"inserted": rep["inserted"], "share": rep["share"], "credits": rep["credits"],
                "source_errors": pl["source_errors"]}

    return {**r, "broll": job.stage("broll", add_broll)}


@jobs.handler("localize")
def localize_(job: jobs.Job) -> dict:
    from . import localize, project as prj, qa
    a = job.params
    p = prj.Project(a["project"])
    branch, lang = a.get("branch", "main"), a["lang"]

    def translate():
        ltl, review = localize.localize(p, p.timeline(branch), lang, _analysis(p) or None, context=p.name, dub=a.get("dub", True),
                                        mode=a.get("mode", "replace"))
        p.save_timeline(ltl, f"{lang} version", branch=a.get("out_branch") or lang)
        return {"review": review}

    review = job.stage("translate-dub", translate)["review"]

    def run():
        ob = a.get("out_branch") or lang
        stats, report = qa.render_with_qa(p, p.timeline(ob), f"{branch}_{ob}", extra_stages=[review], extra={"language": lang})
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

    a = job.params
    if not a.get("v2"):  # legacy request shape (n/min_s/max_s): unchanged behaviour
        def run():
            lib = _analysis(p)
            if not lib:
                raise ValueError("this project has no analysed video yet")
            path = a.get("media") or next(iter(lib))
            media = next(m for m in p.data["media"] if m["path"] == path)
            rows = shorts.make_shorts(p, media, lib[path], n=a.get("n", 3), min_s=a.get("min_s", 18), max_s=a.get("max_s", 58), sfx=True)
            return {"project": p.id, "shorts": rows}

        return job.stage("find-and-build", run)

    def plan():
        lib = _analysis(p)
        if not lib:
            raise ValueError("this project has no analysed video yet")
        path = a.get("media") or next(iter(lib))
        ctl = {k: a[k] for k in ("pick", "time_range", "topic", "speaker", "durations", "intensity", "captions", "music", "aspect",
                                 "brand", "min_score") if a.get(k) not in (None, [], "")}
        if ctl.get("time_range"):
            ctl["time_range"] = tuple(ctl["time_range"])
        items = shorts.factory(path, lib[path], n=a.get("n", 3), platforms=tuple(a.get("platforms") or ("youtube_shorts",)),
                               variants=int(a.get("variants", 1)), **ctl)
        rows = shorts.save_batch(p, items)
        notes = list(getattr(items, "notes", []))
        for r, it in zip(rows, items):
            r["why"] = it["why"]
            r["context_check"] = {k: it["context_check"].get(k) for k in ("status", "why")}
        return {"project": p.id, "shorts": rows, "notes": notes, "moments": len({(r["start"], r["end"]) for r in rows})}

    r = job.stage("select-and-build", plan)
    if not a.get("render"):
        return r
    limit = int(a.get("render_limit", 12))

    def render_all():
        from . import qa
        done = []
        for row in r["shorts"][:limit]:
            tl = p.timeline(row["branch"])
            stats, rep = qa.render_with_qa(p, tl, row["branch"], max_rounds=1)
            out = str(Path(stats["output"]).relative_to(p.dir))
            done.append({"branch": row["branch"], "output": out, "seconds": stats["elapsed"],
                         "quality": _quality(p, tl, out, row["platform"])})
        return {"rendered": done, "not_rendered": [x["branch"] for x in r["shorts"][limit:]]}

    return {**r, **job.stage("render-and-score", render_all)}


@jobs.handler("deliver")
def deliver_(job: jobs.Job) -> dict:
    """Destination selector: one master -> tuned, checked, packaged versions for every selected platform."""
    from . import project as prj, publish
    a = job.params
    p = prj.Project(a["project"])
    tl = p.timeline(a.get("branch", "main"))
    return job.stage("render-check-package", lambda: publish.deliver(p, tl, a["platforms"], a.get("title") or p.name,
                                                                     a.get("description", ""), a.get("lang") or tl.get("lang", "en"),
                                                                     analysis_by_src=_analysis(p) or None))


# Account 3 job kinds (registered by import): publish-upload
from .intelligence.publishing import jobs_handler as _a3_publish  # noqa: E402,F401
from .intelligence import workflows as _a3_workflows  # noqa: E402,F401


@jobs.handler("export")
def export_(job: jobs.Job) -> dict:
    """Re-encode a rendered file with chosen export settings (codec/quality/size target) and verify the result."""
    from . import export, project as prj
    a = job.params
    p = prj.Project(a["project"])
    src = (p.dir / a["render"]).resolve()
    if not str(src).startswith(str(p.dir.resolve())) or not src.is_file():
        raise ValueError(f"render not found: {a['render']}")
    ext = {"av1": ".mkv"}.get(a.get("codec", "h264"), ".mp4")
    out = p.dir / "exports" / f"{src.stem}_{a.get('codec', 'h264')}_{a.get('quality', 'balanced')}{ext}"
    out.parent.mkdir(parents=True, exist_ok=True)

    def run():
        rep = export.export(src, out, codec=a.get("codec", "h264"), quality=a.get("quality", "balanced"), width=a.get("width"),
                            height=a.get("height"), fps=a.get("fps"), max_bytes=a.get("max_bytes"), audio=a.get("audio", "aac"))
        rep["output"] = str(out.relative_to(p.dir)) if out.exists() else None
        return rep

    return job.stage("encode-verify", run)


@jobs.handler("clip-process")
def clip_process(job: jobs.Job) -> dict:
    """Enhancement / VFX on one clip (media-to-media, cached): the clip's src is replaced by the processed segment and
    the original reference kept in `enhanced_from`, so undo restores it. Saved as a new timeline version."""
    from . import enhance, project as prj, vfx
    a = job.params
    p = prj.Project(a["project"])
    branch = a.get("branch", "main")

    def run():
        tl = p.timeline(branch)
        idx = next(i for i, c in enumerate(tl["clips"]) if c["id"] == a["clip"])
        op = a["op"]
        fn = vfx.apply if op in {"replace_background", "tracked_mask"} else enhance.apply
        tl["clips"][idx] = fn(tl["clips"][idx], op, **(a.get("params") or {}))
        vid = p.save_timeline(tl, f"{op} on {a['clip']}", branch)
        return {"project": p.id, "version": vid, "src": tl["clips"][idx]["src"], "op": op}

    return job.stage("process", run)


@jobs.handler("model-download")
def model_download(job: jobs.Job) -> dict:
    """Fetch model weights only with the consent the user gave in the UI (size and licence shown first)."""
    from .generate import downloads
    a = job.params
    c = downloads.consent(a["model"], a["accepted_gb"], a.get("licence_accepted", False), by="ui")
    return job.stage("download", lambda: downloads.fetch(a["model"], consent=c))


@jobs.handler("reference")
def reference_(job: jobs.Job) -> dict:
    """ReferenceStyleProfile from a lawfully supplied reference in the project (structure only, nexus/reference.py)."""
    import json
    from . import project as prj, reference
    a = job.params
    p = prj.Project(a["project"])
    m = next(x for x in p.data["media"] if x["id"] == a["media_id"])

    def run():
        an = _analysis(p).get(m["path"])
        if an is None:
            from .media import analyze
            an = analyze.analyze(m["path"])
        prof = reference.analyze(m["path"], an, name=a.get("name") or m["name"], rights={"basis": a["rights_basis"]})
        d = p.dir / "reports" / "references"
        d.mkdir(parents=True, exist_ok=True)
        rid = f"ref_{m['id']}"
        (d / f"{rid}.json").write_text(json.dumps(prof, ensure_ascii=False, default=str), encoding="utf-8")
        return {"reference_profile_id": rid, "metrics": prof.get("metrics")}

    return job.stage("reference-profile", run)


@jobs.handler("visual-install")
def visual_install(job: jobs.Job) -> dict:
    from .media import visual
    return job.stage("download", lambda: {"installed": visual.ensure_model(allow_download=True), **visual.info()})


# Account 2 tasks on Account 1's remote workers (render steps on CPU workers, Wan video on GPU workers): registered
# with Account 1's dispatcher when its package is installed; a no-op on a branch without it.
from .timeline import remote_render as _remote_render  # noqa: E402

_remote_render.register_account1_tasks()


@jobs.handler("asset-install")
def asset_install(job: jobs.Job) -> dict:
    """Download (resumable, SHA-256 pinned, licence-checked) one library asset."""
    from . import assetlib
    aid = job.params["asset_id"]

    def run():
        last = [0.0]

        def prog(have, total):
            if total and have / total - last[0] >= 0.1:
                last[0] = have / total
                job.progress(have / total)
                job.log(f"{aid}: {have}/{total} bytes")
        rec = assetlib.install(aid, prog)
        return {"asset_id": aid, "sha256": rec.get("sha256"), "size": rec.get("size")}

    return job.stage("download-verify", run)


@jobs.handler("archive")
def archive_(job: jobs.Job) -> dict:
    """Daily output: upload the final + metadata through Account 1 storage, verify, then release the local copy."""
    from . import archive
    a = job.params
    return job.stage("archive", lambda: archive.archive_output(a["project"], a["render"], kind=a.get("kind", "master"),
                                                                release_local=a.get("release_local")))


@jobs.handler("archive-retrieve")
def archive_retrieve(job: jobs.Job) -> dict:
    from . import archive
    return job.stage("retrieve", lambda: {"path": str(archive.retrieve(job.params["record"]))})


@jobs.handler("archive-recover")
def archive_recover(job: jobs.Job) -> dict:
    from . import archive
    return job.stage("recover", lambda: archive.recover())
