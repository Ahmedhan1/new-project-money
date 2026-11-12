"""Multilingual versions of one timeline: adapted script, timed dub, captions, localised on-screen text.

Every language fact (MADLAD tag, script, voice, pace, length ratios, cue size, dub threshold, status) comes from
registry/languages.yaml through `models.language()`. Translation paths:
  madlad                   MADLAD-400 3B, guarded (sentence-count trim, length ratio, invented numbers, wrong script)
                           with a validated local-LLM fallback per line.
  madlad-msa+llm-dialect   (Gulf Arabic: no MADLAD tag) MSA by MADLAD, then dialect adaptation by the local LLM with strict
                           validation (Arabic script only, Gulf-marker density, no Egyptian-only forms, numbers, length).
                           Lines that fail are reported, never shipped as "Gulf".
"""
from __future__ import annotations

import copy
import difflib
import re
import unicodedata

from . import llm, models, qa
from .media import ff
from .speech import mt, tts
from .timeline import subs
from .timeline.render import clip_duration, clip_starts


class LanguageUnavailable(ValueError):
    """The registry marks this language as one the pipeline cannot produce correctly."""


def _langs() -> dict:
    return {c: {"name": e["name"], "tts": e["tts"]} for c, e in models.languages().items()}


LANGS = _langs()   # {code: {name, tts}} - kept for callers of the old constant; the registry is the source
PACE = {e["tts"]: e["pace"] for e in models.languages().values() if e.get("pace", 1.0) != 1.0}

STOPWORDS = set("that this with from have they their there which would could about than then also been were what when will more most very much many some such into over only just even because while where these those other being does".split())

EGYPTIAN_STYLE = """Egyptian colloquial Arabic (Masri) as spoken in Cairo, written in Arabic script only.
Use everyday Egyptian words, for example: ده/دي (this), مش (not), عشان (because), دلوقتي (now), إزاي (how),
ليه (why), كده (like this), اللي (which), أوي (very), بتاع (of), and b-prefixed present verbs such as بيحوّل، بنستخدم.
Example - MSA: "هذا يجعل الطاقة الشمسية أرخص مصدر للكهرباء" -> Masri: "ده بيخلّي الطاقة الشمسية أرخص مصدر للكهربا"."""
GULF_STYLE = """Gulf Arabic (Khaleeji) as spoken in the UAE, Kuwait, Qatar, Bahrain and eastern Saudi Arabia, written in Arabic script only.
Use everyday Gulf words, for example: الحين (now), وايد (very / a lot), شلون (how), ليش (why), شنو / وش (what), مو / مب (not),
أبي / يبي / نبي (want), هال (this, prefix: هالمشكلة), جذي / كذا (like this), زين (good), حق (for/of), اللي (which), قاعد (-ing),
and b-prefixed future verbs such as بيصير، بنشوف. Do NOT use Egyptian words (ده، دي، دلوقتي، إزاي، كده، أوي، مش، بتاع).
Examples - MSA -> Gulf: "نحن الآن بحاجة إلى حل أفضل." -> "الحين نحتاج حل أحسن." ; "لماذا هذه المشكلة كبيرة جدا؟" -> "ليش هالمشكلة كبيرة وايد؟" ;
"هذا ليس سهلاً، لكنه يعمل." -> "هذا مو سهل، بس يشتغل." ; "يبني المهندسون مصانع أكبر." -> "المهندسين قاعدين يبنون مصانع أكبر"."""
DIALECT_STYLE = {"ar-EG": EGYPTIAN_STYLE, "ar-GULF": GULF_STYLE}
EGYPTIAN_MARKERS = models.dialect_markers("ar-EG")["words"]


def entry(lang: str) -> dict:
    return models.language(lang)


def _instruction(lang: str) -> str:
    e = entry(lang)
    if e["code"] in DIALECT_STYLE:
        return DIALECT_STYLE[e["code"]]
    extra = " Write numbers as digits, exactly as in the source." if e["code"] != "en" else ""
    script = {"han": " Use Simplified Chinese characters only.", "devanagari": " Use Devanagari script only.",
              "cyrillic": " Use Cyrillic script only."}.get(e["script"], "")
    return f"{e['name']}. Natural, native phrasing for a spoken video narration - adapt, do not translate word for word.{extra}{script}"


