"""Searchable media understanding on top of media/analyze.py output.

Adds keys, never removes: `shots`, `topics`, `speakers`, `moments`, `motion` at the top level, and the rest under
`understanding` (sentences, fillers, stutters, dead_air, retakes, duplicate shots, keyframes, narrative, notes).

What each signal really is (no vision or diarisation model is involved):
- shots: the detected scenes (PySceneDetect or FFmpeg scene score), measured on FFmpeg-sampled grey frames with NumPy:
  sharpness = Laplacian variance, brightness = mean luma, motion = mean absolute frame difference, shake = jitter of the
  phase-correlation translation between frames. Without a file path the shots carry `measured: false`.
- near-duplicate shots: 64-bit DCT perceptual hash of each shot's keyframe, Hamming distance.
- topics: lexical cohesion (TextTiling-style) over sentence word bags; optional LLM labels via nexus.llm (lazy, optional).
- speakers: only when the transcript already carries `speaker` labels. Otherwise `available: false` - nothing is guessed.
- moments / narrative roles: transparent editorial heuristics with their components; they are not predictions of interest.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from pathlib import Path

STOP = set("the a an and or but of to in on for with is are was were be been it this that as at by from we you they i he she "
           "have has had not so if then than there here our your their its can will just about into over more most very "
           "um uh like okay ok well yeah also do does did what which who would could should really get got going "
           "these those them us my me one all some any know mean thing things lot way".split())
FILLERS_HARD = {"um", "uh", "umm", "uhm", "erm", "er", "ah", "ahh", "hmm", "mm", "mhm", "uhh"}
FILLER_PHRASES_SOFT = [("you", "know"), ("i", "mean")]
FILLERS_LOOSE = {"basically", "literally", "like", "actually", "so"}
LEVEL = {"hard": 0.3, "phrase": 0.6, "loose": 0.9}   # filler kind -> minimum aggressiveness that removes it
BACKREF = {"and", "but", "because", "which", "also", "then", "or", "plus", "therefore", "however", "so"}
PRONOUN = {"it", "this", "that", "these", "those", "they", "he", "she", "there", "it's", "that's", "they're"}
SECOND_PERSON = {"you", "your", "you're", "yours", "yourself", "you'll", "you've"}
CURIOSITY = {"why", "how", "secret", "mistake", "truth", "nobody", "never", "surprising", "surprisingly", "wrong", "myth",
             "hidden", "real", "worst", "best", "biggest", "only", "problem", "trick", "nothing", "imagine", "first"}
CONCLUDERS = ("that's why", "that is why", "which means", "the point", "in short", "bottom line", "remember", "the result",
              "in the end", "overall", "that's how", "that is how", "the lesson", "the takeaway", "in conclusion", "so in the end")
NUMBER_RE = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|twenty|hundred|thousand|million|billion|"
                       r"percent|half|double|triple)\b", re.I)
TERMINAL = (".", "!", "?", "…")


# ---------------------------------------------------------------- text helpers
def tokens(text: str) -> list[str]:
    return re.findall(r"[\w'%]+", text.lower())


def stem(w: str) -> str:
    for suf in ("ing", "edly", "ed", "ies", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def content(text: str) -> list[str]:
    return [stem(w) for w in tokens(text) if w not in STOP and len(w) > 2 and not w.isdigit()]


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


def similarity(a: str, b: str) -> float:
    na, nb = " ".join(tokens(a)), " ".join(tokens(b))
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


# ---------------------------------------------------------------- normalised view of any analysis dict
def duration_of(an: dict) -> float:
    d = an.get("duration") or (an.get("probe") or {}).get("duration")
    if d:
        return float(d)
    ends = [s["end"] for s in segments_of(an)] + [b for _, b in an.get("scenes") or []]
    return float(max(ends or [0.0]))


def segments_of(an: dict) -> list[dict]:
    return list(((an.get("transcript") or {}).get("segments")) or an.get("segments") or [])


def silences_of(an: dict) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in (an.get("silences") or an.get("silence") or [])]


def has_audio(an: dict) -> bool:
    probe = an.get("probe")
    return bool(probe.get("audio")) if probe else True


def sentences(an: dict, max_pause: float = 1.5) -> list[dict]:
    """Sentence units with word timings, rebuilt ACROSS transcript segments: ASR segments (e.g. Whisper's) often break
    in the middle of a sentence, and an editor must cut at sentence boundaries, not at segment boundaries.
    A sentence ends at terminal punctuation, at a pause longer than `max_pause` seconds, or at a speaker change.
    Words without timings are spread over their segment proportionally to their length (marked `timed: false`)."""
    out: list[dict] = []
    stream: list[tuple[dict, int, dict, bool]] = []
    for si, s in enumerate(segments_of(an)):
        words = [w for w in (s.get("words") or []) if str(w.get("word", "")).strip()]
        timed = bool(words)
        if not words:
            toks = s.get("text", "").split()
            total = sum(len(t) for t in toks) or 1
            t = float(s["start"])
            for tok in toks:
                d = (float(s["end"]) - float(s["start"])) * len(tok) / total
                words.append({"start": round(t, 3), "end": round(t + d, 3), "word": tok})
                t += d
        for w in words:
            stream.append(({"start": float(w["start"]), "end": float(w["end"]), "word": str(w["word"]).strip()}, si, s, timed))

    cur: list[tuple[dict, int, dict, bool]] = []

    def flush() -> None:
        if not cur:
            return
        ws = [x[0] for x in cur]
        text = " ".join(w["word"] for w in ws).strip()
        out.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": text, "words": ws,
                    "terminal": text.rstrip("\"')]").endswith(TERMINAL), "seg": cur[0][1], "segs": sorted({x[1] for x in cur}),
                    "speaker": cur[0][2].get("speaker"), "timed": all(x[3] for x in cur)})
        cur.clear()

    for item in stream:
        w, si, seg, _ = item
        if cur:
            prev_w, _, prev_seg, _ = cur[-1]
            if w["start"] - prev_w["end"] > max_pause or seg.get("speaker") != prev_seg.get("speaker"):
                flush()
        cur.append(item)
        if w["word"].rstrip("\"')]").endswith(TERMINAL):
            flush()
    flush()
    for k, s in enumerate(out):
        s["i"] = k
    return out


def all_words(an: dict) -> list[dict]:
    return [w for s in sentences(an) for w in s["words"]]


# ---------------------------------------------------------------- transcript signals
def fillers(sents: list[dict]) -> list[dict]:
    """Filler words/phrases with their kind (hard: um/uh; phrase: you know / I mean; loose: like/basically/actually/so).
    `level` is the aggressiveness from which the director may remove it."""
    out = []
    for s in sents:
        ws = s["words"]
        toks = [re.sub(r"[^\w']", "", w["word"].lower()) for w in ws]
        k = 0
        while k < len(ws):
            pair = tuple(toks[k:k + 2])
            if toks[k] in FILLERS_HARD:
                kind, n = "hard", 1
            elif pair in FILLER_PHRASES_SOFT and (k == 0 or ws[k - 1]["word"].endswith(",") or ws[k + 1]["word"].endswith(",")):
                kind, n = "phrase", 2
            elif toks[k] in FILLERS_LOOSE and (k == 0 or ws[k]["word"].endswith(",")):
                kind, n = "loose", 1
            else:
                k += 1
                continue
            out.append({"sentence": s["i"], "word_index": k, "n": n, "kind": kind, "level": LEVEL[kind],
                        "start": ws[k]["start"], "end": ws[k + n - 1]["end"],
                        "text": " ".join(w["word"] for w in ws[k:k + n])})
            k += n
    return out


def stutters(sents: list[dict]) -> list[dict]:
    """Immediate word repetitions ("the the"): the first occurrence is removable."""
    out = []
    for s in sents:
        toks = [re.sub(r"[^\w']", "", w["word"].lower()) for w in s["words"]]
        for k in range(1, len(toks)):
            if toks[k] and toks[k] == toks[k - 1]:
                w = s["words"][k - 1]
                out.append({"sentence": s["i"], "word_index": k - 1, "start": w["start"], "end": w["end"], "text": w["word"]})
    return out


def retakes(sents: list[dict], threshold: float = 0.8, window: int = 8) -> list[dict]:
    """Sentence-level repeats inside one source: (earlier, later, similarity)."""
    out = []
    for i in range(len(sents)):
        if len(sents[i]["text"]) < 15:
            continue
        for j in range(i + 1, min(i + 1 + window, len(sents))):
            r = similarity(sents[i]["text"], sents[j]["text"])
            if r >= threshold:
                out.append({"earlier": i, "later": j, "similarity": round(r, 3), "text": sents[i]["text"]})
    return out


def dead_air(an: dict, sents: list[dict] | None = None, min_gap: float = 0.7) -> list[dict]:
    """Stretches without speech: leading/trailing, pauses between words, and detected silences (merged)."""
    sents = sents if sents is not None else sentences(an)
    dur = duration_of(an)
    words = [w for s in sents for w in s["words"]]
    spans: list[list] = []
    if words:
        if words[0]["start"] >= min_gap:
            spans.append([0.0, words[0]["start"], "leading"])
        for a, b in zip(words, words[1:]):
            if b["start"] - a["end"] >= min_gap:
                spans.append([a["end"], b["start"], "pause"])
        if dur - words[-1]["end"] >= min_gap:
            spans.append([words[-1]["end"], dur, "trailing"])
    for a, b in silences_of(an):
        if b - a >= min_gap and not any(x[0] <= a and b <= x[1] for x in spans):
            spans.append([a, b, "silence"])
    spans.sort()
    return [{"start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3), "kind": k} for a, b, k in spans]


FIRST_PERSON = {"i", "i'm", "i've", "i'd", "i'll", "me", "my", "mine", "we", "we're", "we've", "we'd", "we'll", "us", "our", "ours"}
SELF_INTRO_RE = re.compile(r"\b(i'?m|i am|this is \w+( \w+)?) (with|from|here (with|for)) [A-Z]", re.I)
PHATIC_RE = re.compile(
    r"\b(how are you( doing)?|how's it going|thank(s| you)( so much)?( for (having|joining|coming|watching))?|"
    r"for your time|good luck|good (morning|afternoon|evening)|nice to (meet|see|talk)|(great|good|happy|glad) to (be|have|see) (here|you|with)|"
    r"hello|hi there|welcome( back)?|can you hear me|are you (there|ready)|you're welcome|my pleasure|bye|see you)\b", re.I)


def dialogue_like(sents: list[dict]) -> bool:
    """True when a transcript reads like an interview/conversation: second-person questions are a meaningful share of its
    sentences (>= 6 % and at least 3). A monologue's occasional rhetorical question does not qualify."""
    q = sum(1 for s in sents if str(s.get("text", "")).rstrip().endswith("?") and SECOND_PERSON & set(tokens(s.get("text", ""))))
    return q >= 3 and q / max(1, len(sents)) >= 0.06


def substance(text: str) -> tuple[float, list[str]]:
    """Cap for an opening line that says nothing: pleasantries, greetings and lines with fewer than four content
    words cannot be a hook however question-like they are. Returns (cap 0..1, reasons)."""
    words = content(text)
    if PHATIC_RE.search(text) and len(words) < 10:
        return 0.15, ["pleasantry/greeting - no information (capped)"]
    if len(words) < 4:
        return 0.35, [f"only {len(words)} content word(s) (capped)"]
    return 1.0, []


def hook_score(text: str) -> tuple[float, list[str]]:
    """Strength of a sentence as an opening line (0..1). Uses nexus.shorts.hook_score when present (same rubric
    the shorts factory uses), otherwise an equivalent local rubric; both are capped by `substance`."""
    cap, capwhy = substance(text)
    try:
        from .. import shorts
        s0, why0 = shorts.hook_score(text)
        return round(min(s0, cap), 3), why0 + capwhy
    except Exception:  # noqa: BLE001 - optional package
        pass
    toks = tokens(text)
    if not toks:
        return 0.0, ["empty"]
    s, why = 0.3, []
    for ok, add, name in ((text.strip().endswith("?"), 0.25, "question"), (bool(NUMBER_RE.search(text)), 0.2, "number"),
                          (bool(SECOND_PERSON & set(toks)), 0.15, "second person"), (bool(CURIOSITY & set(toks)), 0.15, "curiosity word"),
                          (4 <= len(toks) <= 16, 0.1, "punchy length")):
        if ok:
            s += add
            why.append(name)
    if toks[0] in FILLERS_HARD | FILLERS_LOOSE:
        s -= 0.45
        why.append("filler opening (penalty)")
    elif toks[0] in BACKREF:
        s -= 0.3
        why.append("depends on earlier context (penalty)")
    elif toks[0] in PRONOUN:
        s -= 0.1
        why.append("opens on a pronoun (penalty)")
    return round(min(cap, max(0.0, s)), 3), why + capwhy


def self_contained(s: dict) -> bool:
    """A sentence that can stand alone (e.g. lifted to a cold open) without misrepresenting its context."""
    toks = tokens(s["text"])
    if len(toks) < 5 or not s.get("terminal") or substance(s["text"])[0] < 1.0:
        return False
    first = s["text"].lstrip("\"'([ ")[:1]
    if first.isalpha() and first.islower():
        return False  # starts mid-sentence: the transcript split a sentence across segments
    if s.get("prompt_like"):
        return False
    if toks[0] in BACKREF or toks[0] in PRONOUN or toks[0] in FILLERS_HARD:
        return False
    if re.search(r"\b(as i (said|mentioned)|like i said|as we saw|above|below|earlier|that one|this one)\b", s["text"], re.I):
        return False
    return True


def depends_on_previous(s: dict) -> bool:
    toks = tokens(s["text"])
    return bool(toks) and (toks[0] in BACKREF or toks[0] in PRONOUN)


def score_sentences(sents: list[dict], fill: list[dict] | None = None) -> None:
    """Importance 0..100 per sentence, with components (in place): topical weight, hook strength, specificity,
    completeness, fluency (fillers/stutter), delivery pace."""
    fill = fill if fill is not None else fillers(sents)
    per_sent = Counter(f["sentence"] for f in fill)
    docs = [content(s["text"]) for s in sents]
    df = Counter(w for d in docs for w in set(d))
    tf = Counter(w for d in docs for w in d)
    raw = []
    for d in docs:
        raw.append(sum(math.log1p(tf[w]) * math.log1p(len(docs) / df[w]) for w in set(d)) / max(1, len(set(d))) if d else 0.0)
    top = max(raw or [1.0]) or 1.0
    labelled = any(s.get("speaker") for s in sents)
    dialogue = (not labelled) and dialogue_like(sents)
    for s, r in zip(sents, raw):
        n = max(1, len(s["words"]))
        hook, why = hook_score(s["text"])
        toks = set(tokens(s["text"]))
        # Without speaker labels: a line addressed to someone else (second person, no first person) is most likely the
        # interviewer's/host's prompt, and a concrete first-person line is most likely the guest's own account.
        question = s["text"].rstrip().endswith("?")
        s["prompt_like"] = (not labelled) and (bool(SELF_INTRO_RE.search(s["text"])) or (
            dialogue and bool(SECOND_PERSON & toks) and (question or not (FIRST_PERSON & toks))))
        if s["prompt_like"]:
            hook = min(hook, 0.3)
            why = why + ["addressed to someone else - likely an interviewer/host prompt (heuristic, no speaker labels; capped)"]
        elif FIRST_PERSON & toks and len(content(s["text"])) >= 6 and not s["text"].rstrip().endswith("?"):
            hook = min(1.0, hook + 0.15)
            why = why + ["first-hand account"]
        spec = min(1.0, 0.5 * bool(NUMBER_RE.search(s["text"])) +
                   0.25 * len(re.findall(r"(?<!^)(?<![.!?] )\b[A-Z][a-z]{2,}", s["text"])))
        dur = max(0.1, s["end"] - s["start"])
        wpm = n / dur * 60
        pace = 1.0 if 110 <= wpm <= 200 else max(0.0, 1 - min(abs(wpm - 110), abs(wpm - 200)) / 90)
        low = s["text"].lower()
        comp = {"topical": round(r / top, 3), "hook": hook, "specificity": round(spec, 3),
                "completeness": 1.0 if s["terminal"] else 0.4,
                "fluency": round(max(0.0, 1 - 3 * per_sent[s["i"]] / n), 3), "pace": round(pace, 3),
                "concluding": 1.0 if any(c in low for c in CONCLUDERS) else 0.0}
        w = {"topical": 0.35, "hook": 0.2, "specificity": 0.1, "completeness": 0.1, "fluency": 0.1, "pace": 0.05, "concluding": 0.1}
        s["components"] = comp
        s["hook"], s["hook_reasons"] = hook, why
        s["score"] = round(100 * sum(w[k] * comp[k] for k in w), 1)
        s["self_contained"] = self_contained(s)
        s["depends_prev"] = depends_on_previous(s)


# ---------------------------------------------------------------- topics (lexical cohesion)
def segment_topics(sents: list[dict], window: int = 3, min_sents: int = 3, use_llm: bool = False) -> list[dict]:
    """TextTiling-style topic segmentation: cosine similarity of the word bags `window` sentences either side of each
    sentence gap; a gap whose depth (drop below the neighbouring peaks) is clearly above average starts a new topic.
    A pause > 1.5 s adds a little depth (speakers pause between subjects)."""
    n = len(sents)
    min_sents = max(min_sents, n // 30)  # long sources: no topic shorter than ~1/30 of the talk
    bags = [Counter(content(s["text"])) for s in sents]
    bounds: list[int] = []
    gaps: list[dict] = []
    if n >= 2 * min_sents:
        sims = []
        for g in range(1, n):
            left, right = Counter(), Counter()
            for b in bags[max(0, g - window):g]:
                left.update(b)
            for b in bags[g:g + window]:
                right.update(b)
            sims.append(cosine(left, right))
        depths = []
        for k, sim in enumerate(sims):
            i = k  # climb to the nearest peak on each side

            while i > 0 and sims[i - 1] >= sims[i]:
                i -= 1
            lp = sims[i]
            j = k
            while j < len(sims) - 1 and sims[j + 1] >= sims[j]:
                j += 1
            rp = sims[j]
            pause = sents[k + 1]["start"] - sents[k]["end"]
            depths.append((lp - sim) + (rp - sim) + (0.1 if pause > 1.5 else 0.0))
        mean = sum(depths) / len(depths)
        sd = math.sqrt(sum((d - mean) ** 2 for d in depths) / len(depths))
        cut = max(0.1, mean + 0.75 * sd)  # relative: short spoken sentences share few words even within a topic
        for k in sorted(range(len(depths)), key=lambda k: -depths[k]):
            g = k + 1
            if depths[k] < cut:
                break
            if g < min_sents or n - g < min_sents or any(abs(g - b) < min_sents for b in bounds):
                continue
            bounds.append(g)
        gaps = [{"after_sentence": k, "similarity": round(sims[k], 3), "depth": round(depths[k], 3)} for k in range(len(sims))]
    bounds = sorted(bounds)
    edges = [0, *bounds, n]
    topics = []
    total = Counter()
    for b in bags:
        total.update(b)
    for t, (a, b) in enumerate(zip(edges, edges[1:])):
        if b <= a:
            continue
        bag = Counter()
        for x in bags[a:b]:
            bag.update(x)
        rest = total - bag
        kw = sorted(bag, key=lambda w: (-(bag[w] * math.log1p(1 + bag[w] / (1 + rest.get(w, 0)))), w))[:6]
        topics.append({"id": t, "sentences": [a, b - 1], "start": sents[a]["start"], "end": sents[b - 1]["end"],
                       "keywords": kw, "label": " ".join(kw[:3]), "cohesion": round(cosine(bag, rest), 3) if rest else 1.0,
                       "bag": dict(bag)})
    if use_llm and topics:
        _llm_labels(topics, sents)
    for t in topics:
        for k in range(t["sentences"][0], t["sentences"][1] + 1):
            sents[k]["topic"] = t["id"]
    if topics:
        topics[0]["gaps"] = gaps  # the cohesion curve, kept for explainability (empty when too short to segment)
    return topics


def _llm_labels(topics: list[dict], sents: list[dict]) -> None:
    """Optional: a short label per topic from the local LLM, accepted only if built from the topic's own words."""
    try:
        from .. import llm
    except Exception:  # noqa: BLE001
        return
    for t in topics:
        text = " ".join(s["text"] for s in sents[t["sentences"][0]:t["sentences"][1] + 1])[:1500]
        try:
            d = llm.chat_json(f"Give a 2-5 word chapter title for this transcript passage. Use only its own words.\nTEXT: {text}",
                              schema={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
                              require="title", max_tokens=40, retries=1)
            lab = str(d["title"]).strip().strip('"')
        except Exception:  # noqa: BLE001 - model missing or bad answer: the lexical label stands
            return
        vocab = set(tokens(text))
        if lab and len(lab.split()) <= 6 and all(w in vocab for w in tokens(lab) if w not in STOP):
            t["label"], t["label_source"] = lab, "llm"


def narrative_roles(sents: list[dict], topics: list[dict], tangent_cohesion: float = 0.06) -> dict:
    """Per topic: setup / development / climax / resolution / tangent. One sentence is the `hook`.
    Tangent = a middle topic sharing almost no vocabulary with the rest (cohesion below `tangent_cohesion`) and
    of below-median importance. Climax = the most important middle topic."""
    for t in topics:
        sc = [sents[k]["score"] for k in range(t["sentences"][0], t["sentences"][1] + 1)]
        t["importance"] = round(sum(sc) / len(sc), 1) if sc else 0.0
    med = sorted(t["importance"] for t in topics)[len(topics) // 2] if topics else 0
    middle = topics[1:-1] if len(topics) > 2 else []
    for t in topics:
        t["role"] = "development"
    if len(topics) >= 2:
        topics[0]["role"], topics[-1]["role"] = "setup", "resolution"
    for t in middle:
        if t["cohesion"] < tangent_cohesion and t["importance"] <= med:
            t["role"] = "tangent"
    rest = [t for t in middle if t["role"] != "tangent"]
    if rest:
        max(rest, key=lambda t: t["importance"])["role"] = "climax"
    for t in topics:
        for k in range(t["sentences"][0], t["sentences"][1] + 1):
            sents[k]["role"] = t["role"]
    cands = [s for s in sents if s["self_contained"]] or sents
    hook = max(cands, key=lambda s: (s["hook"] * 0.6 + s["score"] / 100 * 0.4, -s["start"])) if cands else None
    if hook is not None:
        hook["role"] = "hook"
    return {"hook": hook["i"] if hook else None, "hook_text": hook["text"] if hook else "",
            "topics": [{"id": t["id"], "role": t["role"], "importance": t["importance"], "cohesion": t["cohesion"]} for t in topics],
            "rule": "hook = strongest self-contained opening line; setup/resolution = first/last topic; climax = most important "
                    f"middle topic; tangent = middle topic with cohesion < {tangent_cohesion} and below-median importance"}


def speakers(sents: list[dict]) -> dict:
    labels = [s.get("speaker") for s in sents]
    if not any(labels):
        return {"available": False, "turns": [], "note": "no speaker labels in the analysis; diarisation was not run, so speaker "
                                                         "turns are unknown (nothing is guessed)"}
    turns: list[dict] = []
    for s in sents:
        sp = s.get("speaker") or "unknown"
        if turns and turns[-1]["speaker"] == sp:
            turns[-1]["end"] = s["end"]
            turns[-1]["sentences"][1] = s["i"]
        else:
            turns.append({"speaker": sp, "start": s["start"], "end": s["end"], "sentences": [s["i"], s["i"]]})
    talk = Counter()
    for t in turns:
        talk[t["speaker"]] += t["end"] - t["start"]
    return {"available": True, "turns": turns, "talk_time": {k: round(v, 2) for k, v in talk.items()}}


def moments(sents: list[dict], top: int = 10) -> list[dict]:
    return [{"start": s["start"], "end": s["end"], "sentence": s["i"], "score": s["score"], "components": s["components"],
             "text": s["text"]} for s in sorted(sents, key=lambda s: (-s["score"], s["start"]))[:top]]


# ---------------------------------------------------------------- shots (picture)
def phash(gray) -> str:
    """64-bit DCT perceptual hash (hex) of a grey frame (NumPy + Pillow)."""
    import numpy as np
    from PIL import Image
    img = np.asarray(Image.fromarray(gray).resize((32, 32), Image.BILINEAR), dtype=np.float64)
    n = 32
    k = np.arange(n)
    dct = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    d = dct @ img @ dct.T
    block = d[:8, :8].flatten()
    med = np.median(block[1:])
    bits = "".join("1" if v > med else "0" for v in block)
    return f"{int(bits, 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def shots(an: dict, path: str | Path | None = None, sents: list[dict] | None = None, max_frames: int = 480) -> tuple[list[dict], dict]:
    """Shots from scenes, measured on sampled frames when a readable file is given. Returns (shots, motion summary)."""
    dur = duration_of(an)
    sents = sents if sents is not None else sentences(an)
    words = [w for s in sents for w in s["words"]]
    scenes = [(float(a), float(b)) for a, b in (an.get("scenes") or []) if b > a] or [(0.0, dur)]
    times, metrics, frames = [], None, None
    note = "not measured (no readable file)"
    if path and Path(path).exists() and (an.get("probe") or {}).get("video", True):
        try:
            from . import analyze
            times, frames = analyze.sample_frames(path, max_frames=max_frames, duration=dur)
            metrics = analyze.frame_metrics(frames) if len(frames) else None
            note = f"measured on {len(times)} sampled frames"
        except Exception as e:  # noqa: BLE001 - measurement is best effort
            note = f"not measured: {type(e).__name__}: {e}"[:160]
    black = [tuple(x) for x in an.get("black") or []]
    freeze = [tuple(x) for x in an.get("freeze") or []]
    out = []
    sharp_all = sorted(metrics["sharpness"]) if metrics else []
    med_sharp = sharp_all[len(sharp_all) // 2] if sharp_all else None
    for k, (a, b) in enumerate(scenes):
        speech = sum(max(0.0, min(b, w["end"]) - max(a, w["start"])) for w in words)
        sh = {"id": k, "start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3),
              "speech_share": round(min(1.0, speech / max(b - a, 1e-3)), 3), "measured": False}
        issues = []
        idx = [i for i, t in enumerate(times) if a <= t < b] if metrics else []
        if idx:
            import numpy as np
            sharp = [metrics["sharpness"][i] for i in idx]
            bright = [metrics["brightness"][i] for i in idx]
            diffs = [metrics["diff"][i] for i in idx[1:]]
            shifts = [metrics["shift"][i] for i in idx[1:]]
            mid = idx[len(idx) // 2]
            key = max(idx, key=lambda i: metrics["sharpness"][i] - 0.5 * abs(i - mid))
            if len(shifts) >= 3:
                sx = np.array([s[0] for s in shifts])
                sy = np.array([s[1] for s in shifts])
                shake = float(np.mean(np.abs(np.diff(sx))) + np.mean(np.abs(np.diff(sy))))
            else:
                shake = 0.0
            sh.update({"measured": True, "sharpness": round(float(np.mean(sharp)), 1), "brightness": round(float(np.mean(bright)), 1),
                       "motion": round(float(np.mean(diffs)) if diffs else 0.0, 4), "shake": round(shake, 2),
                       "keyframe_t": times[key], "phash": phash(frames[key])})
            from .analyze import SOFT_SHARPNESS
            if sh["sharpness"] < SOFT_SHARPNESS or (med_sharp and sh["sharpness"] < 0.35 * med_sharp):
                issues.append("soft")
            if sh["brightness"] < 40:
                issues.append("dark")
            if sh["brightness"] > 220:
                issues.append("blown out")
            if shake > 3.0:
                issues.append("shaky")
        else:
            sh["keyframe_t"] = round((a + b) / 2, 3)
        for kind, spans in (("black", black), ("frozen", freeze)):
            cover = sum(max(0.0, min(b, y) - max(a, x)) for x, y in spans)
            if cover > 0.5 * (b - a):
                issues.append(kind)
        if sh["duration"] < 0.4:
            issues.append("flash frame")
        sh["issues"] = issues
        penalty = {"soft": 0.35, "dark": 0.25, "blown out": 0.25, "shaky": 0.2, "black": 0.9, "frozen": 0.5, "flash frame": 0.3}
        sh["quality"] = round(max(0.0, 1.0 - sum(penalty[i] for i in issues)), 3) if sh["measured"] or issues else None
        tags = list(issues)
        if sh["measured"]:
            tags.append("static" if sh["motion"] < 0.01 else "high motion" if sh["motion"] > 0.06 else "moving")
            tags.append("bright" if sh["brightness"] > 160 else "dark" if sh["brightness"] < 70 else "mid exposure")
            tags.append("sharp" if "soft" not in issues else "blurry")
        tags.append("speech" if sh["speech_share"] > 0.3 else "silent" if sh["speech_share"] < 0.05 else "some speech")
        sh["tags"] = sorted(set(tags))
        out.append(sh)
    motion = {"measured": bool(metrics), "note": note}
    if metrics:
        d = metrics["diff"][1:]
        motion.update({"mean": round(sum(d) / max(1, len(d)), 4), "peak": round(max(d or [0.0]), 4),
                       "samples": len(times), "per_shot": [s.get("motion") for s in out]})
    return out, motion


def duplicate_shots(shot_list: list[dict], max_distance: int = 10) -> list[list[int]]:
    """Groups of near-identical shots (perceptual hash distance <= max_distance)."""
    groups: list[list[int]] = []
    hashed = [s for s in shot_list if s.get("phash")]
    seen: set[int] = set()
    for i, a in enumerate(hashed):
        if a["id"] in seen:
            continue
        g = [a["id"]]
        for b in hashed[i + 1:]:
            if b["id"] not in seen and hamming(a["phash"], b["phash"]) <= max_distance:
                g.append(b["id"])
        if len(g) > 1:
            seen.update(g)
            groups.append(g)
    return groups


def extract_keyframes(path: str | Path, shot_list: list[dict], outdir: str | Path, width: int = 480) -> list[str]:
    """Write one JPEG per shot at its keyframe time (for UI thumbnails / review)."""
    from . import ff
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for s in shot_list:
        p = outdir / f"shot_{s['id']:04d}.jpg"
        ff.run(["-ss", f"{s['keyframe_t']:.3f}", "-i", path, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", p],
               op="keyframe")
        paths.append(str(p))
    return paths


# ---------------------------------------------------------------- the layer
def understand(an: dict, path: str | Path | None = None, use_llm: bool = False, measure: bool = True) -> dict:
    """Everything above for one source. Pure function of the analysis (+ the file when `path` is given)."""
    sents = sentences(an)
    fill = fillers(sents)
    score_sentences(sents, fill)
    topics = segment_topics(sents, use_llm=use_llm) if sents else []
    narrative = narrative_roles(sents, topics) if sents else {"hook": None, "topics": [], "note": "no transcript"}
    shot_list, motion = shots(an, path if measure else None, sents)
    dups = duplicate_shots(shot_list)
    notes = []
    if not sents:
        notes.append("no transcript: topics, fillers, moments and narrative roles are unavailable; editing falls back to shots")
    if not motion["measured"]:
        notes.append(f"shot quality {motion['note']}")
    spk = speakers(sents)
    if not spk["available"]:
        notes.append(spk["note"])
    notes.append("no vision model: shots carry measured picture statistics only, not subjects, objects or faces")
    return {"sentences": sents, "fillers": fill, "stutters": stutters(sents), "retakes": retakes(sents),
            "dead_air": dead_air(an, sents), "topics": topics, "narrative": narrative, "speakers": spk,
            "moments": moments(sents), "shots": shot_list, "motion": motion, "duplicate_shots": dups,
            "keyframes": [{"shot": s["id"], "t": s["keyframe_t"], "phash": s.get("phash")} for s in shot_list], "notes": notes}


def enrich(an: dict, path: str | Path | None = None, use_llm: bool = False) -> dict:
    """Add the understanding keys to an analysis dict in place (additive only) and return it."""
    u = understand(an, path or an.get("path"), use_llm=use_llm)
    for k in ("shots", "topics", "speakers", "moments", "motion"):
        an[k] = u[k]
    an["understanding"] = {k: v for k, v in u.items() if k not in ("shots", "topics", "speakers", "moments", "motion")}
    return an


def ensure(an: dict) -> dict:
    """The understanding of an analysis: from its stored keys when present and consistent, else computed (no file I/O)."""
    if an.get("understanding") and an.get("topics") is not None and an.get("shots") is not None:
        u = dict(an["understanding"])
        for k in ("shots", "topics", "speakers", "moments", "motion"):
            u[k] = an[k]
        return u
    return understand(an, None, measure=False)


# ---------------------------------------------------------------- search
def build_index(understanding_by_src: dict[str, dict]) -> list[dict]:
    """Flat searchable entries: sentences, topics and shots of every source."""
    idx = []
    for src, u in understanding_by_src.items():
        for s in u.get("sentences", []):
            idx.append({"kind": "sentence", "src": src, "start": s["start"], "end": s["end"], "text": s["text"],
                        "terms": content(s["text"]), "tags": [s.get("role", "")], "topic": s.get("topic"), "weight": 1.0})
        for t in u.get("topics", []):
            idx.append({"kind": "topic", "src": src, "start": t["start"], "end": t["end"], "text": t["label"],
                        "terms": [w for w, c in t.get("bag", {}).items() for _ in range(min(c, 3))] + list(t["keywords"]),
                        "tags": [t.get("role", "")], "topic": t["id"], "weight": 1.2})
        for sh in u.get("shots", []):
            idx.append({"kind": "shot", "src": src, "start": sh["start"], "end": sh["end"], "text": ", ".join(sh.get("tags", [])),
                        "terms": [], "tags": sh.get("tags", []), "weight": 1.0, "quality": sh.get("quality")})
    return idx


def search(index: list[dict] | dict, query: str, top: int = 5, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """BM25-style ranking over transcript words, topic keywords and shot tags. `index` may be a list from build_index()
    or a single understanding / analysis dict."""
    if isinstance(index, dict):
        u = index if "sentences" in index else ensure(index)
        index = build_index({index.get("path", "src") if "sentences" not in index else "src": u})
    q_terms = content(query)
    q_low = query.lower()
    n = len(index) or 1
    df = Counter(t for e in index for t in set(e["terms"]))
    avg = sum(len(e["terms"]) for e in index) / n or 1.0
    hits = []
    for e in index:
        if kinds and e["kind"] not in kinds:
            continue
        tf = Counter(e["terms"])
        s = 0.0
        for t in q_terms:
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(e["terms"]) / avg))
        tag_hits = [t for t in e["tags"] if t and re.search(rf"\b{re.escape(t)}\b", q_low)]
        s += 1.5 * len(tag_hits)
        if s > 0:
            hits.append({"kind": e["kind"], "src": e["src"], "start": e["start"], "end": e["end"], "text": e["text"],
                         "score": round(s * e["weight"], 3), "matched": sorted(set(q_terms) & set(tf)) + tag_hits})
    return sorted(hits, key=lambda h: (-h["score"], h["start"]))[:top]
