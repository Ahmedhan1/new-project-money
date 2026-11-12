"""Long video -> ranked vertical shorts.

Everything here is a measurable editorial heuristic, not a prediction of views. A moment is a window of whole
transcript sentences (it never starts or ends mid-sentence) scored with explicit components:

  hook         0.35  first sentence: question, number, second person, curiosity/claim words, punchy length;
                     penalised for filler openings ("so", "um", "okay") and openings that lean on earlier context ("and", "but")
  density      0.20  distinct content words per second, relative to DENSITY_REF
  pace         0.15  words per minute inside PACE_IDEAL, falling linearly to 0 at PACE_LIMITS
  completeness 0.20  ends on a terminated sentence, bonus for a concluding line, penalty if the next sentence continues the thought
  flow         0.10  share of the window that is pause between sentences (low is better)

Ranking is greedy: the best window is taken, then the best window that does not overlap it, and so on.
Without a transcript, windows are built from scene/silence boundaries and the result says so (`basis`, `note`).
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from . import project as prj
from .timeline import ops
from .timeline.render import clip_duration, clip_starts, total_duration

# ---------------------------------------------------------------- vocabulary (editorial rules of thumb)
STOP = set("the a an and or but of to in on for with is are was were be been it this that as at by from we you they i he she "
           "have has had not so if then than there here our your their its can will just about into over more most very "
           "um uh like okay ok well yeah also do does did what which who would could should really get got going "
           "these those them us my me one all some any".split())
FILLER_OPENERS = {"so", "um", "uh", "umm", "erm", "er", "okay", "ok", "well", "like", "yeah", "alright", "right", "anyway",
                  "basically", "hmm", "now"}
BACKREF_OPENERS = {"and", "but", "because", "which", "also", "then", "or", "plus", "therefore", "however"}
PRONOUN_OPENERS = {"it", "this", "that", "these", "those", "they", "he", "she", "there"}
SECOND_PERSON = {"you", "your", "you're", "yours", "yourself", "you'll", "you've"}
CURIOSITY = {"why", "how", "secret", "mistake", "mistakes", "truth", "nobody", "never", "surprising", "surprisingly", "actually",
             "wrong", "myth", "hidden", "real", "stop", "worst", "best", "biggest", "only", "mostly", "problem", "trick", "nothing"}
CLAIM_RE = re.compile(r"\b(is|are|will|won't|can't|cannot|must|always|never|every|no one|nobody|the most|the only)\b", re.I)
NUMBER_RE = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve|twenty|hundred|thousand|million|billion|"
                       r"percent|half|double|triple)\b", re.I)
CONCLUDERS = ("so ", "that's why", "that is why", "which means", "the point", "in short", "bottom line", "remember", "the result",
              "in the end", "overall", "that's how", "that is how", "the lesson", "the takeaway")
TERMINAL = (".", "!", "?", "…")

WEIGHTS = {"hook": 0.35, "density": 0.20, "pace": 0.15, "completeness": 0.20, "flow": 0.10}
DENSITY_REF = 1.4              # distinct content words per second that counts as "dense" (score 1.0)
PACE_IDEAL = (130.0, 190.0)    # words per minute scored 1.0
PACE_LIMITS = (70.0, 250.0)    # words per minute scored 0.0
MAX_INNER_GAP = 2.0            # a pause longer than this inside a window usually means a topic change: window rejected
JUMP_CUT_GAP = 0.35            # pauses between sentences longer than this are removed by build_short
CUT_PAD_OUT, CUT_PAD_IN = 0.12, 0.10   # breathing room kept after the last word / before the next word at a jump cut
MIN_CLIP = 0.8                 # never create a jump-cut piece shorter than this
HEAD_PAD, TAIL_PAD = 0.10, 0.30
HOOK_TITLE_SECONDS = 2.5
DEFAULT_CAPTIONS = {"style": "pop", "position": "middle", "words_per_cue": 3, "highlight": "#FFD400", "uppercase": True, "font_scale": 1.0}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w'%]+", text.lower())


def content_words(text: str) -> list[str]:
    return [w for w in _tokens(text) if w not in STOP and len(w) > 2]


# ---------------------------------------------------------------- sentences
def sentences(analysis: dict) -> list[dict]:
    """Sentence units from transcript words: split on terminal punctuation. A segment end also closes a sentence unless the
    fragment is unterminated and the next segment follows within 0.6 s starting in lower case (Whisper split mid-sentence)."""
    segs = ((analysis.get("transcript") or {}).get("segments")) or []
    out: list[dict] = []
    cur: list[dict] = []

    def flush(seg_i: int) -> None:
        nonlocal cur
        if cur:
            text = " ".join(w["word"] for w in cur).strip()
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": text, "words": cur,
                        "terminal": text.rstrip("\"')]").endswith(TERMINAL), "seg": seg_i})
        cur = []

    for si, s in enumerate(segs):
        words = [w for w in (s.get("words") or []) if w.get("word", "").strip()]
        if not words:  # no word timings: spread the text over the segment proportionally to word length
            toks = s.get("text", "").split()
            total = sum(len(t) for t in toks) or 1
            t, words = s["start"], []
            for tok in toks:
                d = (s["end"] - s["start"]) * len(tok) / total
                words.append({"start": round(t, 3), "end": round(t + d, 3), "word": tok})
                t += d
        for w in words:
            cur.append({"start": float(w["start"]), "end": float(w["end"]), "word": w["word"].strip()})
            if w["word"].strip().rstrip("\"')]").endswith(TERMINAL):
                flush(si)
        nxt = segs[si + 1] if si + 1 < len(segs) else None
        joins = (cur and nxt and nxt["start"] - s["end"] < 0.6 and (nxt.get("text", "").strip()[:1] or "A").islower())
        if not joins:
            flush(si)
    flush(len(segs) - 1)
    return out


# ---------------------------------------------------------------- scoring components
def hook_score(text: str) -> tuple[float, list[str]]:
    toks = _tokens(text)
    reasons, s = [], 0.3
    if not toks:
        return 0.0, ["empty"]
    if text.strip().endswith("?"):
        s += 0.25
        reasons.append("question")
    if NUMBER_RE.search(text):
        s += 0.2
        reasons.append("number")
    if SECOND_PERSON & set(toks):
        s += 0.15
        reasons.append("second person")
    if CURIOSITY & set(toks):
        s += 0.15
        reasons.append("curiosity word")
    if CLAIM_RE.search(text):
        s += 0.1
        reasons.append("claim")
    if 4 <= len(toks) <= 16:
        s += 0.1
        reasons.append("punchy length")
    if toks[0] in FILLER_OPENERS or " ".join(toks[:2]) in ("you know", "i mean", "so um", "kind of", "sort of"):
        s -= 0.45
        reasons.append("filler opening (penalty)")
    elif toks[0] in BACKREF_OPENERS:
        s -= 0.3
        reasons.append("depends on earlier context (penalty)")
    elif toks[0] in PRONOUN_OPENERS:
        s -= 0.1
        reasons.append("opens on a pronoun (penalty)")
    return round(min(1.0, max(0.0, s)), 3), reasons


def pace_score(wpm: float) -> float:
    lo, hi = PACE_IDEAL
    a, b = PACE_LIMITS
    if lo <= wpm <= hi:
        return 1.0
    if wpm < lo:
        return round(max(0.0, (wpm - a) / (lo - a)), 3)
    return round(max(0.0, (b - wpm) / (b - hi)), 3)


def completeness_score(last: dict, nxt: dict | None) -> float:
    t = last["text"].strip()
    low = t.lower()
    s = 0.6 if t.endswith((".", "!")) else 0.3 if t.endswith("?") else 0.2
    if low.startswith(CONCLUDERS) or any(c in low for c in CONCLUDERS if c != "so "):
        s += 0.4
    if nxt is not None:
        nt = _tokens(nxt["text"])
        if nt and nt[0] in BACKREF_OPENERS:
            s -= 0.2  # the next sentence continues this thought: the window is not self-contained
    return round(min(1.0, max(0.0, s)), 3)


def clip_plan(sents: list[dict], start: float, end: float, gap: float = JUMP_CUT_GAP) -> list[tuple[float, float]]:
    """Source ranges of a short: pauses between sentences longer than `gap` are cut out (jump cuts)."""
    blocks: list[list[float]] = [[start, end]]
    forced = [False]
    for a, b in zip(sents, sents[1:]):
        # a dropped retake lies between a and b in the recording: that cut is mandatory whatever the piece length
        must = b.get("prev_end") is not None and b["prev_end"] > a["end"] + 1e-6
        if b["start"] - a["end"] <= gap and not must:
            continue
        cut_out = a["end"] + CUT_PAD_OUT
        next_in = b["start"] - CUT_PAD_IN
        if must:
            cut_out, next_in = min(cut_out, (a["end"] + b["prev_end"]) / 2), max(next_in, b["prev_end"] + 0.02)
        elif cut_out - blocks[-1][0] < MIN_CLIP or cut_out >= next_in:
            continue
        blocks[-1][1] = cut_out
        blocks.append([next_in, end])
        forced.append(must)
    if len(blocks) > 1 and blocks[-1][1] - blocks[-1][0] < MIN_CLIP and not forced[-1]:  # too-short tail piece: undo the last cut
        tail = blocks.pop()
        blocks[-1][1] = tail[1]
    return [(round(a, 3), round(b, 3)) for a, b in blocks]


def suggest_title(text: str, max_words: int = 6) -> str:
    """Deterministic hook title (<= max_words) taken from the text itself."""
    first = re.split(r"(?<=[.!?])\s+", text.strip())[0] if text.strip() else ""
    words = re.findall(r"[\w'%$€£-]+", first)
    while words and words[0].lower() in FILLER_OPENERS | BACKREF_OPENERS:
        words = words[1:]
    if not words:
        return ""
    q = first.rstrip().endswith("?")
    if len(words) > max_words:
        best, best_s = 0, -1.0
        for i in range(0, len(words) - max_words + 1):
            win = words[i:i + max_words]
            sc = sum(w.lower() not in STOP and len(w) > 2 for w in win) + 0.6 * bool(NUMBER_RE.search(" ".join(win))) \
                - 0.5 * (win[0].lower() in STOP) - 0.02 * i
            if sc > best_s:
                best, best_s = i, sc
        words = words[best:best + max_words]
        q = False
    while len(words) > 1 and words[-1].lower() in STOP:
        words = words[:-1]
    title = " ".join(words)
    return title[:1].upper() + title[1:] + ("?" if q else "")


def _llm_title(text: str, max_words: int = 6) -> str | None:
    """Optional local-LLM title, accepted only if short and built from words the moment actually contains."""
    try:
        from . import llm
        d = llm.chat_json(f"Write one hook title of at most {max_words} words for this video clip. Use only facts in the text.\n"
                          f"TEXT: {text[:1200]}", schema={"type": "object", "properties": {"title": {"type": "string"}},
                                                         "required": ["title"]}, require="title", max_tokens=60, retries=1)
        t = str(d["title"]).strip().strip('"')
    except Exception:  # model missing, slow or malformed answer: the deterministic title stands
        return None
    vocab = set(_tokens(text))
    cw = content_words(t)
    if not t or len(t.split()) > max_words or not cw or any(w not in vocab for w in cw):
        return None
    return t


# ---------------------------------------------------------------- moments
def usable_sentences(analysis: dict) -> list[dict]:
    """Sentences minus the EARLIER take of every repeated line (same rule as edit.py: keep the later take). Each keeps the
    end of the sentence before it and the start of the one after it in the original recording (`prev_end`, `next_start`,
    `gap_before`), so a dropped retake is cut out by a jump cut instead of counting as a long pause."""
    allx = sentences(analysis)
    dropped = {r["earlier"] for r in analysis.get("repetitions") or []}
    out = []
    for k, s in enumerate(allx):
        s = {**s, "prev_end": allx[k - 1]["end"] if k else None, "next_start": allx[k + 1]["start"] if k + 1 < len(allx) else None}
        if s["seg"] in dropped:
            continue
        s["gap_before"] = s["start"] - s["prev_end"] if s["prev_end"] is not None else 0.0
        out.append(s)
    return out


def _silence_inside(analysis: dict, sents: list[dict]) -> float:
    """Longest detected silence inside the sentences themselves (a long mid-sentence stall)."""
    longest = 0.0
    for s0, s1 in analysis.get("silences") or []:
        for s in sents:
            longest = max(longest, min(s["end"], s1) - max(s["start"], s0))
    return longest


def _window(sents: list[dict], i: int, j: int, dur: float) -> tuple[float, float]:
    prev_end = sents[i].get("prev_end", sents[i - 1]["end"] if i > 0 else None)
    next_start = sents[j].get("next_start", sents[j + 1]["start"] if j + 1 < len(sents) else None)
    start = max(0.0, sents[i]["start"] - HEAD_PAD, prev_end + 0.02 if prev_end is not None else 0.0)
    end = min(dur, sents[j]["end"] + TAIL_PAD, next_start - 0.02 if next_start is not None else dur)
    return round(min(start, sents[i]["start"]), 3), round(max(end, sents[j]["end"]), 3)


def _score_window(sents: list[dict], i: int, j: int, start: float, end: float) -> dict:
    win = sents[i:j + 1]
    text = " ".join(s["text"] for s in win)
    raw = end - start
    hook, reasons = hook_score(win[0]["text"])
    n_words = sum(len(s["words"]) for s in win)
    speech = sum(s["end"] - s["start"] for s in win) or raw
    density = min(1.0, len(set(content_words(text))) / max(speech, 0.1) / DENSITY_REF)
    wpm = n_words / max(raw, 0.1) * 60
    pauses = sum(max(0.0, b.get("gap_before", b["start"] - a["end"])) for a, b in zip(win, win[1:]))
    comp = {"hook": hook, "density": round(density, 3), "pace": pace_score(wpm),
            "completeness": completeness_score(win[-1], sents[j + 1] if j + 1 < len(sents) else None),
            "flow": round(max(0.0, 1.0 - 3.0 * pauses / max(raw, 0.1)), 3)}
    score = round(100 * sum(WEIGHTS[k] * v for k, v in comp.items()), 1)
    return {"text": text, "components": comp, "score": score, "wpm": round(wpm, 1), "hook_reasons": reasons}


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def _fallback_moments(analysis: dict, min_s: float, max_s: float, top: int) -> list[dict]:
    """No transcript: windows of whole scenes (or plain slices), scored by visual change rate and non-silent share."""
    dur = float(analysis["probe"]["duration"])
    scenes = [tuple(s) for s in (analysis.get("scenes") or []) if s[1] > s[0]]
    basis = "scenes" if len(scenes) > 1 else "duration"
    if basis == "duration":
        step = (min_s + max_s) / 2
        scenes = [(t, min(dur, t + step)) for t in [k * step for k in range(int(math.ceil(dur / step)))]]
    cands = []
    for i in range(len(scenes)):
        for j in range(i, len(scenes)):
            a, b = scenes[i][0], scenes[j][1]
            if b - a > max_s:
                if j == i:  # one long scene: take a max_s slice from its start
                    b = a + max_s
                else:
                    break
            if b - a < min_s and not (i == 0 and j == len(scenes) - 1 and b - a >= min(min_s, dur)):
                continue
            sil = sum(max(0.0, min(b, s1) - max(a, s0)) for s0, s1 in analysis.get("silences") or [])
            cuts = j - i
            comp = {"visual_change": round(min(1.0, cuts / max(b - a, 0.1) / 0.25), 3),
                    "sound": round(max(0.0, 1.0 - sil / max(b - a, 0.1)), 3)}
            cands.append({"start": round(a, 3), "end": round(b, 3), "components": comp,
                          "score": round(100 * (0.5 * comp["visual_change"] + 0.5 * comp["sound"]), 1)})
            if b - a >= max_s:
                break
    chosen: list[dict] = []
    for c in sorted(cands, key=lambda c: (-c["score"], c["start"])):
        if len(chosen) >= top:
            break
        if not any(_overlaps(c, o) for o in chosen):
            chosen.append(c)
    note = ("no transcript: windows follow scene/silence boundaries, so hook, completeness and sentence integrity were NOT assessed"
            if basis == "scenes" else "no transcript and no scene changes: plain time slices; content was NOT assessed")
    for k, c in enumerate(chosen, 1):
        c.update({"rank": k, "duration": round(c["end"] - c["start"], 3), "tight_duration": round(c["end"] - c["start"], 3),
                  "text": "", "title": "", "basis": basis, "note": note, "sentences": []})
    return chosen


def find_moments(analysis: dict, min_s: float = 18, max_s: float = 58, top: int = 5, use_llm: bool = False) -> list[dict]:
    """Ranked, non-overlapping candidate shorts. Each: start, end, duration, tight_duration (after jump cuts), text, score (0-100),
    components, hook_reasons, title, basis ("transcript" | "scenes" | "duration"),
    sentences [i, j] (indices into usable_sentences(analysis): repeated takes are skipped, the later take is kept)."""
    sents = usable_sentences(analysis)
    if not sents:
        return _fallback_moments(analysis, min_s, max_s, top)
    dur = float(analysis["probe"]["duration"])
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            if j > i and sents[j]["gap_before"] > MAX_INNER_GAP:
                break
            start, end = _window(sents, i, j, dur)
            if end - start > max_s:
                break
            if _silence_inside(analysis, sents[j:j + 1]) > MAX_INNER_GAP:
                break
            tight = sum(b - a for a, b in clip_plan(sents[i:j + 1], start, end))
            if tight < min_s:
                continue
            m = _score_window(sents, i, j, start, end)
            m.update({"start": start, "end": end, "duration": round(end - start, 3), "tight_duration": round(tight, 3),
                      "sentences": [i, j], "basis": "transcript"})
            cands.append(m)
    chosen: list[dict] = []
    for c in sorted(cands, key=lambda c: (-c["score"], c["start"])):
        if len(chosen) >= top:
            break
        if not any(_overlaps(c, o) for o in chosen):
            chosen.append(c)
    for k, c in enumerate(chosen, 1):
        c["rank"] = k
        c["title"] = (_llm_title(c["text"]) if use_llm else None) or suggest_title(c["text"])
    return chosen


# ---------------------------------------------------------------- programme-time words / captions
def programme_words(tl: dict, analysis_by_src: dict | None = None) -> list[dict]:
    """Spoken words in programme time: from source transcripts mapped through the clips, else from subtitle word timings."""
    out: list[dict] = []
    analysis_by_src = analysis_by_src or {}
    for c, st in zip(tl["clips"], clip_starts(tl)):
        an = analysis_by_src.get(c.get("src", "")) if c["type"] == "video" else None
        if not an or not an.get("transcript") or c.get("mute"):
            continue
        sp, end = float(c.get("speed", 1.0)), st + clip_duration(c)
        for s in an["transcript"]["segments"]:
            for w in s.get("words") or []:
                if c["in"] - 0.02 <= w["start"] and w["end"] <= c["out"] + 0.02:
                    a = max(st, st + (w["start"] - c["in"]) / sp)
                    b = min(end, st + (w["end"] - c["in"]) / sp)
                    if b > a:
                        out.append({"start": round(float(a), 3), "end": round(float(b), 3), "word": w["word"].strip()})
    if out:
        return sorted(out, key=lambda w: w["start"])
    return sorted(({"start": w["start"], "end": w["end"], "word": w["word"]} for q in tl.get("subtitles", []) for w in q.get("words") or []),
                  key=lambda w: w["start"])


def word_cues(words: list[dict], total: float, per_cue: int = 3) -> list[dict]:
    """Short word-level cues (pop/karaoke) with `words` in programme time; never negative, never past `total`."""
    cues, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= per_cue or w["word"].rstrip("\"')]").endswith(TERMINAL):
            cues.append(cur)
            cur = []
    if cur:
        cues.append(cur)
    out = []
    for k, ws in enumerate(cues):
        a = max(0.0, ws[0]["start"])
        nxt = cues[k + 1][0]["start"] if k + 1 < len(cues) else total
        b = min(total, nxt, max(ws[-1]["end"], a + 0.4))
        if b <= a:
            continue
        out.append({"start": round(a, 3), "end": round(b, 3), "text": " ".join(w["word"] for w in ws),
                    "words": [{"start": round(max(a, w["start"]), 3), "end": round(min(b, max(w["end"], w["start"] + 0.01)), 3), "word": w["word"]}
                              for w in ws]})
    return out


# ---------------------------------------------------------------- build
def _sfx_path(name: str = "whoosh") -> str | None:
    try:
        from .fx import sfx  # lazy: the effects package is optional
        return str(sfx.make(name))
    except Exception:  # not installed yet (ImportError) or generation failed: shorts work without sound accents
        return None


def build_short(src_path: str, analysis: dict, moment: dict, size: tuple[int, int] = (1080, 1920), style: dict | None = None,
                sfx: bool = False, title: str | None = None) -> dict:
    """Vertical timeline cut exactly on the moment, with jump cuts, movement, word captions, a hook title and a progress bar."""
    W, H = size
    v = analysis["probe"].get("video") or {"width": W, "height": H}
    lang = (analysis.get("transcript") or {}).get("language", "en")
    tl = prj.new_timeline(W, H, lang)
    tl["platform"] = "shorts"
    tl["audio_fx"] = "voice" if analysis["probe"].get("audio") else "none"
    landscape = v["width"] > v["height"]
    fit = "blur" if landscape and H > W else "cover"
    all_s = usable_sentences(analysis)
    sl = moment.get("sentences") or [None, None]
    i, j = sl[0], sl[1]
    win =all_s[i:j + 1] if i is not None and j is not None else [s for s in all_s if moment["start"] <= s["start"] and s["end"] <= moment["end"]]
    if win:
        blocks = clip_plan(win, moment["start"], moment["end"])
    else:  # no transcript: one clip per scene inside the window
        cuts = sorted({moment["start"], moment["end"], *[s[0] for s in analysis.get("scenes") or [] if moment["start"] + MIN_CLIP < s[0] < moment["end"] - MIN_CLIP]})
        blocks = list(zip(cuts, cuts[1:]))
    for k, (a, b) in enumerate(blocks):
        c = ops.video_clip(src_path, float(a), float(b), fit=fit, focus=[0.5, 0.5])
        d = clip_duration(c)
        if k % 2:
            c["effects"] = [{"type": "punch_in", "start": 0.0, "end": round(d, 3), "scale": 1.12}]
        else:
            c["effects"] = [{"type": "zoom", "from": 1.0, "to": 1.06}]
        if d > 6.0:  # a long unbroken piece still gets a visual change every ~4 s
            c["effects"] += [{"type": "punch_in", "start": round(t, 3), "end": round(min(d, t + 2.0), 3), "scale": 1.1}
                             for t in [4.0 * n for n in range(1, int(d // 4.0) + 1)] if t < d - 1.0]
        tl["clips"].append(c)
    title = title if title is not None else (moment.get("title") or suggest_title(moment.get("text", "")))
    if title and tl["clips"]:
        d0 = clip_duration(tl["clips"][0])
        tl["clips"][0]["text"] = [{"text": title, "style": "callout", "start": 0.0, "end": round(min(HOOK_TITLE_SECONDS, d0), 3)}]
    total = total_duration(tl)
    caps = {**DEFAULT_CAPTIONS, **(style or {})}
    words = programme_words(tl, {src_path: analysis})
    if words:
        tl["subtitles"] = word_cues(words, total, int(caps["words_per_cue"]))
        tl["captions"] = caps
    tl["overlays"] = [{"type": "progress_bar", "color": "#3EC5FF", "height": 8}]
    starts = clip_starts(tl)
    tl["markers"] = [{"t": round(t, 3), "label": "jump cut"} for t in starts[1:]]
    notes = []
    if not words:
        notes.append(moment.get("note") or "no transcript: no captions")
    if sfx and len(starts) > 1:
        p = _sfx_path("whoosh")
        if p:
            tl["audio"] += [{"src": p, "start": round(max(0.0, t - 0.12), 3), "volume": 0.35, "role": "sfx"} for t in starts[1:]]
        else:
            notes.append("sfx requested but nexus.fx.sfx is not available: no whooshes added")
    tl["source_moment"] = {"src": str(src_path), "start": moment["start"], "end": moment["end"], "score": moment.get("score"),
                           "basis": moment.get("basis", "transcript")}
    tl["growth_notes"] = notes
    return tl


def make_shorts(project, media, analysis: dict, n: int = 3, size: tuple[int, int] = (1080, 1920), sfx: bool = False,
                min_s: float = 18, max_s: float = 58, use_llm: bool = False) -> list[dict]:
    """Find the n best moments, build each short and save it on its own branch short-1..n of the project."""
    src = media["path"] if isinstance(media, dict) else str(Path(media))
    rows = []
    for k, m in enumerate(find_moments(analysis, min_s, max_s, top=n, use_llm=use_llm), 1):
        tl = build_short(src, analysis, m, size, sfx=sfx)
        branch = f"short-{k}"
        vid = project.save_timeline(tl, f"short {k}: {m['title'] or 'untitled'} ({m['start']:.1f}-{m['end']:.1f}s, score {m['score']})", branch)
        rows.append({"branch": branch, "version": vid, "duration": round(total_duration(tl), 3), "score": m["score"], "title": m["title"],
                     "start": m["start"], "end": m["end"], "basis": m["basis"], "note": m.get("note", ""), "components": m["components"]})
    return rows