def _runs(text: str) -> list[tuple[str, str]]:
    """Letter runs grouped by script: [(script|None, run)]. Combining marks (Devanagari matras, Arabic harakat) stay
    with the run they belong to; Han is one run even without spaces."""
    runs: list[tuple[str | None, str]] = []
    for ch in text:
        mark = unicodedata.category(ch).startswith("M")
        if not (ch.isalpha() or mark):
            runs.append(("", ""))
            continue
        sc = runs[-1][0] if mark and runs and runs[-1][1] else models.char_script(ch)
        if runs and runs[-1][1] and runs[-1][0] == sc:
            runs[-1] = (sc, runs[-1][1] + ch)
        else:
            runs.append((sc, ch))
    return [(sc, r) for sc, r in runs if r]


def _acronym(run: str) -> bool:
    return run.isascii() and run.isupper() and len(run) <= 5    # AI, CO, USB: legitimately Latin in any script


def foreign_words(text: str, lang: str) -> list[str]:
    """Letter runs written in the wrong script for the target language (code-switching artefacts)."""
    script = models.script_for(lang)
    return [r for sc, r in _runs(text) if sc != script and not (script != "latin" and _acronym(r))]


def script_share(text: str, lang: str) -> float:
    script = models.script_for(lang)
    letters = [ch for ch in text if ch.isalpha()]
    return sum(models.in_script(ch, script) for ch in letters) / max(len(letters), 1)


def _clean(text: str) -> str:
    text = text.strip().strip('"«»“”「」').strip()
    text = re.sub(r"^(translation|übersetzung|traduction|traducción|перевод|अनुवाद|翻译|译文|الترجمة|gulf|خليجي)\s*[:：]\s*", "", text, flags=re.I)
    return re.sub(r"^\d+[.)]\s*", "", text.splitlines()[0] if text else text).strip()


def _line_problems(src: str, got: str, lang: str, source_lang: str = "en") -> list[str]:
    probs = []
    if not got.strip():
        return ["empty"]
    if foreign_words(got, lang):
        probs.append("wrong-script")
    if mt.too_long(src, got, lang, source_lang):
        probs.append("too-long")
    if mt.too_short(src, got, lang, source_lang):
        probs.append("too-short")
    if mt.added_numbers(src, got, source_lang, lang):
        probs.append("added-number")
    return probs


def translate_lines(lines: list[str], lang: str, context: str = "", source_lang: str = "en") -> list[str]:
    """Primary: MADLAD-400 (dedicated MT). Fallback per line: the local LLM with validation.
    Dialects without a MADLAD tag (Gulf) go MSA -> validated LLM adaptation (`adapt_dialect`)."""
    if not lines:
        return []
    e = entry(lang)
    if e.get("msa_pivot"):
        msa = translate_lines(lines, e["msa_pivot"], context, source_lang)
        return adapt_dialect(lines, msa, lang, context)["lines"]
    try:
        out = mt.translate(lines, e["code"], source_lang)
    except Exception:  # model missing / not enough RAM -> LLM path
        return llm_translate_lines(lines, lang, context, source_lang)
    for i, (src, got) in enumerate(zip(lines, out)):
        if _line_problems(src, got, lang, source_lang):
            alt = llm_translate_lines([src], lang, context, source_lang)[0]
            if len(_line_problems(src, alt, lang, source_lang)) < len(_line_problems(src, got, lang, source_lang)):
                out[i] = alt
    return out


def llm_translate_lines(lines: list[str], lang: str, context: str = "", source_lang: str = "en") -> list[str]:
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
            bad = len(foreign_words(got, lang)) + 1000 * (not got) + 100 * ("too-long" in _line_problems(line, got, lang, source_lang)
                                                                          or len(got) > 3 * len(line) + 40)
            if bad < best_bad:
                best, best_bad = got, bad
            if bad == 0:
                break
        out.append(best)
    return out


