"""Egyptian vs Gulf Arabic as distinct profiles: marker lexicons, forbidden markers per register, a dialect
classifier (EG / Gulf / MSA scores) and a dialect adaptation pass (model router, task "translation") with a
deterministic lexical-substitution fallback.

Marker lists are hand-curated function words and fixed expressions (not a trained model). They tell the varieties
apart on typical narration text; they cannot judge naturalness - a native reviewer still approves every line.
"""
from __future__ import annotations

import re

# weight 1.0 = distinctive for the variety; 0.3-0.5 = shared with another variety (counts, but weakly)
PROFILES: dict[str, dict] = {
    "ar-EG": {
        "name": "Egyptian Arabic (Cairene Masri)",
        "markers": {"ده": 1, "دي": 1, "دول": 1, "مش": 1, "عشان": 1, "علشان": 1, "دلوقتي": 1, "إزاي": 1, "ازاي": 1,
                    "كده": 1, "أوي": 1, "اوي": 1, "بتاع": 1, "بتاعة": 1, "عايز": 1, "عايزين": 1, "إمتى": 1, "فين": 1,
                    "إيه": 1, "ايه": 1, "مفيش": 1, "لسه": 1, "بقى": 0.7, "كويس": 1, "كويسة": 1, "حاجة": 0.7,
                    "إحنا": 0.4, "احنا": 0.4, "اللي": 0.3, "بس": 0.3, "فيه": 0.3, "زي": 0.5, "خالص": 0.7, "برضه": 1,
                    "النهارده": 1, "انهارده": 1, "امبارح": 1, "بكرة": 0.7},
        "patterns": [(r"^ب[يتن][ء-ي]{3,}$", 0.3, "b-prefixed present verb"),
                     (r"^ه[يتن][ء-ي]{3,}$", 0.3, "h- future prefix")],
        "extra_lexicon": [("لا يوجد", "مفيش"), ("يوجد", "فيه"), ("كثيرا", "كتير"), ("كثيراً", "كتير"), ("كثير", "كتير"),
                          ("كثيرة", "كتيرة"), ("ماذا", "إيه"), ("جيدة", "كويسة"), ("أيضا", "كمان"), ("أيضاً", "كمان"), ("لم يكن", "ماكانش")],
    },
    "ar-Gulf": {
        "name": "Gulf Arabic (khaleeji)",
        "markers": {"وايد": 1, "شلون": 1, "ليش": 1, "الحين": 1, "زين": 1, "شنو": 1, "هذي": 1, "هذولا": 1, "أبي": 1,
                    "ابي": 1, "نبي": 1, "يبي": 1, "تبي": 1, "مو": 1, "جذي": 1, "ذيك": 0.7, "ذاك": 0.4, "شي": 0.5,
                    "راح": 0.5, "إي": 0.7, "وين": 0.6, "هذا": 0.3, "احنا": 0.4, "اللي": 0.3, "بس": 0.3, "فيه": 0.3,
                    "يبيله": 1, "عشان": 0.2, "ترى": 0.6, "سوا": 0.5},
        "patterns": [],
        "extra_lexicon": [("لا يوجد", "ما فيه"), ("يوجد", "فيه"), ("كثيرا", "وايد"), ("كثيراً", "وايد"), ("كثير", "وايد"),
                          ("كثيرة", "وايد"), ("ماذا", "شنو"), ("جيدة", "زينة"), ("هذا", "هذا"), ("لم يكن", "ما كان")],
    },
    "ar": {
        "name": "Modern Standard Arabic",
        "markers": {"سوف": 1, "لماذا": 1, "ليس": 1, "ليست": 1, "لقد": 1, "الذي": 1, "التي": 1, "الذين": 1, "ماذا": 1,
                    "الآن": 1, "كيف": 0.8, "هذه": 1, "ذلك": 0.8, "تلك": 0.8, "لكن": 0.5, "ولكن": 0.7, "أيضا": 0.8,
                    "أيضاً": 0.8, "جدا": 0.8, "جداً": 0.8, "هؤلاء": 1, "لن": 0.8, "لم": 0.6, "قد": 0.5, "يوجد": 1,
                    "حيث": 0.8, "هذا": 0.4, "إذ": 0.7, "كثيرا": 0.6, "كثيراً": 0.6},
        "patterns": [],
        "extra_lexicon": [],
    },
}
STRONG = 0.7
# MSA-only forms that a colloquial register must not contain (a semi_formal register allows them)
REGISTERS = ("colloquial", "semi_formal")


def _strong(tag: str) -> set[str]:
    return {w for w, x in PROFILES[tag]["markers"].items() if x >= STRONG}


def forbidden(tag: str, register: str = "colloquial") -> set[str]:
    """Markers that must not appear in text of this profile: the other dialect's distinctive words always,
    and MSA-only words in the colloquial register."""
    if register not in REGISTERS:
        raise ValueError(f"register must be one of {REGISTERS}")
    other = "ar-Gulf" if tag == "ar-EG" else "ar-EG"
    out = set(_strong(other))
    if register == "colloquial":
        out |= _strong("ar")
    return out - set(PROFILES[tag]["markers"])


