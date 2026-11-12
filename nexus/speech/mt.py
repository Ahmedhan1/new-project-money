"""Machine translation with MADLAD-400 3B (Apache-2.0) on CTranslate2, CPU int8.

Chosen over the local LLM after a side-by-side test (docs/results/C_language.json):
the 4B LLM produced grammar errors, a meaning error and code-switched words; MADLAD
gave correct MSA and German and genuine Egyptian Arabic (language tag `arz`).
"""
from __future__ import annotations

import gc
import re
import time

from .. import costguard, db, models

TAGS = {"en": "en", "ar": "ar", "ar-EG": "arz", "de": "de", "fr": "fr", "es": "es", "tr": "tr"}
_tr = None
_sp = None


def target_tag(lang: str) -> str:
    """MADLAD target tag: the legacy table, else the language pack's mt.target (packs list only tags MADLAD supports)."""
    if lang in TAGS:
        return TAGS[lang]
    from . import packs
    return packs.pack(lang)["mt"]["target"]


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


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?؟。])\s+", text.strip()) if s]


# Typical character-length ratio target/source for spoken text; well above it means invented content.
MAX_RATIO = {"ar": 1.3, "ar-EG": 1.3, "de": 1.38, "en": 1.4}


def too_long(src: str, tgt: str, lang: str) -> bool:
    return len(tgt) > MAX_RATIO.get(lang, 1.5) * len(src) + 6


def _decode(tr, sp, lines: list[str], lang: str, **kw) -> list[str]:
    toks = [sp.encode(f"<2{target_tag(lang)}> {l}", out_type=str) for l in lines]
    res = tr.translate_batch(toks, max_batch_size=4, **kw)
    out = []
    for src, r in zip(lines, res):
        txt = sp.decode_pieces(r.hypotheses[0]).strip()
        # The model sometimes appends a paraphrase of the same sentence; never return more sentences than the source had.
        out.append(" ".join(_sentences(txt)[:max(1, len(_sentences(src)))]))
    return out


def translate(lines: list[str], lang: str) -> list[str]:
    """Translate 1:1. Lines that come back suspiciously long (the model adding a clause of its own)
    are re-decoded with settings that favour short, literal output; the shortest candidate wins."""
    costguard.authorize("madlad-mt", "translate")
    tr, sp = load()
    t0 = time.time()
    out = _decode(tr, sp, lines, lang, beam_size=4, max_decoding_length=220, repetition_penalty=1.15, no_repeat_ngram_size=5)
    for i, (src, got) in enumerate(zip(lines, out)):
        if not too_long(src, got, lang):
            continue
        cap = int(len(sp.encode(src)) * 1.5) + 8
        cands = [got]
        for kw in ({"beam_size": 1}, {"beam_size": 6, "length_penalty": 0.4}, {"beam_size": 2, "repetition_penalty": 1.3}):
            cands += _decode(tr, sp, [src], lang, max_decoding_length=cap, no_repeat_ngram_size=5, **kw)
        ok = [c for c in cands if not too_long(src, c, lang) and re.search(r"[.!?؟]$", c)]
        out[i] = min(ok or cands, key=len)
    db.metric("mt_sec_per_line", (time.time() - t0) / max(len(lines), 1))
    return out
