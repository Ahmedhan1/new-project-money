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
    return re.findall(r"[\w'%]+", text.lower().replace("’", "'"))


def _duration(analysis: dict) -> float:
    """Source duration from `probe.duration` (media/analyze.py) or top-level `duration` (roadmap contract), else the transcript end."""
    d = (analysis.get("probe") or {}).get("duration") or analysis.get("duration")
    if d:
        return float(d)
    segs = ((analysis.get("transcript") or {}).get("segments")) or analysis.get("segments") or []
    return max((float(x["end"]) for x in segs), default=0.0) + 0.5


def content_words(text: str) -> list[str]:
    return [w for w in _tokens(text) if w not in STOP and len(w) > 2]


# ---------------------------------------------------------------- sentences
def sentences(analysis: dict) -> list[dict]:
    """Sentence units from transcript words: split on terminal punctuation. A segment end also closes a sentence unless the
    fragment is unterminated and the next segment follows within 0.6 s starting in lower case (Whisper split mid-sentence)."""
    segs = ((analysis.get("transcript") or {}).get("segments")) or analysis.get("segments") or []
    out: list[dict] = []
    cur: list[dict] = []

    def flush(seg_i: int) -> None:
        nonlocal cur
        if cur:
            text = " ".join(w["word"] for w in cur).strip()
            labels = [w["speaker"] for w in cur if w.get("speaker")]
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": text, "words": cur,
                        "terminal": text.rstrip("\"')]").endswith(TERMINAL), "seg": seg_i,
                        "speaker": max(set(labels), key=labels.count) if labels else None})
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
            sp = w.get("speaker") or s.get("speaker")
            cur.append({"start": float(w["start"]), "end": float(w["end"]), "word": w["word"].strip(), **({"speaker": sp} if sp else {})})
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
    dur = _duration(analysis)
    scenes =[tuple(s) for s in (analysis.get("scenes") or []) if s[1] > s[0]]
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
    dur = _duration(analysis)
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


# ==================================================================================================== shorts factory v2 (GRW-2)
# select() -> context_check() -> compose() per hook variant -> factory() per platform. Everything below is deterministic and
# offline; optional local-LLM refinement is imported lazily and validated against the transcript. Scores are editorial
# heuristics (`content_potential`, `hook_strength`, `pacing`, `completeness`), never a forecast of views.
ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "16:9": (1920, 1080)}
INTENSITY = {  # editing intensity -> jump-cut gaps, punch-in frequency, caption words per cue, sound accents
    "light":  {"sentence_gap": 0.6, "word_gap": None, "punch": "first", "long_every": 6.0, "words_per_cue": 4, "sfx": False, "scale": 1.06},
    "medium": {"sentence_gap": JUMP_CUT_GAP, "word_gap": 0.8, "punch": "alternate", "long_every": 4.0, "words_per_cue": 3, "sfx": False,
               "scale": 1.12},
    "heavy":  {"sentence_gap": 0.25, "word_gap": 0.45, "punch": "every", "long_every": 2.5, "words_per_cue": 2, "sfx": True, "scale": 1.16},
}
CAPTION_STYLES = ("classic", "boxed", "pop", "karaoke")
VARIANTS = ("natural", "cold_open", "text_hook", "text_hook_b")
VARIANT_CAPTIONS = {"natural": "pop", "cold_open": "karaoke", "text_hook": "boxed", "text_hook_b": "classic"}
VARIANT_TEXT_STYLE = {"natural": "callout", "cold_open": "callout", "text_hook": "title", "text_hook_b": "kinetic"}
SCORE_NOTE = "editorial heuristics measured on the transcript; not a prediction of views or reach"
OVERLAP_TOL = 1.0          # distinct shorts may share at most this many seconds of source (window padding)
DUP_SHARE = 0.5            # share of identical sentences above which two windows count as the same content
CONTEXT_MAX_GAP = 3.0      # a pause longer than this before/after a sentence ends the context search (topic boundary)
CONTEXT_MAX_EXTEND = 3     # sentences a window may grow on each side to recover context
RANGE_SLACK = 10.0         # seconds a context extension may reach outside a requested time range (recorded in `why`)
NEEDS_CONTEXT_PENALTY = 0.6
COLD_OPEN_MAX = 7.0        # a teaser line longer than this is not a cold open
TEXT_HOOK_SECONDS = 3.0

# ---------------------------------------------------------------- context vocabulary (English; other languages: not assessed)
LEAD_FILLERS = {"so", "um", "uh", "umm", "erm", "er", "okay", "ok", "well", "yeah", "alright", "anyway", "basically", "hmm", "oh"}
DANGLING_PHRASES = ("as i said", "as i mentioned", "like i said", "as i was saying", "as we said", "as we discussed", "as mentioned",
                    "like we said", "like i mentioned", "going back to", "back to that", "to go back", "this is why", "that is why",
                    "that's why", "which is why", "this is how", "that's how", "that is how", "the other one", "the other thing",
                    "the same thing", "same thing", "that one", "this one", "in that case", "in this case", "on top of that",
                    "after that", "because of that", "because of this", "for that reason", "which means", "that means", "this means",
                    "the second one", "the first one", "the last one", "and then")
ANSWER_OPENERS = {"yes", "no", "nope", "exactly", "absolutely", "correct", "definitely", "precisely", "indeed"}
NOT_ANSWER_AFTER_NO = {"one", "matter", "longer", "more", "way", "doubt", "wonder", "need"}
PRON_DEM = {"it", "this", "that", "these", "those"}
PRON_PERS = {"he", "she", "they", "him", "her", "them", "his", "their", "he's", "she's", "they're", "they've", "he'd", "she'd", "they'd",
             "they'll", "he'll", "she'll"}
PRON_CONTRACT = {"that's", "this's", "these're", "those're", "it'll", "that'll", "it'd", "that'd"}
VERBISH = {"is", "was", "are", "were", "does", "did", "do", "has", "had", "have", "will", "would", "can", "could", "should", "means",
           "meant", "makes", "made", "works", "worked", "happened", "turned", "shows", "showed", "said", "says", "gets", "got", "goes",
           "went", "comes", "came", "sounds", "seems", "depends", "matters", "helps", "explains", "changed", "changes", "led", "leads"}
GENERIC_IT = {"important", "easy", "hard", "possible", "impossible", "worth", "true", "clear", "obvious", "likely", "unlikely", "time",
              "fine", "okay", "interesting", "funny", "crazy", "amazing", "normal", "common", "rare", "simple", "difficult", "raining",
              "estimated", "said", "known", "believed", "thought", "a", "an", "not", "never", "always", "often", "getting", "becoming"}
CONDITIONALS = ("if", "unless", "when", "only if", "as long as", "provided", "assuming", "in case")
FRAMING = ("people think", "people say", "people believe", "some say", "some people say", "some people think", "many think",
           "many people think", "you might think", "you'd think", "you would think", "the myth is", "a common myth", "the misconception",
           "it's not true that", "it is not true that", "they tell you", "they'll tell you", "everyone says", "conventional wisdom",
           "the claim is", "some claim", "critics say", "i used to think", "i used to believe", "the idea that", "we were told",
           "you've been told", "you have been told", "the common belief")
LIMITING = ("unless", "except", "only if", "as long as", "provided", "not always", "not really", "not exactly", "well actually",
            "actually no", "actually not", "not quite", "in theory", "in some cases", "only when", "but only")
CONTRAST = ("but", "however", "although", "though", "that said", "on the other hand", "then again", "yet", "still")
REVERSALS = ("that's a myth", "that is a myth", "that's not true", "that is not true", "that's wrong", "that is wrong", "which is wrong",
             "which is false", "that's false", "that is false", "is a myth", "i was wrong", "just kidding", "i'm kidding", "or so they say",
             "or so i thought", "turns out that's", "turned out to be wrong", "but that's not", "doesn't actually", "isn't actually",
             "is not actually", "that's not how", "that is not how", "that's not the case", "that is not the case", "the opposite is true",
             "none of that is true", "not true at all", "completely wrong")
