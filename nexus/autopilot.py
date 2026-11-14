"""Autopilot: one request -> finished videos. The simple UI (`/simple`, `nexus/ui/simple.html`) submits only this job.

Modes (all run as durable, resumable job stages on the existing engines - nothing here renders by itself):
  edit      a video the owner uploaded or linked (direct media file URL) ->
              goal "auto"    : the director edits it (profile + optional target length + instructions)
              goal "summary" : the director cuts it down to `target_seconds`
              goal "scenes"  : keep exactly the scene ranges the owner ticked in the scene picker
              goal "shorts"  : no master, only shorts
            -> master render with QA (unless goal "shorts") -> shorts (vertical, captions) -> platform packages
  idea      a topic sentence -> research -> script -> narration -> visuals -> video (nexus.create)
  script    the owner's own script text, used verbatim (one scene per paragraph)
  discover  pick a topic by itself: Wikipedia pageview trends in the chosen niche (emerging > rising > biggest ratio),
            then the idea pipeline
Outputs are listed in the job result as {"label", "path"} (paths relative to the project) for the UI to play/download.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import jobs

VERTICAL = ("youtube_shorts", "tiktok", "instagram_reels", "snapchat_spotlight")
LANDSCAPE = ("youtube", "facebook", "instagram_feed")
PROFILES = {"auto": "fast_social", "documentary": "cinematic_documentary", "explainer": "educational_explainer",
            "podcast": "podcast_edit", "vlog": "travel_vlog", "interview": "interview_storytelling", "social": "fast_social"}
MAX_URL_BYTES = 4 * 2 ** 30
PLATFORM_PAGE = re.compile(r"(^|\.)(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|facebook\.com|fb\.watch|x\.com|twitter\.com|"
                           r"vimeo\.com|dailymotion\.com|twitch\.tv|snapchat\.com|pornhub\.com|xvideos\.com|xnxx\.com|xhamster\.com|redtube\.com|"
                           r"youporn\.com|onlyfans\.com)$", re.I)


class AutopilotError(ValueError):
    pass


def _analysis(p) -> dict:
    out = {}
    for m in p.data["media"]:
        f = p.dir / "reports" / f"analysis_{m['id']}.json"
        if f.exists():
            out[m["path"]] = json.loads(f.read_text(encoding="utf-8"))
    return out


def _display(name: str) -> str:
    try:
        from . import platforms
        return platforms.get(name)["display"].split(" (")[0]
    except Exception:  # noqa: BLE001
        return name


def validate(params: dict) -> dict:
    """Normalise a request (also used by the API before submitting, so mistakes are reported immediately)."""
    mode = params.get("mode")
    if mode not in ("edit", "idea", "script", "discover"):
        raise AutopilotError("mode must be edit, idea, script or discover")
    plats = [x for x in (params.get("platforms") or []) if x in VERTICAL + LANDSCAPE]
    out = {"mode": mode, "platforms": plats, "lang": (params.get("lang") or "auto")[:8], "title": (params.get("title") or "").strip()[:120]}
    if mode == "edit":
        goal = params.get("goal") or "auto"
        if goal not in ("auto", "summary", "scenes", "shorts"):
            raise AutopilotError("goal must be auto, summary, scenes or shorts")
        out.update(goal=goal, style=params.get("style") if params.get("style") in PROFILES else "auto",
                   instructions=(params.get("instructions") or "").strip()[:2000], project=params.get("project"),
                   url=(params.get("url") or "").strip() or None,
                   target_seconds=float(params["target_seconds"]) if params.get("target_seconds") else None,
                   shorts=max(0, min(10, int(params.get("shorts") if params.get("shorts") is not None else (3 if goal == "shorts" else 0)))),
                   scenes=[[float(a), float(b)] for a, b in (params.get("scenes") or []) if float(b) > float(a)])
        out["dub"], out["subtitles"] = wanted_languages(params, out["instructions"])
        if goal == "shorts" and not out["shorts"]:
            out["shorts"] = 3
        if goal == "scenes" and not out["scenes"]:
            raise AutopilotError("choose at least one scene to keep")
        if not out["project"] and not out["url"]:
            raise AutopilotError("upload a video or paste a link to a video file")
        if out["url"]:
            from urllib.parse import urlparse
            host = (urlparse(out["url"]).hostname or "").lower()
            if PLATFORM_PAGE.search(host):
                raise AutopilotError("لينكات صفحات مواقع الفيديو (يوتيوب، تيك توك، إنستجرام، فيسبوك، والمواقع المشابهة) مش بتتحمل — "
                                     "شروطها بتمنع ده. ارفع ملف الفيديو اللي تملكه، أو حط لينك مباشر للملف (.mp4) أو ملف على Google Drive.")
    elif mode == "idea":
        out["idea"] = (params.get("idea") or "").strip()[:500]
        if len(out["idea"]) < 3:
            raise AutopilotError("write the idea of the video")
    elif mode == "script":
        out["script"] = (params.get("script") or "").strip()[:20000]
        if len(out["script"]) < 20:
            raise AutopilotError("paste the script (at least a few sentences)")
    else:
        out["niche"] = params.get("niche") or "science"
    if mode != "edit":
        out["vertical"] = bool(params.get("vertical"))
        out["scenes_n"] = max(3, min(12, int(params.get("scenes_n") or 6)))
    return out


LANGS = {"ar": ("عربي", "arabic|عرب"), "en": ("English", "english|انجليز|إنجليز|انكليز"), "fr": ("Français", "french|فرنس"),
         "de": ("Deutsch", "german|ألمان|الماني"), "es": ("Español", "spanish|اسبان|إسبان"), "tr": ("Türkçe", "turkish|ترك")}


def wanted_languages(params: dict, instructions: str = "") -> tuple[list[str], list[str]]:
    """Dub / subtitle languages from explicit fields, or understood from the owner's own words
    ("دبلجه عربي وترجمه" -> dub ar + subtitles ar). Without a named language, Arabic is assumed."""
    dub = [x for x in (params.get("dub") or []) if x in LANGS]
    subs = [x for x in (params.get("subtitles") or []) if x in LANGS]
    t = (instructions or "").lower()
    named = [k for k, (_n, pat) in LANGS.items() if re.search(pat, t)] or ["ar"]
    if not dub and re.search(r"دبلج|dub|voice ?over|تعليق صوتي", t):
        dub = named
    if not subs and re.search(r"ترجم|subtitl|translat|كابشن|caption", t):
        subs = named
    return dub[:3], [x for x in subs if x not in dub][:3]


def submit(params: dict) -> dict:
    return jobs.submit("autopilot", validate(params), params.get("project") or "")


def _lang(text: str, wanted: str) -> str:
    if wanted and wanted != "auto":
        return wanted
    return "ar" if re.search(r"[؀-ۿ]", text or "") else "en"


def script_from_text(text: str, title: str = "") -> dict:
    """The owner's text -> the approved-script shape create.create_video uses verbatim (one scene per paragraph)."""
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if p.strip()]
    if len(paras) == 1:  # one block: split into ~3-sentence scenes
        sents = re.split(r"(?<=[.!?؟。])\s+", paras[0])
        paras = [" ".join(sents[i:i + 3]) for i in range(0, len(sents), 3)]
    scenes = []
    for p in paras[:20]:
        first = re.split(r"(?<=[.!?؟])\s+", p)[0]
        words = re.findall(r"[^\W\d_]{4,}", p)
        scenes.append({"narration": p, "on_screen_text": first[:70], "visual_query": " ".join(words[:4]) or first[:40]})
    return {"title": title or (paras[0][:60] if paras else "Video"), "scenes": scenes}


