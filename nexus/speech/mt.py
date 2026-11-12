"""Machine translation with MADLAD-400 3B (Apache-2.0) on CTranslate2, CPU int8.

Chosen over the local LLM after a side-by-side test (docs/results/C_language.json):
the 4B LLM produced grammar errors, a meaning error and code-switched words; MADLAD
gave correct MSA and German and genuine Egyptian Arabic (language tag `arz`).
Target tags, length ratios and sentence terminators come from registry/languages.yaml; every tag there was verified to
exist in the model vocabulary (`vocab_has_tag`). Gulf/Najdi/Levantine tags do NOT exist in MADLAD-400.
"""
from __future__ import annotations

import gc
import re
import time

from .. import config, costguard, db, models

_tr = None
_sp = None


def load():
    global _tr, _sp
    if _tr is None:
        costguard.authorize("madlad-mt", "load")
        import ctranslate2
        import sentencepiece as spm
        d = models.ensure_model("mt-madlad400-3b")
        _tr = ctranslate2.Translator(str(d), device="cpu", compute_type="int8", intra_threads=4)
        _sp = spm.SentencePieceProcessor(str(d / "spiece.model"))
    return _tr, _sp


def unload() -> None:
    global _tr, _sp
    _tr = _sp = None
    gc.collect()


def tag_for(lang: str) -> str:
    """MADLAD target tag for a registry language (KeyError when the language has no MADLAD tag, e.g. Gulf Arabic)."""
    e = models.language(lang)
    if not e.get("madlad"):
        raise KeyError(f"{e['code']} has no MADLAD-400 tag ({e.get('translation')})")
    return e["madlad"]


def vocab_has_tag(tag: str, model_dir=None) -> bool:
    """Check the real model vocabulary (shared_vocabulary.json) instead of assuming a tag exists."""
    import json
    d = model_dir or (config.MODELS / models.catalog()["models"]["mt-madlad400-3b"]["dir"])
    return f"<2{tag}>" in set(json.loads((d / "shared_vocabulary.json").read_text(encoding="utf-8")))


_TERMINATORS = ".!?\u061f\u3002\uff01\uff1f\u0964\u0965\u2026"


def _sentences(text: str) -> list[str]:
    # Spaced scripts end a sentence at terminator + space; Han (。！？) and Devanagari (।) need no space after it.
    parts = re.split(r"(?<=[.!?\u061f\u2026])\s+|(?<=[\u3002\uff01\uff1f\u0964\u0965])\s*", text.strip())
    return [p for p in parts if p and p.strip()]


def _ratio(lang: str) -> dict:
    e = models.language(lang, strict=False)
    return (e or {}).get("length_ratio") or {"typical": 1.0, "max": 1.5, "min": 0.4}


def length_ratio(src: str, tgt: str, lang: str, source_lang: str = "en") -> float:
    """Target length relative to what is typical for this language pair (1.0 = typical). Characters, not words:
    Han has no spaces and Arabic/Devanagari words differ in length, so characters are the comparable unit."""
    exp = _ratio(lang)["typical"] / _ratio(source_lang)["typical"]
    return len(tgt.strip()) / max(len(src.strip()) * exp, 1)


def too_long(src: str, tgt: str, lang: str, source_lang: str = "en") -> bool:
    r = _ratio(lang)
    scale = r["typical"] / _ratio(source_lang)["typical"]
    return len(tgt.strip()) > r["max"] / _ratio(lang)["typical"] * scale * len(src.strip()) + 6 * min(scale, 1.0)


def too_short(src: str, tgt: str, lang: str, source_lang: str = "en") -> bool:
    r = _ratio(lang)
    return len(tgt.strip()) < r["min"] / _ratio(source_lang)["typical"] * len(src.strip())


_EN_NUM = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
                                      "sixteen seventeen eighteen nineteen".split())}
_EN_NUM.update({w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())})
_EN_MULT = {"hundred": 100, "thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9}


def _english_number_words(text: str) -> set[str]:
    """'ninety percent' -> {'90'}, 'sixteen thousand' -> {'16000'} (spoken-style English sources)."""
    out, cur, any_ = set(), 0, False
    for w in re.findall(r"[a-z]+", text.lower()) + ["."]:
        if w in _EN_NUM:
            cur, any_ = cur + _EN_NUM[w], True
        elif w in _EN_MULT and any_:
            cur *= _EN_MULT[w]
        elif w == "and" and any_:
            continue
        else:
            if any_:
                out.add(str(cur))
            cur, any_ = 0, False
    return out


def numbers(text: str, lang: str = "en") -> set[str]:
    """Numbers in a text, numeral-system independent (Arabic-Indic, Persian, Devanagari, full-width digits -> ASCII) and
    separator independent ('16,000' == '16.000' == '16 000'). English number words count too."""
    t = text.translate(models.DIGITS).replace("\u066b", ".").replace("\u066c", ",")
    t = re.sub(r"(?<=\d)[\s\u00a0\u202f.,'](?=\d)", "", t)
    found = set(re.findall(r"\d+", t))
    if lang.split("-")[0] == "en":
        found |= _english_number_words(text)
    return found


def added_numbers(src: str, tgt: str, source_lang: str = "en", lang: str = "") -> set[str]:
    """Numbers the translation contains that the source never had - MADLAD sometimes invents a clause such as
    'a drop of about 25%' (measured on fr/es/pt/it, 2026-11-12)."""
    return numbers(tgt, lang or "xx") - numbers(src, source_lang)


def _decode(tr, sp, lines: list[str], lang: str, **kw) -> list[str]:
    toks = [sp.encode(f"<2{tag_for(lang)}> {l}", out_type=str) for l in lines]
    res = tr.translate_batch(toks, max_batch_size=4, **kw)
    out = []
    for src, r in zip(lines, res):
        txt = sp.decode_pieces(r.hypotheses[0]).strip()
        # The model sometimes appends a paraphrase of the same sentence; never return more sentences than the source had.
        out.append(" ".join(_sentences(txt)[:max(1, len(_sentences(src)))]))
    return out


def _flaws(src: str, c: str, lang: str, source_lang: str) -> tuple:
    return (bool(added_numbers(src, c, source_lang, lang)), too_long(src, c, lang, source_lang),
            re.search(f"[{_TERMINATORS}]$", c) is None, too_short(src, c, lang, source_lang))


def translate(lines: list[str], lang: str, source_lang: str = "en") -> list[str]:
    """Translate 1:1. Lines that come back suspiciously long, or with numbers the source never had (the model adding a
    clause of its own), are re-decoded with settings that favour short, literal output; the best clean candidate wins."""
    costguard.authorize("madlad-mt", "translate")
    tr, sp = load()
    t0 = time.time()
    out = _decode(tr, sp, lines, lang, beam_size=4, max_decoding_length=220, repetition_penalty=1.15, no_repeat_ngram_size=5)
    for i, (src, got) in enumerate(zip(lines, out)):
        if not too_long(src, got, lang, source_lang) and not added_numbers(src, got, source_lang, lang):
            continue
        cap = int(len(sp.encode(src)) * 1.5) + 8
        cands = [got]
        for kw in ({"beam_size": 1}, {"beam_size": 6, "length_penalty": 0.4}, {"beam_size": 2, "repetition_penalty": 1.3}):
            cands += _decode(tr, sp, [src], lang, max_decoding_length=cap, no_repeat_ngram_size=5, **kw)

        out[i] = min(cands, key=lambda c, s=src: (_flaws(s, c, lang, source_lang), len(c)))
    db.metric("mt_sec_per_line", (time.time() - t0) / max(len(lines), 1))
    return out