SETUP_RE = re.compile(r"(here's why|here is why|the reason is|let me tell you|the answer is|here's the thing|here is the thing|"
                      r"here's how|here is how|there are (two|three|four|five|\d+) (things|reasons|ways|steps)|the trick is|it comes down to)"
                      r"\W*$", re.I)
LIST_START_RE = re.compile(r"\b(first|firstly|number one|(two|three|four|five|\d+) (things|reasons|ways|steps|mistakes))\b", re.I)
LIST_NEXT = ("second", "secondly", "third", "thirdly", "finally", "lastly", "and finally", "number two", "number three", "the next one")
QUOTE_RE = re.compile(r"[\"“”]|\b(quote|unquote)\b", re.I)
ATTRIB_RE = re.compile(r"\b(said|says|told|tells|wrote|writes|claimed|claims|according to|asked|argued|argues|put it|tweeted|stated)\b", re.I)


def _low(text: str) -> str:
    return " ".join(_tokens(text))


def _lead(text: str) -> list[str]:
    toks = _tokens(text)
    k = 0
    while k < len(toks) - 1 and toks[k] in LEAD_FILLERS:
        k += 1
    return toks[k:]


def _starts(low: str, phrases) -> str | None:
    return next((p for p in phrases if low == p or low.startswith(p + " ")), None)


def _contains(low: str, phrases) -> str | None:
    pad = f" {low} "
    return next((p for p in phrases if f" {p} " in pad), None)


def _norm(analysis: dict) -> dict:
    """Accept both the media/analyze.py shape (`probe`, `transcript.segments`, `silences`) and the roadmap contract shape
    (`duration`, `video{w,h}`, top-level `segments`, `silence`). Returns a shallow copy in the analyze.py shape."""
    an = dict(analysis or {})
    if not an.get("transcript") and an.get("segments"):
        an["transcript"] = {"segments": an["segments"], "language": an.get("language", "en")}
    probe = dict(an.get("probe") or {})
    probe["duration"] = _duration(an)
    v = dict(probe.get("video") or an.get("video") or {})
    if "w" in v and "width" not in v:
        v["width"], v["height"] = v["w"], v["h"]
    probe["video"] = v or None
    probe.setdefault("audio", an.get("audio", {"channels": 2}))
    an["probe"] = probe
    if an.get("silences") is None and an.get("silence") is not None:
        an["silences"] = an["silence"]
    return an


# ---------------------------------------------------------------- context preservation
def _pronoun_dangles(lead: list[str]) -> bool:
    if not lead:
        return False
    a, b = lead[0], lead[1] if len(lead) > 1 else ""
    c = lead[2] if len(lead) > 2 else ""
    if a in PRON_CONTRACT or a in PRON_PERS:
        return True
    if a == "it's":
        return b not in GENERIC_IT
    if a == "it":
        return b in VERBISH and c not in GENERIC_IT
    if a in PRON_DEM:
        return b in VERBISH
    return False


def _issue(kind: str, severity: str, at: str, detail: str, need: int | None) -> dict:
    return {"type": kind, "severity": severity, "at": at, "detail": detail, "need": need}


def _context_issues(sents: list[dict], i: int, j: int) -> list[dict]:
    """Problems that would make the window [i, j] misleading or confusing out of context. `need` is the sentence index the
    window would have to include to fix it (None = cannot be fixed by extending)."""
    out: list[dict] = []
    first, last = sents[i], sents[j]
    prev = sents[i - 1] if i > 0 and first.get("gap_before", 0.0) <= CONTEXT_MAX_GAP else None
    nxt = sents[j + 1] if j + 1 < len(sents) and sents[j + 1].get("gap_before", 0.0) <= CONTEXT_MAX_GAP else None
    low, lead = _low(first["text"]), _lead(first["text"])
    stripped = " ".join(lead)
    # -- start: references to something said before the window
    ph = _starts(low, DANGLING_PHRASES) or _starts(stripped, DANGLING_PHRASES)
    if ph:
        out.append(_issue("dangling_reference", "major", "start", f'opens with "{ph}", which points at something said before', i - 1 if prev else None))
    elif lead and lead[0] in BACKREF_OPENERS:
        out.append(_issue("dangling_reference", "major", "start", f'opens with "{lead[0]}", continuing an earlier thought', i - 1 if prev else None))
    elif _pronoun_dangles(lead):
        out.append(_issue("dangling_reference", "major", "start", f'opens with "{" ".join(lead[:2])}": a pronoun with no antecedent in the window',
                          i - 1 if prev else None))
    # -- start: an answer without its question
    q_idx = next((k for k in (i - 1, i - 2) if k >= 0 and sents[k]["text"].rstrip().endswith("?")
                  and all(sents[m].get("gap_before", 0.0) <= CONTEXT_MAX_GAP for m in range(k + 1, i + 1))), None)
    is_answer = bool(lead) and lead[0] in ANSWER_OPENERS and not (lead[0] == "no" and len(lead) > 1 and lead[1] in NOT_ANSWER_AFTER_NO)
    turn = prev is not None and prev.get("speaker") and first.get("speaker") and prev["speaker"] != first["speaker"]
    if is_answer:
        out.append(_issue("answer_without_question", "major", "start", f'starts with the answer "{lead[0]}"' +
                          (" and the question is just before" if q_idx is not None else "; no question found just before"), q_idx))
    elif turn and prev["text"].rstrip().endswith("?"):
        out.append(_issue("answer_without_question", "major", "start",
                          f"starts with {first['speaker']}'s answer; the question by {prev['speaker']} is just before", i - 1))
    # -- start: condition or framing that lives in the sentence before
    if prev is not None:
        pl = _low(prev["text"])
        cond = _starts(pl, CONDITIONALS)
        if lead[:1] == ["then"] or (cond and (not prev["terminal"] or prev["text"].rstrip().endswith((",", ";", ":")))):
            out.append(_issue("condition_outside", "critical", "start", f'the condition ("{prev["text"][:70]}") is in the sentence before', i - 1))
        fr = _contains(pl, FRAMING)
        if fr:
            out.append(_issue("claim_framed_outside", "critical", "start",
                              f'the sentence before frames what follows as a belief ("{fr}"); without it the claim reads as the speaker\'s own', i - 1))
    elif lead[:1] == ["then"]:
        out.append(_issue("condition_outside", "critical", "start", 'opens with "then": its condition is not in the window', None))
    # -- end: unfinished thought, undelivered setup, list cut short
    win_text = " ".join(s["text"] for s in sents[i:j + 1])
    if not last["terminal"]:
        out.append(_issue("cut_off_conclusion", "major", "end", "the last sentence is unfinished", j + 1 if nxt else None))
    elif last["text"].rstrip().endswith(":") or SETUP_RE.search(last["text"].rstrip(" .!")):
        out.append(_issue("cut_off_conclusion", "major", "end", "ends on a set-up whose pay-off is after the window", j + 1 if nxt else None))
    if nxt is not None:
        nl, nlead = _low(nxt["text"]), " ".join(_lead(nxt["text"]))
        lim = _starts(nl, LIMITING) or _starts(nlead, LIMITING)
        rev = _contains(nl, REVERSALS)
        if rev:
            out.append(_issue("negation_outside", "critical", "end", f'the next sentence reverses or rejects what was said ("{rev}")', j + 1))
        elif lim:
            out.append(_issue("qualifier_outside", "critical", "end", f'the next sentence limits the claim ("{lim} ...")', j + 1))
        elif _starts(nl, CONTRAST) or _starts(nlead, CONTRAST):
            out.append(_issue("qualifier_outside", "major", "end", f'the next sentence continues with a contrast ("{nxt["text"][:50]}")', j + 1))
        elif _starts(nlead, LIST_NEXT) and LIST_START_RE.search(win_text):
            out.append(_issue("cut_off_conclusion", "major", "end", "a list is announced and the next item is after the window", j + 1))
    # -- quoted claim separated from its attribution
    if QUOTE_RE.search(win_text) and not ATTRIB_RE.search(win_text):
        near = [k for k in (i - 1, j + 1) if 0 <= k < len(sents) and ATTRIB_RE.search(sents[k]["text"])]
        out.append(_issue("quote_without_attribution", "critical", "start" if near and near[0] < i else "end",
                          "a quoted claim is in the window but who said it is " + ("just outside" if near else "not stated nearby"),
                          near[0] if near else None))
    return out


