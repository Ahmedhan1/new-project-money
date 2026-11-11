"""Film & series department: series bible, season plan, continuity-aware episodes, short-film plans."""
from __future__ import annotations

import re

from . import create, db, llm, project as prj, qa

SYSTEM = "You are a showrunner and screenwriter. You follow the requested output format exactly, with no extra commentary."


def _fields(raw: str) -> dict[str, list[str]]:
    """Parse 'KEY: value' lines (keys may repeat). Plain text is far more reliable than JSON on a small local model."""
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"^\s*\**([A-Z][A-Z_ ]{2,20}?)\**\s*\d*\s*:\s*(.+)$", raw, re.M):
        out.setdefault(m.group(1).strip().upper().replace(" ", "_"), []).append(m.group(2).strip(" *"))
    return out


def _parts(line: str, n: int) -> list[str]:
    p = [x.strip() for x in line.split("|")]
    return (p + [""] * n)[:n]


# ------------------------------------------------------------------ bible
def create_series(concept: str, episodes: int = 3) -> dict:
    raw = llm.chat(
        f"Create a series bible for this concept: {concept}\n\nAnswer in exactly this format:\n"
        "TITLE: <series title>\nGENRE: <genre>\nLOGLINE: <one sentence>\n"
        "RULE: <a rule of this world>\nRULE: <second rule>\nRULE: <third rule>\n"
        "CHARACTER: <name> | <role> | <appearance in 8 words> | <defining trait> | <what they want>\n"
        "CHARACTER: ... (exactly 3 characters with distinct single-word names)\n"
        "LOCATION: <name> | <look in 8 words>\nLOCATION: <name> | <look>\n"
        "VISUAL: <visual identity: palette, lighting, camera style>\nAUDIO: <music mood in 4 words>",
        SYSTEM, max_tokens=520, temperature=0.7)
    f = _fields(raw)
    chars = [dict(zip(("name", "role", "appearance", "trait", "goal"), _parts(c, 5))) for c in f.get("CHARACTER", [])][:4]
    if len(chars) < 2 or not f.get("TITLE"):
        raise ValueError(f"series bible generation failed: {raw[:300]}")
    mood = (f.get("AUDIO") or ["calm"])[0].lower()
    bible = {"title": f["TITLE"][0], "genre": (f.get("GENRE") or [""])[0], "logline": (f.get("LOGLINE") or [concept])[0],
             "world_rules": f.get("RULE", []), "characters": chars,
             "locations": [dict(zip(("name", "look"), _parts(x, 2))) for x in f.get("LOCATION", [])],
             "visual_identity": (f.get("VISUAL") or [""])[0], "audio_identity": mood,
             # production identity reused by every episode -> consistent look and sound
             "style": {"grade": "cinematic", "mood": next((m for m in ("tense", "inspiring", "documentary", "upbeat", "calm") if m in mood), "tense"),
                       "music_seed": abs(hash(f["TITLE"][0])) % 1000, "voice": "en_US-joe-medium"},
             "episode_structure": ["recap (from episode 2)", "opening title", "3-4 story scenes", "cliffhanger", "end card"],
             "canon": [], "episodes": [], "concept": concept}
    bible["season"] = plan_season(bible, episodes)
    db.put_doc("series", bible)
    return bible


def _bible_text(b: dict) -> str:
    chars = "\n".join(f"- {c['name']}: {c['role']}; {c['appearance']}; {c['trait']}; wants {c['goal']}" for c in b["characters"])
    locs = "\n".join(f"- {l['name']}: {l['look']}" for l in b["locations"])
    canon = "\n".join(f"- (ep {e['episode']}) {e['event']}" for e in b["canon"]) or "- none yet"
    return (f"SERIES: {b['title']} ({b['genre']})\nLOGLINE: {b['logline']}\nWORLD RULES:\n" + "\n".join(f"- {r}" for r in b["world_rules"])
            + f"\nCHARACTERS:\n{chars}\nLOCATIONS:\n{locs}\nCANONICAL EVENTS SO FAR:\n{canon}")


def plan_season(b: dict, n: int) -> list[dict]:
    raw = llm.chat(f"{_bible_text(b)}\n\nPlan a season of {n} episodes with one continuous story arc. Each episode ends on a cliffhanger that the "
                   f"next episode resolves.\nFormat, one line per episode:\nEPISODE: <title> | <2-sentence summary> | <cliffhanger>",
                   SYSTEM, max_tokens=90 * n + 60, temperature=0.7)
    eps = [dict(zip(("title", "summary", "cliffhanger"), _parts(x, 3))) for x in _fields(raw).get("EPISODE", [])][:n]
    return [{"number": i + 1, **e} for i, e in enumerate(eps)]


