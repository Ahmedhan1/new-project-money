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
PACE = {"ar": 0.85}  # the Arabic voice reads slowly; this brings it to a natural narration pace

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
    odd = [i + 1 for i, (s, t) in enumerate(zip(source, target)) if len(t) < 0.4 * len(s) or (lang != "en" and mt.too_long(s, t, lang))]
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
            txt = " ".join(s["text"] for s in segs if s["start"] >= c["in"] - 0.3 and s["end"] <= c["out"] + 0.3)
        if txt:
            units.append({"clip": i, "start": st, "text": txt.strip()})
    return units


def localize(project, tl: dict, lang: str, analyses: dict | None = None, context: str = "", dub: bool = True,
             source_lang: str = "en", allow_noncommercial: bool = False) -> tuple[dict, dict]:
    """Return (localised timeline, language review). The source timeline is not modified."""
    units = narration_units(tl, analyses)
    out = copy.deepcopy(tl)
    out["lang"] = lang
    src_lines = [u["text"] for u in units]
    overlay_texts = [t["text"] for c in tl["clips"] for t in c.get("text", [])]
    if lang == source_lang:
        lines, olines, back = src_lines, overlay_texts, None
    else:
        lines = translate_lines(src_lines, lang, context)
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
    review = language_review(src_lines, lines, lang, back)
    if lang != source_lang and back is None and "back_note" in locals():
        review["checks"].append(qa._c(f"back-translation [{lang}]", False, back_note, "minor"))
    review["lines"] = [{"source": s, "target": t} for s, t in zip(src_lines, lines)]
    voice_info = None
    voice_id = None
    if dub and lang != source_lang and lp.get(lang):
        try:
            voice_id = lp.voice_for(lang, allow_noncommercial)["id"]
        except LookupError as e:
            review["checks"].append(qa._c(f"dub [{lang}]", False, f"not produced: {e}", "minor"))
            dub = False
    if dub and lang != source_lang:
        out["audio"] = []
        adir = project.dir / "audio" / lang
        for u, line in zip(units, lines):
            c = out["clips"][u["clip"]]
            wav = adir / f"u{u['clip']:03d}.wav"
            voice_info = tts.synthesize(line, wav, LANGS[lang]["tts"], voice=voice_id, length_scale=PACE.get(LANGS[lang]["tts"], 1.0))
            slot = clip_duration(c)
            need = voice_info["duration"] + 0.25
            if need > slot * 1.05:  # 1st: a slightly brisker read
                voice_info = tts.synthesize(line, wav, LANGS[lang]["tts"], voice=voice_id,
                                            length_scale=PACE.get(LANGS[lang]["tts"], 1.0) * 0.88)
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
            review["checks"].append(qa.dub_review(wav, line, lang))
        starts = clip_starts(out)
        cues = []
        for u, line in zip(units, lines):
            wav = adir / f"u{u['clip']:03d}.wav"
            st = starts[u["clip"]] + 0.1
            out["audio"].append({"src": str(wav), "start": round(st, 3), "clip": out["clips"][u["clip"]]["id"]})
            cues += _cues(line, st, st + ff.duration(wav), lang)
        out["subtitles"] = subs.fix_timing(cues)
        review["voice"] = {k2: voice_info[k2] for k2 in ("voice", "license", "commercial", "engine")} if voice_info else None
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