def _span(sents: list[dict], i: int, j: int) -> tuple[float, float]:
    return sents[i]["start"] - HEAD_PAD, sents[j]["end"] + TAIL_PAD


def resolve_context(sents: list[dict], i: int, j: int, *, max_raw: float, time_range: tuple[float, float] | None = None) -> dict:
    """Grow [i, j] sentence by sentence until no fixable context problem remains (bounded by max_raw seconds, topic-boundary
    pauses and CONTEXT_MAX_EXTEND). Returns the context_check record including the final window `sentences`."""
    i0, j0 = i, j
    resolved: list[dict] = []
    lo = (time_range[0] - RANGE_SLACK) if time_range else -1e9
    hi = (time_range[1] + RANGE_SLACK) if time_range else 1e9
    blocked: dict[str, str] = {}
    for _ in range(4 * CONTEXT_MAX_EXTEND):
        moved = False
        for x in _context_issues(sents, i, j):
            need = x["need"]
            if need is None or i <= need <= j:
                continue
            ni, nj = min(i, need), max(j, need)
            a, b = _span(sents, ni, nj)
            why = None
            if i0 - ni > CONTEXT_MAX_EXTEND or nj - j0 > CONTEXT_MAX_EXTEND:
                why = f"would need more than {CONTEXT_MAX_EXTEND} extra sentences"
            elif b - a > max_raw:
                why = f"would exceed the {max_raw:.0f}s length limit"
            elif sents[ni]["start"] < lo or sents[nj]["end"] > hi:
                why = "would leave the requested time range by more than the allowed slack"
            if why:
                blocked[x["type"] + x["at"]] = why
                continue
            resolved.append({**{k: v for k, v in x.items() if k != "need"},
                             "action": f"extended {'back' if ni < i else 'forward'} to include: \"{sents[need]['text'][:80]}\""})
            i, j, moved = ni, nj, True
            break
        if not moved:
            break
    remaining = []
    for x in _context_issues(sents, i, j):
        x = {k: v for k, v in x.items() if k != "need"}
        x["unresolved_because"] = blocked.get(x["type"] + x["at"], "extending the window cannot supply it")
        remaining.append(x)
    bad = [x for x in remaining if x["severity"] in ("major", "critical")]
    meaning_risk = any(x["severity"] == "critical" for x in bad)
    status = "needs_context" if bad else "extended" if (i, j) != (i0, j0) else "ok"
    why = [f"{x['type']} at {x['at']}: {x['detail']} -> {x['action']}" for x in resolved]
    why += [f"{x['type']} at {x['at']}: {x['detail']} (unresolved: {x['unresolved_because']})" for x in bad]
    if meaning_risk:
        why.append("cutting here could change what the speaker meant: this window is never produced as a short")
    if time_range and (sents[i]["start"] < time_range[0] or sents[j]["end"] > time_range[1]):
        why.append("context extension reaches outside the requested time range")
    return {"status": status, "ok": not bad, "meaning_risk": meaning_risk, "issues": bad, "notes": [x for x in remaining if x not in bad],
            "resolved": resolved, "sentences": [i, j], "original_sentences": [i0, j0],
            "added_before": i0 - i, "added_after": j - j0, "why": why or ["no dangling reference, cut-off conclusion, outside qualifier or "
                                                                          "separated quote detected (English rule set)"]}


def context_check(analysis: dict, moment: dict, *, max_s: float | None = None) -> dict:
    """Public wrapper: context check (and the extension it would make) for a moment from find_moments/select."""
    an = _norm(analysis)
    sents = usable_sentences(an)
    sl = moment.get("sentences") or []
    if not sents or len(sl) != 2:
        return {"status": "not_assessed", "ok": False, "meaning_risk": False, "issues": [], "notes": [], "resolved": [],
                "sentences": sl, "why": ["no transcript: context was NOT assessed"]}
    return resolve_context(sents, sl[0], sl[1], max_raw=max_s or max(60.0, moment.get("duration", 0) * 1.5 + 10))


# ---------------------------------------------------------------- selection
class Selection(list):
    """List of moments that also carries `notes` (filters that could not be applied, refusals), `rejected` candidates with the
    reason, and the `filters` used. `as_dict()` is the JSON form for the API."""

    def __init__(self, items=(), notes=None, rejected=None, filters=None):
        super().__init__(items)
        self.notes = list(notes or [])
        self.rejected = list(rejected or [])
        self.filters = dict(filters or {})

    def as_dict(self) -> dict:
        return {"items": list(self), "notes": self.notes, "rejected": self.rejected, "filters": self.filters}


def _weights(weights: dict | None) -> dict:
    if not weights:
        return dict(WEIGHTS)
    w = {k: max(0.0, float(weights.get(k, 0.0) or 0.0)) for k in WEIGHTS}
    tot = sum(w.values())
    return {k: v / tot for k, v in w.items()} if tot > 0 else dict(WEIGHTS)


