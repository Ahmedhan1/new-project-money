"""Director (DIR-1): long footage -> production plan -> condensed cinematic master -> platform cuts and shorts.

    plan(analyses, brief, ...)                    human-readable, machine-applicable plan (nothing rendered)
    build_master(project, src, analysis, plan)    cinematic master timeline
    build_deliverables(project, src, analysis, plan)  {deliverable id: timeline}, each tuned by platforms.tune
    run(project, media_paths, brief, platforms, ...)  analysis reuse -> plan -> master -> deliverables (+ optional renders)

Every editorial decision is a measurable heuristic (sentence importance, redundancy, lexical cohesion), never a view
prediction. Sentences are never cut in the middle. Multi-source input keeps the sources in the order given.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from pathlib import Path

from . import edit, platforms as plat, presets, project as prj
from .timeline import ops
from .timeline.render import clip_duration, clip_starts, total_duration

BACKREF = {"and", "but", "because", "which", "also", "then", "or", "plus", "therefore", "however", "so", "it", "this", "that", "these",
           "those", "they", "he", "she", "there", "here", "um", "uh", "well", "okay", "ok"}
CHAPTER_MIN_SENTS = 3
CHAPTER_MIN_S = 20.0
CHAPTER_TARGET_S = 75.0       # roughly one chapter per this many seconds of source speech
TITLE_CARD_S = 2.0
CHAPTER_CARD_S = 1.6
SHORT_MIN_S = 15.0
SHORT_MAX_S = 58.0
LONG_KINDS = ("long", "feed")


# ---------------------------------------------------------------- helpers
def _lib(src, analysis) -> dict[str, dict]:
    if src:
        return {str(src): analysis}
    if isinstance(analysis, dict) and "probe" not in analysis:
        return analysis
    raise ValueError("give src + analysis, or src=None and {src: analysis}")


def _vec(texts: list[str]) -> Counter:
    return Counter(w for t in texts for w in edit.content_words(t))


def _cos(a: Counter, b: Counter) -> float:
    num = sum(a[w] * b[w] for w in a.keys() & b.keys())
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def boundaries(sents: list[dict], k: int = 3, max_chapters: int | None = None, chapter_s: float = CHAPTER_TARGET_S) -> list[int]:
    """Topic-shift boundaries by lexical cohesion (TextTiling-style depth scores). Returns sentence indices that start a chapter."""
    n = len(sents)
    if n < 2 * CHAPTER_MIN_SENTS:
        return []
    sims = [_cos(_vec([s["text"] for s in sents[max(0, g - k):g]]), _vec([s["text"] for s in sents[g:g + k]])) for g in range(1, n)]
    depth = []
    for i, s in enumerate(sims):
        lp = max(sims[:i + 1])
        rp = max(sims[i:])
        depth.append((lp - s) + (rp - s))
    mean = sum(depth) / len(depth)
    sd = math.sqrt(sum((d - mean) ** 2 for d in depth) / len(depth))
    cands = sorted(range(len(depth)), key=lambda i: -depth[i])
    span = sents[-1]["hi"] - sents[0]["lo"]
    max_chapters = max_chapters or max(1, round(span / chapter_s))
    chosen: list[int] = []
    for i in cands:
        g = i + 1
        if len(chosen) + 1 >= max_chapters or depth[i] < mean + 0.3 * sd or depth[i] <= 0:
            break
        cuts = sorted(chosen + [g])
        edges = [0, *cuts, n]
        if all(b - a >= CHAPTER_MIN_SENTS and sents[b - 1]["hi"] - sents[a]["lo"] >= CHAPTER_MIN_S for a, b in zip(edges, edges[1:])):
            chosen.append(g)
    return sorted(chosen)


def _keywords(texts: list[str], others: list[str], n: int = 3) -> list[str]:
    """Distinctive words of `texts` against `others`: tf * idf, longer words preferred, no numbers; ties keep first occurrence."""
    words = [w for t in texts for w in re.findall(r"\w+", t.lower()) if w in edit.content_words(w) and not any(ch.isdigit() for ch in w)]
    tf = Counter(words)
    first = {w: k for k, w in reversed(list(enumerate(words)))}
    df = Counter(w for t in others for w in set(edit.content_words(t)))
    idf = {w: math.log((1 + len(others)) / (1 + df[w])) + 1 for w in tf}
    scored = sorted(tf, key=lambda w: (-(tf[w] * idf[w] * min(1.5, len(w) / 6)), first[w]))
    return scored[:n]


def _name(texts: list[str], others: list[str]) -> str:
    kw = _keywords(texts, others, 2)
    return " & ".join(w.capitalize() for w in kw) or "Part"


def _llm_names(chapters: list[dict], brief: str) -> list[str] | None:
    """Plain-text protocol: one 'N: name' line per chapter. Any problem -> None (deterministic names stay)."""
    try:
        from . import llm
        listing = "\n".join(f"{k + 1}: {c['excerpt'][:300]}" for k, c in enumerate(chapters))
        raw = llm.chat(f"Brief: {brief}\nChapters of a video (excerpts):\n{listing}\n\nGive each chapter a short title (max 4 words). "
                       f"Answer exactly {len(chapters)} lines, format 'N: title', nothing else.",
                       "You name video chapters. You follow the output format exactly.", max_tokens=120, temperature=0.3)
        got = {int(a): b.strip(" \"'*.")[:40] for a, b in re.findall(r"^\s*(\d+)\s*[:.)-]\s*(.+)$", raw, re.M)}
        names = [got.get(k + 1, "") for k in range(len(chapters))]
        return names if all(2 <= len(x) <= 40 for x in names) else None
    except Exception:
        return None


def self_contained(s: dict) -> bool:
    words = s["text"].split()
    first = re.sub(r"[^\w']", "", words[0].lower()) if words else ""
    return 5 <= len(words) <= 32 and s["text"].rstrip("\"')]").endswith(edit.TERMINAL) and first not in BACKREF and 2.0 <= s["dur"] <= 12.0


def _apply_selection(plan: dict, target_s: float) -> None:
    sents = plan["sentences"]
    kept, reasons = edit.select_sentences(sents, target_s)
    for k, s in enumerate(sents):
        s["kept"] = k in kept
        s["reason"] = "" if k in kept else reasons.get(k, "condensed")
    for c in plan["chapters"]:
        ids = c["sentences"]
        c["kept_s"] = round(sum(sents[i]["dur"] for i in ids if sents[i]["kept"]), 2)
        c["kept_sentences"] = sum(1 for i in ids if sents[i]["kept"])


def _cold_open(plan: dict) -> dict | None:
    kept = [s for s in plan["sentences"] if s["kept"]]
    if len(kept) < 4:
        return None
    best = max((s for s in kept if self_contained(s)), key=lambda s: s["score"], default=None)
    if best is None or best["gid"] == kept[0]["gid"] or best["score"] <= kept[0]["score"]:
        return None
    return {"gid": best["gid"], "src": best["src"], "text": best["text"], "start": best["start"], "end": best["end"], "dur": best["dur"],
            "reason": "strongest self-contained sentence (highest importance, no back-reference opener), teased before the title; "
                      "it also plays in its own place"}


# ---------------------------------------------------------------- plan
def plan(analyses: dict, brief: str, target_s: float | None = None, platforms=("youtube", "tiktok", "instagram-reels"), langs=("en",),
         preset: str = "cinematic", use_llm: bool = False, chapter_s: float = CHAPTER_TARGET_S) -> dict:
    """Production plan. `chapter_s`: about one chapter per this many seconds of a source (topic shifts decide where). `analyses`: {src: analysis} (or one analysis dict). Not rendered; every number is from the analysis."""
    if "probe" in analyses:
        analyses = {"source": analyses}
    sents: list[dict] = []
    chapters: list[dict] = []
    sources = []
    for si, (src, an) in enumerate(analyses.items()):
        ss = edit.sentences(an)
        dur = float(an["probe"]["duration"])
        sources.append({"src": src, "index": si, "duration": round(dur, 2), "sentences": len(ss),
                        "language": (an.get("transcript") or {}).get("language", "en")})
        if not ss:  # no transcript: the source is one chapter of scenes, kept as it is
            chapters.append({"src": src, "source": si, "start": 0.0, "end": round(dur, 3), "sentences": [], "excerpt": "",
                             "speechless": True})
            continue
        base = len(sents)
        for s in ss:
            sents.append({**{k: s[k] for k in ("i", "seg", "start", "end", "lo", "hi", "text", "score", "dur")}, "pieces": s["pieces"],
                          "src": src, "source": si, "gid": len(sents)})
        cuts = [0, *boundaries(ss, chapter_s=chapter_s), len(ss)]
        for a, b in zip(cuts, cuts[1:]):
            chapters.append({"src": src, "source": si, "start": 0.0 if a == 0 else ss[a]["lo"], "end": round(dur, 3) if b == len(ss) else ss[b - 1]["hi"],
                             "sentences": list(range(base + a, base + b)), "excerpt": " ".join(s["text"] for s in ss[a:b])})
    all_texts = [c["excerpt"] for c in chapters]
    for k, c in enumerate(chapters):
        c["id"] = f"ch{k + 1}"
        c["name"] = _name([c["excerpt"]], all_texts[:k] + all_texts[k + 1:]) if c["excerpt"] else Path(c["src"]).stem
        sc = [sents[i]["score"] for i in c["sentences"]]
        c["raw_importance"] = sum(sc) / len(sc) if sc else 0.0
        c["source_s"] = round(c["end"] - c["start"], 2)
        c["tight_s"] = round(sum(sents[i]["dur"] for i in c["sentences"]), 2)
    top = max([c["raw_importance"] for c in chapters] or [1.0]) or 1.0
    for c in chapters:
        c["importance"] = round(c["raw_importance"] / top, 3)
        for i in c["sentences"]:
            sents[i]["chapter"] = c["id"]
            sents[i]["boost"] = round(0.3 * c["importance"], 3)
    namer = "deterministic keywords (lexical TF-IDF)"
    if use_llm and chapters:
        names = _llm_names(chapters, brief)
        if names:
            for c, nm in zip(chapters, names):
                c["name"] = nm
            namer = "local LLM"
        else:
            namer += " (LLM names rejected or unavailable)"
    source_s = sum(s["duration"] for s in sources)
    tight_s = sum(s["dur"] for s in sents)
    if target_s is None:
        target_s = tight_s if tight_s <= 180 else max(180.0, 0.5 * tight_s)
    p = {"brief": brief, "preset": preset, "langs": list(langs), "sources": sources, "chapters": chapters, "sentences": sents,
         "chapter_namer": namer, "notes": []}
    _apply_selection(p, target_s)
    kept_s = sum(s["dur"] for s in sents if s["kept"])
    p["title"] = (brief.strip().rstrip(".")[:60] if brief and len(brief) <= 60 else " ".join(w.capitalize() for w in _keywords(all_texts, [], 3)))
    p["cold_open"] = _cold_open(p)
    reasons: dict[str, float] = {}
    for an in analyses.values():
        if (an.get("transcript") or {}).get("segments"):
            for r in edit.tighten_words(an)["removed"]:
                key = re.sub(r"\s.*", "", r["reason"].replace("false start", "false-start").replace("repeated take", "repeated-take"))
                reasons[key] = round(reasons.get(key, 0.0) + r["out"] - r["in"], 2)
    reasons["condensed"] = round(sum(s["dur"] for s in sents if not s["kept"]), 2)
    p["master"] = {"target_s": round(target_s, 2), "speech_s": round(kept_s, 2), "source_s": round(source_s, 2), "tightened_s": round(tight_s, 2),
                   "removed_s": round(source_s - kept_s, 2), "removed_by_reason": reasons,
                   "cards_s": round(TITLE_CARD_S + CHAPTER_CARD_S * max(0, len(chapters) - 1), 2),
                   "cold_open_s": (p["cold_open"] or {}).get("dur", 0.0)}
    est_master = kept_s + p["master"]["cards_s"] + p["master"]["cold_open_s"]
    p["master"]["duration_s"] = round(est_master, 2)
    dels = []
    for pid in platforms:
        sp = plat.spec(pid)
        W, H = sp["size"]
        base = {"platform": sp["id"], "size": [W, H], "aspect": sp["aspect"][0], "captions": sp["captions"].get("style"),
                "caption_position": sp["captions"].get("position")}
        if sp["kind"] in LONG_KINDS and (not sp.get("max_s") or est_master <= sp["max_s"]):
            dels.append({**base, "id": f"{sp['id']}-master", "kind": "master", "duration_s": round(est_master, 1),
                         "why": "condensed master fits the platform limit"})
        elif sp["kind"] in LONG_KINDS:
            tgt = round(min(sp["max_s"] * 0.9, (sp.get("ideal_s") or [0, sp["max_s"]])[1]), 1)
            dels.append({**base, "id": f"{sp['id']}-cut", "kind": "cut", "target_s": tgt, "duration_s": tgt,
                         "why": f"master ({est_master:.0f}s) is above the {sp['max_s']}s limit: a shorter condensed cut"})
        else:
            mx = min(SHORT_MAX_S, float(sp.get("max_s") or SHORT_MAX_S) - 2)
            dels.append({**base, "id": f"{sp['id']}-short-1", "kind": "short", "max_s": mx, "duration_s": None,
                         "why": "short-form platform: best self-contained moment, vertical"})
    p["deliverables"] = dels
    src_lang = sources[0]["language"] if sources else "en"
    for lg in langs:
        if lg != src_lang:
            p["notes"].append(f"language {lg}: localise the saved timelines with the localisation job (nexus.localize); not done by the director")
    co = p["cold_open"]
    p["summary"] = [f"{len(sources)} source(s), {source_s:.0f}s total, {len(sents)} sentences, {len(chapters)} chapter(s) ({namer})",
                    f"master: ~{est_master:.0f}s ({kept_s:.0f}s speech after tightening/condensing, target {target_s:.0f}s); removed "
                    + ", ".join(f"{k} {v:.1f}s" for k, v in reasons.items() if v),
                    *(f"chapter {c['id']} '{c['name']}': {c['start']:.1f}-{c['end']:.1f}s of {Path(c['src']).name}, importance {c['importance']:.2f}, "
                      f"keeps {c.get('kept_sentences', 0)}/{len(c['sentences'])} sentences" for c in chapters),
                    f"cold open: \"{co['text'][:80]}\"" if co else "cold open: none (first sentence is already the strongest, or none is self-contained)",
                    *(f"deliverable {d['id']}: {d['kind']} {d['size'][0]}x{d['size'][1]} captions {d['captions']} - {d['why']}" for d in dels)]
    return p


def replan(p: dict, target_s: float) -> dict:
    """Same plan, different master length (used for platform cuts)."""
    q = copy.deepcopy(p)
    _apply_selection(q, target_s)
    q["master"]["target_s"] = target_s
    q["cold_open"] = _cold_open(q) if p.get("cold_open") else None
    return q


# ---------------------------------------------------------------- master
def _clips_for(sents: list[dict], fit: str, focus: dict, label: str | None = None) -> list[dict]:
    out: list[dict] = []
    for s in sents:
        for a, b in s["pieces"]:
            if out and out[-1]["src"] == s["src"] and abs(out[-1]["out"] - a) < 1e-3:
                out[-1]["out"] = round(b, 3)
                out[-1]["segs"] = sorted(set(out[-1]["segs"]) | {s["seg"]})
                out[-1]["score"] = max(out[-1]["score"], s["score"])
                continue
            fx = focus.get(s["src"])
            c = ops.video_clip(s["src"], a, b, fit=fit, focus=[fx if fx is not None else 0.5, 0.5], segs=[s["seg"]], score=s["score"],
                               chapter=s.get("chapter"))
            if fx is not None:
                c["focus_source"] = "face"
            if label:
                c["label"] = label
            out.append(c)
    return out


def _sfx(name: str, seed: int = 0) -> str | None:
    try:
        from .fx import sfx
        return str(sfx.make(name, seed=seed))
    except Exception:  # FX package missing or failed: the master works without sound accents
        return None


def build_master(project, src, analysis, plan: dict, size: tuple[int, int] | None = None, music_bed: bool = True) -> dict:
    """Cinematic master: cold open, title card, chapter cards, tightened + condensed sentences in order, preset look, captions,
    music bed with ducking, section sound accents (if nexus.fx.sfx exists), fades. `project` is unused except for future hooks."""
    lib = _lib(src, analysis)
    an0 = next(iter(lib.values()))
    v = an0["probe"].get("video") or {"width": 1920, "height": 1080}
    W, H = size or (v["width"], v["height"])
    lang = (an0.get("transcript") or {}).get("language", "en")
    style = presets.get(plan.get("preset") or "cinematic")
    tl = prj.new_timeline(W, H, lang)
    tl["audio_fx"] = "voice" if an0["probe"].get("audio") else "none"
    focus = {s: (a.get("quality") or {}).get("face_focus_x") for s, a in lib.items()}

    def fit_for(path):
        pv = lib[path]["probe"].get("video") or {"width": W, "height": H}
        return "cover" if (pv["width"] > pv["height"]) == (W > H) else "blur"
    sents = plan["sentences"]
    co = plan.get("cold_open")
    if co:
        tl["clips"] += _clips_for([sents[co["gid"]]], fit_for(co["src"]), focus, "cold open")
        tl["clips"][0]["fade_in"] = 0.3
    tl["clips"].append(ops.color_clip(TITLE_CARD_S, "0x0b1020", text=[{"text": plan.get("title") or "", "style": "title"}],
                                      fade_in=0.3 if co else 0.5, fade_out=0.3, role="title_card", label="title card"))
    markers, cards = [], []
    for k, ch in enumerate(plan["chapters"]):
        kept = [sents[i] for i in ch["sentences"] if sents[i]["kept"]]
        if not kept and not ch.get("speechless"):
            continue
        if k > 0 and len(plan["chapters"]) > 1:
            tl["clips"].append(ops.color_clip(CHAPTER_CARD_S, "0x101828", text=[{"text": ch["name"], "style": "kinetic"}], fade_in=0.25,
                                              fade_out=0.25, role="chapter_card", label=f"chapter card: {ch['name']}", chapter=ch["id"]))
            cards.append(len(tl["clips"]) - 1)
        first = len(tl["clips"])
        if ch.get("speechless"):
            tl["clips"].append(ops.video_clip(ch["src"], ch["start"], ch["end"], fit=fit_for(ch["src"]), chapter=ch["id"]))
        else:
            tl["clips"] += _clips_for(kept, fit_for(ch["src"]), focus)
        markers.append((cards[-1] if k > 0 and cards else first, ch["name"]))
    if len(tl["clips"]) > 1:
        tl["clips"][-1]["fade_out"] = 0.8
    if not co:
        tl["clips"][0]["fade_in"] = 0.5
    tl["grade"] = style.get("grade", "cinematic")
    tl = presets.apply(tl, style)
    lb = [e for e in style.get("effects") or [] if e.get("type") == "letterbox"]
    for c in tl["clips"]:   # cards share the footage's letterbox so the frame does not jump between shapes
        if c["type"] == "color" and lb:
            c.setdefault("effects", copy.deepcopy(lb))
    edit.retime_subtitles(tl, lib)
    starts = clip_starts(tl)
    total = total_duration(tl)
    tl["markers"] = [{"t": 0.0 if n == 0 else round(starts[i], 3), "label": f"chapter: {name}"} for n, (i, name) in enumerate(markers)]
    if co:
        tl["markers"].insert(0, {"t": 0.0, "label": "cold open"})
    if music_bed and total > 0:
        from .audio import music
        tl["music"] = {"src": str(music.music_bed(total, style.get("mood", "calm"), seed=len(sents))), "volume": style.get("music_volume", 0.3),
                       "duck": True}
    title_i = next(i for i, c in enumerate(tl["clips"]) if c.get("role") == "title_card")
    accents = [("impact", title_i)] + [("whoosh", i) for i in cards]
    for n, (name, i) in enumerate(accents):
        p = _sfx(name, n)
        if p:
            tl["audio"].append({"src": p, "start": round(max(0.0, starts[i] - 0.08), 3), "volume": 0.35, "role": "sfx"})
    tl["title"] = plan.get("title")
    tl["description"] = plan.get("brief", "")
    tl["director"] = {"kind": "master", "chapters": [c["name"] for c in plan["chapters"]], "cold_open": bool(co),
                      "speech_s": round(sum(s["dur"] for s in sents if s["kept"]), 2)}
    return tl


# ---------------------------------------------------------------- shorts
def _own_moment(plan: dict, max_s: float) -> list[dict]:
    """Fallback moment picker: the run of consecutive kept-or-not sentences (same source) with the best mean importance, a
    self-contained opener and SHORT_MIN_S..max_s of tightened speech. Returns the sentences."""
    sents = plan["sentences"]
    best, best_v = None, -1e9
    for i in range(len(sents)):
        d = 0.0
        for j in range(i, len(sents)):
            if sents[j]["src"] != sents[i]["src"]:
                break
            d += sents[j]["dur"]
            if d > max_s:
                break
            if d < min(SHORT_MIN_S, max_s * 0.5) and j < len(sents) - 1 and sents[j + 1]["src"] == sents[i]["src"]:
                continue
            win = sents[i:j + 1]
            v = sum(s["score"] for s in win) / len(win) + 0.4 * self_contained(sents[i]) + 0.2 * (d >= SHORT_MIN_S)
            if v > best_v:
                best, best_v = win, v
    return best or sents[:1]


def _short_tl(plan: dict, lib: dict, pid: str, max_s: float) -> tuple[dict, str]:
    sp = plat.spec(pid)
    W, H = sp["size"]
    try:
        from . import shorts
    except ImportError:
        shorts = None
    if shorts is not None:
        best = None
        for src, an in lib.items():
            if not (an.get("transcript") or {}).get("segments"):
                continue
            try:
                ms = shorts.find_moments(an, min_s=min(SHORT_MIN_S, max_s * 0.5), max_s=max_s, top=1)
            except Exception:
                ms = []
            if ms and (best is None or ms[0]["score"] > best[2]["score"]):
                best = (src, an, ms[0])
        if best:
            src, an, m = best
            tl = shorts.build_short(src, an, m, size=(W, H))
            return tl, f"shorts.find_moments: {m['start']:.1f}-{m['end']:.1f}s of {Path(src).name}, score {m['score']}"
    win = _own_moment(plan, max_s)
    tl = prj.new_timeline(W, H, (next(iter(lib.values())).get("transcript") or {}).get("language", "en") if lib else "en")
    tl["audio_fx"] = "voice"
    focus = {s: (a.get("quality") or {}).get("face_focus_x") for s, a in lib.items()}
    tl["clips"] = _clips_for(win, sp["fit"], focus)
    if tl["clips"]:
        d0 = clip_duration(tl["clips"][0])
        tl["clips"][0]["text"] = [{"text": edit_title(win[0]["text"]), "style": "callout", "start": 0.0, "end": round(min(2.5, d0), 3)}]
    edit.retime_subtitles(tl, lib)
    tl["captions"] = dict(sp["captions"])
    return tl, f"director fallback picker: sentences {win[0]['gid']}-{win[-1]['gid']} of {Path(win[0]['src']).name}"


def edit_title(text: str, n: int = 6) -> str:
    words = [w for w in re.findall(r"[\w'’-]+", text)]
    return " ".join(words[:n]).capitalize()


# ---------------------------------------------------------------- deliverables
def build_deliverables(project, src, analysis, plan: dict, master: dict | None = None) -> dict[str, dict]:
    """One timeline per planned deliverable, each tuned to its platform. Notes are stored in tl['platform_notes']."""
    lib = _lib(src, analysis)
    master = master or build_master(project, src, analysis, plan)
    out: dict[str, dict] = {}
    for d in plan["deliverables"]:
        if d["kind"] == "master":
            base, how = master, "master"
        elif d["kind"] == "cut":
            base, how = build_master(project, src, analysis, replan(plan, d["target_s"])), f"condensed cut to {d['target_s']}s"
        else:
            base, how = _short_tl(plan, lib, d["platform"], d.get("max_s", SHORT_MAX_S))
        tl, notes = plat.tune(base, d["platform"], lib)
        tl["platform_notes"] = [{"change": f"built from: {how}", "signal": "spec", "confidence": "spec"}] + notes
        tl["deliverable"] = d["id"]
        tl.setdefault("title", plan.get("title"))
        tl.setdefault("description", plan.get("brief", ""))
        out[d["id"]] = tl
    return out


# ---------------------------------------------------------------- run
def _analysis_for(project, m: dict) -> dict:
    cache = project.dir / "reports" / f"analysis_{m['id']}_{m['sha256'][:12]}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    from .media import analyze
    an = analyze.analyze(m["path"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(an, ensure_ascii=False))
    project.data["analysis"][m["id"]] = {"name": m["name"], "duration": an["probe"]["duration"], "issues": an["issues"],
                                         "scenes": len(an["scenes"]), "text": (an["transcript"] or {}).get("text", "")}
    project.save()
    return an


def run(project, media_paths, brief: str, platforms=("youtube", "tiktok", "instagram-reels"), target_s: float | None = None,
        langs=("en",), preset: str = "cinematic", render=False, use_llm: bool = False, chapter_s: float = CHAPTER_TARGET_S) -> dict:
    """Analysis (reused from the project's report cache) -> plan -> master -> deliverables, saved on branches director-*.
    render: False, True (everything) or a list of deliverable ids / 'master'. Returns {plan, rows, branches}."""
    lib: dict[str, dict] = {}
    for p in media_paths:
        m = project.import_media(p)
        lib[m["path"]] = _analysis_for(project, m)
    pl = plan(lib, brief, target_s, platforms, langs, preset, use_llm, chapter_s)
    (project.dir / "reports").mkdir(parents=True, exist_ok=True)
    (project.dir / "reports" / "director_plan.json").write_text(json.dumps(pl, indent=1, ensure_ascii=False))
    master = build_master(project, None, lib, pl)
    branches = {"master": ("director-master", project.save_timeline(master, f"director master: {pl['title']}", "director-master"))}
    dels = build_deliverables(project, None, lib, pl, master)
    for k, tl in dels.items():
        branches[k] = (f"director-{k}", project.save_timeline(tl, f"director deliverable {k}", f"director-{k}"))
    want = set(([*dels, "master"] if render is True else list(render or [])))
    rows = []
    source_s = pl["master"]["source_s"]
    for k, tl in [("master", master), *dels.items()]:
        pid = tl.get("platform") or "master"
        row = {"deliverable": k, "platform": pid, "branch": branches[k][0], "duration": round(total_duration(tl), 2),
               "size": f"{tl['width']}x{tl['height']}", "shots": sum(1 for c in tl["clips"] if c["type"] == "video"),
               "removed_s": round(source_s - sum(clip_duration(c) for c in tl["clips"] if c["type"] == "video"), 2),
               "notes": [n["change"] for n in tl.get("platform_notes", [])]}
        if pid != "master":
            ck = plat.checklist(tl, pid)
            row["checklist"] = "passed" if ck["passed"] else "failed: " + "; ".join(c["check"] for c in ck["checks"] if not c["ok"] and c["severity"] != "minor")
            md = plat.metadata(project, tl, pid, langs[0] if langs else "en")
            row["title"] = md["title"]
            (project.dir / "reports" / f"director_{k}_metadata.json").write_text(json.dumps({**md, "notes": tl.get("platform_notes", []),
                                                                                             "checklist": ck}, indent=1, ensure_ascii=False))
        if k in want:
            from .timeline import render as rnd
            out = project.dir / "renders" / f"director_{k}.mp4"
            st = rnd.render(tl, out, project.dir / "work")
            row.update({"output": str(out), "rendered_duration": round(st["duration"], 2), "mb": round(out.stat().st_size / 1e6, 2),
                        "render_s": st.get("elapsed")})
        rows.append(row)
    return {"plan": pl, "rows": rows, "branches": {k: v[0] for k, v in branches.items()}, "master": master, "deliverables": dels}