# ---------------------------------------------------------------- dialects
def dialect_density(text: str, dialect: str) -> dict:
    """Share of words that are typical forms of the dialect (and, for Gulf, of Egyptian-only forms that must be absent)."""
    m = models.dialect_markers(dialect)
    words = [w.strip("،.؟!؛:\"'") for w in text.split()]
    words = [w for w in words if w]
    pref = re.compile(m["prefixes"]) if m.get("prefixes") else None

    def hit(w: str, vocab: list[str]) -> bool:
        return any(w == v or (len(v) > 2 and w.endswith(v)) for v in vocab)
    hits = sum(hit(w, m["words"]) or (pref is not None and pref.match(w) is not None) for w in words)
    excl = sum(w in m.get("exclude", []) for w in words)
    n = max(len(words), 1)
    return {"density": hits / n, "excluded": excl / n, "min": m["min_density"], "words": len(words)}


def dialect_ok(text: str, dialect: str) -> bool:
    d = dialect_density(text, dialect)
    return d["density"] >= d["min"] and d["excluded"] <= d["density"] / 3


def adapt_dialect(src_lines: list[str], msa_lines: list[str], lang: str, context: str = "", attempts: int = 4) -> dict:
    """MSA -> dialect by the local LLM. A candidate is accepted only if it is Arabic-script only, carries the dialect's
    markers (and not Egyptian-only forms), keeps every number of the source and stays within 0.6-1.4x the MSA length.
    Returns {lines, valid: [bool], problems: {line: [..]}}; invalid lines keep the best candidate and are reported."""
    e = entry(lang)
    system = (f"You adapt narration into spoken {e['name']}. You output only the adapted sentence in Arabic script, nothing else.")
    out, valid, problems = [], [], {}
    for i, (src, msa) in enumerate(zip(src_lines, msa_lines)):
        prompt = (f"Rewrite this Modern Standard Arabic narration line in {DIALECT_STYLE[e['code']]}\n"
                  "Keep every fact and number exactly; add nothing, drop nothing. Do not leave it in formal MSA.\n"
                  + (f"Topic: {context}\n" if context else "") + f"English original (for meaning): {src}\nMSA: {msa}\n{e['name']}:")
        best, best_score, best_probs = msa, 10 ** 6, ["not adapted"]
        for a in range(attempts):
            got = _clean(llm.chat(prompt, system, max_tokens=220, temperature=0.2 + 0.2 * a))
            probs = []
            if not got or foreign_words(got, lang):
                probs.append("wrong-script")
            if not dialect_ok(got, e["code"]):
                probs.append(f"dialect markers {dialect_density(got, e['code'])['density']:.0%}")
            if got.strip() == msa.strip():
                probs.append("unchanged MSA")
            if not (0.6 * len(msa) <= len(got) <= 1.4 * len(msa) + 6):
                probs.append("length")
            lost = mt.numbers(src) - mt.numbers(got, "ar")
            if lost or mt.added_numbers(src, got, "en", "ar"):
                probs.append("numbers")
            score = len(probs) * 10 + ("wrong-script" in probs) * 50
            if score < best_score:
                best, best_score, best_probs = got, score, probs
            if not probs:
                break
        out.append(best)
        valid.append(not best_probs)
        if best_probs:
            problems[i + 1] = best_probs
    return {"lines": out, "valid": valid, "problems": problems}


# ---------------------------------------------------------------- review
def _sim(a: str, b: str, lang: str) -> float:
    """Word overlap; per character for Han (no word spaces)."""
    if models.script_for(lang) == "han":
        def norm(t: str) -> str:
            t = unicodedata.normalize("NFKC", t).translate(models.DIGITS)
            return "".join(ch for ch in t if ch.isalnum())
        return round(difflib.SequenceMatcher(None, norm(a), norm(b)).ratio(), 3)
    return qa.text_similarity(a, b)


def dub_check(audio_path, intended_text: str, lang: str, threshold: float | None = None) -> dict:
    """Independent check of a dub line: Whisper transcribes it back (language + Simplified prompt from the registry) and
    the share of script words (Han: characters) it hears is compared with the language's measured threshold."""
    from .speech import asr
    e = entry(lang)
    thr = e.get("dub_threshold", 0.6) if threshold is None else threshold
    t = asr.transcribe(audio_path, language=e["code"], words=False)
    sim = _sim(intended_text, t["text"], lang)
    return qa._c(f"dub intelligibility [{lang}]", sim >= thr,
                 f"Whisper heard {sim:.0%} of the script {'characters' if e['script'] == 'han' else 'words'} back (threshold {thr:.0%})"
                 + ("" if sim >= thr else f": \"{t['text'][:90]}\""), "major", fix="resynthesize") | {"similarity": sim, "heard": t["text"]}


