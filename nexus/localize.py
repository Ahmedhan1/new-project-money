"""Multilingual versions of one timeline: adapted script, timed dub, captions, localised on-screen text."""
from __future__ import annotations

import copy
import re

from . import llm, qa
from .media import ff
from .speech import mt, packs as lp, tts
from .timeline import subs
from .timeline.render import clip_duration, clip_starts

def _langs() -> dict:
    """Language table derived from the language packs (nexus/registry/languages.yaml)."""
    from .speech import packs as _p
    return {t: {"name": x["name"], "tts": t.split("-")[0]} for t, x in _p.packs().items()}


LANGS = _langs()

STOPWORDS = set("that this with from have they their there which would could about than then also been were what when will more most very much many some such into over only just even because while where these those other being does".split())
PACE = {"ar": 0.85}
VOICEOVER_ORIGINAL_GAIN = 0.2   # original speech under a voice-over: about -14 dB, audible but not competing
VOICEOVER_SPILL = 0.5           # a voice-over line may run this far past its cut (never into the next line's start)
VOICEOVER_MIN_SPEED = 0.85
VOICEOVER_MAX_DRIFT = 1.2       # a voice-over line starting later than this after its cut is flagged for review      # slowest the real speaker's picture may be played under a voice-over
VOICEOVER_DELAY = 0.6           # the voice-over starts a beat after the original, as in broadcast voice-over  # the Arabic voice reads slowly; this brings it to a natural narration pace

EGYPTIAN_STYLE = """Egyptian colloquial Arabic (Masri) as spoken in Cairo, written in Arabic script only.
Use everyday Egyptian words, for example: ده/دي (this), مش (not), عشان (because), دلوقتي (now), إزاي (how),
ليه (why), كده (like this), اللي (which), أوي (very), بتاع (of), and b-prefixed present verbs such as بيحوّل، بنستخدم.
Example - MSA: "هذا يجعل الطاقة الشمسية أرخص مصدر للكهرباء" -> Masri: "ده بيخلّي الطاقة الشمسية أرخص مصدر للكهربا"."""
EGYPTIAN_MARKERS = ["ده", "دي", "مش", "عشان", "علشان", "دلوقتي", "إزاي", "ازاي", "ليه", "كده", "اللي", "أوي", "قوي", "بتاع",
                    "إحنا", "احنا", "كمان", "بقى", "لسه", "عايز", "هي دي", "فيه", "زي", "يعني"]


def packs() -> dict:
    return lp.packs()


def pack(tag: str) -> dict:
    return lp.pack(tag)


def qa_cues(cues: list[dict], tag: str, **kw) -> dict:
    """Language QA of target-language caption cues (speech/packs.qa)."""
    return lp.qa(cues, tag, **kw)


def _instruction(lang: str) -> str:
    style = ((lp.get(lang) or {}).get("dialect") or {}).get("llm_style")
    if style:
        return style
    extra = " Write numbers the way a narrator would say them." if lang != "en" else ""
    return f"{LANGS[lang]['name']}. Natural, native phrasing for a spoken video narration - adapt, do not translate word for word.{extra}"


def foreign_words(text: str, lang: str) -> list[str]:
    """Words written in the wrong script for the target language (code-switching artefacts)."""
    if lp.get(lang):
        return lp.foreign_runs(text, lang)
    bad = []
    for w in re.findall(r"[^\W\d_]+", text):
        arabic = sum("\u0600" <= ch <= "\u06ff" or "\ufb50" <= ch <= "\ufefc" for ch in w)
        latin = sum(ch.isascii() or "\u00c0" <= ch <= "\u024f" for ch in w)
        if lang.startswith("ar") and arabic != len(w):
            bad.append(w)
        elif not lang.startswith("ar") and latin != len(w):
            bad.append(w)
    return bad


def _clean(text: str) -> str:
    text = text.strip().strip('"«»“”').strip()
    text = re.sub(r"^(translation|übersetzung|الترجمة)\s*[:：]\s*", "", text, flags=re.I)
    return re.sub(r"^\d+[.)]\s*", "", text.splitlines()[0] if text else text).strip()