AR_LETTERS = "\u0621-\u064a\u066e-\u06d3"
AR_LEFT, AR_RIGHT = f"(?<![{AR_LETTERS}])", f"(?![{AR_LETTERS}\u064b-\u0652])"
_DIAC = re.compile(r"[ً-ْٰـ]")


def _norm(w: str) -> str:
    return _DIAC.sub("", w).strip("،.؟!,?:;«»\"'()")


def tokens(text: str) -> list[str]:
    return [t for t in (_norm(w) for w in text.split()) if t]


def _variants(tok: str) -> list[str]:
    """The token and the token without one conjunction prefix (و / ف): 'والحين' counts as 'الحين'."""
    out = [tok]
    if len(tok) > 3 and tok[0] in "وف":
        out.append(tok[1:])
    return out


def hits(text: str, tag: str) -> list[tuple[str, float]]:
    prof = PROFILES[tag]
    found = []
    for tok in tokens(text):
        for v in _variants(tok):
            if v in prof["markers"]:
                found.append((v, float(prof["markers"][v])))
                break
        else:
            for pat, wgt, _note in prof["patterns"]:
                if re.match(pat, tok):
                    found.append((tok, wgt))
                    break
    return found


def classify(text: str) -> dict:
    """EG / Gulf / MSA scores (shares that sum to 1), the label and the marker evidence. 'uncertain' when the text
    carries too little evidence (raw weight < 1) or the top two are within 20 %."""
    raw = {t: round(sum(w for _, w in hits(text, t)), 2) for t in ("ar-EG", "ar-Gulf", "ar")}
    total = sum(raw.values())
    scores = {k: round(v / total, 3) if total else 0.0 for k, v in raw.items()}
    ranked = sorted(raw, key=raw.get, reverse=True)
    label = ranked[0]
    if total < 1.0 or raw[ranked[0]] - raw[ranked[1]] < 0.2 * raw[ranked[0]]:
        label = "uncertain"
    names = {"ar-EG": "EG", "ar-Gulf": "Gulf", "ar": "MSA"}
    return {"scores": {names[k]: v for k, v in scores.items()}, "raw": {names[k]: v for k, v in raw.items()},
            "label": names.get(label, label), "words": len(tokens(text)),
            "evidence": {names[t]: sorted({w for w, _ in hits(text, t)}) for t in raw}}


def forbidden_hits(text: str, tag: str, register: str = "colloquial") -> list[str]:
    bad = forbidden(tag, register)
    return [tok for tok in tokens(text) if any(v in bad for v in _variants(tok))]


# ------------------------------------------------------------------ adaptation
def lexical_adapt(text: str, tag: str) -> tuple[str, list[str]]:
    """Deterministic fallback: the language pack's lexicon/patterns (nexus.speech.packs.adapt) plus this profile's
    extra phrases. Function words only - grammar stays as the MT wrote it."""
    from ...speech import packs as lp
    out, changes = lp.adapt(text, tag)
    # packs.adapt treats Arabic punctuation (U+060C comma, U+061F question mark) as letters, so a word right before
    # them is missed there (integration request IR-2); repeat its lexicon with letter-only boundaries.
    pairs = [(m, e["dialect"]) for e in (lp.pack(tag).get("dialect") or {}).get("lexicon") or [] for m in e.get("msa") or []]
    for msa, dia in pairs + PROFILES[tag]["extra_lexicon"]:
        if msa == dia:
            continue
        pat = AR_LEFT + re.escape(msa) + AR_RIGHT
        new, n = re.subn(pat, dia, out)
        if n:
            changes.append(f"{msa} -> {dia} (x{n})")
            out = new
    return out, changes


def _prompt(lines: list[str], tag: str, register: str) -> str:
    from ...speech import packs as lp
    style = ((lp.get(tag) or {}).get("dialect") or {}).get("llm_style") or PROFILES[tag]["name"]
    avoid = "، ".join(sorted(forbidden(tag, register)))
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines))
    return (f"Rewrite each numbered Arabic line in {PROFILES[tag]['name']} for a spoken video narration.\n{style}\n"
            f"Register: {register.replace('_', '-')}. Never use these words: {avoid}.\n"
            "Keep the meaning, every number (as digits) and every name exactly; do not add or drop information; "
            f"Arabic script only. Return JSON {{\"lines\": [...]}} with exactly {len(lines)} strings in order.\n\n{numbered}")


QUESTION_SRC = {"لماذا", "كيف", "ماذا", "أين", "متى", "ليه", "ليش", "إزاي", "ازاي", "شلون", "شنو", "إيه", "ايه", "فين", "وين", "إمتى", "هل"}
QUESTION_DIALECT = {"ليه", "ليش", "إزاي", "ازاي", "شلون", "شنو", "إيه", "ايه", "فين", "وين", "إمتى"}
_PREFIX = re.compile(r"^(?:[وف])?(?:ال|[بهل])?(?:[يتنأا])?")


def _core(tok: str) -> str:
    """Rough stem for comparison: drop a conjunction, the article or a b-/h-/l- prefix and a person prefix."""
    c = _PREFIX.sub("", tok, count=1)
    return c if len(c) >= 3 else tok