def discover_topic(niche: str) -> dict:
    """Pick a topic by itself from real demand signals (Wikipedia pageviews, last 7 days vs previous 28)."""
    from .intelligence.market import niches, pipeline, sources
    wl = niches.watchlist([niche])
    if not wl:
        raise AutopilotError(f"unknown niche '{niche}'")
    rep = pipeline.trend_monitor(sources.Ctx(), wl)
    rows = [r for r in rep["topics"] if r.get("status") != "unknown"]
    order = {"emerging": 0, "rising": 1, "stable": 2, "falling": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -(r.get("ratio") or 0)))
    if not rows:
        pick = wl[0]
        return {"topic": pick["topic"], "why": "no trend data reachable today: first topic of the niche watchlist", "candidates": []}
    pick = rows[0]
    return {"topic": pick.get("title") or pick["topic"],
            "why": f"{pick['status']} on Wikipedia: last-7-day views {pick.get('ratio')}x the previous 28 days",
            "candidates": [{"topic": r.get("title") or r["topic"], "status": r["status"], "ratio": r.get("ratio")} for r in rows[:8]]}


@jobs.handler("autopilot")
def autopilot(job: jobs.Job) -> dict:
    a = job.params
    if a["mode"] == "edit":
        return _edit(job, a)
    return _create(job, a)