def translate_lines(lines: list[str], lang: str, context: str = "") -> list[str]:
    """Primary: MADLAD-400 (dedicated MT). Fallback per line: the local LLM with validation."""
    if not lines:
        return []
    try:
        out = mt.translate(lines, lang)
    except Exception:  # model missing / not enough RAM -> LLM path
        out = llm_translate_lines(lines, lang, context)
        return [_finish_line(x, lang) for x in out]
    for i, (src, got) in enumerate(zip(lines, out)):
        if not got or foreign_words(got, lang) or mt.too_long(src, got, lang):
            try:
                out[i] = llm_translate_lines([src], lang, context)[0]
            except Exception:  # no local LLM installed: keep the MT line; language QA reports what is wrong with it
                pass
    return [_finish_line(x, lang) for x in out]


GROUP_MAX_WORDS = 28  # longer inputs made MADLAD drop whole clauses (a 49-word group lost its last sentence)


def _short(src: str, tgt: str, lang: str) -> bool:
    """Target much shorter than the source: content was probably dropped (word count for spaced scripts)."""
    if " " in tgt.strip() and lang not in ("zh", "ja"):
        return len(tgt.split()) < 0.6 * len(src.split())
    return len(tgt) < 0.4 * len(src)


def sentence_groups(lines: list[str], max_words: int = GROUP_MAX_WORDS) -> list[list[int]]:
    """Consecutive lines that together form whole sentences (a line not ending in . ! ? continues into the next),
    without growing past `max_words` source words."""
    groups, cur = [], []
    for i, ln in enumerate(lines):
        if cur and sum(len(lines[j].split()) for j in cur) + len(ln.split()) > max_words:
            groups.append(cur)
            cur = []
        cur.append(i)
        if re.search(r"[.!?…。！？]['\")\]]?\s*$", ln.strip()) or i == len(lines) - 1:
            groups.append(cur)
            cur = []
    return groups


def split_by_share(text: str, weights: list[int]) -> list[str]:
    """Split a translated sentence over the clips it spans, in proportion to the source words of each clip.
    Scripts without spaces (zh) are split by characters."""
    if len(weights) == 1:
        return [text]
    spaced = " " in text.strip()
    toks = text.split() if spaced else list(text.strip())
    total = max(1, sum(weights))
    out, pos = [], 0
    for k, w in enumerate(weights):
        n = len(toks) - pos if k == len(weights) - 1 else max(1 if pos < len(toks) else 0, round(len(toks) * w / total))
        part = toks[pos: pos + n]
        pos += n
        out.append((" " if spaced else "").join(part))
    return out


def translate_sentences(lines: list[str], lang: str, context: str = "") -> list[str]:
    """Translate whole sentences, never fragments: lines are joined into sentences, translated, then split back.
    Fragment input made MT invent endings (\"the other big thing in\" -> \"... in der Wohnung\")."""
    groups = sentence_groups(lines)
    joined = [" ".join(lines[i] for i in g) for g in groups]
    tr = translate_lines(joined, lang, context)
    redo = [k for k, (g, src, t) in enumerate(zip(groups, joined, tr)) if len(g) > 1 and _short(src, t, lang)]
    if redo:  # merged sentence came back short: translate those lines one by one instead (never lose content)
        parts = translate_lines([lines[i] for k in redo for i in groups[k]], lang, context)
        out, pos = [""] * len(lines), 0
        for k in redo:
            for i in groups[k]:
                out[i] = parts[pos]
                pos += 1
        groups_left = [(g, t) for k, (g, t) in enumerate(zip(groups, tr)) if k not in redo]
    else:
        out, groups_left = [""] * len(lines), list(zip(groups, tr))
    for g, t in groups_left:
        for i, part in zip(g, split_by_share(t, [len(lines[i].split()) for i in g])):
            out[i] = part
    return out


def _finish_line(text: str, lang: str) -> str:
    """Dialect adaptation (rule-based, for packs with a dialect profile) and the language's typography."""
    p = lp.get(lang)
    if not p:
        return text
    if (p.get("dialect") or {}).get("lexicon"):
        text, _ = lp.adapt(text, lang)
    return lp.typography(text, lang)