_JUNK = re.compile(r"<think>|```|\{\"|^\s*(Translation|Here is|以下是|翻译)", re.I | re.M)


def language_review(source: list[str], target: list[str], lang: str, back: list[str] | None = None, source_lang: str = "en",
                    dialect_info: dict | None = None) -> dict:
    """Independent language QA: script purity, alignment, numbers (any numeral system), added/dropped content,
    dialect markers, back-translation meaning, invented clauses. Thresholds per language from the registry."""
    e = entry(lang)
    checks = []
    joined = " ".join(target)
    share = script_share(joined, lang)
    checks.append(qa._c(f"script [{lang}]", share > 0.85, f"{share:.0%} {e['script']} letters", "major", fix="retranslate"))
    junk = _JUNK.search(joined)
    checks.append(qa._c(f"clean text [{lang}]", not junk, "no model artefacts" if not junk else f"artefact: {junk.group(0)[:20]}", "major", fix="retranslate"))
    mixed = {i + 1: foreign_words(t, lang) for i, t in enumerate(target) if foreign_words(t, lang)}
    checks.append(qa._c(f"no code-switching [{lang}]", not mixed, "every word is in the target script" if not mixed else
                        f"wrong-script words after retries: {mixed}", "major", fix="retranslate"))
    checks.append(qa._c(f"line alignment [{lang}]", len(source) == len(target) and all(t.strip() for t in target),
                        f"{len(target)}/{len(source)} lines", fix="retranslate"))
    lost = [(i + 1, sorted(mt.numbers(s, source_lang) - mt.numbers(t, lang))) for i, (s, t) in enumerate(zip(source, target))
            if mt.numbers(s, source_lang) - mt.numbers(t, lang)]
    checks.append(qa._c(f"numbers preserved [{lang}]", not lost,
                        "all numbers carried over (any numeral system)" if not lost else
                        f"numbers missing/changed in lines {lost[:4]} (may be spelled out - verify)", "minor"))
    added = [(i + 1, sorted(mt.added_numbers(s, t, source_lang, lang))) for i, (s, t) in enumerate(zip(source, target))
             if mt.added_numbers(s, t, source_lang, lang)]
    checks.append(qa._c(f"no invented numbers [{lang}]", not added, "no number the source did not contain" if not added else
                        f"lines {added} contain numbers the source never said - invented content", "major", fix="retranslate"))
    if e["code"] != source_lang:
        odd = [i + 1 for i, (s, t) in enumerate(zip(source, target)) if mt.too_short(s, t, lang, source_lang) or mt.too_long(s, t, lang, source_lang)]
        ratios = [round(mt.length_ratio(s, t, lang, source_lang), 2) for s, t in zip(source, target)]
        checks.append(qa._c(f"no added/dropped content [{lang}]", not odd,
                            f"line lengths consistent with the source (relative ratios {ratios})" if not odd else
                            f"lines {odd} are much longer/shorter than the source (relative ratios {ratios}) - possible invented or missing content",
                            "major", fix="retranslate"))
    if e.get("dialect"):
        d = dialect_density(joined, e["dialect"])
        ok = dialect_ok(joined, e["dialect"])
        label = {"ar-EG": "Egyptian", "ar-GULF": "Gulf"}.get(e["dialect"], e["dialect"])
        detail = f"{d['density']:.0%} of words are typical {label} forms (min {d['min']:.0%})"
        if d.get("excluded"):
            detail += f"; {d['excluded']:.0%} Egyptian-only forms"
        if dialect_info and dialect_info.get("problems"):
            detail += f"; lines rejected by the adapter: {dialect_info['problems']}"
        checks.append(qa._c(f"{label} dialect markers", ok and not (dialect_info and dialect_info.get("problems")),
                            detail + ("" if ok else f" - reads as MSA/other dialect, not {label}"), "major"))
    if back is not None:
        sims = [qa.text_similarity(s, b) for s, b in zip(source, back)]
        low = [i + 1 for i, x in enumerate(sims) if x < 0.35]
        checks.append(qa._c(f"back-translation meaning [{lang}]", len(low) <= len(sims) // 4,
                            f"mean overlap {sum(sims) / max(len(sims), 1):.0%}; weak lines {low} (read back by a different model)", "major"))
        # Added-content detector: content words the independent reader found that the source never said.
        added_c = {}
        for i, (src, b) in enumerate(zip(source, back)):
            sw = {w[:5] for w in re.findall(r"[a-z]{4,}", src.lower())}
            extra = [w for w in re.findall(r"[a-z]{4,}", b.lower()) if w[:5] not in sw and w not in STOPWORDS]
            if len(extra) >= 3 and len(extra) > 0.45 * max(len(sw), 1):
                added_c[i + 1] = extra[:6]
        checks.append(qa._c(f"no invented clauses [{lang}]", not added_c, "back-translation adds nothing new" if not added_c else
                            f"read-back contains ideas not in the source: {added_c}", "major", fix="retranslate"))
    checks.append(qa._c(f"native-speaker review [{lang}]", e["code"] == source_lang, "source language" if e["code"] == source_lang else
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
            txt = " ".join(s["text"] for s in segs if s["start"] >= c["in"] - 0.3 and s["end"] <= c["out"] + 0.3)
        if txt:
            units.append({"clip": i, "start": st, "text": txt.strip()})
    return units


def source_language_of(analyses: dict | None) -> dict | None:
    """Whisper's detection stored in the analysis transcripts: {code, whisper, probability} of the longest transcript."""
    from .speech import asr
    best = None
    for a in (analyses or {}).values():
        t = (a or {}).get("transcript") or {}
        if t.get("language") and (best is None or (t.get("duration") or 0) > (best.get("duration") or 0)):
            best = t
    if not best:
        return None
    return {"whisper": best["language"], "code": asr.registry_code_for_whisper(best["language"]),
            "probability": best.get("language_probability", 0.0), "dialect_unknown": best["language"] == "ar", "duration": best.get("duration")}


def localize(project, tl: dict, lang: str, analyses: dict | None = None, context: str = "", dub: bool = True,
             source_lang: str = "en", experimental: bool = False) -> tuple[dict, dict]:
    """Return (localised timeline, language review). The source timeline is not modified.
    Languages the registry marks `unavailable` are refused unless experimental=True (then the review records why they fail).
    Translating into the language the footage is already in is refused (declared source or confident Whisper detection)."""
    from .speech import asr
    e = entry(lang)
    lang = e["code"]
    if e["status"] == "unavailable" and not experimental:
        raise LanguageUnavailable(f"{e['name']} ({lang}) is not available: {e['reason']}")
    source_lang = models.resolve_lang(source_lang) or source_lang
    detected = source_language_of(analyses)
    is_source = asr.same_language(source_lang, lang)
    if not is_source:
        asr.check_translation_pair(source_lang, lang, detected)
    units = narration_units(tl, analyses)
    out = copy.deepcopy(tl)
    out["lang"] = lang
    src_lines = [u["text"] for u in units]
    overlay_texts = [t["text"] for c in tl["clips"] for t in c.get("text", [])]
    dialect_info = None
    if is_source:
        lines, olines, back = src_lines, overlay_texts, None
    else:
        if e.get("msa_pivot"):
            msa = translate_lines(src_lines, e["msa_pivot"], context, source_lang)
            dialect_info = adapt_dialect(src_lines, msa, lang, context) | {"msa": msa}
            lines = dialect_info["lines"]
        else:
            lines = translate_lines(src_lines, lang, context, source_lang)
        olines = translate_lines(overlay_texts, lang, context, source_lang) if overlay_texts else []
        back = llm_translate_lines(lines, source_lang, context)  # different model than the translator = independent evidence
    k = 0
    for c in out["clips"]:
        for t in c.get("text", []):
            t["text"], t["lang"] = olines[k], lang
            k += 1
    review = language_review(src_lines, lines, lang, back, source_lang, dialect_info)
    review["lines"] = [{"source": s, "target": t} | ({"back": b} if back else {}) for s, t, b in zip(src_lines, lines, back or [None] * len(lines))]
    review["language"] = {k2: e.get(k2) for k2 in ("code", "name", "script", "direction", "madlad", "translation", "status", "reason")}
    if detected:
        review["source_detected"] = detected
    if dialect_info:
        review["dialect_adaptation"] = {"valid": dialect_info["valid"], "problems": dialect_info["problems"], "msa": dialect_info["msa"]}
    if e["status"] == "unavailable":
        review["checks"].append(qa._c(f"language status [{lang}]", False, f"registry: unavailable - {e['reason']}", "major"))
    voice_info = None
    pace = float(e.get("pace", 1.0))
    if dub and not is_source:
        out["audio"] = []
        adir = project.dir / "audio" / lang
        for u, line in zip(units, lines):
            c = out["clips"][u["clip"]]
            wav = adir / f"u{u['clip']:03d}.wav"
            voice_info = tts.synthesize(line, wav, e["tts"], length_scale=pace)
            slot = clip_duration(c)
            need = voice_info["duration"] + 0.25
            if need > slot * 1.05:  # 1st: a slightly brisker read
                voice_info = tts.synthesize(line, wav, e["tts"], length_scale=pace * 0.88)
                need = voice_info["duration"] + 0.25
            if need > slot:  # 2nd: stretch the picture so the sentence is never chopped
                if c["type"] == "video":
                    c["speed"] = round(float(c.get("speed", 1.0)) * slot / need, 3)
                    if c["speed"] < 0.6:
                        review["checks"].append(qa._c(f"dub timing [{lang}] scene {u['clip']}", False,
                                                      f"picture slowed to {c['speed']}x to fit the translation - consider a shorter line", "minor"))
                else:
                    c["duration"] = round(need, 3)
            if c["type"] == "video":
                c["mute"] = True  # original speech replaced (no source separation deployed: original music/effects go too)
            c["narration"] = line
            review["checks"].append(dub_check(wav, line, lang))
        starts = clip_starts(out)
        cues = []
        for u, line in zip(units, lines):
            wav = adir / f"u{u['clip']:03d}.wav"
            st = starts[u["clip"]] + 0.1
            out["audio"].append({"src": str(wav), "start": round(st, 3), "clip": out["clips"][u["clip"]]["id"]})
            cues += subs.cues_from_text(line, st, st + ff.duration(wav), max_chars=e.get("cue_chars"), lang=lang)
        out["subtitles"] = subs.fix_timing(cues)
        review["voice"] = {k2: voice_info.get(k2) for k2 in ("voice", "license", "commercial", "engine")} if voice_info else None
        if voice_info and not voice_info["commercial"]:
            review["checks"].append(qa._c(f"voice licence [{lang}]", False, f"{voice_info['voice']}: {voice_info['license']}", "major"))
        if voice_info and voice_info.get("engine") == "espeak-ng":
            review["checks"].append(qa._c(f"dub voice quality [{lang}]", False, "eSpeak NG formant voice (clear licence, robotic): draft dub only - "
                                          "no commercially cleared neural voice for this language yet", "major"))
        if voice_info and voice_info.get("voice") in models.catalog()["voices"]:
            attr = models.catalog()["voices"][voice_info["voice"]].get("attribution")
            if attr:
                review["voice"]["attribution"] = attr
                review["checks"].append(qa._c(f"voice attribution [{lang}]", False, f"credit required in the video description: {attr}", "minor"))
    elif not is_source:
        # subtitles only: keep original audio, translate cue text over the same timings
        starts = clip_starts(out)
        cues = []
        for u, line in zip(units, lines):
            c = out["clips"][u["clip"]]
            cues += subs.cues_from_text(line, starts[u["clip"]] + 0.1, starts[u["clip"]] + clip_duration(c) - 0.1,
                                        max_chars=e.get("cue_chars"), lang=lang)
        out["subtitles"] = subs.fix_timing(cues)
    review["passed"] = all(c["ok"] or c["severity"] == "minor" for c in review["checks"])
    return out, review
