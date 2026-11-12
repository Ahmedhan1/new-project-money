"""Director (DIR-1): long raw footage + a brief -> an explainable edit plan -> a timeline -> an EDL.

    profiles() -> dict
    plan(analysis_by_src, brief) -> plan            # pure: reads analyses only, never files
    build(plan, size=(1920, 1080)) -> tl             # timeline per docs/PHASE3_PLAN.md + the clip schema
    edl(tl, fmt="json" | "cmx3600") -> str
    direct(sources, analysis_by_src, brief) -> {"plan", "master", "edl", "notes"}

brief = {"profile", "target_seconds", "instructions", "platforms": [...], "languages": [...], "shorts": n,
         optional: "title", "end_text", "use_llm", "drop_tangents", "music": path}

How an edit is decided (every step leaves a record in plan["decisions"]):
 1. understand every source (media/understand.py): sentences with word timings, importance, topics, narrative roles.
 2. duplicate takes (inside a source and across sources) collapse to the best take: fluency, completeness, picture
    quality, importance; ties go to the later take (the speaker usually repeats a line to fix it).
 3. sentences are grouped into "thoughts": a sentence that leans on the previous one ("and", "but", "it", "that"...) is
    never separated from it. Thoughts are the unit of selection, so nothing is ever cut mid-sentence or mid-thought.
 4. tangents (topic with no shared vocabulary, below-median importance) are dropped when the profile says so.
 5. inside a thought, pauses longer than the profile's tolerance and fillers (by aggressiveness) become cuts that sit in
    the silence between two words, never inside a word.
 6. compression to target_seconds drops the lowest-priority thoughts first; the hook, the ending and anything the
    instructions asked to keep are protected; the exact programme length is recomputed after every change.
 7. order: topics in first-appearance order (topics from several sources that share vocabulary are grouped), the
    strongest self-contained line may be lifted to a cold open. Crossfades only at topic boundaries and only where both
    sides have enough silence; jump cuts are hidden with alternating punch-ins when the profile allows it.
 8. long talking stretches get cutaways from silent/low-speech shots (as full-frame `pip`) or reframing punch-ins.
"""
from __future__ import annotations

import copy
import functools
import json
import math
import re
from pathlib import Path

import yaml

from . import config
from .media import understand as und

DEFAULT_PROFILE = "documentary"
PAD_IN, PAD_OUT = 0.10, 0.18       # breathing room kept before the first / after the last word of a piece
GUARD = 0.03                       # never come closer than this to a neighbouring (unused) word
MIN_PIECE = 0.45                   # a cut may not leave a fragment shorter than this
MIN_XFADE = 0.2
SPEECH_BROLL_MAX = 0.15            # a shot with less speech than this share can be b-roll


# ---------------------------------------------------------------- profiles
@functools.lru_cache(maxsize=1)
def _profiles() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "direction.yaml").read_text(encoding="utf-8"))["profiles"]


def profiles() -> dict:
    return copy.deepcopy(_profiles())


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def get_profile(name: str | None, overrides: dict | None = None) -> dict:
    allp = _profiles()
    p = _merge(allp.get(name or DEFAULT_PROFILE) or allp[DEFAULT_PROFILE], overrides or {})
    p["name"] = name if name in allp else DEFAULT_PROFILE
    return p


# ---------------------------------------------------------------- instructions
_NUM = r"(\d+(?:\.\d+)?)"
_SKIP_TERMS = {"pause", "pauses", "silence", "silences", "dead", "air", "filler", "fillers", "um", "ums", "music", "caption",
               "captions", "subtitles", "transition", "transitions", "title", "titles", "everything", "it", "them"}


def parse_instructions(text: str | None, use_llm: bool = False) -> dict:
    """Deterministic reading of simple editing instructions. Returns
    {"target_seconds", "pace" (factor on shot length and pause tolerance), "music", "captions", "pause_tolerance",
     "filler_removal", "keep": [terms], "drop": [terms], "overrides": {profile fields}, "understood": [...], "llm": bool}."""
    t = (text or "").lower()
    r: dict = {"target_seconds": None, "pace": 1.0, "music": None, "captions": None, "pause_tolerance": None,
               "filler_removal": None, "keep": [], "drop": [], "overrides": {}, "understood": [], "llm": False}
    m = re.search(_NUM + r"\s*[- ]?\s*(seconds?|secs?|s|minutes?|mins?|m)\b", t)
    if m:
        v = float(m.group(1)) * (60 if m.group(2).startswith("m") else 1)
        r["target_seconds"] = v
        r["understood"].append(f"target length {v:g}s")
    elif re.search(r"\b(a|one) minute\b", t):
        r["target_seconds"] = 60.0
        r["understood"].append("target length 60s")
    if re.search(r"\b(faster|quicker|snappier|punchier|tighter|more energy|fast[- ]paced)\b", t):
        r["pace"] = 0.7
        r["understood"].append("faster pacing (shot length and pause tolerance x0.7)")
    elif re.search(r"\b(slower|calmer|let it breathe|more breathing room|relaxed|slow[- ]paced)\b", t):
        r["pace"] = 1.4
        r["understood"].append("slower pacing (shot length and pause tolerance x1.4)")
    if re.search(r"\b(no|without|remove( the)?|drop( the)?|skip( the)?) (background )?music\b", t):
        r["music"] = False
        r["understood"].append("no music")
    elif re.search(r"\b(add|with|use) (some )?(background )?music\b", t):
        r["music"] = True
        r["understood"].append("music on")
    if re.search(r"\b(no|without) (captions|subtitles)\b", t):
        r["captions"] = False
        r["understood"].append("no burned captions")
    if re.search(r"\b(remove|cut|no|kill|drop|tighten)( all)?( the)? (pauses|silences?|dead air|gaps)\b", t):
        r["pause_tolerance"] = 0.25
        r["understood"].append("remove pauses (tolerance 0.25s)")
    if re.search(r"\b(remove|cut|no|drop|clean up)( all)?( the)? (fillers?|filler words|ums?|uhs?|ums and uhs)\b", t):
        r["filler_removal"] = 1.0
        r["understood"].append("remove all fillers")
    if re.search(r"\b(no transitions|hard cuts only|only hard cuts|no crossfades?)\b", t):
        r["overrides"]["transitions"] = {"vocabulary": ["hard_cut", "jump_cut"], "crossfade_at_topics": False}
        r["understood"].append("hard cuts only")
    if re.search(r"\b(black and white|black & white|b&w|monochrome)\b", t):
        r["overrides"]["grade"] = "bw"
        r["understood"].append("black-and-white grade")
    for word, grade in (("warm", "warm"), ("cool", "cool"), ("vivid", "vivid")):
        if re.search(rf"\b{word}(er)? (look|grade|colou?rs?)\b", t):
            r["overrides"]["grade"] = grade
            r["understood"].append(f"{grade} grade")
    phrase = r"(?:the |all )?(?:part |section |bit |topic |stuff |segment )?(?:about |on |with )?([a-z][\w' -]*?)" \
             r"(?: (?:part|section|topic|bit|stuff|segment))?(?=[,.;!]|$| and | but )"
    for verb, key in ((r"keep|include|focus on|make sure to keep", "keep"), (r"remove|cut|drop|skip|lose|leave out", "drop")):
        for m in re.finditer(rf"\b(?:{verb}) {phrase}", t):
            terms = [w for w in und.content(m.group(1)) if w not in {und.stem(x) for x in _SKIP_TERMS} and w not in _SKIP_TERMS]
            if terms:
                r[key] += terms
                r["understood"].append(f"{key} material about: {' '.join(terms)}")
    if use_llm and text:
        _llm_instructions(text, r)
    return r