def llm_translate_lines(lines: list[str], lang: str, context: str = "") -> list[str]:
    """Translate/adapt line by line (strict 1:1 alignment). Each line is validated and retried:
    a small local model occasionally code-switches, so wrong-script output is rejected, not shipped."""
    out: list[str] = []
    system = "You are a professional audiovisual translator. You output only the translated sentence, nothing else."
    for line in lines:
        prompt = (f"Target language: {_instruction(lang)}\n" + (f"Topic of the video: {context}\n" if context else "")
                  + f"Keep every fact and number exactly. Use only the target language's own script and words.\n\nSentence: {line}")
        best, best_bad = "", 10 ** 6
        for attempt in range(4):
            got = _clean(llm.chat(prompt, system, max_tokens=260, temperature=0.15 + 0.25 * attempt))
            bad = len(foreign_words(got, lang)) + (1000 if not got or len(got) > 3 * len(line) + 40 else 0)
            if bad < best_bad:
                best, best_bad = got, bad
            if bad == 0:
                break
        out.append(best)
    return out


def _numbers(text: str) -> set[str]:
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    return set(re.findall(r"\d+(?:[.,]\d+)?", text.translate(trans)))


def language_review(source: list[str], target: list[str], lang: str, back: list[str] | None = None) -> dict:
    """Independent language QA. Deterministic checks + (weak) back-translation evidence."""
    checks = []
    joined = " ".join(target)
    checks += qa.script_checks(joined, lang)
    mixed = {i + 1: foreign_words(t, lang) for i, t in enumerate(target) if foreign_words(t, lang)}
    checks.append(qa._c(f"no code-switching [{lang}]", not mixed, "every word is in the target script" if not mixed else
                        f"wrong-script words after retries: {mixed}", "major", fix="retranslate"))
    checks.append(qa._c(f"line alignment [{lang}]", len(source) == len(target) and all(t.strip() for t in target),
                        f"{len(target)}/{len(source)} lines", fix="retranslate"))
    lost = [(i + 1, sorted(_numbers(s) - _numbers(t))) for i, (s, t) in enumerate(zip(source, target)) if _numbers(s) - _numbers(t)]
    checks.append(qa._c(f"numbers preserved [{lang}]", not lost,
                        "all digits carried over" if not lost else f"numbers missing/changed in lines {lost[:4]} (may be spelled out - verify)", "minor"))
    odd = [i + 1 for i, (s, t) in enumerate(zip(source, target)) if _short(s, t, lang) or (lang != "en" and mt.too_long(s, t, lang))]
    checks.append(qa._c(f"no added/dropped content [{lang}]", not odd, "line lengths consistent with the source" if not odd else
                        f"lines {odd} are much longer/shorter than the source - possible invented or missing content", "major", fix="retranslate"))
    if lang == "ar-EG":
        words = joined.split()
        hits = sum(any(w.strip("،.؟!").endswith(m) or w.strip("،.؟!") == m for m in EGYPTIAN_MARKERS) or
                   re.match(r"^(بي|بت|بن|بيت|هي|هت|هن)\w{2,}", w) is not None for w in words)
        dens = hits / max(len(words), 1)
        checks.append(qa._c("Egyptian dialect markers", dens >= 0.06,
                            f"{dens:.0%} of words are typical Masri forms" + ("" if dens >= 0.06 else " - reads as MSA, not Egyptian"), "major"))
    elif (lp.get(lang) or {}).get("dialect"):
        dens = lp.dialect_density(joined, lang)
        name = lp.pack(lang)["name"]
        checks.append(qa._c(f"{name} dialect markers", dens >= 0.04,
                            f"{dens:.0%} of words are typical {name} forms" + ("" if dens >= 0.04 else " - reads as MSA"), "major"))
    if back is not None:
        sims = [qa.text_similarity(s, b) for s, b in zip(source, back)]
        low = [i + 1 for i, x in enumerate(sims) if x < 0.35]
        checks.append(qa._c(f"back-translation meaning [{lang}]", len(low) <= len(sims) // 4,
                            f"mean overlap {sum(sims) / max(len(sims), 1):.0%}; weak lines {low} (read back by a different model)", "major"))
    if back is not None:
        # Added-content detector: content words the independent reader found that the source never said.
        added = {}
        for i, (src, b) in enumerate(zip(source, back)):
            sw = {w[:5] for w in re.findall(r"[a-z]{4,}", src.lower())}
            extra = [w for w in re.findall(r"[a-z]{4,}", b.lower()) if w[:5] not in sw and w not in STOPWORDS]
            if len(extra) >= 3 and len(extra) > 0.45 * max(len(sw), 1):
                added[i + 1] = extra[:6]
        checks.append(qa._c(f"no invented clauses [{lang}]", not added, "back-translation adds nothing new" if not added else
                            f"read-back contains ideas not in the source: {added}", "major", fix="retranslate"))
    checks.append(qa._c(f"native-speaker review [{lang}]", lang == "en", "source language" if lang == "en" else
                        "REQUIRED before publishing - machine translation can still miss nuance and tone", "minor"))
    return {"stage": f"language-{lang}", "checks": checks, "passed": all(c["ok"] or c["severity"] == "minor" for c in checks)}