# ---------------------------------------------------------------- edit an existing video
def _edit(job: jobs.Job, a: dict) -> dict:
    from . import project as prj
    if "dub" not in a:  # submitted before languages were a field: understand them from the instructions
        a = {**a}
        a["dub"], a["subtitles"] = wanted_languages(a, a.get("instructions", ""))

    def importing():
        if a.get("project"):
            p = prj.Project(a["project"])
            if not p.data["media"]:
                raise AutopilotError("the project has no video")
            return {"project": p.id, "media": p.data["media"][0]["id"]}
        from .cloud import ingest
        from .costguard import PAID_HOSTS
        from urllib.parse import urlparse
        if (urlparse(a["url"]).hostname or "").lower() in PAID_HOSTS:
            raise AutopilotError("refused: paid endpoint")
        # An owner-supplied direct media file: a plain GET of a file (no API, no account, nothing billable), so it is not
        # limited to the costguard host list (D-26); SSRF rules, size cap and type sniffing still apply.
        from . import config
        tmp = config.HOME / "tmp" / "autopilot" / f"{job.id}.bin"
        r = ingest.fetch(a["url"], tmp, MAX_URL_BYTES)
        kind = ingest.sniff(tmp)
        if not kind.startswith(("video", "audio")):
            tmp.unlink(missing_ok=True)
            raise AutopilotError(f"the link is not a video or audio file (it is {kind})")
        name = Path(urlparse(r.final_url).path).name or "video.mp4"
        if "." not in name:
            name += ".mp4"
        final = tmp.with_name(name)
        tmp.replace(final)
        p = prj.Project.create(a.get("title") or Path(name).stem, "edit")
        m = p.import_media(final, move=True)
        return {"project": p.id, "media": m["id"], "bytes": r.size}

    imp = job.stage("import", importing)
    job.set_project(imp["project"])
    p = prj.Project(imp["project"])
    m = next(x for x in p.data["media"] if x["id"] == imp["media"])

    def analysing():
        f = p.dir / "reports" / f"analysis_{m['id']}.json"
        if not f.exists():
            from .media import analyze
            an = analyze.analyze(m["path"])
            f.write_text(json.dumps(an, ensure_ascii=False), encoding="utf-8")
        an = json.loads(f.read_text(encoding="utf-8"))
        return {"duration": an["probe"]["duration"], "scenes": len(an["scenes"]),
                "speech": bool((an.get("transcript") or {}).get("segments"))}

    info = job.stage("analyze", analysing)
    outputs: list[dict] = []
    an_all = _analysis(p)
    an = an_all[m["path"]]
    title = a.get("title") or p.name

    if a["goal"] != "shorts":
        def editing():
            from . import director, edit
            if a["goal"] == "scenes":
                prop = edit.propose(an, "tighten")
                prop["keep"] = [{"in": s, "out": e, "score": 1.0, "segs": []} for s, e in sorted(a["scenes"])]
                tl = edit.build_timeline(m["path"], an, prop)
                vid = p.save_timeline(tl, f"autopilot: {len(a['scenes'])} chosen scenes")
                return {"version": vid, "how": f"خلّيت الـ {len(a['scenes'])} مشاهد اللي اخترتها بالترتيب", "seconds": sum(e - s for s, e in a["scenes"])}
            target = a.get("target_seconds")
            if a["goal"] == "summary" and not target:
                target = max(30.0, min(600.0, info["duration"] * 0.3))
            if info["speech"]:
                brief = {"profile": PROFILES[a.get("style") or "auto"], "title": title,
                         **({"target_seconds": target} if target else {}), **({"instructions": a["instructions"]} if a.get("instructions") else {})}
                try:
                    out = director.direct([m["path"]], an_all, brief)
                    vid = p.save_timeline(out["master"], f"autopilot: director {brief['profile']}")
                    s = out["plan"]["summary"]
                    return {"version": vid, "how": f"المخرج الذكي ({brief['profile']}): شال الكلام الضعيف والسكتات والتكرار ورتّب الأقوى", "seconds": s.get("programme_seconds"), "notes": out["notes"][:8]}
                except Exception as e:  # noqa: BLE001 - fall back to the simpler speech/scene editor, and say so
                    fallback = f"المخرج الذكي ماقدرش ({type(e).__name__}: {str(e)[:160]})؛ استخدمت المونتاج الأساسي (الكلام والمشاهد)"
            else:
                fallback = "الفيديو مفيهوش كلام: اتعدل حسب المشاهد والجودة"
            prop = edit.propose(an, "highlights" if target else "tighten", target)
            tl = edit.build_timeline(m["path"], an, prop)
            vid = p.save_timeline(tl, "autopilot: basic edit")
            return {"version": vid, "how": fallback, "seconds": prop["proposed_duration"]}

        ed = job.stage("edit", editing)

        def rendering():
            from . import qa
            stats, rep = qa.render_with_qa(p, p.timeline("main"), f"autopilot_{int(time.time())}")
            return {"output": str(Path(stats["output"]).relative_to(p.dir)), "status": rep["status"], "issues": rep["known_issues"][:8],
                    "seconds": stats.get("duration")}

        ren = job.stage("render", rendering)
        outputs.append({"label": "الفيديو بعد التعديل", "path": ren["output"], "kind": "master"})
    else:
        ed, ren = {"how": "شورتس بس"}, None

    vertical = [x for x in a["platforms"] if x in VERTICAL] or (["youtube_shorts"] if a["shorts"] else [])
    if a["shorts"]:
        def shorting():
            from . import qa, shorts
            items = shorts.factory(m["path"], an, n=a["shorts"], platforms=tuple(vertical[:1]), captions="classic", aspect="9:16")
            rows = shorts.save_batch(p, items)
            done = []
            for row, it in zip(rows, items):
                stats, _rep = qa.render_with_qa(p, p.timeline(row["branch"]), row["branch"], max_rounds=1)
                done.append({"path": str(Path(stats["output"]).relative_to(p.dir)), "title": it.get("title") or "",
                             "why": it.get("why"), "seconds": round(it["moment"]["end"] - it["moment"]["start"], 1)})
            return {"shorts": done, "notes": list(getattr(items, "notes", []))[:5]}

        sh = job.stage("shorts", shorting)
        for i, s in enumerate(sh["shorts"], 1):
            t = s["title"] if s.get("title") and not str(s["title"]).startswith("short-") else ""
            outputs.append({"label": (f"شورت {i}: {t}" if t else f"شورت {i}")[:90], "path": s["path"], "kind": "short", "why": s.get("why")})

    if ren:
        outputs += _languages(job, p, an_all, a, title, (an.get("transcript") or {}).get("language", "en"))

    land = [x for x in a["platforms"] if x in LANDSCAPE]
    if land and ren:
        def packaging():
            from . import publish
            return publish.deliver(p, p.timeline("main"), land, title, "", (an.get("transcript") or {}).get("language", "en"),
                                   analysis_by_src=an_all)

        pk = job.stage("platforms", packaging)
        for name, meta in pk["packages"].items():
            outputs.append({"label": f"نسخة {_display(name)}", "path": meta["video"], "kind": "platform", "platform": name,
                            "cover": meta.get("cover"), "ok": meta.get("ok")})
    return {"project": p.id, "mode": "edit", "goal": a["goal"], "edit": ed, "render": ren, "outputs": outputs}