# ------------------------------------------------------------------ episodes
def write_episode(b: dict, number: int, scenes: int = 4) -> dict:
    outline = next((e for e in b["season"] if e["number"] == number), {"title": f"Episode {number}", "summary": "", "cliffhanger": ""})
    prev = b["episodes"][-1] if b["episodes"] else None
    prev_txt = (f"PREVIOUS EPISODE ({prev['title']}) ended like this: {prev['cliffhanger']}\nPrevious summary: {prev['summary']}\n"
                "Scene 1 MUST pick up directly from that ending.\n") if prev else "This is the first episode: introduce the world and the characters.\n"
    raw = llm.chat(
        f"{_bible_text(b)}\n\n{prev_txt}Write episode {number}: \"{outline['title']}\". Outline: {outline['summary']}\n"
        f"Use ONLY the characters and locations above (no new named characters). Respect the world rules and canonical events.\n"
        f"Write exactly {scenes} scenes as third-person narration, then the ending.\nFormat:\n"
        "SCENE: <location name> | <2 sentences of narration naming the characters involved> | <on-screen caption, max 5 words>\n"
        "SCENE: ...\nCLIFFHANGER: <one sentence>\nSUMMARY: <2-sentence summary of this episode>\n"
        "EVENT: <a new canonical fact established in this episode>\nEVENT: <another>",
        SYSTEM, max_tokens=620, temperature=0.75)
    f = _fields(raw)
    sc = [dict(zip(("location", "narration", "on_screen_text"), _parts(x, 3))) for x in f.get("SCENE", [])][:scenes + 1]
    sc = [s for s in sc if len(s["narration"]) > 20]
    if len(sc) < 2:
        raise ValueError(f"episode generation failed: {raw[:300]}")
    return {"number": number, "title": outline["title"], "scenes": sc, "cliffhanger": (f.get("CLIFFHANGER") or [outline["cliffhanger"]])[0],
            "summary": (f.get("SUMMARY") or [outline["summary"]])[0], "events": f.get("EVENT", [])[:3]}