def narration_units(tl: dict, analyses: dict | None = None) -> list[dict]:
    """What is spoken in each clip: from narration tracks (generated videos) or from the source transcript (edits)."""
    units = []
    starts = clip_starts(tl)
    for i, (c, st) in enumerate(zip(tl["clips"], starts)):
        txt = c.get("narration")
        if not txt and analyses and c.get("src") in analyses and analyses[c["src"]].get("transcript"):
            segs = analyses[c["src"]]["transcript"]["segments"]
            words = [w for s in segs for w in (s.get("words") or [])]
            if words:  # exactly the words heard in this clip: whole sentences when the cut follows sentence boundaries
                txt = " ".join(str(w.get("word", "")).strip() for w in words
                               if c["in"] - 0.05 <= (w["start"] + w["end"]) / 2 <= c["out"] + 0.05).strip()
                # (segment-based text gave fragments such as "the other big thing in", which MT completed with
                # invented words: "...in der Wohnung")
            else:
                txt = " ".join(s["text"] for s in segs if s["start"] >= c["in"] - 0.3 and s["end"] <= c["out"] + 0.3)
        if txt:
            units.append({"clip": i, "start": st, "text": txt.strip()})
    return units


def _voice_reference(project, tl: dict, analyses: dict | None) -> dict | None:
    """For SILMA voice cloning: a clean 4-7.5 s line of the original speaker (wav + transcript), or None."""
    from .speech import silma
    if not silma.available() or not analyses:
        return None
    for c in tl.get("clips", []):
        src = c.get("src")
        ref = silma.pick_reference(analyses.get(src) or {}) if src else None
        if ref:
            try:
                wav = silma.extract_reference(src, ref, project.dir / "audio" / "voice_reference.wav")
                return {"wav": str(wav), "text": ref["text"]}
            except Exception:  # noqa: BLE001 - no reference: Piper voice instead
                return None
    return None


def _restore_background(project, out: dict, review: dict, lang: str) -> None:
    """Dubbed (muted) clips get their own music/effects back from a Demucs background stem (GPU only)."""
    from .audio import separate
    muted = {c["id"] for c in out["clips"] if c.get("mute") and c.get("type") == "video"}
    if not muted:
        return
    if not separate.available():
        review["checks"].append(qa._c(f"dub background [{lang}]", False, "no source separation here (GPU studio only): "
                                      "the original music/effects under dubbed scenes are silent", "minor"))
        return
    bg = {}
    for src in {c["src"] for c in out["clips"] if c.get("id") in muted}:
        try:
            bg[src] = str(separate.background(src, project.dir / "audio" / "separation"))
        except Exception as e:  # noqa: BLE001 - the dub still works, only without its background
            review["checks"].append(qa._c(f"dub background [{lang}]", False, f"separation failed: {str(e)[:160]}", "minor"))
    items = separate.background_items(out, bg, muted)
    out.setdefault("audio", []).extend(items)
    if items:
        review["checks"].append(qa._c(f"dub background [{lang}]", True,
                                      f"original music/effects kept under {len(items)} dubbed scene(s) ({separate.LICENSE})", "minor"))


