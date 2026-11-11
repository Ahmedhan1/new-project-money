"""NEXUS Video Remix Studio: several sources -> one library -> several new, differently structured versions."""
from __future__ import annotations

import re
from pathlib import Path

from . import edit, llm, project as prj
from .audio import music
from .media import analyze
from .speech import tts
from .timeline import ops
from .timeline.render import clip_duration, clip_starts


def build_library(project, paths: list[str | Path]) -> dict:
    """Import + analyse every source. Returns {media_path: analysis} and stores a summary on the project."""
    lib = {}
    for p in paths:
        m = project.import_media(p)
        an = analyze.analyze(m["path"])
        lib[m["path"]] = an
        project.data["analysis"][m["id"]] = {"name": m["name"], "duration": an["probe"]["duration"], "issues": an["issues"],
                                             "scenes": len(an["scenes"]), "text": (an["transcript"] or {}).get("text", "")}
    project.save()
    return lib


def segments(lib: dict) -> list[dict]:
    """Flat list of usable spoken segments across all sources (later take wins over repeated takes)."""
    out = []
    for si, (path, an) in enumerate(lib.items()):
        if not an.get("transcript"):
            continue
        dropped = {r["earlier"] for r in an["repetitions"]}
        dur = an["probe"]["duration"]
        for i, s in enumerate(an["transcript"]["segments"]):
            if i in dropped or s["end"] - s["start"] < 0.8:
                continue
            out.append({"key": f"{si}.{i}", "source": si, "src": path, "in": max(0, s["start"] - 0.15), "out": min(dur, s["end"] + 0.2),
                        "text": s["text"], "score": s.get("score", 0.0)})
    return out


def plan_story(segs: list[dict], brief: str) -> dict:
    """Ask the local LLM for a narrative: title, a new spoken opening, an order, section headings.
    The LLM only chooses among existing segment keys; anything invalid falls back to a deterministic plan."""
    listing = "\n".join(f"[{s['key']}] {s['text']}" for s in segs)
    fallback = {"title": brief.title()[:60] or "Remix", "opening": f"{brief.rstrip('.')}. Here is how the pieces fit together.",
                "sections": [{"heading": f"Part {si + 1}", "keys": [s["key"] for s in segs if s["source"] == si]}
                             for si in sorted({s["source"] for s in segs})]}
    valid = {s["key"] for s in segs}
    raw = ""
    for attempt in range(3):
        # Plain-text protocol: far more reliable than JSON mode on a small local model.
        raw = llm.chat(
            f"You are a documentary editor. Brief: {brief}\n\nSpoken segments from several source videos (the number before the dot "
            f"is the source video):\n{listing}\n\n"
            "Build ONE coherent story. Make 3 sections that follow an argument (problem -> solution -> how it fits together), and in each "
            "section MIX segments from different source videos where they talk about related things. Use every segment exactly once.\n"
            "Answer in exactly this format and nothing else:\n"
            "TITLE: <max 6 words>\nOPENING: <one new spoken sentence that hooks the viewer>\n"
            "SECTION: <heading, max 4 words> | <keys separated by commas, e.g. 0.1, 2.0, 1.3>\nSECTION: ...\nSECTION: ...",
            "You plan video edits. You follow the output format exactly.", max_tokens=400, temperature=0.3 + 0.25 * attempt)
        title = re.search(r"^TITLE:\s*(.+)$", raw, re.M)
        opening = re.search(r"^OPENING:\s*(.+)$", raw, re.M)
        used, sections = set(), []
        for head, keys in re.findall(r"^SECTION:\s*([^|\n]*)\|(.*)$", raw, re.M):
            ks = [k for k in re.findall(r"\d+\.\d+", keys) if k in valid and k not in used]
            used.update(ks)
            if ks:
                sections.append({"heading": head.strip(" \"'*")[:40], "keys": ks})
        if title and opening and len(sections) >= 2 and len(used) >= 0.7 * len(segs):
            leftovers = [s["key"] for s in segs if s["key"] not in used]
            if leftovers:  # never silently drop material the editor did not place
                sections[-1]["keys"] += leftovers
            return {"title": title.group(1).strip(" \"'*")[:60], "opening": opening.group(1).strip(" \"'*")[:240],
                    "sections": sections, "planner": f"local LLM (attempt {attempt + 1})"}
    return {**fallback, "planner": "deterministic fallback (LLM plan rejected)", "rejected_plan": raw[:400]}