def _stem(t: str) -> str:
    for suf in ("ies", "es", "s", "ing", "ed"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def _topic_terms(topic) -> list[str]:
    if not topic:
        return []
    raw = topic if isinstance(topic, (list, tuple, set)) else [topic]
    terms = []
    for t in raw:
        toks = [x for x in _tokens(str(t)) if x not in STOP]
        terms += toks or _tokens(str(t))
    return list(dict.fromkeys(_stem(t) for t in terms))


def _topic_spans(an: dict, topic) -> list[tuple[float, float, str]]:
    """Spans from analysis['topics'] (written by another workstream; shape tolerated, absence is fine) whose label or keywords
    match the query."""
    terms = set(_topic_terms(topic))
    out = []
    for t in an.get("topics") or []:
        if not isinstance(t, dict):
            continue
        words = set(_topic_terms([t.get("label") or t.get("name") or t.get("topic") or "", *(t.get("keywords") or [])]))
        spans = t.get("spans") or ([(t["start"], t["end"])] if "start" in t and "end" in t else [])
        if terms & words:
            out += [(float(a), float(b), t.get("label") or t.get("name") or t.get("topic") or "") for a, b in spans]
    return out


def _topic_match(text: str, start: float, end: float, terms: list[str], spans: list) -> dict | None:
    toks = {_stem(t) for t in _tokens(text)}
    hits = [t for t in terms if t in toks]
    over = [lab for a, b, lab in spans if min(b, end) - max(a, start) >= 0.5 * (end - start)]
    if not hits and not over:
        return None
    rel = len(hits) / max(len(terms), 1)
    if over:
        rel = max(rel, 1.0)
    return {"terms": hits, "relevance": round(rel, 3), "source": "analysis.topics" if over else "lexical", "topics": over}


def editorial_scores(m: dict) -> dict:
    """0-100 editorial heuristics with honest names. content_potential is the weighted ranking score."""
    c = m.get("components") or {}
    if "hook" not in c:  # no transcript: only picture/sound measures exist
        return {"content_potential": m.get("score"), "hook_strength": None, "pacing": round(100 * c.get("visual_change", 0), 1),
                "completeness": None, "note": SCORE_NOTE + "; no transcript: hook and completeness not assessed"}
    comp = 100 * c.get("completeness", 0)
    if m.get("needs_context"):
        comp *= NEEDS_CONTEXT_PENALTY
    return {"content_potential": m.get("score"), "hook_strength": round(100 * c["hook"], 1),
            "pacing": round(100 * (0.6 * c.get("pace", 0) + 0.4 * c.get("flow", 0)), 1), "completeness": round(comp, 1), "note": SCORE_NOTE}


def features_of(moment: dict) -> dict:
    """Feature vector of a moment for growth.calibrate (the editorial components, 0-1)."""
    return {k: float(v) for k, v in (moment.get("components") or {}).items()}


def _sig(s: dict) -> str:
    return " ".join(_tokens(s["text"]))


def _ov(a: dict, b: dict) -> float:
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def _speaker_share(win: list[dict]) -> dict:
    tot: dict[str, float] = {}
    for s in win:
        if s.get("speaker"):
            tot[s["speaker"]] = tot.get(s["speaker"], 0.0) + s["end"] - s["start"]
    t = sum(tot.values()) or 1.0
    return {k: round(v / t, 3) for k, v in sorted(tot.items(), key=lambda kv: -kv[1])}


def select(analysis: dict, *, time_range: tuple[float, float] | None = None, topic=None, speaker: str | None = None,
           durations=(30, 45, 60), top: int = 10, weights: dict | None = None, overlap_tol: float = OVERLAP_TOL,
           include_flagged: bool = False, use_llm: bool = False) -> Selection:
    """Candidate shorts across the whole source. One entry per idea (a starting sentence), each with duration `options`
    (one per target in `durations`, context-checked); the primary option is the best-scoring one. Distinct ideas never share
    more than `overlap_tol` seconds of source and never repeat the same sentences. Filters: `time_range` (seconds),
    `topic` (keywords, matched lexically; analysis['topics'] used when present), `speaker` (only with speaker labels)."""
    an = _norm(analysis)
    filters = {"time_range": list(time_range) if time_range else None, "topic": topic, "speaker": speaker, "durations": list(durations)}
    notes: list[str] = []
    sents = usable_sentences(an)
    dur = _duration(an)
    targets = sorted({float(d) for d in durations if float(d) > 0}) or [45.0]
    if speaker is not None:
        labels = sorted({s["speaker"] for s in sents if s.get("speaker")})
        if not labels:
            return Selection([], notes=["speaker labels not available: this analysis has no speaker diarisation, so moments cannot be "
                                        "filtered by speaker (nothing was guessed)"], filters=filters)
        if speaker not in labels:
            return Selection([], notes=[f"speaker {speaker!r} not found; labels in this analysis: {labels}"], filters=filters)
    if not sents:
        if topic:
            return Selection([], notes=["no transcript: topic filtering needs words, nothing was selected"], filters=filters)
        ms = _fallback_moments(an, min(targets) * 0.6, max(targets) * 1.15, max(top, 1) * 3)
        if time_range:
            ms = [m for m in ms if m["start"] >= time_range[0] - 1e-6 and m["end"] <= time_range[1] + 1e-6]
        ms = ms[:top]
        for k, m in enumerate(ms, 1):
            m.update({"rank": k, "options": [], "needs_context": False, "speaker": None, "topic_match": None,
                      "context_check": {"status": "not_assessed", "ok": False, "meaning_risk": False, "issues": [], "resolved": [],
                                        "why": ["no transcript: context was NOT assessed"]}})
            m["scores"] = editorial_scores(m)
        return Selection(ms, notes=[ms[0]["note"]] if ms else ["no transcript and no usable window"], filters=filters)
    lo, hi = (float(time_range[0]), float(time_range[1])) if time_range else (0.0, dur)
    idx = [k for k, s in enumerate(sents) if s["start"] >= lo - 1e-6 and s["end"] <= hi + 1e-6]
    if not idx:
        return Selection([], notes=[f"no complete sentence inside {lo:.1f}-{hi:.1f}s"], filters=filters)
    terms = _topic_terms(topic)
    tspans = _topic_spans(an, topic) if topic else []
    if topic and an.get("topics") is None:
        notes.append("analysis has no 'topics': topic matched lexically against the transcript")
    W = _weights(weights)
    max_raw = max(targets) * 1.25 + 5.0
    cache: dict = {}

    def scored(i: int, j: int) -> dict:
        if (i, j) not in cache:
            a, b = _window(sents, i, j, dur)
            if time_range and sents[i]["start"] >= lo and sents[j]["end"] <= hi:
                a, b = max(a, lo), min(b, hi)
            m = _score_window(sents, i, j, a, b)
            m["score"] = round(100 * sum(W[k] * m["components"][k] for k in W), 1)
            tight = sum(y - x for x, y in clip_plan(sents[i:j + 1], a, b))
            m.update({"start": a, "end": b, "duration": round(b - a, 3), "tight_duration": round(tight, 3), "sentences": [i, j],
                      "basis": "transcript"})
            cache[(i, j)] = m
        return cache[(i, j)]

    ideas, rejected = [], []
    last = idx[-1]
    for i in idx:
        opts: dict[tuple[int, int], dict] = {}
        for D in targets:
            best = None
            for j in range(i, last + 1):
                if j > i and sents[j]["gap_before"] > MAX_INNER_GAP:
                    break
                a, b = _window(sents, i, j, dur)
                if b - a > D * 1.2 + 2.0:
                    break
                if _silence_inside(an, sents[j:j + 1]) > MAX_INNER_GAP:
                    break
                m = scored(i, j)
                if m["tight_duration"] < D * 0.6:
                    continue
                fit = m["score"] - 40.0 * abs(m["tight_duration"] - D) / D
                if best is None or fit > best[0]:
                    best = (fit, j)
            if best is None:
                continue
            cc = resolve_context(sents, i, best[1], max_raw=max_raw, time_range=time_range)
            ci, cj = cc["sentences"]
            m = dict(scored(ci, cj))
            if (ci, cj) in opts:
                opts[(ci, cj)]["targets"].append(D)
                continue
            m["targets"], m["context_check"] = [D], cc
            m["needs_context"] = not cc["ok"]
            m["rank_score"] = m["score"] * (NEEDS_CONTEXT_PENALTY if m["needs_context"] else 1.0)
            win = sents[ci:cj + 1]
            m["speakers"] = _speaker_share(win)
            m["speaker"] = next(iter(m["speakers"]), None)
            if speaker is not None and m["speakers"].get(speaker, 0.0) < 0.5:
                continue
            if topic:
                tm = _topic_match(m["text"], m["start"], m["end"], terms, tspans)
                if tm is None:
                    continue
                m["topic_match"] = tm
                m["rank_score"] += 10.0 * tm["relevance"]
            else:
                m["topic_match"] = None
            opts[(ci, cj)] = m
        if opts:
            ideas.append(sorted(opts.values(), key=lambda o: o["tight_duration"]))
    chosen: list[dict] = []
    chosen_sigs: list[set] = []
    for opts in sorted(ideas, key=lambda os: -max(o["rank_score"] for o in os)):
        if len(chosen) >= top:
            break
        risky = [o for o in opts if o["context_check"]["meaning_risk"]]
        if risky and not include_flagged:
            if len(risky) == len(opts) and len(rejected) < 25:
                rejected.append({"start": opts[0]["start"], "end": opts[-1]["end"], "text": opts[0]["text"][:120],
                                 "reason": "meaning risk: " + "; ".join(opts[0]["context_check"]["why"])})
            opts = [o for o in opts if not o["context_check"]["meaning_risk"]]
        opts = [o for o in opts if not any(_ov(o, k) > overlap_tol for c in chosen for k in c["options"])]
        if not opts:
            continue
        prim = max(opts, key=lambda o: (o["rank_score"], -abs(o["tight_duration"] - targets[len(targets) // 2])))
        sig = {_sig(s) for s in sents[prim["sentences"][0]:prim["sentences"][1] + 1]}
        dup = next((k for k, cs in enumerate(chosen_sigs) if len(sig & cs) / max(min(len(sig), len(cs)), 1) >= DUP_SHARE), None)
        if dup is not None:
            if len(rejected) < 25:
                rejected.append({"start": prim["start"], "end": prim["end"], "text": prim["text"][:120],
                                 "reason": f"near-duplicate of selected moment {dup + 1} (same sentences)"})
            continue
        moment = {k: v for k, v in prim.items() if k != "targets"}
        moment["target_duration"] = min(prim["targets"], key=lambda d: abs(d - prim["tight_duration"]))
        moment["options"] = [{"target": min(o["targets"], key=lambda d: abs(d - o["tight_duration"])), "start": o["start"], "end": o["end"],
                              "duration": o["duration"], "tight_duration": o["tight_duration"], "sentences": o["sentences"],
                              "score": o["score"], "context_status": o["context_check"]["status"], "primary": o is prim}
                             for o in opts]
        chosen.append(moment)
        chosen_sigs.append(sig)
    chosen.sort(key=lambda m: (-m["rank_score"], m["start"]))
    for k, m in enumerate(chosen, 1):
        m["rank"] = k
        m["title"] = (_llm_title(m["text"]) if use_llm else None) or suggest_title(m["text"])
        m["scores"] = editorial_scores(m)
    if speaker is not None and not chosen:
        notes.append(f"no window where {speaker!r} speaks at least half of the time")
    if topic and not chosen:
        notes.append(f"no window mentions {terms}")
    return Selection(chosen, notes=notes, rejected=rejected, filters=filters)


# ---------------------------------------------------------------- cut plan with word timings (dead-space removal)
def cut_plan(win: list[dict], start: float, end: float, sentence_gap: float = JUMP_CUT_GAP, word_gap: float | None = None) -> list[tuple[float, float]]:
    """clip_plan (pauses between sentences, mandatory retake cuts) plus cuts at pauses between WORDS longer than word_gap."""
    blocks = clip_plan(win, start, end, gap=sentence_gap)
    if not word_gap:
        return blocks
    words = [w for s in win for w in s["words"]]
    out: list[tuple[float, float]] = []
    for a, b in blocks:
        ws = [w for w in words if a - 1e-6 <= w["start"] and w["end"] <= b + 1e-6]
        cur = a
        for w, nx in zip(ws, ws[1:]):
            if nx["start"] - w["end"] <= word_gap:
                continue
            cut_out, next_in = w["end"] + CUT_PAD_OUT, nx["start"] - CUT_PAD_IN
            if cut_out - cur < MIN_CLIP or b - next_in < MIN_CLIP or cut_out >= next_in:
                continue
            out.append((round(cur, 3), round(cut_out, 3)))
            cur = next_in
        out.append((round(cur, 3), round(b, 3)))
    return out


# ---------------------------------------------------------------- reframing
def _focus_entries(an: dict) -> list[dict]:
    """Subject positions from the analysis if a workstream recorded them: `focus` / `quality.focus` ([{start,end,x,y}]) or
    `faces` / `quality.faces` ([{t|start,end, x,y[,w,h] | boxes:[...]}]); coordinates 0-1 or pixels."""
    q = an.get("quality") if isinstance(an.get("quality"), dict) else {}
    v = an["probe"].get("video") or {}
    vw, vh = float(v.get("width") or 1), float(v.get("height") or 1)
    out = []
    for e in (an.get("focus") or q.get("focus") or []):
        if isinstance(e, dict) and "x" in e:
            out.append({"start": float(e.get("start", e.get("t", 0.0))), "end": float(e.get("end", e.get("t", 0.0)) or 0.0),
                        "xs": [float(e["x"])], "ys": [float(e.get("y", 0.5))]})
    for e in (an.get("faces") or q.get("faces") or []):
        if not isinstance(e, dict):
            continue
        boxes = e.get("boxes") or [e]
        xs, ys = [], []
        for bx in boxes:
            if "x" not in bx:
                continue
            x, y = float(bx["x"]) + float(bx.get("w", 0)) / 2, float(bx.get("y", 0.5)) + float(bx.get("h", 0)) / 2
            xs.append(x / vw if x > 1.5 else x)
            ys.append(y / vh if y > 1.5 else y)
        if xs:
            t0 = float(e.get("start", e.get("t", 0.0)))
            out.append({"start": t0, "end": float(e.get("end", t0 + 0.5)), "xs": xs, "ys": ys})
    for e in out:
        e["end"] = max(e["end"], e["start"] + 0.5)
    return out


def reframe_for(an: dict, a: float, b: float, W: int, H: int, fit: str = "auto") -> tuple[str, list[float], str]:
    """(fit mode, crop focus for render._fit, note) for source range [a, b] shown at W x H."""
    v = an["probe"].get("video") or {}
    sw, sh = float(v.get("width") or W), float(v.get("height") or H)
    narrower = W / H < sw / sh - 0.01
    ents = [e for e in _focus_entries(an) if e["start"] < b and a < e["end"]]
    if fit in ("cover", "blur", "contain") and not ents:
        return fit, [0.5, 0.5], f"fit={fit} as requested, centred"
    if not narrower:
        return (fit if fit != "auto" else "cover"), [0.5, 0.5], "target is not narrower than the source: no subject crop needed"
    if not ents:
        return (fit if fit != "auto" else "cover"), [0.5, 0.5], ("no subject position data in the analysis: centre crop (no vision "
                                                                 "model is installed, subjects are not tracked)")
    spread = max(max(e["xs"]) - min(e["xs"]) for e in ents)
    cx = statistics_mean([x for e in ents for x in e["xs"]])
    cy = statistics_mean([y for e in ents for y in e["ys"]])
    if fit == "auto" and spread > 0.35:
        return "blur", [0.5, 0.5], f"wide two-person shot (subjects {spread:.2f} of the frame apart): blurred-background fit keeps both"
    s = max(W / sw, H / sh)
    iw, ih = sw * s, sh * s
    fx = min(1.0, max(0.0, (cx * iw - W / 2) / (iw - W))) if iw > W + 1 else 0.5
    fy = min(1.0, max(0.0, (cy * ih - H / 2) / (ih - H))) if ih > H + 1 else 0.5
    mode = fit if fit != "auto" else "cover"
    return mode, [round(fx, 3), round(fy, 3)], f"crop follows the subject position recorded in the analysis (x={cx:.2f})"


def statistics_mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.5


# ---------------------------------------------------------------- compose one short (one hook variant)
def _cold_open_line(sents: list[dict], i: int, j: int) -> int | None:
    """The strongest line inside (i, j] that can stand alone: no dangling start and nothing right after it that qualifies or
    reverses it. None when no line beats the natural opening clearly."""
    base = hook_score(sents[i]["text"])[0]
    best, best_s = None, base + 0.1
    for k in range(i + 1, j + 1):
        s = sents[k]
        if s["end"] - s["start"] > COLD_OPEN_MAX or not s["terminal"]:
            continue
        hs = hook_score(s["text"])[0]
        if hs <= best_s:
            continue
        alone = [x for x in _context_issues(sents, k, k) if x["at"] == "start" or x["severity"] == "critical"]
        alone = [x for x in alone if not (x["at"] == "end" and x["need"] is not None and x["need"] <= j and x["severity"] != "critical")]
        if alone:
            continue
        best, best_s = k, hs
    return best


def _hook_titles(text: str, natural: str, use_llm: bool = False) -> list[str]:
    from . import growth  # lazy: growth imports this module
    out = [natural] if natural else []
    for h in growth.hook_variants(text, n=6, use_llm=use_llm):
        t = h["title"]
        if all(set(_tokens(t)) != set(_tokens(o)) for o in out):
            out.append(t)
    while len(out) < 4:
        out.append(f"{natural or 'Watch this'} ({len(out) + 1})")
    return out


def plan_variants(analysis: dict, moment: dict, n: int = 3, use_llm: bool = False) -> list[dict]:
    """Up to n hook variants for a moment: natural start, cold open (only when the context check passed), on-screen text hooks.
    Each: {"name","title","caption_style","text_style","cold_open": sentence index | None, "why"}."""
    an = _norm(analysis)
    sents = usable_sentences(an)
    sl = moment.get("sentences") or []
    titles = _hook_titles(moment.get("text", ""), moment.get("title") or suggest_title(moment.get("text", "")), use_llm)
    cc = moment.get("context_check") or {}
    k = None
    why_no_cold = None
    if not sents or len(sl) != 2:
        why_no_cold = "no transcript: cold open not possible"
    elif not cc.get("ok"):
        why_no_cold = "context check did not pass: no cold open (a line out of order could mislead)"
    else:
        k = _cold_open_line(sents, sl[0], sl[1])
        if k is None:
            why_no_cold = "no line in the moment is clearly stronger than its opening and safe to show first"
    plan = [{"name": "natural", "title": titles[0], "cold_open": None, "why": "starts where the speaker starts; title card from the moment"}]
    if k is not None:
        plan.append({"name": "cold_open", "title": titles[1], "cold_open": k,
                     "why": f"teases the strongest line (\"{sents[k]['text'][:60]}\") first, then cuts back to the start; context check passed"})
    plan.append({"name": "text_hook", "title": titles[2 if k is not None else 1], "cold_open": None,
                 "why": "natural start with a large on-screen text hook from growth.hook_variants"})
    plan.append({"name": "text_hook_b", "title": titles[3 if k is not None else 2], "cold_open": None,
                 "why": "second text-hook wording for an A/B test"})
    for p in plan:
        p["caption_style"] = VARIANT_CAPTIONS[p["name"]]
        p["text_style"] = VARIANT_TEXT_STYLE[p["name"]]
        if p["name"] == "natural" and why_no_cold:
            p["note"] = why_no_cold
    return plan[:max(1, n)]


def _clip_effects(k: int, d: float, P: dict) -> list[dict]:
    if d > P["long_every"] * 1.5:  # long piece: alternating short punch-ins, never stacked on a full-length one
        eff = [{"type": "zoom", "from": 1.0, "to": 1.04}]
        t = P["long_every"]
        while t < d - 1.0:
            eff.append({"type": "punch_in", "start": round(t, 3), "end": round(min(d, t + P["long_every"] / 2), 3), "scale": P["scale"]})
            t += P["long_every"]
        return eff
    if P["punch"] == "first":
        return [{"type": "zoom", "from": 1.0, "to": P["scale"]}] if k == 0 else []
    if P["punch"] == "alternate":
        return [{"type": "punch_in", "start": 0.0, "end": round(d, 3), "scale": P["scale"]}] if k % 2 else [{"type": "zoom", "from": 1.0, "to": 1.06}]
    return [{"type": "punch_in", "start": 0.0, "end": round(d, 3), "scale": P["scale"] if k % 2 else 1.08}]


def first_seconds(tl: dict, secs: float = 3.0) -> dict:
    """What a viewer gets in the first `secs` seconds: source ranges, on-screen text, caption style. Used to prove that
    variants are real A/B candidates."""
    ranges, texts = [], []
    for c, st in zip(tl["clips"], clip_starts(tl)):
        if st >= secs:
            break
        d = clip_duration(c)
        if c["type"] == "video":
            take = min(d, secs - st) * float(c.get("speed", 1.0))
            ranges.append([c.get("src"), round(c["in"], 3), round(c["in"] + take, 3)])
        texts += [[t.get("style"), t.get("text")] for t in c.get("text") or [] if st + float(t.get("start", 0)) < secs]
    words = [w["word"] for q in tl.get("subtitles") or [] for w in q.get("words") or [] if w["start"] < secs]
    return {"source": ranges, "text": texts, "caption_style": (tl.get("captions") or {}).get("style") if tl.get("subtitles") else None,
            "words": words}


def compose(src: str, analysis: dict, moment: dict, *, variant: dict | str | None = None, aspect: str = "9:16",
            size: tuple[int, int] | None = None, intensity: str = "medium", captions=None, music=None, lang: str | None = None,
            brand: dict | None = None, fit: str = "auto", sfx: bool | None = None) -> dict:
    """One short as a timeline. `variant` from plan_variants (or a name). Controls: aspect 9:16|1:1|4:5|16:9 (or size),
    intensity light|medium|heavy, captions (style name | dict | False), music (path | {"src","volume"} | None),
    lang (target language tag, passed through), brand {"primary","accent","watermark","font_scale"}, fit auto|cover|blur|contain."""
    an = _norm(analysis)
    if intensity not in INTENSITY:
        raise ValueError(f"intensity must be one of {list(INTENSITY)}")
    P = INTENSITY[intensity]
    if isinstance(variant, str) or variant is None:
        variant = {"name": variant or "natural", "title": moment.get("title") or suggest_title(moment.get("text", "")), "cold_open": None}
    name = variant["name"]
    W, H = size or ASPECTS.get(aspect) or ASPECTS["9:16"]
    src_lang = (an.get("transcript") or {}).get("language", "en")
    tl = prj.new_timeline(W, H, src_lang)
    tl["audio_fx"] = "voice" if an["probe"].get("audio") else "none"
    notes: list[str] = []
    all_s = usable_sentences(an)
    sl = moment.get("sentences") or [None, None]
    i, j = sl[0], sl[1]
    win = all_s[i:j + 1] if i is not None and j is not None else []
    if win:
        blocks = cut_plan(win, moment["start"], moment["end"], P["sentence_gap"], P["word_gap"])
    else:
        cuts = sorted({moment["start"], moment["end"], *[s[0] for s in an.get("scenes") or [] if moment["start"] + MIN_CLIP < s[0] < moment["end"] - MIN_CLIP]})
        blocks = list(zip(cuts, cuts[1:]))
    reframe_notes = []

    def clip(a: float, b: float) -> dict:
        mode, focus, why = reframe_for(an, a, b, W, H, fit)
        reframe_notes.append(why)
        return ops.video_clip(src, float(a), float(b), fit=mode, focus=focus)

    for k, (a, b) in enumerate(blocks):
        c = clip(a, b)
        c["effects"] = _clip_effects(k, clip_duration(c), P)
        tl["clips"].append(c)
    title = variant.get("title") or ""
    text_style = variant.get("text_style") or VARIANT_TEXT_STYLE.get(name, "callout")
    teaser_len = 0.0
    if variant.get("cold_open") is not None and win:
        s = all_s[variant["cold_open"]]
        a, b = max(moment["start"], s["start"] - 0.05), min(moment["end"], s["end"] + CUT_PAD_OUT)
        t = clip(a, b)
        teaser_len = clip_duration(t)
        t["effects"] = [{"type": "punch_in", "start": 0.0, "end": round(teaser_len, 3), "scale": 1.15}]
        tl["clips"].insert(0, t)
        if len(tl["clips"]) > 1:
            tl["clips"][1]["effects"] = [{"type": "flash", "at": 0.0}] + list(tl["clips"][1].get("effects") or [])
    if title and tl["clips"]:
        d0 = clip_duration(tl["clips"][0])
        hold = TEXT_HOOK_SECONDS if name.startswith("text_hook") else HOOK_TITLE_SECONDS
        tl["clips"][0]["text"] = [{"text": title, "style": text_style, "start": 0.0, "end": round(min(hold, d0), 3)}]
    total = total_duration(tl)
    # captions
    pinned = captions is not None
    if captions is False:
        caps = None
    else:
        caps = {**DEFAULT_CAPTIONS, "words_per_cue": P["words_per_cue"], "style": VARIANT_CAPTIONS.get(name, "pop")}
        if isinstance(captions, str):
            if captions not in CAPTION_STYLES:
                raise ValueError(f"caption style must be one of {CAPTION_STYLES}")
            caps["style"] = captions
        elif isinstance(captions, dict):
            caps.update(captions)
    words = programme_words(tl, {src: an})
    if caps and words:
        tl["subtitles"] = word_cues(words, total, int(caps["words_per_cue"]))
        tl["captions"] = caps
    elif caps:
        notes.append(moment.get("note") or "no transcript: no captions")
    # brand identity -> timeline keys (docs/PHASE3_PLAN.md contract)
    bar = {"type": "progress_bar", "color": "#3EC5FF", "height": 8}
    tl["overlays"] = [bar]
    if brand:
        if brand.get("primary"):
            bar["color"] = brand["primary"]
        if tl.get("captions") is not None:
            if brand.get("accent"):
                tl["captions"]["highlight"] = brand["accent"]
            if brand.get("font_scale"):
                tl["captions"]["font_scale"] = float(brand["font_scale"])
                tl["subtitle_style"] = {"size": round(0.048 * float(brand["font_scale"]), 4)}  # honoured by today's ASS renderer
        if brand.get("watermark"):
            tl["overlays"].append({"type": "watermark", "text": str(brand["watermark"]), "color": brand.get("primary", "#FFFFFF")})
        tl["brand"] = dict(brand)
    # music, sound accents, language tag
    if music:
        m = music if isinstance(music, dict) else {"src": str(music)} if isinstance(music, (str, Path)) else None
        if m and m.get("src"):
            tl["music"] = {"volume": 0.25, "duck": True, **m}
        else:
            notes.append("music on but no music file was given: no music bed added (NEXUS ships no music)")
    starts = clip_starts(tl)
    if (P["sfx"] if sfx is None else sfx) and len(starts) > 1:
        p = _sfx_path("whoosh")
        if p:
            tl["audio"] += [{"src": p, "start": round(max(0.0, t - 0.12), 3), "volume": 0.3, "role": "sfx"} for t in starts[1:]]
        else:
            notes.append("sfx requested but nexus.fx.sfx is not available: no whooshes added")
    if lang:
        tl["target_lang"] = lang
        if lang.split("-")[0] != src_lang.split("-")[0]:
            notes.append(f"target language {lang} recorded only: captions are in the source language ({src_lang}); translation is LANG-1's job")
    tl["markers"] = [{"t": round(t, 3), "label": "jump cut"} for t in starts[1:]]
    if teaser_len:
        tl["markers"][0]["label"] = "cold open -> back to the start"
    tl["source_moment"] = {"src": str(src), "start": moment["start"], "end": moment["end"], "score": moment.get("score"),
                           "basis": moment.get("basis", "transcript"), "sentences": moment.get("sentences")}
    rn = list(dict.fromkeys(reframe_notes))
    tl["growth_notes"] = notes + [f"reframe: {r}" for r in rn]
    tl["short"] = {"variant": name, "title": title, "intensity": intensity, "aspect": aspect if not size else f"{W}x{H}",
                   "caption_style": (tl.get("captions") or {}).get("style"), "captions_pinned": pinned, "teaser_seconds": round(teaser_len, 3),
                   "first_3s": first_seconds(tl)}
    return tl


# ---------------------------------------------------------------- platforms (WS F lazily; growth.platform_norms fallback)
FALLBACK_PLATFORMS = {  # used only when nexus.platforms cannot be imported; rules of thumb, not platform data
    "youtube":            {"norms": "youtube", "aspect": "16:9"},
    "youtube_shorts":     {"norms": "shorts",  "aspect": "9:16"},
    "tiktok":             {"norms": "tiktok",  "aspect": "9:16"},
    "instagram_reels":    {"norms": "reels",   "aspect": "9:16"},
    "instagram_feed":     {"norms": "reels",   "aspect": "4:5"},
    "facebook":           {"norms": "reels",   "aspect": "9:16"},
    "snapchat_spotlight": {"norms": "shorts",  "aspect": "9:16"},
    "shorts": {"norms": "shorts", "aspect": "9:16"}, "reels": {"norms": "reels", "aspect": "9:16"},
    "linkedin": {"norms": "linkedin", "aspect": "1:1"},
}


def _platforms_module():
    try:
        from . import platforms  # WS F; may not exist yet
        return platforms
    except Exception:  # ImportError or a broken module: fall back to growth.platform_norms
        return None


def platform_profile(name: str, mod=None) -> dict:
    """Normalised view of a platform: aspect, duration bounds, text limits, norms key, source."""
    from . import growth
    fb = FALLBACK_PLATFORMS.get(name, {"norms": "shorts", "aspect": "9:16"})
    norms = growth.PLATFORM_NORMS.get(fb["norms"], growth.PLATFORM_NORMS["shorts"])
    prof = {"name": name, "aspect": fb["aspect"], "norms_key": fb["norms"], "ideal_duration": norms["ideal_duration"],
            "max_duration": norms["hard_max"], "limits": dict(growth.TEXT_LIMITS.get(name, growth.TEXT_LIMITS["default"])),
            "source": "growth.platform_norms (rule of thumb)"}
    if mod is None:
        return prof
    try:
        p = mod.get(name) or {}
    except Exception as e:  # unknown name in WS F's registry
        prof["source"] += f"; nexus.platforms.get failed: {e}"
        return prof
    v = p.get("video") if isinstance(p.get("video"), dict) else {}
    asp = p.get("aspect") or v.get("aspect")
    if isinstance(asp, (list, tuple)):
        asp = asp[0] if asp else None
    if asp in ASPECTS:
        prof["aspect"] = asp
    dur = p.get("duration") if isinstance(p.get("duration"), dict) else {}
    prof["max_duration"] = p.get("max_duration") or dur.get("max") or prof["max_duration"]
    ideal = p.get("ideal_duration") or dur.get("ideal")
    if isinstance(ideal, (list, tuple)) and len(ideal) == 2:
        prof["ideal_duration"] = tuple(ideal)
    txt = p.get("text") if isinstance(p.get("text"), dict) else p
    for k in ("title_max", "description_max", "caption_max", "hashtags_max"):
        if txt.get(k) is not None:
            prof["limits"][k] = txt[k]
    prof["source"] = "nexus.platforms"
    prof["kind"] = p.get("kind")
    return prof


def option_for_platform(moment: dict, prof: dict) -> tuple[dict, str | None]:
    """The moment's duration option that suits the platform: the primary unless it is longer than the platform's ideal
    upper bound and a shorter option fits."""
    opts = moment.get("options") or []
    prim = next((o for o in opts if o.get("primary")), None)
    if not opts or prim is None:
        return moment, None
    hi = (prof.get("ideal_duration") or (0, None))[1]
    hard = prof.get("max_duration")
    if hi and prim["tight_duration"] > hi:
        fits = [o for o in opts if o["tight_duration"] <= hi]
        if fits:
            o = max(fits, key=lambda o: o["score"])
            return _with_option(moment, o), f"used the {o['tight_duration']:.0f}s option: the {prim['tight_duration']:.0f}s cut is longer than {prof['name']}'s ideal {hi}s"
    if hard and prim["tight_duration"] > hard:
        return moment, f"WARNING: {prim['tight_duration']:.0f}s is over {prof['name']}'s {hard}s limit and no shorter option exists"
    return moment, None


def _with_option(moment: dict, o: dict) -> dict:
    m = dict(moment)
    m.update({k: o[k] for k in ("start", "end", "duration", "tight_duration", "sentences")})
    m["text"] = moment["text"] if o.get("primary") else None
    return m


def _tune(tl: dict, name: str, prof: dict, mod) -> tuple[dict, list[str]]:
    W, H = tl["width"], tl["height"]
    if mod is not None:
        try:
            tl, notes = mod.tune(tl, name)
            try:
                tl["safe_zone"] = mod.safe_zone(name, W, H)
            except Exception as e:  # safe_zone missing for this platform
                notes = list(notes) + [f"no safe zone from nexus.platforms: {e}"]
            tl["platform"] = name
            return tl, [f"tuned by nexus.platforms.tune({name!r})"] + [str(n) for n in notes]
        except Exception as e:  # WS F module present but failing: do not lose the short
            mod_err = f"nexus.platforms.tune failed ({e}); "
    else:
        mod_err = ""
    from . import growth
    N = growth.PLATFORM_NORMS[prof["norms_key"]]
    tl["platform"] = name
    notes = [mod_err + f"nexus.platforms not available: tuned with growth.platform_norms[{prof['norms_key']!r}] (editorial rules of thumb, "
             "not platform data)"]
    tl["safe_zone"] = {"top": round(N["safe_top"] * H), "bottom": round((1 - N["safe_bottom"]) * H), "left": 0, "right": 0}
    if N["vertical"] and tl.get("captions"):
        tl["captions"]["position"] = "middle"
    lo, hi = N["ideal_duration"]
    d = total_duration(tl)
    if not lo <= d <= hi:
        notes.append(f"{d:.0f}s is outside the {lo}-{hi}s rule-of-thumb range for {name}")
    return tl, notes


# ---------------------------------------------------------------- batch
def _style_controls(style) -> dict:
    if not style:
        return {}
    if isinstance(style, str):
        return {"captions": style} if style in CAPTION_STYLES else {"intensity": style} if style in INTENSITY else {}
    s = dict(style)
    out = {k: s[k] for k in ("intensity", "captions", "music", "aspect", "brand", "fit", "sfx", "lang") if k in s}
    if "caption_style" in s and "captions" not in out:
        out["captions"] = {"style": s["caption_style"], **({"words_per_cue": max(1, min(6, round(s["caption_words_per_cue"])))}
                                                          if s.get("caption_words_per_cue") else {})}
    return out


def factory(src: str, analysis: dict, *, n=3, platforms=("youtube_shorts",), variants: int = 1, style=None, **controls) -> Selection:
    """N shorts x variants x platforms. Returns a Selection (a list) of
    {"moment","variant","platform","timeline","why","scores","context_check","title","description","hashtags","caption"}.
    n: int, or "all" (every qualified moment: context ok and score >= min_score). Controls: pick=[ranks], time_range, topic,
    speaker, durations, weights, min_score, intensity, captions, music, aspect, size, lang, brand, fit, sfx, use_llm.
    Moments whose cut could change the speaker's meaning are never built."""
    from . import growth
    an = _norm(analysis)
    ctl = {**_style_controls(style), **controls}
    sel_kw = {k: ctl.pop(k) for k in ("time_range", "topic", "speaker", "durations", "weights", "overlap_tol") if k in ctl}
    pick = ctl.pop("pick", None)
    min_score = ctl.pop("min_score", None)
    use_llm = bool(ctl.pop("use_llm", False))
    aspect_pinned = "aspect" in ctl or "size" in ctl
    unknown = set(ctl) - {"intensity", "captions", "music", "aspect", "size", "lang", "brand", "fit", "sfx"}
    if unknown:
        raise ValueError(f"unknown controls: {sorted(unknown)}")
    want_all = n in (None, "all")
    top = 500 if want_all else max(int(n), max(pick) if pick else 0)
    sel = select(an, top=top, use_llm=use_llm, **sel_kw)
    out = Selection([], notes=list(sel.notes), rejected=sel.rejected, filters={**sel.filters, "n": n, "pick": pick, "variants": variants})
    moments = list(sel)
    if pick:
        have = {m["rank"] for m in moments}
        miss = sorted(set(pick) - have)
        if miss:
            out.notes.append(f"ranks {miss} do not exist (only {len(moments)} moment(s) found)")
        moments = [m for m in moments if m["rank"] in set(pick)]
    elif want_all:
        moments = [m for m in moments if not m.get("needs_context") and (min_score is None or m["score"] >= min_score)]
    else:
        moments = [m for m in moments if min_score is None or m["score"] >= min_score][:int(n)]
    names = [platforms] if isinstance(platforms, str) else list(platforms)
    mod = _platforms_module()
    if mod is None:
        out.notes.append("nexus.platforms is not importable: per-platform tuning used growth.platform_norms rules of thumb")
    for m in moments:
        plan = plan_variants(an, m, variants, use_llm=use_llm)
        if len(plan) < variants:
            out.notes.append(f"moment {m['rank']}: only {len(plan)} distinct variant(s) possible")
        for name in names:
            prof = platform_profile(name, mod)
            mm, opt_note = option_for_platform(m, prof)
            if mm.get("text") is None:
                lo_i, hi_i = mm["sentences"]
                mm["text"] = " ".join(s["text"] for s in usable_sentences(an)[lo_i:hi_i + 1])
            kw = {k: v for k, v in ctl.items()}
            if not aspect_pinned:
                kw["aspect"] = prof["aspect"]
            for v in plan:
                if mm is not m and v.get("cold_open") is not None and not (mm["sentences"][0] < v["cold_open"] <= mm["sentences"][1]):
                    continue  # the teaser line is not inside this shorter option
                tl = compose(src, an, mm, variant=v, **kw)
                tl, tune_notes = _tune(tl, name, prof, mod)
                rep = growth.retention_report(tl, {src: an}, prof["norms_key"])
                texts = growth.platform_text(mm["text"] or "", title=v["title"], platform=name, limits=prof["limits"], use_llm=use_llm)
                cc = m["context_check"]
                why = [f"rank {m['rank']}: content_potential {m['score']} ({', '.join(f'{k} {x}' for k, x in m['components'].items())})"]
                why += [f"hook: {', '.join(m.get('hook_reasons') or []) or 'n/a'}"]
                why += [f"context: {cc.get('status')}: " + "; ".join(cc.get("why") or [])]
                why += [f"variant {v['name']}: {v['why']}"] + ([v["note"]] if v.get("note") else [])
                why += ([opt_note] if opt_note else []) + tune_notes + tl.get("growth_notes", [])
                if m.get("topic_match"):
                    why.append(f"topic match ({m['topic_match']['source']}): {m['topic_match']['terms'] or m['topic_match']['topics']}")
                why.append(f"retention rubric {rep['score']}/100 ({rep['note']})")
                scores = {**m["scores"], "retention_rubric": rep["score"]}
                out.append({"moment": {k: mm[k] for k in ("rank", "start", "end", "duration", "tight_duration", "sentences", "text", "basis")
                                       if k in mm}, "variant": v["name"], "platform": name, "timeline": tl, "why": why, "scores": scores,
                            "context_check": cc, "title": texts["title"], "description": texts["description"],
                            "hashtags": texts["hashtags"], "caption": texts["caption"], "text_source": texts["source"],
                            "first_3s": tl["short"]["first_3s"]})
    return out


def save_batch(project, items: list[dict]) -> list[dict]:
    """Save every factory item on its own branch short-<rank>-<variant>-<platform>. Rendering stays a separate job."""
    rows = []
    for it in items:
        m = it["moment"]
        branch = f"short-{m['rank']}-{it['variant']}-{it['platform']}"
        vid = project.save_timeline(it["timeline"], f"{it['title'] or 'short'} ({m['start']:.1f}-{m['end']:.1f}s, {it['variant']}, "
                                                     f"{it['platform']})", branch)
        rows.append({"branch": branch, "version": vid, "duration": round(total_duration(it["timeline"]), 3), "title": it["title"],
                     "variant": it["variant"], "platform": it["platform"], "start": m["start"], "end": m["end"], "scores": it["scores"],
                     "context_status": it["context_check"].get("status"), "description": it["description"], "hashtags": it["hashtags"]})
    return rows