def _languages(job: jobs.Job, p, an_all: dict, a: dict, title: str, src_lang: str) -> list[dict]:
    """Dubbed and/or subtitled versions of the master (nexus.localize: MT + review + TTS), each rendered with QA."""
    from . import localize, qa
    outs = []
    for lang, dub in [(x, True) for x in a.get("dub") or []] + [(x, False) for x in a.get("subtitles") or []]:
        if lang == src_lang and not dub:
            continue
        branch = f"{lang}_{'dub' if dub else 'subs'}"

        def run(lang=lang, dub=dub, branch=branch):
            ltl, review = localize.localize(p, p.timeline("main"), lang, an_all or None, context=title, dub=dub, source_lang=src_lang,
                                            allow_noncommercial=True, mode="replace")
            p.save_timeline(ltl, f"autopilot: {lang} {'dub' if dub else 'subtitles'}", branch=branch)
            stats, rep = qa.render_with_qa(p, p.timeline(branch), f"main_{branch}", max_rounds=1)
            warn = [str(w) for w in (review.get("warnings") or [])][:3] if isinstance(review, dict) else []
            return {"output": str(Path(stats["output"]).relative_to(p.dir)), "status": rep["status"], "warnings": warn}

        r = job.stage(f"{'dub' if dub else 'subs'}-{lang}", run)
        name = LANGS.get(lang, (lang,))[0]
        label = f"نسخة مدبلجة {name}" if dub else f"نسخة مترجمة {name}"
        if dub and lang == "ar":
            label += " (صوت Kareem — ترخيصه التجاري غير مؤكد: راجعه قبل نشر فيديو بيكسب)"
        outs.append({"label": label, "path": r["output"], "kind": "language", "lang": lang})
    return outs