def _assemble(project, lib: dict, segs: list[dict], plan: dict, *, size, grade, mood, seed, crossfade, cards, opening_voice=True,
              lang="en") -> dict:
    by_key = {s["key"]: s for s in segs}
    tl = prj.new_timeline(*size, lang=lang)
    tl["grade"], tl["audio_fx"] = grade, "voice"
    narr = []
    if opening_voice and plan.get("opening"):
        wav = project.dir / "audio" / f"opening_{abs(hash(plan['opening'])) % 10 ** 8}.wav"
        r = tts.synthesize(plan["opening"], wav, lang)
        tl["clips"].append(ops.color_clip(r["duration"] + 0.8, text=[{"text": plan["title"], "style": "title"}], fade_in=0.5,
                                          narration=plan["opening"]))
        narr.append((0, wav))
    else:
        tl["clips"].append(ops.color_clip(2.0, text=[{"text": plan["title"], "style": "title"}], fade_in=0.4))
    sfx = []
    for sec in plan["sections"]:
        if cards and sec.get("heading"):
            tl["clips"].append(ops.color_clip(1.6, "0x101828", text=[{"text": sec["heading"], "style": "kinetic"}],
                                              transition="crossfade", transition_dur=0.4, xfade="slideleft"))
            sfx.append(len(tl["clips"]) - 1)
        for n, k in enumerate(sec["keys"]):
            s = by_key[k]
            c = ops.video_clip(s["src"], s["in"], s["out"], fit="blur" if size[0] < size[1] else "cover")
            if crossfade and n == 0:
                c.update(transition="crossfade", transition_dur=0.4)
            tl["clips"].append(c)
    tl["clips"][-1]["fade_out"] = 0.6
    starts = clip_starts(tl)
    tl["audio"] = [{"src": str(w), "start": round(starts[i] + 0.3, 3), "clip": tl["clips"][i]["id"]} for i, w in narr]
    for i in sfx:  # sound design: a whoosh under every section card
        tl["audio"].append({"src": str(music.whoosh(0.7, seed=i)), "start": round(max(0, starts[i] - 0.1), 3), "volume": 0.5, "role": "sfx"})
    total = starts[-1] + clip_duration(tl["clips"][-1])
    tl["music"] = {"src": str(music.music_bed(total, mood, seed)), "volume": 0.35, "duck": True}
    edit.retime_subtitles(tl, lib)
    if narr:  # caption the new opening line too
        from .timeline import subs
        from .media import ff
        tl["subtitles"] = subs.fix_timing(subs.cues_from_text(plan["opening"], 0.3, 0.3 + ff.duration(narr[0][1])) + tl["subtitles"])
    return tl


def make_versions(project, lib: dict, brief: str) -> dict[str, dict]:
    """Three genuinely different edits on separate branches. Sources and earlier versions are never touched."""
    segs = segments(lib)
    story = plan_story(segs, brief)
    best = sorted(segs, key=lambda s: -s["score"])[:max(4, len(segs) // 2)]
    best_keys = [s["key"] for s in sorted(best, key=lambda s: (s["source"], s["in"]))]
    highlight = {"title": story["title"], "opening": None, "sections": [{"heading": "", "keys": best_keys}]}
    chrono = {"title": story["title"], "opening": story["opening"],
              "sections": [{"heading": Path(p).stem.split("_", 1)[-1].replace("_", " ").title(), "keys": [s["key"] for s in segs if s["source"] == si]}
                           for si, p in enumerate(lib)]}
    versions = {
        "A-story": _assemble(project, lib, segs, story, size=(1280, 720), grade="cinematic", mood="inspiring", seed=11, crossfade=True, cards=True),
        "B-highlights-vertical": _assemble(project, lib, segs, highlight, size=(720, 1280), grade="vivid", mood="upbeat", seed=23,
                                           crossfade=False, cards=False, opening_voice=False),
        "C-chronological-doc": _assemble(project, lib, segs, chrono, size=(1280, 720), grade="documentary", mood="documentary", seed=37,
                                         crossfade=True, cards=True),
    }
    for name, tl in versions.items():
        project.save_timeline(tl, f"remix {name}", branch=name)
    project.data["remix"] = {"brief": brief, "plan": story, "versions": list(versions)}
    project.save()
    return versions


def compare(versions: dict[str, dict]) -> list[dict]:
    """Side-by-side facts about each version (for the comparison view)."""
    rows = []
    for name, tl in versions.items():
        starts = clip_starts(tl)
        rows.append({"version": name, "size": f"{tl['width']}x{tl['height']}", "duration": round(starts[-1] + clip_duration(tl["clips"][-1]), 1),
                     "shots": len(tl["clips"]), "grade": tl["grade"], "music": Path(tl["music"]["src"]).stem.split("_")[0],
                     "sources_used": len({c["src"] for c in tl["clips"] if c.get("src")}),
                     "order": [re.sub(r"^\d+_", "", Path(c["src"]).stem)[:10] + f"@{c['in']:.0f}s" for c in tl["clips"] if c.get("src")]})
    return rows