def _llm_instructions(text: str, r: dict) -> None:
    """Optional refinement with the local LLM: only fills fields the deterministic reading left empty, and only with
    validated values. Absent model (CI) = no change."""
    try:
        from . import llm
        d = llm.chat_json("Extract editing instructions as JSON with keys target_seconds (number or null), pace "
                          "('faster'|'slower'|null), music (true|false|null), keep (list of topic words), drop (list of topic "
                          f"words).\nINSTRUCTIONS: {text[:800]}", max_tokens=200, retries=1)
    except Exception:  # noqa: BLE001 - model missing or bad answer
        return
    if not isinstance(d, dict):
        return
    if r["target_seconds"] is None and isinstance(d.get("target_seconds"), (int, float)) and 5 <= d["target_seconds"] <= 7200:
        r["target_seconds"] = float(d["target_seconds"])
    if r["pace"] == 1.0 and d.get("pace") in ("faster", "slower"):
        r["pace"] = 0.7 if d["pace"] == "faster" else 1.4
    if r["music"] is None and isinstance(d.get("music"), bool):
        r["music"] = d["music"]
    words = set(und.tokens(text))
    for key in ("keep", "drop"):
        if not r[key] and isinstance(d.get(key), list):
            r[key] = [und.stem(w) for x in d[key] for w in und.tokens(str(x)) if w in words and w not in und.STOP]
    r["llm"] = True
    r["understood"].append("refined by the local LLM (validated against the instruction text)")


# ---------------------------------------------------------------- helpers
def _snap(t: float, lo: float, hi: float, fps: float) -> float:
    """Snap a cut to a frame boundary if one lies inside [lo, hi]; otherwise keep the exact (between-words) time."""
    for cand in (math.floor(t * fps) / fps, math.ceil(t * fps) / fps):
        if lo - 1e-9 <= cand <= hi + 1e-9:
            return round(cand, 4)
    return round(t, 4)


def _dec(decisions: list, src: str, start: float, end: float, action: str, kind: str, reason: str, score: float, **kw) -> dict:
    d = {"id": f"d{len(decisions) + 1}", "src": src, "start": round(float(start), 3), "end": round(float(end), 3),
         "action": action, "kind": kind, "reason": reason, "score": round(float(score), 3), **kw}
    decisions.append(d)
    return d


def _shot_at(shots: list[dict], a: float, b: float) -> dict | None:
    best, ov = None, 0.0
    for s in shots:
        o = min(b, s["end"]) - max(a, s["start"])
        if o > ov:
            best, ov = s, o
    return best


def _pace_factor(curve: str, x: float) -> float:
    """Shot-length multiplier at programme position x (0..1)."""
    if curve == "build":
        return 1.2 - 0.45 * x
    if curve == "front_loaded":
        return 0.65 if x < 0.2 else 1.0
    if curve == "wave":
        return 0.8 + 0.3 * (0.5 + 0.5 * math.sin(2 * math.pi * 3 * x))
    return 1.0


# ---------------------------------------------------------------- plan
def _sources(analysis_by_src: dict) -> dict:
    srcs = {}
    for k, (src, an) in enumerate(analysis_by_src.items()):
        u = und.ensure(an)
        sents = u["sentences"]
        for s in sents:
            s.setdefault("score", 0.0)
            s.setdefault("topic", 0)
            s.setdefault("role", "development")
        srcs[src] = {"order": k, "an": an, "u": u, "dur": und.duration_of(an), "sents": sents,
                     "words": sorted((w["start"], w["end"]) for s in sents for w in s["words"]),
                     "shots": u.get("shots") or [], "lang": (an.get("transcript") or {}).get("language", "en")}
    return srcs


def _neighbours(words: list[tuple[float, float]], a: float, b: float) -> tuple[float, float]:
    """End of the last word before `a` and start of the first word after `b` in the source (any word, used or not)."""
    lo, hi = 0.0, math.inf
    for s, e in words:
        if e <= a + 1e-6:
            lo = max(lo, e)
        elif s >= b - 1e-6:
            hi = min(hi, s)
            break
    return lo, hi


def _duplicate_takes(srcs: dict, decisions: list, threshold: float = 0.8) -> set:
    """Collapse repeated takes (same source or across sources) to the best one. Returns dropped (src, i) keys."""
    allx = [(src, s) for src, S in srcs.items() for s in S["sents"]]
    parent = {(src, s["i"]): (src, s["i"]) for src, s in allx}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(allx)):
        sa, xa = allx[a]
        if len(xa["text"]) < 15:
            continue
        for b in range(a + 1, len(allx)):
            sb, xb = allx[b]
            if sa == sb and xb["i"] - xa["i"] > 8:
                continue
            if abs(len(xa["text"]) - len(xb["text"])) > 0.4 * max(len(xa["text"]), len(xb["text"])):
                continue
            if und.similarity(xa["text"], xb["text"]) >= threshold:
                parent[root((sb, xb["i"]))] = root((sa, xa["i"]))
    for src, S in srcs.items():  # analyze.find_repetitions (segment indices) agrees by construction; add any it found
        for r in S["an"].get("repetitions") or []:
            e = [s for s in S["sents"] if s["seg"] == r.get("earlier")]
            l_ = [s for s in S["sents"] if s["seg"] == r.get("later")]
            if e and l_ and und.similarity(e[-1]["text"], l_[-1]["text"]) >= 0.7:
                parent[root((src, l_[-1]["i"]))] = root((src, e[-1]["i"]))
    groups: dict = {}
    for src, s in allx:
        groups.setdefault(root((src, s["i"])), []).append((src, s))
    dropped = set()
    for g in groups.values():
        if len(g) < 2:
            continue

        def take_score(item):
            src, s = item
            shot = _shot_at(srcs[src]["shots"], s["start"], s["end"])
            q = shot.get("quality") if shot and shot.get("quality") is not None else 0.7
            c = s.get("components") or {}
            return round(0.45 * c.get("fluency", 1.0) + 0.2 * c.get("completeness", 1.0) + 0.2 * q + 0.15 * s.get("score", 0) / 100, 4)

        ranked = sorted(g, key=lambda it: (take_score(it), srcs[it[0]]["order"] == 0, it[1]["start"]), reverse=True)
        best = ranked[0]
        for src, s in ranked[1:]:
            dropped.add((src, s["i"]))
            _dec(decisions, src, s["start"], s["end"], "drop", "duplicate_take",
                 f"repeated take of \"{best[1]['text'][:60]}\"; kept the take at {best[0]} {best[1]['start']:.2f}s "
                 f"(take score {take_score(best)} vs {take_score((src, s))})", take_score((src, s)), text=s["text"])
        _dec(decisions, best[0], best[1]["start"], best[1]["end"], "note", "best_take",
             f"best of {len(g)} takes (take score {take_score(best)}: fluency, completeness, picture quality, importance)",
             take_score(best), text=best[1]["text"])
    return dropped