def continuity_review(b: dict, ep: dict) -> dict:
    """Deterministic continuity checks + an independent reviewer prompt (different instructions, low temperature)."""
    checks = []
    text = " ".join(s["narration"] for s in ep["scenes"]) + " " + ep["cliffhanger"]
    names = {c["name"].split()[0] for c in b["characters"]}
    present = {n for n in names if re.search(rf"\b{re.escape(n)}\b", text)}
    checks.append(qa._c("recurring characters appear", len(present) >= min(2, len(names)), f"{sorted(present)} of {sorted(names)}", "major", fix="rewrite"))
    known = names | {w for l in b["locations"] for w in l["name"].split()} | {w for w in re.findall(r"[A-Z][a-z]+", b["title"] + " " + _bible_text(b))}
    caps = {w for w in re.findall(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]{3,}\b", text)}
    unknown = sorted(w for w in caps - known if text.count(w) >= 2)
    checks.append(qa._c("no unestablished named characters", not unknown, "all recurring names are in the bible" if not unknown
                        else f"names not in the bible: {unknown[:6]}", "major", fix="rewrite"))
    locs = [l["name"].lower() for l in b["locations"]]
    used = [s["location"] for s in ep["scenes"]]
    off = [u for u in used if not any(l in u.lower() or u.lower() in l for l in locs)]
    checks.append(qa._c("locations from the bible", len(off) <= len(used) // 2, f"scenes set in {sorted(set(used))}" + (f"; not in bible: {off}" if off else ""), "minor"))
    if b["episodes"]:
        prev = b["episodes"][-1]
        cw = {w for w in re.findall(r"[a-z]{5,}", prev["cliffhanger"].lower())}
        first = (ep["scenes"][0]["narration"] + " " + ep["scenes"][1]["narration"]).lower()
        hit = sorted(w for w in cw if w[:5] in first)
        checks.append(qa._c("picks up previous cliffhanger", len(hit) >= 1, f"opening echoes: {hit[:5]}" if hit else
                            f"opening does not reference: \"{prev['cliffhanger'][:80]}\"", "major", fix="rewrite"))
        verdict = llm.chat(f"{_bible_text(b)}\n\nNEW EPISODE TEXT:\n{text}\n\nYou are a continuity supervisor. Does the new episode contradict any world rule "
                           "or canonical event above? Answer with one line: 'VERDICT: OK' or 'VERDICT: CONTRADICTION - <what>'.",
                           "You are a strict continuity supervisor.", max_tokens=80, temperature=0.1)
        ok = "CONTRADICTION" not in verdict.upper()
        checks.append(qa._c("continuity supervisor (LLM)", ok, verdict.strip()[:160], "major", fix="rewrite"))
    return {"stage": "continuity", "checks": checks, "passed": all(c["ok"] or c["severity"] == "minor" for c in checks)}


def next_episode(series_id: str, scenes: int = 4, rounds: int = 3) -> tuple[dict, dict, dict]:
    """Write the next episode with continuity gates; commit it to the bible only after review."""
    b = db.get_doc("series", series_id)
    number = len(b["episodes"]) + 1
    best = None
    for attempt in range(rounds):
        ep = write_episode(b, number, scenes)
        review = continuity_review(b, ep)
        fails = sum(not c["ok"] and c["severity"] != "minor" for c in review["checks"])
        if best is None or fails < best[2]:
            best = (ep, review, fails)
        if fails == 0:
            break
    ep, review, _ = best
    review["attempts"] = attempt + 1
    b["episodes"].append(ep)
    b["canon"] += [{"episode": number, "event": e} for e in ep["events"]]
    db.put_doc("series", b, series_id)
    return b, ep, review


def produce_episode(b: dict, ep: dict, review: dict, size=(1280, 720)) -> dict:
    """Render an episode with the series' fixed production identity (grade, voice, music, opening)."""
    p = prj.Project.create(f"{b['title']} - E{ep['number']:02d} {ep['title']}", "series", series=b["id"], episode=ep["number"])
    scenes = []
    if ep["number"] > 1:  # recap generated from the stored summary of the previous episode
        prev = next(e for e in b["episodes"] if e["number"] == ep["number"] - 1)
        scenes.append({"narration": f"Previously on {b['title']}. {prev['summary']}", "on_screen_text": "Previously", "visual_query": ""})
    scenes += [{"narration": s["narration"], "on_screen_text": s["on_screen_text"] or s["location"], "visual_query": ""} for s in ep["scenes"]]
    scenes.append({"narration": ep["cliffhanger"], "on_screen_text": "To be continued", "visual_query": ""})
    script = {"title": f"{b['title']}: {ep['title']}", "topic": b["logline"], "scenes": scenes}
    tl = create.produce(p, script, size=size, style=b["style"], use_stock=False)
    p.save_timeline(tl, f"episode {ep['number']}")
    stats, report = qa.render_with_qa(p, tl, f"episode_{ep['number']:02d}", extra_stages=[review], extra={"series": b["id"]})
    return {"project": p.id, "output": stats["output"], "render": stats, "report": report, "style": p.data["style"]}


# ------------------------------------------------------------------ short film
def film_plan(concept: str, scenes: int = 5) -> dict:
    raw = llm.chat(
        f"Plan a short film. Concept: {concept}\nFormat:\nTITLE: <title>\nLOGLINE: <one sentence>\nTHEME: <theme>\n"
        "CHARACTER: <name> | <role> | <appearance> | <arc: how they change>\nCHARACTER: ... (2-3 characters)\n"
        f"SCENE: <act 1, 2 or 3> | <location, time of day> | <what happens, 2 sentences> | <key line of dialogue>\n(exactly {scenes} scenes)\n"
        "SHOT: <scene number> | <shot size: wide/medium/close-up> | <camera move> | <what we see>\n(2 shots per scene)",
        SYSTEM, max_tokens=900, temperature=0.75)
    f = _fields(raw)
    plan = {"concept": concept, "title": (f.get("TITLE") or ["Untitled"])[0], "logline": (f.get("LOGLINE") or [""])[0], "theme": (f.get("THEME") or [""])[0],
            "characters": [dict(zip(("name", "role", "appearance", "arc"), _parts(c, 4))) for c in f.get("CHARACTER", [])],
            "scenes": [dict(zip(("act", "location", "action", "dialogue"), _parts(s, 4))) for s in f.get("SCENE", [])],
            "shot_list": [dict(zip(("scene", "size", "move", "description"), _parts(s, 4))) for s in f.get("SHOT", [])]}
    if len(plan["scenes"]) < 3 or not plan["characters"]:
        raise ValueError(f"film plan generation failed: {raw[:300]}")
    db.put_doc("film", plan)
    return plan


def continue_project(project_id: str) -> dict:
    """'Continue the previous video in the same style': recover everything needed from a stored project."""
    p = prj.Project(project_id)
    tl = p.timeline()
    return {"style": p.data.get("style", {}), "script": p.data.get("script"), "series": p.data.get("series"),
            "size": [tl["width"], tl["height"]], "grade": tl.get("grade"), "music": tl.get("music"),
            "structure": [c["type"] for c in tl["clips"]], "last_narration": next((c.get("narration") for c in reversed(tl["clips"]) if c.get("narration")), None)}
