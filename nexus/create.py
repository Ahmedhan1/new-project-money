"""Create-from-zero: idea -> research -> script -> review -> storyboard -> visuals -> narration -> timeline."""
from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image, ImageDraw

from . import assets, llm, net, project as prj, qa
from .audio import music
from .speech import tts
from .timeline import ops, subs, text as ttext
from .timeline.render import clip_duration, clip_starts

STOP = set("the a an and or but of to in on for with is are was were be been it this that as at by from we you they have has had not "
           "can will its their our your more most very also into than then about over when which who how what why".split())


# ------------------------------------------------------------------ research
def rank_hits(topic: str, hits: list[dict]) -> list[str]:
    """Order Wikipedia search hits by how much of the topic they cover, so the script is grounded in the page about
    the topic rather than a neighbouring one (K-4). Topic words that few hits mention are the distinctive ones and
    weigh more; a title match counts double. Ties keep the search engine's order."""
    import math
    stem = lambda w: w[:5]
    want = {stem(w) for w in _content(topic)} or {stem(w) for w in re.findall(r"\w{3,}", topic.lower())}
    parsed = []
    for h in hits:
        title = {stem(w) for w in re.findall(r"[a-zA-Z]{3,}", h.get("title", "").lower())}
        snip = {stem(w) for w in re.findall(r"[a-zA-Z]{4,}", re.sub(r"<[^>]+>", "", h.get("snippet", "")).lower())}
        parsed.append((h["title"], title, snip))
    df = {w: sum(w in t or w in sn for _, t, sn in parsed) for w in want}
    idf = {w: math.log(1 + len(parsed) / (1 + df[w])) for w in want}
    total = sum(idf.values()) or 1.0
    scored = []
    for i, (name, title, snip) in enumerate(parsed):
        cover = sum(idf[w] * (2 if w in title else 1 if w in snip else 0) for w in want) / (2 * total)
        extra = len(title - want) / max(len(title), 1)  # a title mostly about something else is penalised a little
        scored.append((-(cover - 0.1 * extra), i, name))
    return [t for _, _, t in sorted(scored)]


def research(topic: str, lang: str = "en", max_pages: int = 2, notes: str | None = None) -> dict:
    """Collect source text from Wikipedia (with URLs for the source list). Falls back to user notes when offline."""
    sources, corpus = [], []
    if notes:
        sources.append({"title": "User-supplied notes", "url": None, "license": "user"})
        corpus.append(notes)
    try:
        host = f"https://{lang}.wikipedia.org/w/api.php"
        hits = net.get_json(host, {"action": "query", "list": "search", "srsearch": topic, "format": "json", "srlimit": 8}, max_wait=45)
        titles = rank_hits(topic, hits.get("query", {}).get("search", []))[:max_pages]
        if titles:
            pages = net.get_json(host, {"action": "query", "prop": "extracts|info", "exintro": 1, "explaintext": 1, "inprop": "url",
                                        "titles": "|".join(titles), "format": "json", "redirects": 1}, max_wait=45)
            for pg in pages.get("query", {}).get("pages", {}).values():
                ex = (pg.get("extract") or "").strip()
                if len(ex) > 200:
                    corpus.append(ex[:3500])
                    sources.append({"title": pg["title"], "url": pg.get("fullurl"), "license": "CC BY-SA 4.0 (Wikipedia) - used for facts, not copied"})
    except (net.RemoteUnavailable, Exception) as e:  # research source down: say so, continue with what we have
        sources.append({"title": f"Wikipedia unavailable ({type(e).__name__})", "url": None, "license": None})
    return {"topic": topic, "sources": sources, "text": "\n\n".join(corpus), "grounded": bool(corpus)}


# ------------------------------------------------------------------ script
def _script_schema(scenes: int) -> dict:
    """Grammar for the script: the local model cannot return an empty object, the wrong number of scenes,
    or a one-sentence scene - those were the failure modes seen in acceptance test D."""
    return {"type": "object", "required": ["title", "scenes"], "properties": {
        "title": {"type": "string"},
        "scenes": {"type": "array", "minItems": scenes, "maxItems": scenes, "items": {
            "type": "object", "required": ["narration", "on_screen_text", "visual_query"], "properties": {
                "narration": {"type": "array", "minItems": 2, "maxItems": 3, "items": {"type": "string"}},
                "on_screen_text": {"type": "string"}, "visual_query": {"type": "string"}}}}}}