def vocabulary(tag: str) -> set[str]:
    """Every dialect/MSA form the profiles and the language pack know (markers + lexicon targets)."""
    from ...speech import packs as lp
    v = {w for p in PROFILES.values() for w in p["markers"]}
    v |= {d for _, d in PROFILES[tag]["extra_lexicon"]} | {w for m, _ in PROFILES[tag]["extra_lexicon"] for w in m.split()}
    for e in (lp.pack(tag).get("dialect") or {}).get("lexicon") or []:
        v |= set(_norm(e["dialect"]).split()) | {_norm(m) for m in e.get("msa") or []}
    return v


def novel_words(src: str, cand: str, tag: str) -> list[str]:
    """Words of `cand` that neither come from `src` (same rough stem) nor belong to the known dialect vocabulary.
    A rewrite into dialect changes function words and verb prefixes; content words it invents are suspicious
    (the 4B router model produced non-words such as بيو and كايل in the first Egyptian run)."""
    st = {_core(t) for t in tokens(src)} | set(tokens(src))
    voc = vocabulary(tag)
    out = []
    for t in tokens(cand):
        if re.fullmatch(r"[\d\u0660-\u0669.,]+", t) or t in voc or t in st or _core(t) in st:
            continue
        if any(v in voc or v in st or _core(v) in st for v in _variants(t)):
            continue
        out.append(t)
    return out


def _accept(src: str, cand: str, tag: str, register: str, baseline: str) -> tuple[bool, str]:
    from .translation_qa import numbers
    if not cand or not cand.strip():
        return False, "empty"
    letters = [c for c in cand if c.isalpha()]
    if letters and sum("\u0600" <= c <= "\u06ff" for c in letters) / len(letters) < 0.85:
        return False, "not Arabic script"
    if sorted(numbers(src)) != sorted(numbers(cand)):
        return False, f"numbers changed {numbers(src)} -> {numbers(cand)}"
    r = len(cand) / max(1, len(src))
    if not 0.6 <= r <= 1.6:
        return False, f"length ratio {r:.2f}"
    q_src = sum(1 for t in tokens(src) if any(v in QUESTION_SRC for v in _variants(t))) + src.count("\u061f") + src.count("?")
    q_new = [t for t in tokens(cand) if any(v in QUESTION_DIALECT for v in _variants(t))]
    if len(q_new) > q_src:
        return False, f"introduces question words {q_new} the source does not have (meaning changed)"
    nov = novel_words(src, cand, tag)
    if len(nov) > max(1, int(0.1 * len(tokens(cand)))):
        return False, f"words with no source or lexicon counterpart (possible invented words): {nov[:6]}"
    want = "EG" if tag == "ar-EG" else "Gulf"
    c, b = classify(cand), classify(baseline)
    if c["raw"][want] < b["raw"][want] and len(forbidden_hits(cand, tag, register)) >= len(forbidden_hits(baseline, tag, register)):
        return False, "no more dialectal than the lexical fallback"
    if c["label"] not in (want, "uncertain") and c["raw"][want] < 1:
        return False, f"classified as {c['label']}"
    return True, "ok"


def adapt_lines(lines: list[str], tag: str, privacy: str = "internal", register: str = "colloquial",
                use_router: bool = True) -> list[dict]:
    """Adapt MSA (or weakly dialectal) lines to the profile. Router first (one call for all lines), validated per
    line; every rejected/unavailable line falls back to the lexical pass. Each result records its method."""
    if tag not in ("ar-EG", "ar-Gulf"):
        raise ValueError("dialect profiles: ar-EG, ar-Gulf")
    lex = [lexical_adapt(t, tag) for t in lines]
    routed, note = None, "router not used"
    if use_router and lines:
        from ..model_router import NoProviderAvailable, complete
        schema = {"type": "object", "required": ["lines"], "properties": {"lines": {"type": "array", "minItems": len(lines)}}}
        try:
            r = complete("translation", _prompt(lines, tag, register), system="You are a native Arabic dialect editor.",
                         schema=schema, privacy=privacy, max_tokens=1800, temperature=0.3)
            got = [re.sub(r"^\s*\d+[.)]\s*", "", str(x)).strip() for x in (r.data or {}).get("lines", [])]
            routed, note = (got[:len(lines)], f"{r.provider}/{r.model}") if len(got) >= len(lines) else (None, f"router returned {len(got)} lines")
        except NoProviderAvailable as e:
            note = f"no provider: {str(e)[:200]}"
    out = []
    for i, src in enumerate(lines):
        base, changes = lex[i]
        res = {"source": src, "text": base, "method": "lexical", "changes": changes, "router": note,
               "router_text": routed[i] if routed is not None else None}
        if routed is not None:
            ok, why = _accept(src, routed[i], tag, register, base)
            if ok:
                res.update(text=routed[i], method="router", changes=[])
            else:
                res["router_rejected"] = {"text": routed[i], "reason": why}
        res["classifier"] = classify(res["text"])
        res["forbidden_hits"] = forbidden_hits(res["text"], tag, register)
        out.append(res)
    return out