def _thoughts(srcs: dict, dropped: set, max_sents: int = 4, max_len: float = 25.0) -> list[dict]:
    out = []
    for src, S in srcs.items():
        cur: dict | None = None
        prev = None
        for s in S["sents"]:
            if (src, s["i"]) in dropped:
                continue
            joins = (cur is not None and prev is not None and s.get("depends_prev") and s.get("topic") == prev.get("topic")
                     and s["start"] - prev["end"] <= 2.0 and len(cur["sents"]) < max_sents and s["end"] - cur["sents"][0]["start"] <= max_len)
            if joins:
                cur["sents"].append(s)
            else:
                cur = {"id": f"t{len(out) + 1}", "src": src, "sents": [s]}
                out.append(cur)
            prev = s
    for t in out:
        sc = [s.get("score", 0) for s in t["sents"]]
        t.update({"start": t["sents"][0]["start"], "end": t["sents"][-1]["end"], "topic": t["sents"][0].get("topic", 0),
                  "text": " ".join(s["text"] for s in t["sents"]), "score": round(max(sc) + 0.25 * sum(sc) / len(sc), 2),
                  "roles": sorted({s.get("role", "development") for s in t["sents"]}),
                  "speaker": t["sents"][0].get("speaker")})
    return out


def _pieces(t: dict, S: dict, prof: dict, fill_keys: set, decisions: list) -> list[dict]:
    """Source ranges of one thought after removing long pauses and selected fillers. Every cut lies between words."""
    tol = float(prof["pause_tolerance"])
    words = []
    for s in t["sents"]:
        for k, w in enumerate(s["words"]):
            words.append({**w, "remove": (s["i"], k) in fill_keys, "sent": s["i"]})
    runs: list[dict] = []
    removed: list[dict] = []
    for w in words:
        if w["remove"]:
            removed.append(w)
            if runs:
                runs[-1]["closed"] = "filler"
            continue
        if runs and not runs[-1].get("closed") and w["start"] - runs[-1]["words"][-1]["end"] <= tol:
            runs[-1]["words"].append(w)
            continue
        reason = runs[-1].get("closed") if runs and runs[-1].get("closed") else ("pause" if runs else None)
        runs.append({"words": [w], "after": reason})
    if not runs:
        return []
    # never leave a fragment shorter than MIN_PIECE: merge it back (re-including a filler if one was cut there)
    k = 1
    while k < len(runs):
        a, b = runs[k - 1], runs[k]
        short = (b["words"][-1]["end"] - b["words"][0]["start"] < MIN_PIECE) or (a["words"][-1]["end"] - a["words"][0]["start"] < MIN_PIECE)
        if short:
            between = [w for w in words if a["words"][-1]["end"] <= w["start"] and w["end"] <= b["words"][0]["start"] and w["remove"]]
            for w in between:
                removed.remove(w)
                _dec(decisions, t["src"], w["start"], w["end"], "keep", "filler",
                     f"filler \"{w['word']}\" kept: removing it would leave a fragment shorter than {MIN_PIECE}s", 0.0)
            a["words"] += between + b["words"]
            a["words"].sort(key=lambda w: w["start"])
            runs.pop(k)
        else:
            k += 1
    for w in removed:
        _dec(decisions, t["src"], w["start"], w["end"], "drop", "filler", f"filler \"{w['word']}\" removed "
             f"(filler aggressiveness {prof['filler_removal']})", float(prof["filler_removal"]))
    pieces = []
    for r in runs:
        fw, lw = r["words"][0], r["words"][-1]
        lo, _ = _neighbours(S["words"], fw["start"], fw["start"])
        _, hi = _neighbours(S["words"], lw["end"], lw["end"])
        hi = min(hi, S["dur"])
        room_before, room_after = fw["start"] - lo - GUARD, hi - lw["end"] - GUARD
        pieces.append({"src": t["src"], "thought": t["id"], "topic": t.get("cluster", t["topic"]),
                       "first": fw["start"], "last": lw["end"], "lo": lo + GUARD if lo > 0 else 0.0, "hi": hi - GUARD,
                       "in": max(0.0, fw["start"] - min(PAD_IN, max(0.0, room_before))),
                       "out": lw["end"] + min(PAD_OUT, max(0.0, room_after)),
                       "words": [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in r["words"]],
                       "after": r["after"], "speaker": t.get("speaker")})
    for a, b in zip(pieces, pieces[1:]):
        if b["in"] > a["out"]:
            why = "filler removed" if b["after"] == "filler" else f"pause {b['first'] - a['last']:.2f}s > tolerance {tol:g}s"
            _dec(decisions, t["src"], a["out"], b["in"], "drop", "pause" if b["after"] != "filler" else "filler_gap",
                 f"jump cut: {why}", round(b["first"] - a["last"], 3))
    return pieces


def _clusters(thoughts: list[dict], srcs: dict) -> dict:
    """Topic clusters across sources (shared vocabulary), in first-appearance order. Returns topic key -> (rank, label)."""
    from collections import Counter
    topics = []
    for src, S in sorted(srcs.items(), key=lambda kv: kv[1]["order"]):
        for t in S["u"].get("topics") or [{"id": 0, "bag": {}, "label": "", "start": 0.0}]:
            topics.append((src, t))
    clusters: list[dict] = []
    mapping = {}
    for src, t in topics:
        bag = Counter(t.get("bag") or {})
        best, sim = None, 0.0
        for c in clusters:
            if src in c["srcs"]:
                continue
            x = und.cosine(bag, c["bag"])
            if x > sim:
                best, sim = c, x
        if best is not None and sim >= 0.35:
            best["bag"].update(bag)
            best["srcs"].add(src)
            mapping[(src, t["id"])] = best["rank"]
        else:
            clusters.append({"rank": len(clusters), "bag": Counter(bag), "srcs": {src}, "label": t.get("label", "")})
            mapping[(src, t["id"])] = clusters[-1]["rank"]
    labels = {c["rank"]: c["label"] for c in clusters}
    for t in thoughts:
        t["cluster"] = mapping.get((t["src"], t["topic"]), 0)
    return labels


def _layout(sel: list[dict], pieces: dict, prof: dict, cards: dict, srcs: dict) -> tuple[list[dict], float]:
    """Programme items for the selected thoughts in order, with cut types and exact duration (crossfades subtracted)."""
    tr = prof["transitions"]
    vocab = set(tr.get("vocabulary") or ["hard_cut"])
    items: list[dict] = []
    if cards.get("title") and cards.get("title_at") == 0:
        items.append({"kind": "card", "role": "title", "duration": cards["title_seconds"], "text": cards["title"]})
    for n, t in enumerate(sel):
        if cards.get("title") and cards.get("title_at") == 1 and n == 1:
            items.append({"kind": "card", "role": "title", "duration": cards["title_seconds"], "text": cards["title"]})
        for k, p in enumerate(pieces[t["id"]]):
            items.append({"kind": "piece", **copy.deepcopy(p), "cut": "jump_cut" if k else None})
    if cards.get("end"):
        items.append({"kind": "card", "role": "end", "duration": cards["end_seconds"], "text": cards["end"]})
    if cards.get("title") and cards.get("title_at") == 1 and len(sel) <= 1:
        items.insert(1 if items else 0, {"kind": "card", "role": "title", "duration": cards["title_seconds"], "text": cards["title"]})
    punch = float(tr.get("punch_in", 1.0))
    parity = 0
    for k in range(1, len(items)):
        a, b = items[k - 1], items[k]
        if b["kind"] == "card" or a["kind"] == "card":
            b["cut"] = "hard_cut"
            continue
        if b["cut"] == "jump_cut":
            parity += 1
            if punch > 1.0 and parity % 2:
                b["punch"] = punch
            continue
        parity = 0
        if a["topic"] != b["topic"] and tr.get("crossfade_at_topics") and "crossfade" in vocab:
            d = min(float(tr.get("dur", 0.5)), a["hi"] - a["last"], b["first"] - b["lo"])
            if d >= MIN_XFADE:
                a["out"] = max(a["out"], a["last"] + d)
                b["in"] = min(b["in"], b["first"] - d)
                b["cut"], b["xfade_dur"] = "crossfade", round(d, 3)
                continue
            b["cut"] = "hard_cut"
            b["why_not_crossfade"] = f"only {max(0.0, d):.2f}s of silence around the topic boundary"
            continue
        if a.get("speaker") and b.get("speaker") and a["speaker"] != b["speaker"] and "j_cut" in vocab:
            b["cut"] = "j_cut"
        elif a["src"] != b["src"] and "l_cut" in vocab:
            b["cut"] = "l_cut"
        else:
            b["cut"] = "hard_cut"
    fps = float(config.FPS)
    for it in items:
        if it["kind"] == "piece":
            it["in"] = _snap(it["in"], it["lo"], it["first"], fps)
            it["out"] = _snap(it["out"], it["last"], it["hi"], fps)
    total = sum((it["out"] - it["in"]) if it["kind"] == "piece" else it["duration"] for it in items)
    total -= sum(it.get("xfade_dur", 0.0) for it in items if it.get("cut") == "crossfade")
    return items, round(total, 3)