def write_script(topic: str, res: dict, scenes: int = 5, audience: str = "a curious general audience", style: str = "clear, vivid explainer",
                 series_context: str = "", feedback: str = "") -> dict:
    facts = res["text"][:5000] or "(no research text available - write only widely known, uncontroversial facts)"
    r = llm.chat_json(
        f"Write the narration script for a short video.\nTopic: {topic}\nAudience: {audience}\nStyle: {style}\n{series_context}\n"
        f"Use ONLY facts from these research notes. Do not invent numbers, dates or names.\n--- NOTES ---\n{facts}\n--- END NOTES ---\n\n"
        # Wording here was A/B-tested against Qwen3-4B: arrows ("->") and "e.g." inside the JSON example made it
        # answer with an empty object. Keep it plain; `require` retries if the model still returns nothing.
        f"Write exactly {scenes} scenes that build one story: a hook, then how it works, then why it matters, then a takeaway. "
        "Scene 1 must open with a hook (a surprising fact or question). The last scene must give a takeaway. "
        "Each scene explains ONE new idea and must not repeat an earlier scene.\n"
        "Every narration is 2 or 3 full spoken sentences, 22 to 40 words in total, concrete and specific. "
        "Visuals must show the real subject of the scene, never a metaphor.\n"
        + (f"Your previous draft was rejected. Fix exactly these problems:\n{feedback}\n" if feedback else "") +
        'Return JSON: {"title": "max 7 words", "scenes": [{"narration": ["first spoken sentence", "second spoken sentence"], '
        '"on_screen_text": "max 5 words", "visual_query": "2-3 word photo search phrase, concrete nouns"}]}',
        require="scenes", schema=_script_schema(scenes),
        max_tokens=1700, temperature=0.6)
    for s in r.get("scenes", []):  # narration arrives as a list of sentences (the schema forces 2-3 of them)
        if isinstance(s.get("narration"), list):
            s["narration"] = " ".join(x.strip() if x.strip()[-1:] in ".!?" else x.strip() + "." for x in map(str, s["narration"]) if x.strip())
    sc = [{"narration": re.sub(r"\s+", " ", str(s.get("narration", ""))).strip(), "on_screen_text": str(s.get("on_screen_text", ""))[:48],
           "visual_query": str(s.get("visual_query", topic))[:60]} for s in r.get("scenes", []) if s.get("narration")]
    return {"title": str(r.get("title", topic))[:70], "topic": topic, "scenes": sc}


def _content(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zA-Z]{4,}", text.lower()) if w not in STOP]


def review_script(script: dict, res: dict, want_scenes: int) -> dict:
    """Stage 1 pre-production review. Grounding is checked by string evidence, not by the model that wrote the script."""
    checks = []
    sc = script["scenes"]
    checks.append(qa._c("structure", len(sc) == want_scenes, f"{len(sc)} scenes (wanted {want_scenes})", "major", fix="rewrite"))
    words = sum(len(s["narration"].split()) for s in sc)
    checks.append(qa._c("length", 8 * len(sc) <= words <= 60 * len(sc), f"{words} words, ~{words / 2.5:.0f}s of narration", "major", fix="rewrite"))
    thin = [(i + 1, len(s["narration"].split())) for i, s in enumerate(sc)
            if len(s["narration"].split()) < 14 or len(re.findall(r"[.!?](?:\s|$)", s["narration"])) < 2]
    checks.append(qa._c("scene depth", not thin, "every scene has at least 2 sentences / 14 words" if not thin
                        else f"scenes too thin to carry a shot (scene, words): {thin} - each needs 2-3 sentences, 22-40 words", "major", fix="rewrite"))
    from .intelligence import strategy as _strategy  # factual framing: no sensational claim the research does not support
    fr = _strategy.framing({"title": script.get("title", ""), "hook": "", "scenes": sc}, {"claims": [{"text": res.get("text", "")}]})
    checks.append(qa._c("framing supported by research", fr["ok"], "no sensational framing" if fr["ok"] else
                        f"unsupported framing {fr['unsupported_framing']} - state what the sources say, as a question if needed", "major", fix="rewrite"))
    hook = sc[0]["narration"] if sc else ""
    checks.append(qa._c("hook", "?" in hook or bool(re.search(r"\d|never|every|most|only|imagine|did you", hook, re.I)),
                        f"opening: \"{hook[:70]}\"", "minor"))
    src = res["text"].lower()
    stems = {w[:6] for w in _content(src)}
    if res["grounded"]:
        weak, bad_nums = [], []
        for i, s in enumerate(sc):
            cw = _content(s["narration"])
            cov = sum(w[:6] in stems for w in cw) / max(len(cw), 1)
            if cov < 0.5:
                weak.append((i + 1, f"{cov:.0%}"))
            for n in re.findall(r"\d[\d,.]*", s["narration"]):
                if n.rstrip(".,").replace(",", "") not in src.replace(",", ""):
                    bad_nums.append((i + 1, n))
        checks.append(qa._c("numbers traceable to sources", not bad_nums, "every figure appears in the research text" if not bad_nums
                            else f"figures not found in sources (scene, value): {bad_nums}", "major", fix="rewrite-scene"))
        checks.append(qa._c("claims grounded in sources", not weak, "narration vocabulary is covered by the research text" if not weak
                            else f"scenes with low source coverage: {weak}", "major", fix="rewrite-scene"))
    else:
        checks.append(qa._c("fact-check", False, "no research text was available - facts are UNVERIFIED", "major"))
    dup = len({s["narration"][:40] for s in sc}) < len(sc)
    checks.append(qa._c("no repeated scenes", not dup, "all scenes distinct"))
    checks.append(qa._c("scene feasibility", all(s["visual_query"] for s in sc), "every scene has a concrete visual to source"))
    return {"stage": "1-pre-production", "checks": checks, "passed": all(c["ok"] or c["severity"] == "minor" for c in checks),
            "sources": res["sources"]}