# ---------------------------------------------------------------- make a new video
def _create(job: jobs.Job, a: dict) -> dict:
    from . import create, project as prj
    picked = None
    if a["mode"] == "discover":
        picked = job.stage("discover", lambda: discover_topic(a.get("niche") or "science"))
        topic = picked["topic"]
    elif a["mode"] == "idea":
        topic = a["idea"]
    else:
        topic = a.get("title") or a["script"][:80]
    script = script_from_text(a["script"], a.get("title")) if a["mode"] == "script" else None
    lang = _lang(a.get("idea") or a.get("script") or "", a.get("lang"))
    size = (720, 1280) if a.get("vertical") else (1280, 720)
    out = create.create_video(topic, scenes=a.get("scenes_n", 6), lang=lang, size=size, stage=job.stage, on_project=job.set_project,
                              script=script)
    p = prj.Project(out["project"])
    outputs = [{"label": out["script"].get("title") or topic, "path": str(Path(out["output"]).relative_to(p.dir))
                if Path(out["output"]).is_absolute() else out["output"], "kind": "master"}]
    if a["platforms"]:
        def packaging():
            from . import publish
            return publish.deliver(p, p.timeline("main"), a["platforms"], out["script"].get("title") or topic, "", lang)

        pk = job.stage("platforms", packaging)
        for name, meta in pk["packages"].items():
            outputs.append({"label": f"نسخة {_display(name)}", "path": meta["video"], "kind": "platform", "platform": name, "cover": meta.get("cover"),
                            "ok": meta.get("ok")})
    return {"project": p.id, "mode": a["mode"], "topic": topic, "picked": picked, "status": out["report"]["status"],
            "issues": out["report"]["known_issues"][:8], "outputs": outputs}