def localize(project, tl: dict, lang: str, analyses: dict | None = None, context: str = "", dub: bool = True,
             source_lang: str = "en", allow_noncommercial: bool = False, mode: str = "replace") -> tuple[dict, dict]:
    """Return (localised timeline, language review). The source timeline is not modified."""
    units = narration_units(tl, analyses)
    out = copy.deepcopy(tl)
    out["lang"] = lang
    src_lines = [u["text"] for u in units]
    overlay_texts = [t["text"] for c in tl["clips"] for t in c.get("text", [])]
    if lang == source_lang:
        lines, olines, back = src_lines, overlay_texts, None
    else:
        lines = translate_sentences(src_lines, lang, context)
        olines = translate_lines(overlay_texts, lang, context) if overlay_texts else []
        try:
            back = llm_translate_lines(lines, source_lang, context)  # different model than the translator = independent evidence
        except Exception as e:  # noqa: BLE001 - local LLM not installed: no back-translation evidence, said in the review
            back, back_note = None, f"back-translation not run ({type(e).__name__}: local LLM unavailable) - meaning was not cross-checked"
    k = 0
    for c in out["clips"]:
        for t in c.get("text", []):
            t["text"], t["lang"] = olines[k], lang
            k += 1
    # Review whole sentences (numbers and meaning move between the clips a sentence spans once it is split back).
    groups = sentence_groups(src_lines)
    j_src = [" ".join(src_lines[i] for i in g) for g in groups]
    j_tgt = [" ".join(lines[i] for i in g if lines[i]) for g in groups]
    j_back = [" ".join(back[i] for i in g if back[i]) for g in groups] if back else None
    review = language_review(j_src, j_tgt, lang, j_back)
    if lang != source_lang and back is None and "back_note" in locals():
        review["checks"].append(qa._c(f"back-translation [{lang}]", False, back_note, "minor"))
    review["lines"] = [{"source": s, "target": t} for s, t in zip(src_lines, lines)]
    voice_info = None
    voice_id = None
    reference = _voice_reference(project, tl, analyses) if dub and lang != source_lang else None
    if dub and lang != source_lang and lp.get(lang):
        try:
            voice_id = lp.voice_for(lang, allow_noncommercial)["id"]
        except LookupError as e:
            if not reference:  # SILMA (commercial licence, cloned voice) can still dub without a Piper voice
                review["checks"].append(qa._c(f"dub [{lang}]", False, f"not produced: {e}", "minor"))
                dub = False
    if dub and lang != source_lang:
        out["audio"] = []
        vo_delay: dict = {}
        adir = project.dir / "audio" / lang
        for u, line in zip(units, lines):
            c = out["clips"][u["clip"]]
            wav = adir / f"u{u['clip']:03d}.wav"
            voice_info = tts.synthesize(line, wav, LANGS[lang]["tts"], voice=voice_id, reference=reference, length_scale=PACE.get(LANGS[lang]["tts"], 1.0))
            slot = clip_duration(c)
            need = voice_info["duration"] + 0.25
            if need > slot * 1.05:  # 1st: a slightly brisker read
                voice_info = tts.synthesize(line, wav, LANGS[lang]["tts"], voice=voice_id, reference=reference,
                                            length_scale=PACE.get(LANGS[lang]["tts"], 1.0) * 0.88)
                need = voice_info["duration"] + 0.25
            if mode == "voiceover":
                # The picture belongs to the real speaker: never put them in slow motion. Order: brisker read (1.25x),
                # shorter delay for this line, up to VOICEOVER_SPILL s carried over the cut (an L-cut), and only then a
                # gentle slow-down (>= VOICEOVER_MIN_SPEED) with a review flag. (The first German voice-over slowed
                # 19 of 39 clips, down to 0.6x.)
                if voice_info["duration"] + 0.1 > slot:
                    voice_info = tts.synthesize(line, wav, LANGS[lang]["tts"], voice=voice_id, reference=reference,
                                                length_scale=PACE.get(LANGS[lang]["tts"], 1.0) * 0.8)
                d = voice_info["duration"]
                vo_delay[u["clip"]] = round(min(VOICEOVER_DELAY, max(0.0, slot - d - 0.1)), 3)
                over = d + 0.1 + vo_delay[u["clip"]] - slot - VOICEOVER_SPILL
                if over > 0 and c["type"] == "video":
                    sp = max(VOICEOVER_MIN_SPEED, slot / (slot + over))
                    c["speed"] = round(float(c.get("speed", 1.0)) * sp, 3)
                    if slot / (slot + over) < VOICEOVER_MIN_SPEED:
                        review["checks"].append(qa._c(f"voice-over fit [{lang}] scene {u['clip']}", False,
                                                      f"translation is {over:.1f}s too long even at 1.25x read and {VOICEOVER_MIN_SPEED}x "
                                                      "picture - shorten this line in review", "major", fix="shorten-line"))
                need = 0.0
            if need > slot:  # 2nd: stretch the picture so the sentence is never chopped
                if c["type"] == "video":
                    c["speed"] = round(float(c.get("speed", 1.0)) * slot / need, 3)
                    if c["speed"] < 0.6:
                        review["checks"].append(qa._c(f"dub timing [{lang}] scene {u['clip']}", False,
                                                      f"picture slowed to {c['speed']}x to fit the translation - consider a shorter line", "minor"))
                else:
                    c["duration"] = round(need, 3)
            if c["type"] == "video":
                if mode == "voiceover":  # UN-style voice-over: the real speaker stays audible underneath
                    c["volume"] = round(float(c.get("volume", 1.0)) * VOICEOVER_ORIGINAL_GAIN, 3)
                else:
                    c["mute"] = True  # original speech replaced; its music/effects come back from a Demucs stem (GPU)
            c["narration"] = line
            review["checks"].append(qa.dub_review(wav, line, lang))
        starts = clip_starts(out)
        cues = []
        prev_end = -1.0
        for u, line in zip(units, lines):
            wav = adir / f"u{u['clip']:03d}.wav"
            st = starts[u["clip"]] + 0.1
            at = st + (vo_delay.get(u["clip"], VOICEOVER_DELAY) if mode == "voiceover" else 0.0)
            if mode == "voiceover":
                # a line that spilled over its cut pushes the next one back: two synthetic voices never overlap
                # (the first run with spill had 4 overlaps)
                at = max(at, prev_end + 0.08)
                if at - starts[u["clip"]] > VOICEOVER_MAX_DRIFT:
                    review["checks"].append(qa._c(f"voice-over drift [{lang}] scene {u['clip']}", False,
                                                  f"line starts {at - starts[u['clip']]:.1f}s after its cut because the previous one "
                                                  "ran long - shorten the previous line in review", "major", fix="shorten-line"))
                prev_end = at + ff.duration(wav)
            out["audio"].append({"src": str(wav), "start": round(at, 3),
                                 "clip": out["clips"][u["clip"]]["id"], "role": "narration", "synthetic": True, "lang": lang})
            cues += _cues(line, at if mode == "voiceover" else st, (at if mode == "voiceover" else st) + ff.duration(wav), lang)
        if mode == "voiceover" and prev_end > 0:
            from .timeline.render import total_duration as _total
            over = prev_end + 0.3 - _total(out)
            last = out["clips"][-1] if out["clips"] else None
            if over > 0 and last is not None and last.get("type") == "video":
                # the last line runs past the picture: hold the final frame until it has been said (never cut a voice off)
                last["freeze"] = {"at": round(max(0.0, clip_duration(last) - 0.05), 3), "duration": round(over, 3)}
                review["checks"].append(qa._c(f"voice-over end [{lang}]", True,
                                              f"final frame held {over:.1f}s so the last line finishes", "minor"))
        _restore_background(project, out, review, lang)
        out["subtitles"] = subs.fix_timing(cues)
        review["voice"] = {k2: voice_info[k2] for k2 in ("voice", "license", "commercial", "engine")} if voice_info else None
        review["dub_mode"] = mode
        review["label"] = ("AI voice-over (synthetic voice) over the original speaker" if mode == "voiceover"
                           else "AI dub (synthetic voice replaces the original speech)")
        out["dub"] = {"mode": mode, "label": review["label"], "voice": review["voice"], "lang": lang}
        if voice_info and not voice_info["commercial"]:
            review["checks"].append(qa._c(f"voice licence [{lang}]", False, f"{voice_info['voice']}: {voice_info['license']}", "major"))
    elif lang != source_lang:
        # subtitles only: keep original audio, translate cue text over the same timings
        starts = clip_starts(out)
        cues = []
        for u, line in zip(units, lines):
            c = out["clips"][u["clip"]]
            cues += _cues(line, starts[u["clip"]] + 0.1, starts[u["clip"]] + clip_duration(c) - 0.1, lang)
        out["subtitles"] = subs.fix_timing(cues)
    if lp.get(lang) and out.get("subtitles"):
        for c in lp.qa(out["subtitles"], lang)["checks"]:
            review["checks"].append(qa._c(f"subtitles {c['id']} [{lang}]", c["ok"], c["detail"], c["severity"]))
    review["passed"] = all(c["ok"] or c["severity"] == "minor" for c in review["checks"])
    return out, review


def _cues(text: str, start: float, end: float, lang: str) -> list[dict]:
    """Caption cues by the language pack's rules (reading speed, line length, CJK character breaking)."""
    if lp.get(lang):
        return [{k: v for k, v in c.items() if k != "over_cps"} for c in lp.segment(text, start, end, lang)]
    return subs.cues_from_text(text, start, end)