def script_with_review(topic: str, res: dict, scenes: int = 5, rounds: int = 3, **kw) -> tuple[dict, dict]:
    """Write -> review -> rewrite until the gates pass or we run out of rounds (then the issues stay in the report)."""
    best, feedback = None, ""
    for _ in range(rounds):
        script = write_script(topic, res, scenes, feedback=feedback, **kw)
        review = review_script(script, res, scenes)
        failed = [c for c in review["checks"] if not c["ok"] and c["severity"] != "minor"]
        fails = len(failed)
        if best is None or fails < best[2]:
            best = (script, review, fails)
        if fails == 0:
            break
        # the reviewer's findings go back to the writer - a blind retry tends to repeat the same mistake
        feedback = "\n".join(f"- {c['check']}: {c['detail']}" for c in failed)
    best[1]["attempts"] = _ + 1
    return best[0], best[1]


# ------------------------------------------------------------------ visuals
def card(text_: str, sub: str, W: int, H: int, out: Path, hue: int = 0) -> Path:
    """Generated fallback visual: gradient background + headline (used when no licensed image is found)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    palettes = [((12, 24, 52), (30, 96, 150)), ((40, 14, 54), (150, 50, 110)), ((10, 44, 40), (30, 140, 110)), ((52, 30, 10), (170, 110, 40))]
    a, b = palettes[hue % len(palettes)]
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3)))
    for k in range(6):  # soft decorative circles
        r = int(H * (0.18 + 0.07 * k))
        cx, cy = int(W * (0.12 + 0.17 * k)), int(H * (0.2 + 0.12 * ((k * 3) % 5)))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=tuple(min(255, c + 26) for c in b), width=3)
    rgba = img.convert("RGBA")
    ttext.draw_text_block(rgba, text_, "title")
    if sub:
        ttext.draw_text_block(rgba, sub, "subtitle")
    rgba.convert("RGB").save(out, quality=92)
    return out


def storyboard_sheet(frames: list[tuple[Path, str]], out: Path, cols: int = 3) -> Path:
    tw, th = 480, 270
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * 12, rows * (th + 58) + 12), (18, 20, 28))
    d = ImageDraw.Draw(sheet)
    f = ttext.font(17)
    for i, (path, cap) in enumerate(frames):
        x, y = 12 + (i % cols) * (tw + 12), 12 + (i // cols) * (th + 58)
        with Image.open(path) as im:
            im = im.convert("RGB")
            s = max(tw / im.width, th / im.height)
            im = im.resize((int(im.width * s) + 1, int(im.height * s) + 1))
            sheet.paste(im.crop(((im.width - tw) // 2, (im.height - th) // 2, (im.width + tw) // 2, (im.height + th) // 2)), (x, y))
        for li, line in enumerate(ttext.wrap(d, f"{i + 1}. {cap}", f, tw)[:2]):
            d.text((x, y + th + 6 + li * 22), line, font=f, fill=(225, 228, 235))
    sheet.save(out)
    return out


# ------------------------------------------------------------------ production
def produce(project, script: dict, *, lang: str = "en", size=(1280, 720), style: dict | None = None, use_stock: bool = True,
            extra_clips: dict[int, str] | None = None) -> dict:
    """Script -> timeline. `style` (grade, mood, music_seed, voice) is stored on the project for later continuation.
    `extra_clips` maps scene index -> path of a pre-rendered animation to use instead of a still."""
    style = {"grade": "cinematic", "mood": "inspiring", "music_seed": 7, "voice": None, **(style or {})}
    W, H = size
    tl = prj.new_timeline(W, H, lang)
    tl["grade"] = "none"
    gen, used, frames = project.data.setdefault("generated", {}), set(), []
    tl["clips"].append(ops.color_clip(2.4, text=[{"text": script["title"], "style": "title"}], fade_in=0.5))
    narr = []
    for i, s in enumerate(script["scenes"]):
        wav = project.dir / "audio" / lang / f"scene{i:02d}.wav"
        v = tts.synthesize(s["narration"], wav, lang, style["voice"])
        dur = round(v["duration"] + 0.7, 3)
        overlay = [{"text": s["on_screen_text"], "style": "lower_third", "start": 0.4, "end": min(dur - 0.3, 5.0)}] if s.get("on_screen_text") else []
        if extra_clips and i in extra_clips:
            from .media import ff
            ad = ff.duration(extra_clips[i])
            clip = ops.video_clip(extra_clips[i], 0, min(ad, dur), mute=True, fit="contain", speed=round(min(1.0, min(ad, dur) / dur), 3))
            gen[extra_clips[i]] = {"license": "generated by NEXUS (original)", "source": "animation studio"}
            frames.append((None, s["on_screen_text"] or s["narration"][:40]))
        else:
            from .intelligence.quality import scene_plan
            rejected: list = []
            ctx = scene_plan.context_for(script, s)
            row = (assets.find_image(s["visual_query"], project.id, used, fallbacks=(s.get("on_screen_text", ""), script.get("topic", "")),
                                     validate=lambda c, ctx=ctx: scene_plan.validate_asset(c, ctx), rejected=rejected)
                   if use_stock and s.get("visual_query") else None)  # an empty query means "title card on purpose" (recap, cliffhanger)
            plan = {"scene": i, "query": s.get("visual_query"), "rejected": rejected}
            if row:
                used.add(row["id"])
                img, prov = row["path"], {"license": row["license"], "source": row["source"], "attribution": row["attribution"],
                                          "query": s["visual_query"], "matched_query": row.get("matched_query")}
                plan.update(kind="licensed_photo", source=row.get("url"), title=row.get("title"))
                clip = ops.image_clip(img, dur, kenburns="in" if i % 2 == 0 else "out", grade=style["grade"], provenance=prov, text=overlay)
            else:
                alt = scene_plan.alternative_visual(s, project.dir / "work", i, (W, H), lang, dur)
                if alt:  # an intentional explanatory visual (fact graphic), never a silent title card
                    img, prov = alt["path"], alt["provenance"]
                    plan.update(kind=alt["kind"], source="NEXUS motion graphic", reason=alt["reason"])
                    clip = ops.video_clip(img, 0, dur, mute=True, fit="contain", provenance=prov, text=[])
                else:
                    img = str(card(s["on_screen_text"] or script["title"], "", W, H, project.dir / "work" / f"card{i:02d}.jpg", i))
                    prov = {"license": "generated by NEXUS (original)", "source": "title-card generator"}
                    overlay = []
                    plan.update(kind="title_card_fallback", reason="no licensed image passed the context gate and no fact graphic fits - "
                                "needs another visual source or explicit approval")
                    clip = ops.image_clip(img, dur, kenburns="in" if i % 2 == 0 else "out", grade=style["grade"], provenance=prov, text=overlay)
            project.data.setdefault("visual_plan", [])
            project.data["visual_plan"] = [x for x in project.data["visual_plan"] if x.get("scene") != i] + [plan]
            gen[img] = prov
            frames.append((Path(img) if plan["kind"] != "fact_graphic" else None, s["on_screen_text"] or s["narration"][:40]))
        clip.update(narration=s["narration"], transition="crossfade", transition_dur=0.5, scene=i)
        if extra_clips and i in extra_clips:
            clip["text"] = overlay
        tl["clips"].append(clip)
        narr.append(wav)
    creds = assets.credits(project.id)
    if creds:  # attribution is part of the video, not an afterthought
        tl["clips"].append(ops.color_clip(3.0, text=[{"text": "Image credits: " + " • ".join(c.split(". Source")[0] for c in creds)[:420],
                                                       "style": "subtitle"}], transition="crossfade", transition_dur=0.5, fade_out=0.6))
    else:
        tl["clips"][-1]["fade_out"] = 0.6
    starts = clip_starts(tl)
    cues = []
    for (i, wav) in enumerate(narr):
        from .media import ff
        st = starts[i + 1] + 0.45
        tl["audio"].append({"src": str(wav), "start": round(st, 3), "clip": tl["clips"][i + 1]["id"]})
        cues += subs.cues_from_text(script["scenes"][i]["narration"], st, st + ff.duration(wav))
    tl["subtitles"] = subs.fix_timing(cues)
    total = starts[-1] + clip_duration(tl["clips"][-1])
    tl["music"] = {"src": str(music.music_bed(total, style["mood"], style["music_seed"])), "volume": 0.3, "duck": True}
    project.data["style"] = {**style, "size": list(size), "lang": lang}
    project.data["script"] = script
    project.data["credits"] = creds
    still = [(p, c) for p, c in frames if p is not None]
    if still:
        project.data["storyboard"] = str(storyboard_sheet(still, project.dir / "reports" / "storyboard.png"))
    (project.dir / "reports" / "script.json").write_text(json.dumps(script, indent=1, ensure_ascii=False), encoding="utf-8")
    project.save()
    if style.get("preset"):
        from . import presets
        tl = presets.apply(tl, style)
    return tl


def _timed_stage(name: str, fn):
    """Default stage runner (no job ledger): just measure, so `nexus profile` has real numbers."""
    import time
    from . import db
    t0 = time.time()
    out = fn()
    db.metric(f"stage_seconds.create-from-idea.{name}", round(time.time() - t0, 3))
    return json.loads(json.dumps(out, ensure_ascii=False, default=str))


def create_video(topic: str, *, scenes: int = 5, lang: str = "en", size=(1280, 720), notes: str | None = None, style: dict | None = None,
                 name: str | None = None, stage=None, on_project=None, preset: str | None = None) -> dict:
    """The whole from-zero workflow. Returns paths + the approval report.

    Runs as named stages. When called from a durable job, `stage` is `Job.stage`: each finished stage is
    checkpointed, so a crashed/restarted worker resumes at the first unfinished stage instead of redoing
    research, script-writing or narration."""
    if preset:  # a named production preset supplies look, sound, pacing and (for vertical presets) the frame size
        from . import presets
        style = presets.get(preset, **(style or {}))
        size = tuple(style.get("size") or size)
    stage = stage or _timed_stage
    pid = stage("project", lambda: prj.Project.create(name or topic[:60], "create").id)
    if on_project:
        on_project(pid)
    res = stage("research", lambda: research(topic, notes=notes))

    def _script():
        kw = {"style": style["script_style"]} if style and style.get("script_style") else {}
        script, pre = script_with_review(topic, res, scenes, **kw)
        return {"script": script, "pre": pre}

    sp = stage("script", _script)
    script, pre = sp["script"], sp["pre"]

    def _assemble():
        project = prj.Project(pid)
        tl = produce(project, script, lang=lang, size=tuple(size), style=style)
        return {"timeline": project.save_timeline(tl, "first assembly")}

    vid = stage("assemble", _assemble)["timeline"]

    def _render():
        project = prj.Project(pid)
        from .intelligence.quality import scene_plan
        tl_ = project.timeline(vid=vid)
        stages = [pre, scene_plan.visual_stage(project.data.get("visual_plan", []), project.data.get("approved_fallback", False)),
                  scene_plan.typography_stage(tl_, lang)]
        stats, report = qa.render_with_qa(project, tl_, "master", extra_stages=stages,
                                          extra={"sources": res["sources"], "credits": project.data.get("credits", [])})
        return {"stats": stats, "report": report}

    rr = stage("render-qa", _render)
    project = prj.Project(pid)
    return {"project": pid, "output": rr["stats"]["output"], "script": script, "research": res["sources"], "render": rr["stats"],
            "report": rr["report"], "storyboard": project.data.get("storyboard")}