def plan(analysis_by_src: dict, brief: dict | None = None) -> dict:
    """Explainable edit plan. Pure: uses only the analysis dicts (word timings, scenes, measured shots if present)."""
    brief = dict(brief or {})
    ins = parse_instructions(brief.get("instructions"), bool(brief.get("use_llm")))
    prof = get_profile(brief.get("profile"), ins["overrides"])
    pace = ins["pace"]
    prof["shot_len"] = [round(prof["shot_len"][0] * pace, 3), round(prof["shot_len"][1] * pace, 3)]
    prof["pause_tolerance"] = round(ins["pause_tolerance"] if ins["pause_tolerance"] is not None else prof["pause_tolerance"] * pace, 3)
    if ins["filler_removal"] is not None:
        prof["filler_removal"] = ins["filler_removal"]
    if ins["music"] is not None:
        prof["music"]["enabled"] = ins["music"]
    if ins["captions"] is not None:
        prof["captions_enabled"] = ins["captions"]
    if brief.get("drop_tangents") is not None:
        prof["drop_tangents"] = bool(brief["drop_tangents"])
    target = brief.get("target_seconds") or ins["target_seconds"]
    target = float(target) if target else None
    srcs = _sources(analysis_by_src)
    decisions: list[dict] = []
    notes: list[str] = []
    if not any(S["sents"] for S in srcs.values()):
        return _plan_montage(srcs, brief, prof, ins, target, decisions, notes)
    for src, S in srcs.items():
        if any(not s.get("timed", True) for s in S["sents"]):
            notes.append(f"{src}: some segments had no word timings; word times were estimated, so cut points are approximate")
    dropped = _duplicate_takes(srcs, decisions)
    thoughts = _thoughts(srcs, dropped)
    cluster_labels = _clusters(thoughts, srcs)
    # ------- fillers chosen by aggressiveness
    fill_keys = set()
    for src, S in srcs.items():
        for f in S["u"].get("fillers") or []:
            if f["level"] <= float(prof["filler_removal"]) + 1e-9:
                for j in range(f["n"]):
                    fill_keys.add((f["sentence"], f["word_index"] + j))
        for f in S["u"].get("stutters") or []:
            if float(prof["filler_removal"]) >= 0.3:
                fill_keys.add((f["sentence"], f["word_index"]))
    pieces = {}
    for t in thoughts:
        S = srcs[t["src"]]
        keys = {k for k in fill_keys} if t["src"] == next(iter(srcs)) or len(srcs) == 1 else set()
        if len(srcs) > 1:  # filler keys are per source
            keys = set()
            for f in S["u"].get("fillers") or []:
                if f["level"] <= float(prof["filler_removal"]) + 1e-9:
                    keys |= {(f["sentence"], f["word_index"] + j) for j in range(f["n"])}
            for f in S["u"].get("stutters") or []:
                if float(prof["filler_removal"]) >= 0.3:
                    keys.add((f["sentence"], f["word_index"]))
        pieces[t["id"]] = _pieces(t, S, prof, keys, decisions)
    thoughts = [t for t in thoughts if pieces[t["id"]]]
    # ------- priorities, protection, tangents, instructions
    hook = None
    for S in srcs.values():
        h = (S["u"].get("narrative") or {}).get("hook")
        if h is not None:
            cand = next((t for t in thoughts if t["src"] in srcs and any(s["i"] == h and s is S["sents"][h] for s in t["sents"])), None)
            if cand and (hook is None or cand["sents"][0].get("hook", 0) > hook["sents"][0].get("hook", 0)):
                hook = cand
    keep_terms, drop_terms = set(ins["keep"]), set(ins["drop"])
    for t in thoughts:
        t["priority"] = t["score"]
        t["protected"] = []
        stems = set(und.content(t["text"])) | set(und.content(cluster_labels.get(t["cluster"], "")))
        if "climax" in t["roles"]:
            t["priority"] += 8
        if "resolution" in t["roles"]:
            t["priority"] += 4
        if "setup" in t["roles"]:
            t["priority"] += 3
        if t is hook:
            t["priority"] += 15
            t["protected"].append("hook")
        if keep_terms & stems:
            t["priority"] += 100
            t["protected"].append("instruction: keep " + " ".join(sorted(keep_terms & stems)))
        t["drop_reason"] = None
        if drop_terms & stems and not t["protected"]:
            t["drop_reason"] = ("instruction", "instructions asked to remove material about " + " ".join(sorted(drop_terms & stems)))
        elif "tangent" in t["roles"] and prof.get("drop_tangents") and not t["protected"]:
            coh = next((x["cohesion"] for x in srcs[t["src"]]["u"].get("topics") or [] if x["id"] == t["topic"]), 0.0)
            t["drop_reason"] = ("tangent", f"tangent: topic shares almost no vocabulary with the rest (cohesion {coh})")
    # ending
    ending = prof.get("ending", "resolution")
    last_cluster = max(t["cluster"] for t in thoughts) if thoughts else 0
    if ending == "resolution":
        cands = [t for t in thoughts if t["cluster"] == last_cluster and not t["drop_reason"]]
        conc = [t for t in cands if any((s.get("components") or {}).get("concluding") for s in t["sents"])]
        end_t = max(conc, key=lambda t: t["start"]) if conc else (max(cands, key=lambda t: (t["src"] == thoughts[-1]["src"], t["start"])) if cands else None)
        if end_t:
            end_t["protected"].append("ending: resolution")
            end_t["priority"] += 10
    elif ending == "callback" and hook:
        hk = set(und.content(hook["text"]))
        later = [t for t in thoughts if t is not hook and not t["drop_reason"] and t["start"] >= hook["start"]]
        if later:
            cb = max(later[len(later) * 2 // 3:] or later, key=lambda t: (len(hk & set(und.content(t["text"]))), t["score"]))
            cb["protected"].append("ending: callback to the hook")
            cb["priority"] += 10
    # ------- ordering (arc) + opening
    order = sorted(thoughts, key=lambda t: (t["cluster"], srcs[t["src"]]["order"], t["start"]))
    opening = prof.get("opening", "hook_first")
    lifted = False
    if hook and opening in ("cold_open", "hook_first"):
        if order and order[0] is hook:
            _dec(decisions, hook["src"], hook["start"], hook["end"], "note", "opening",
                 "the strongest self-contained line already opens the programme", hook["sents"][0].get("hook", 0))
        elif hook["sents"][0].get("self_contained") and len(hook["sents"]) <= 3 and "tangent" not in hook["roles"]:
            order.remove(hook)
            order.insert(0, hook)
            lifted = True
            _dec(decisions, hook["src"], hook["start"], hook["end"], "lift", "opening",
                 f"lifted to the {'cold open' if opening == 'cold_open' else 'opening hook'}: strongest self-contained line "
                 f"(hook {hook['sents'][0].get('hook', 0)}: {', '.join(hook['sents'][0].get('hook_reasons') or [])}); it does not "
                 "lean on earlier context, and it is not repeated later", hook["sents"][0].get("hook", 0))
        else:
            _dec(decisions, hook["src"], hook["start"], hook["end"], "note", "opening",
                 "best hook not lifted: it leans on surrounding context, so moving it could misrepresent it", hook["sents"][0].get("hook", 0))
    title = brief.get("title")
    tcfg = prof.get("titles") or {}
    cards = {"title": title, "title_seconds": float(tcfg.get("card_seconds", 2.5)),
             "title_at": 0 if opening == "title_first" else 1 if (opening == "cold_open" and lifted) else None,
             "end": None, "end_seconds": float(tcfg.get("card_seconds", 2.5))}
    if title and cards["title_at"] is None and opening == "cold_open":
        cards["title_at"] = 0
    if ending == "end_card":
        cards["end"] = brief.get("end_text") or title
        if not cards["end"]:
            notes.append("profile ends on an end card but no title/end_text was given: no end card")
    # ------- selection to target
    for t in order:
        if t["drop_reason"]:
            for s in t["sents"]:
                _dec(decisions, t["src"], s["start"], s["end"], "drop", t["drop_reason"][0], t["drop_reason"][1], s.get("score", 0),
                     text=s["text"], thought=t["id"])
    cand = [t for t in order if not t["drop_reason"]]
    selected = list(cand)
    cut_line = None
    est = _layout(selected, pieces, prof, cards, srcs)[1]
    tol = max(1.5, 0.04 * target) if target else 0.0
    compressed: list[dict] = []
    if target:
        while est > target + tol:
            free = [t for t in selected if not t["protected"]] or [t for t in selected if t is not hook and len(selected) > 1]
            if not free:
                break
            victim = min(free, key=lambda t: (t["priority"], -(t["end"] - t["start"])))
            selected.remove(victim)
            compressed.append(victim)
            cut_line = victim["priority"]
            est = _layout(selected, pieces, prof, cards, srcs)[1]
        for t in sorted(compressed, key=lambda t: -t["priority"]):  # refill gaps the greedy pass opened
            if est >= target - tol:
                break
            trial = sorted(selected + [t], key=lambda x: order.index(x))
            e2 = _layout(trial, pieces, prof, cards, srcs)[1]
            if e2 <= target + tol:
                selected, est = trial, e2
                compressed.remove(t)
        if est < target - tol:
            notes.append(f"usable material ({est:.1f}s after removing duplicates, tangents and dead air) is shorter than the "
                         f"target {target:.0f}s; nothing was padded")
    for t in compressed:
        for s in t["sents"]:
            _dec(decisions, t["src"], s["start"], s["end"], "drop", "compression",
                 f"compressed to the {target:.0f}s target: thought priority {t['priority']:.1f} is below the cut line "
                 f"{cut_line:.1f} (lowest-priority material goes first; hook, ending and requested topics are protected)",
                 s.get("score", 0), text=s["text"], thought=t["id"])
    for t in selected:
        why = [f"importance {t['score']:.1f}"] + [f"role {r}" for r in t["roles"] if r != "development"] + t["protected"]
        for s in t["sents"]:
            _dec(decisions, t["src"], s["start"], s["end"], "keep", "sentence", "kept: " + ", ".join(why), s.get("score", 0),
                 text=s["text"], thought=t["id"])
    items, est = _layout(selected, pieces, prof, cards, srcs)
    items = _shot_pass(items, srcs, prof, decisions, est)
    for it in items:
        if it["kind"] == "piece" and not it.get("cutaway"):
            _dec(decisions, it["src"], it["in"], it["out"], "keep", "clip", f"programme clip ({it.get('cut') or 'first'} in)",
                 1.0, thought=it["thought"])
    _coverage(srcs, items, decisions)
    chapters = _chapters(items, cluster_labels)
    return _finish(brief, prof, ins, target, tol, items, est, decisions, notes, srcs, chapters, hook, lifted)


def _shot_pass(items: list[dict], srcs: dict, prof: dict, decisions: list, total: float) -> list[dict]:
    """Keep visible shot lengths inside the profile range: long pieces are split at word gaps (contiguous, so the sound
    is untouched) and alternate parts get a cutaway (silent/low-speech shot as full-frame pip) or a punch-in reframe.
    Pieces over low-quality picture are covered by b-roll when any is available."""
    lo, hi = prof["shot_len"]
    br = prof.get("broll") or {}
    punch = float(prof["transitions"].get("punch_in", 1.0))
    can_punch = punch > 1.0 or "punch_in" in (prof.get("allowed_effects") or [])
    punch = punch if punch > 1.0 else 1.08
    used_ranges: list[tuple] = [(it["src"], it["in"], it["out"]) for it in items if it["kind"] == "piece"]
    pool = []
    if br.get("policy") == "cutaway":
        for src, S in srcs.items():
            for sh in S["shots"]:
                q = sh.get("quality")
                if sh["speech_share"] <= SPEECH_BROLL_MAX and (q is None or q >= 0.5) and sh["duration"] >= float(br.get("min", 1.5)):
                    if not any(s == src and a < sh["end"] and sh["start"] < b for s, a, b in used_ranges):
                        pool.append({"src": src, **sh, "uses": 0})
    out: list[dict] = []
    t = 0.0
    for it in items:
        if it["kind"] != "piece":
            out.append(it)
            t += it["duration"]
            continue
        d = it["out"] - it["in"]
        x = t / max(total, 1e-6)
        mx = hi * _pace_factor(prof.get("pacing", "steady"), x)
        S = srcs[it["src"]]
        shot = _shot_at(S["shots"], it["in"], it["out"])
        bad = shot is not None and shot.get("quality") is not None and shot["quality"] < 0.5
        if d <= mx and not bad:
            out.append(it)
            t += d
            continue
        # split points: middle of the gaps between consecutive words (never inside a word)
        gaps = [round((a["end"] + b["start"]) / 2, 4) for a, b in zip(it["words"], it["words"][1:])]
        bounds = [it["in"]]
        aim = max(lo, min(mx, (lo + mx) / 2 if d > 2 * mx else d / max(1, math.ceil(d / mx))))
        while it["out"] - bounds[-1] > mx:
            cands = [g for g in gaps if g - bounds[-1] >= max(MIN_PIECE, lo * 0.6) and it["out"] - g >= MIN_PIECE]
            if not cands:
                break
            nxt = min(cands, key=lambda g: (g - bounds[-1] > mx, abs(g - bounds[-1] - aim)))
            if nxt <= bounds[-1]:
                break
            bounds.append(nxt)
        bounds.append(it["out"])
        subs = list(zip(bounds, bounds[1:]))
        for k, (a, b) in enumerate(subs):
            p = copy.deepcopy(it)
            p["in"], p["out"] = a, b
            p["words"] = [w for w in it["words"] if a <= w["start"] and w["end"] <= b]
            if k:
                p["cut"], p.pop("xfade_dur", None) = "continuous", None
                p["lo"], p["first"] = a, a
            if k < len(subs) - 1:
                p["hi"], p["last"] = b, b
            need = b - a
            want_cover = bad or (k % 2 == 1)
            choice = None
            if want_cover and pool:
                fits = [s for s in pool if s["duration"] >= need - 1e-6]
                if fits:
                    choice = min(fits, key=lambda s: (s["uses"], -(s.get("quality") or 0.7), -s["duration"]))
            if choice is not None:
                c_in = round(choice["start"] + (choice["duration"] - need) / 2, 3)
                choice["uses"] += 1
                why = (f"cover low-quality picture ({', '.join(shot.get('issues') or [])})" if bad else
                       f"cutaway: {d:.1f}s talking stretch is longer than the {mx:.1f}s shot-length target")
                p["cutaway"] = {"src": choice["src"], "in": c_in, "out": round(c_in + need, 3), "shot": choice["id"], "why": why}
                _dec(decisions, choice["src"], c_in, c_in + need, "keep", "broll",
                     f"b-roll over {it['src']} {a:.2f}-{b:.2f}s: {why} (shot quality {choice.get('quality')}, speech share "
                     f"{choice['speech_share']})", choice.get("quality") if choice.get("quality") is not None else 0.7)
            elif k % 2 == 1 and can_punch:
                p["punch"] = punch
                p["reframe"] = f"punch-in reframe: {d:.1f}s stretch exceeds the {mx:.1f}s shot-length target and no b-roll fits"
            out.append(p)
        if len(subs) == 1 and d > mx and not (pool or can_punch):
            it["note"] = f"long shot kept ({d:.1f}s): no b-roll available and the profile allows no reframing"
        t += d
    return out


def _coverage(srcs: dict, items: list[dict], decisions: list) -> None:
    """Every second of every source is accounted for: whatever is neither kept nor already explained gets a reason."""
    for src, S in srcs.items():
        dur = S["dur"]
        spans = sorted([(it["in"], it["out"]) for it in items if it["kind"] == "piece" and it["src"] == src] +
                       [(d["start"], d["end"]) for d in decisions if d["src"] == src and d["action"] in ("drop", "keep") and d["kind"] != "broll"])
        t, holes = 0.0, []
        for a, b in spans:
            if a > t + 0.02:
                holes.append((t, a))
            t = max(t, b)
        if dur > t + 0.02:
            holes.append((t, dur))
        words = [w for w in S["words"]]
        for a, b in holes:
            spoken = any(w[0] < b and a < w[1] for w in words)
            shot = _shot_at(S["shots"], a, b)
            q = shot.get("quality") if shot else None
            if spoken:
                reason, kind = "speech outside every selected thought (edge of a dropped span)", "unselected"
            elif S["sents"]:
                reason, kind = f"dead air: {b - a:.2f}s without speech between selected material", "dead_air"
            else:
                reason, kind = "not selected", "unselected"
            if shot and shot.get("issues") and not spoken:
                reason += f"; picture issues: {', '.join(shot['issues'])}"
            _dec(decisions, src, a, b, "drop", kind, reason, q if q is not None else round(b - a, 3))


def _chapters(items: list[dict], labels: dict) -> list[dict]:
    out, t, last = [], 0.0, None
    for it in items:
        if it["kind"] == "piece" and it["topic"] != last:
            out.append({"t": round(max(0.0, t - (it.get("xfade_dur") or 0.0)), 3), "topic": it["topic"],
                        "label": labels.get(it["topic"], "") or f"Part {len(out) + 1}"})
            last = it["topic"]
        t += (it["out"] - it["in"]) if it["kind"] == "piece" else it["duration"]
        if it.get("cut") == "crossfade":
            t -= it.get("xfade_dur", 0.0)
    if out:
        out[0]["t"] = 0.0
    return out


def _finish(brief, prof, ins, target, tol, items, est, decisions, notes, srcs, chapters, hook, lifted) -> dict:
    for it in items:  # drop internal bookkeeping that the timeline does not need
        for k in ("lo", "hi", "first", "last", "after"):
            it.pop(k, None)
    kept = sum(1 for d in decisions if d["action"] == "keep" and d["kind"] == "sentence")
    dropped = [d for d in decisions if d["action"] == "drop"]
    langs = sorted({S["lang"] for S in srcs.values()})
    if any(not (S["u"].get("speakers") or {}).get("available") for S in srcs.values() if S["sents"]):
        notes.append("speaker turns unknown (no diarisation labels in the analysis): J-cuts on speaker change were not used "
                     "and lower thirds carry no names")
    if not any(sh.get("measured") for S in srcs.values() for sh in S["shots"]):
        notes.append("shot quality was not measured (analysis without frame measurements): low-quality-shot rules were not applied")
    return {"version": 1, "profile": prof["name"], "profile_params": prof, "brief": {k: v for k, v in brief.items() if k != "music"},
            "instructions": ins, "target_seconds": target, "tolerance": tol, "estimated_duration": est,
            "items": items, "chapters": chapters, "decisions": decisions,
            "summary": {"sources": len(srcs), "source_seconds": round(sum(S["dur"] for S in srcs.values()), 2),
                        "programme_seconds": est, "clips": sum(1 for it in items if it["kind"] == "piece"),
                        "sentences_kept": kept, "spans_dropped": len(dropped),
                        "hook": hook["text"][:140] if hook else None, "cold_open": lifted},
            "grade": prof["grade"], "music": dict(prof["music"]), "captions": prof.get("captions"),
            "captions_enabled": prof.get("captions_enabled", True), "look": prof.get("look") or [],
            "audio_fx": "voice" if any(S["sents"] for S in srcs.values()) else "none", "lang": langs[0] if langs else "en",
            "notes": notes}


def _plan_montage(srcs, brief, prof, ins, target, decisions, notes) -> dict:
    """No transcript anywhere: a picture-led cut from measured shots (quality, duplicates, shot length)."""
    lo, hi = prof["shot_len"]
    units = []
    for src, S in srcs.items():
        groups = und.duplicate_shots(S["shots"])
        dup_drop = set()
        for g in groups:
            best = max(g, key=lambda i: (S["shots"][i].get("quality") or 0.0, S["shots"][i]["duration"]))
            for i in g:
                if i != best:
                    dup_drop.add(i)
        for sh in S["shots"]:
            q = sh.get("quality")
            if sh["id"] in dup_drop:
                _dec(decisions, src, sh["start"], sh["end"], "drop", "duplicate_shot", "near-duplicate of a better shot "
                     "(perceptual hash)", q if q is not None else 0.0)
                continue
            if q is not None and q < 0.5:
                _dec(decisions, src, sh["start"], sh["end"], "drop", "low_quality", f"low picture quality: {', '.join(sh['issues'])}", q)
                continue
            if sh["duration"] < min(lo, MIN_PIECE * 2):
                _dec(decisions, src, sh["start"], sh["end"], "drop", "too_short", f"shot {sh['duration']:.2f}s is shorter than "
                     f"the profile minimum", q if q is not None else 0.0)
                continue
            n = min(sh["duration"], hi)
            mid = sh.get("keyframe_t", (sh["start"] + sh["end"]) / 2)
            a = max(sh["start"], min(mid - n / 2, sh["end"] - n))
            fps = float(config.FPS)
            a = _snap(a, sh["start"], sh["end"] - n, fps)
            units.append({"src": src, "shot": sh, "in": a, "out": round(a + n, 4), "priority":
                          100 * (q if q is not None else 0.7) + 20 * min(1.0, (sh.get("motion") or 0.0) / 0.05)})
    selected = list(units)
    xf = float(prof["transitions"].get("dur", 0.5)) if prof["transitions"].get("crossfade_at_topics") else 0.0

    def est(sel):
        n_src_changes = sum(1 for a, b in zip(sel, sel[1:]) if a["src"] != b["src"])
        return sum(u["out"] - u["in"] for u in sel) - xf * n_src_changes

    tol = max(1.5, 0.04 * target) if target else 0.0
    if target:
        while est(selected) > target + tol and len(selected) > 1:
            v = min(selected, key=lambda u: u["priority"])
            selected.remove(v)
            _dec(decisions, v["src"], v["in"], v["out"], "drop", "compression", f"compressed to target: shot priority "
                 f"{v['priority']:.1f} (quality and motion) was the lowest", v["priority"])
    selected.sort(key=lambda u: (srcs[u["src"]]["order"], u["in"]))
    items = []
    for k, u in enumerate(selected):
        cut = None
        if k:
            cut = "crossfade" if xf and u["src"] != selected[k - 1]["src"] else "hard_cut"
        it = {"kind": "piece", "src": u["src"], "in": u["in"], "out": u["out"], "words": [], "topic": srcs[u["src"]]["order"],
              "thought": f"shot{u['shot']['id']}", "cut": cut, "speaker": None}
        if cut == "crossfade":
            it["xfade_dur"] = xf
        items.append(it)
        _dec(decisions, u["src"], u["in"], u["out"], "keep", "shot", f"picture-led montage: shot priority {u['priority']:.1f}",
             u["priority"])
    for u in units:
        if u not in selected:
            continue
        sh = u["shot"]
        for a, b in ((sh["start"], u["in"]), (u["out"], sh["end"])):
            if b - a > 0.02:
                _dec(decisions, u["src"], a, b, "drop", "trim", f"shot trimmed to the profile shot length ({hi:.1f}s max)", b - a)
    _coverage(srcs, items, decisions)
    total = est(selected)
    if target and total < target - tol:
        notes.append(f"usable shots total {total:.1f}s, shorter than the target {target:.0f}s; nothing was padded")
    notes.append("no transcript in any source: picture-led montage from measured shots; no hook, topics or sentence logic")
    return _finish(brief, prof, ins, target, tol, items, round(total, 3), decisions, notes, srcs,
                   [{"t": 0.0, "topic": 0, "label": "Part 1"}] if items else [], None, False)


# ---------------------------------------------------------------- build
def build(plan: dict, size: tuple[int, int] = (1920, 1080), music: bool = True) -> dict:
    """Timeline from a plan. Every automated decision stays editable with timeline.ops."""
    from . import project as prj
    from .timeline import ops, subs
    from .timeline.render import clip_duration, clip_starts, total_duration
    W, H = size
    tl = prj.new_timeline(W, H, plan.get("lang", "en"))
    tl["grade"], tl["audio_fx"] = plan["grade"], plan["audio_fx"]
    prof = plan["profile_params"]
    tr = prof["transitions"]
    look = [e for e in (plan.get("look") or []) if e.get("type") in set(prof.get("allowed_effects") or []) | {"letterbox", "grain", "vignette"}]
    seen_speakers: set = set()
    tcfg = prof.get("titles") or {}
    for k, it in enumerate(plan["items"]):
        if it["kind"] == "card":
            c = ops.color_clip(it["duration"], text=[{"text": it["text"], "style": "title"}] if it.get("text") else [],
                               fade_in=0.4, fade_out=0.4, label=f"{it['role']} card")
            c["notes"] = {"role": it["role"]}
        else:
            c = ops.video_clip(it["src"], it["in"], it["out"], fit="cover", focus=[0.5, 0.5], label=f"{it['thought']}")
            fx = copy.deepcopy(look)
            if it.get("punch"):
                fx.append({"type": "punch_in", "start": 0.0, "end": round(it["out"] - it["in"], 3), "scale": it["punch"]})
            if fx:
                c["effects"] = fx
            notes = {"cut": it.get("cut") or "start", "thought": it["thought"]}
            if it.get("cut") in ("j_cut", "l_cut"):
                notes["audio_overlap"] = 0.4  # speculative until docs/TIMELINE.md (WS A) defines J/L-cut keys
            if it.get("cutaway"):
                ca = it["cutaway"]
                c["pip"] = {"src": ca["src"], "in": ca["in"], "x": 0.0, "y": 0.0, "w": 1.0, "shape": "rect"}
                notes["cutaway"] = ca
            for key in ("reframe", "why_not_crossfade", "note"):
                if it.get(key):
                    notes[key] = it[key]
            c["notes"] = notes
            if it.get("cut") == "crossfade":
                c["transition"], c["xfade"], c["transition_dur"] = "crossfade", tr.get("xfade", "fade"), it["xfade_dur"]
            sp = it.get("speaker")
            if sp and tcfg.get("lower_thirds") and sp not in seen_speakers:
                seen_speakers.add(sp)
                c["text"] = [{"text": str(sp), "style": "lower_third", "start": 0.3, "end": round(min(3.5, it["out"] - it["in"]), 3)}]
        tl["clips"].append(c)
    if tl["clips"]:
        first, last = tl["clips"][0], tl["clips"][-1]
        first.setdefault("fade_in", 0.4)
        last["fade_out"] = {"fade_out": 1.5, "end_card": 0.6}.get(prof.get("ending"), 0.8)
        last["fade_out"] = min(last["fade_out"], clip_duration(last) / 2)
    if plan.get("brief", {}).get("title") and prof.get("opening") == "hook_first" and tl["clips"] and not any(
            it["kind"] == "card" and it["role"] == "title" for it in plan["items"]):
        c0 = tl["clips"][1] if len(tl["clips"]) > 1 else tl["clips"][0]
        c0.setdefault("text", []).append({"text": plan["brief"]["title"], "style": "lower_third", "start": 0.2,
                                          "end": round(min(3.0, clip_duration(c0)), 3)})
    starts = clip_starts(tl)
    total = total_duration(tl)
    # captions from the planned words (programme time)
    words = []
    pieces = [it for it in plan["items"]]
    for c, it, st in zip(tl["clips"], pieces, starts):
        if it["kind"] != "piece":
            continue
        end = st + clip_duration(c)
        for w in it.get("words") or []:
            a, b = st + (w["start"] - c["in"]), st + (w["end"] - c["in"])
            if a >= st - 1e-6 and b <= end + 1e-6 and b > a:
                words.append({"start": round(a, 3), "end": round(b, 3), "word": w["word"]})
    words.sort(key=lambda w: w["start"])
    if words:
        cues = subs.cues_from_transcript([{"start": 0, "end": 0, "text": "", "words": words}])
        for q in cues:
            q["end"] = min(q["end"], total)
            q["words"] = [w for w in words if q["start"] - 1e-3 <= w["start"] and w["end"] <= q["end"] + 0.3]
        tl["subtitles"] = [q for q in cues if q["end"] > q["start"]]
    tl["burn_subtitles"] = bool(plan.get("captions_enabled", True))
    if plan.get("captions"):
        tl["captions"] = copy.deepcopy(plan["captions"])
    tl["chapters"] = copy.deepcopy(plan.get("chapters") or [])
    tl["markers"] = [{"t": ch["t"], "label": f"chapter: {ch['label']}"} for ch in tl["chapters"]] if tcfg.get("chapters") else []
    if tcfg.get("chapter_text"):
        for ch in tl["chapters"][1:]:
            for c, st in zip(tl["clips"], starts):
                if c["type"] == "video" and abs(st - ch["t"]) < 0.05 + float(c.get("transition_dur", 0)) and not c.get("text"):
                    c["text"] = [{"text": ch["label"].title(), "style": "lower_third", "start": 0.4, "end": round(min(3.0, clip_duration(c)), 3)}]
                    break
    tl["director"] = {"profile": plan["profile"], "target_seconds": plan.get("target_seconds"),
                      "estimated_duration": plan["estimated_duration"], "notes": list(plan.get("notes") or [])}
    m = plan.get("music") or {}
    tl["music"] = None
    music_src = (plan.get("brief") or {}).get("music_src")
    if m.get("enabled") and music:
        try:
            if not music_src:
                from .audio import music as mus
                music_src = str(mus.music_bed(total + 1.0, m.get("mood", "calm")))
            tl["music"] = {"src": music_src, "volume": float(m.get("volume", 0.25)), "duck": True, "mood": m.get("mood")}
        except Exception as e:  # noqa: BLE001 - music is optional
            tl["director"]["notes"].append(f"music requested but not available: {type(e).__name__}")
    return tl


# ---------------------------------------------------------------- EDL
def _tc(t: float, fps: int) -> str:
    f = int(round(max(0.0, t) * fps))
    return f"{f // (3600 * fps):02d}:{f // (60 * fps) % 60:02d}:{f // fps % 60:02d}:{f % fps:02d}"


def edl(tl: dict, fmt: str = "json", title: str = "NEXUS MASTER") -> str:
    """Edit decision list of a timeline. `json`: one event per clip with source and record times, cut type and notes.
    `cmx3600`: classic EDL (non-drop frame; dissolves as the standard two-line C/D form; title/colour cards as BL)."""
    from .timeline.render import clip_duration, clip_starts
    fps = int(round(float(tl.get("fps", config.FPS))))
    starts = clip_starts(tl)
    events = []
    for n, (c, st) in enumerate(zip(tl["clips"], starts), 1):
        d = clip_duration(c)
        ev = {"event": n, "type": c["type"], "src": c.get("src"), "reel": "AX" if c["type"] == "video" else "BL",
              "src_in": c.get("in", 0.0), "src_out": c.get("out", d), "rec_in": round(st, 3), "rec_out": round(st + d, 3),
              "transition": "dissolve" if c.get("transition") == "crossfade" else "cut",
              "transition_dur": float(c.get("transition_dur", 0.5)) if c.get("transition") == "crossfade" else 0.0,
              "cut": (c.get("notes") or {}).get("cut"), "effects": [e.get("type") for e in c.get("effects") or []],
              "text": [t["text"] for t in c.get("text") or []], "pip": c.get("pip")}
        events.append(ev)
    if fmt == "json":
        return json.dumps({"title": title, "fps": fps, "events": events}, indent=1, ensure_ascii=False)
    if fmt != "cmx3600":
        raise ValueError(f"unknown EDL format {fmt}")
    lines = [f"TITLE: {title[:70]}", "FCM: NON-DROP FRAME", ""]
    for k, ev in enumerate(events):
        n = ev["event"]
        reel = ev["reel"]
        ch = "AA/V" if ev["type"] == "video" else "V"
        s_in, s_out = ev["src_in"], ev["src_out"]
        if ev["type"] != "video":
            s_in, s_out = 0.0, ev["rec_out"] - ev["rec_in"]
        if ev["transition"] == "dissolve" and k:
            prev = events[k - 1]
            df = int(round(ev["transition_dur"] * fps))
            p_reel = prev["reel"]
            p_out = prev["src_out"] if prev["type"] == "video" else prev["rec_out"] - prev["rec_in"]
            lines.append(f"{n:03d}  {p_reel:<8} {('AA/V' if prev['type'] == 'video' else 'V'):<5} C        "
                         f"{_tc(p_out, fps)} {_tc(p_out, fps)} {_tc(ev['rec_in'], fps)} {_tc(ev['rec_in'], fps)}")
            lines.append(f"{n:03d}  {reel:<8} {ch:<5} D    {df:03d} {_tc(s_in, fps)} {_tc(s_out, fps)} "
                         f"{_tc(ev['rec_in'], fps)} {_tc(ev['rec_out'], fps)}")
        else:
            lines.append(f"{n:03d}  {reel:<8} {ch:<5} C        {_tc(s_in, fps)} {_tc(s_out, fps)} "
                         f"{_tc(ev['rec_in'], fps)} {_tc(ev['rec_out'], fps)}")
        if ev["src"]:
            lines.append(f"* FROM CLIP NAME: {Path(ev['src']).name}")
        if ev["text"]:
            lines.append(f"* COMMENT: TEXT {' / '.join(ev['text'])[:60]}")
        if ev["cut"] and ev["cut"] not in ("start",):
            lines.append(f"* COMMENT: {ev['cut'].upper().replace('_', ' ')}")
        if ev["pip"]:
            lines.append(f"* COMMENT: CUTAWAY {Path(ev['pip']['src']).name} IN {_tc(ev['pip']['in'], fps)}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- one call
def direct(sources: list[str], analysis_by_src: dict | None, brief: dict | None = None, size: tuple[int, int] = (1920, 1080),
           analyse_missing: bool = True) -> dict:
    """Analyse what is missing, plan, build, and write both EDL forms. Optional extras (lazy, never required):
    shorts candidates (nexus.shorts), a retention check of the master (nexus.growth), platform tuning (nexus.platforms)."""
    brief = dict(brief or {})
    analyses = dict(analysis_by_src or {})
    notes: list[str] = []
    for src in sources:
        if src not in analyses:
            if not analyse_missing:
                raise KeyError(f"no analysis for {src}")
            from .media import analyze
            analyses[src] = analyze.analyze(src, transcribe=brief.get("transcribe", True))
        elif analyses[src].get("shots") is None and Path(src).exists():
            und.enrich(analyses[src], src)  # measure shots when the file is here and the analysis predates the layer
    p = plan({s: analyses[s] for s in sources}, brief)
    tl = build(p, size)
    out = {"plan": p, "master": tl, "edl": {"json": edl(tl, "json"), "cmx3600": edl(tl, "cmx3600")}, "notes": notes + p["notes"]}
    try:
        from . import growth
        rep = growth.retention_report(tl, analyses, "youtube")
        out["retention"] = {"score": rep.get("score"), "failed": [c["check"] for c in rep.get("checks", []) if not c.get("ok")]}
        notes.append(f"retention rubric (editorial rules of thumb, not a view prediction): {rep.get('score')}/100")
    except Exception as e:  # noqa: BLE001 - optional package
        notes.append(f"retention rubric not run: {type(e).__name__}")
    n = int(brief.get("shorts") or 0)
    if n:
        try:
            from . import shorts
            out["shorts"] = [{"src": s, **m} for s in sources for m in shorts.find_moments(analyses[s], top=n)][:n * max(1, len(sources))]
            out["shorts"] = sorted(out["shorts"], key=lambda m: -m.get("score", 0))[:n]
        except Exception as e:  # noqa: BLE001
            notes.append(f"shorts not produced: {type(e).__name__}")
    plats = [x for x in brief.get("platforms") or [] if x]
    if plats:
        try:
            from . import platforms  # WS F, may not exist yet
            out["platform_cuts"] = {}
            for name in plats:
                tuned, pnotes = platforms.tune(tl, name)
                out["platform_cuts"][name] = {"timeline": tuned, "notes": pnotes}
        except Exception as e:  # noqa: BLE001
            notes.append(f"platform tuning not available ({type(e).__name__}): only the master was built")
    out["notes"] = notes + p["notes"]
    return out
