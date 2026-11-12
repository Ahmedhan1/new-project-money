"""Translation QA that catches real omissions and additions, line by line, plus the LocalizationManifest workflow.

Checks per (source line, target line):
  sentence_alignment   target has fewer/more sentences than the source (missing / extra sentence)
  length_ratio         target length outside the language's expected band (dropped / invented content)
  numbers              every number of the source survives (Arabic-Indic / Devanagari digits and 1,200 / 1.200 /
                       1 200 / 3.5 / 3,5 are normalised; spelled-out numbers are NOT recognised and are reported)
  protected_terms      glossary terms (per target language) and protected names must appear in the target
  names                capitalised source names: present verbatim (Latin targets) or confirmed by the back-translation
  repetition           repeated n-grams inside a line, and duplicated lines
  unsupported_content  content words of the back-translation that the source never said (needs a back-translation)
  omission_backcheck   source content words missing from the back-translation
Each line gets a confidence in [0, 1] derived from its findings (a heuristic, documented in the doc, not a calibrated
probability). Back-translation goes through the model router (task "translation"); when no provider is available the
checks that need it say "not run" instead of passing.
"""
from __future__ import annotations

import json
import re
import time

from ..contracts import ContractError, make, new_id, validate

SEVERITY_COST = {"critical": 0.5, "major": 0.3, "minor": 0.1, "info": 0.0}
# expected target/source character-length band per target (wide on purpose: catches whole missing clauses only)
LENGTH_BAND = {"zh": (0.15, 0.8), "ja": (0.2, 0.9), "ko": (0.25, 1.0), "ar": (0.55, 1.4), "hi": (0.6, 1.7)}
DEFAULT_BAND = (0.6, 1.6)
STOP = set("""the a an and or but of to in on at for with by from as is are was were be been being this that these those
it its into over than then also which who whom whose what when where while about after before more most very much many
some such only just even because there their they them have has had will would could should can may might not no yes
our your his her him she he we you i me my per each other same both any all""".split())
DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹०१२३४५६७८९", "0123456789" * 3)


# ------------------------------------------------------------------ helpers
def sentences(text: str) -> list[str]:
    parts = re.split(r"(?:(?<=[.!?\u061f\u0964])|(?<=[.!?\u061f\u0964]['\")\]\u00bb\u201d]))\s+|(?<=[\u3002\uff01\uff1f])", text.strip())
    out: list[str] = []
    for p in (p.strip() for p in parts if re.search(r"\w", p)):
        if out and p[:1].islower():   # "Ramses II. gebaut", "ca. 3", "z. B." - not a sentence end
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def _norm_num(s: str) -> str:
    s = re.sub(r"[ '\u2019]", "", s)
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    else:
        for sep in (",", "."):
            if sep in s:
                parts = s.split(sep)
                thousands = len(parts) > 2 or (len(parts[1]) == 3 and len(parts[0]) <= 3)
                s = "".join(parts) if thousands else s.replace(sep, ".")
    if re.fullmatch(r"\d+\.\d+", s):
        s = s.rstrip("0").rstrip(".")
    return s


def numbers(text: str) -> list[str]:
    """Normalised numbers: digits of any script; 1,200 / 1.200 / 1 200 -> 1200; 3,5 / 3.5 -> 3.5."""
    t = text.translate(DIGIT_MAP).replace("\u066b", ".").replace("\u066c", ",")
    return [_norm_num(m.group(0)) for m in re.finditer(r"\d+(?:[,.]\d+|[ '\u2019\u00a0\u202f]\d{3}(?!\d))*", t)]


def names(text: str) -> list[str]:
    """Capitalised word runs that are not sentence-initial (English source): candidate proper names."""
    found = []
    for sent in sentences(text):
        toks = re.findall(r"[A-Za-z][\w'-]*", sent)
        run: list[str] = []
        for i, tok in enumerate(toks):
            if tok[0].isupper() and (i > 0 or tok.lower() not in STOP) and not (i == 0 and len(toks) > 1 and not toks[1][0].isupper()):
                run.append(tok)
            else:
                if run:
                    found.append(" ".join(run))
                run = []
        if run:
            found.append(" ".join(run))
    return [n for n in dict.fromkeys(found) if n.lower() not in STOP]


# a back-translation legitimately says "height" for "high", "meters" for "metres": fold such pairs before comparing
FOLD = {"height": "high", "length": "long", "width": "wide", "depth": "deep", "construction": "build", "built": "build",
        "building": "build", "inaugurated": "opened"}


def _fold(w: str) -> str:
    w = FOLD.get(w, w)
    w = re.sub(r"tre(s?)$", r"ter\1", w)        # metre -> meter, kilometres -> kilometers
    w = re.sub(r"is(e|ed|es|ing)$", r"iz\1", w)  # fertilised -> fertilized
    return w


def content_words(text: str) -> list[str]:
    return [_fold(w) for w in re.findall(r"[a-z][a-z'-]{2,}", text.lower()) if w not in STOP]


def _stem(w: str) -> str:
    return w[:5]


def _script_spaced(lang: str) -> bool:
    return lang.split("-")[0] not in ("zh", "ja")


def repeated_ngrams(text: str, lang: str, n: int = 3) -> list[str]:
    toks = text.split() if _script_spaced(lang) else [c for c in text if c.strip()]
    n = n if _script_spaced(lang) else 4
    seen, rep = set(), []
    for i in range(len(toks) - n + 1):
        g = tuple(toks[i:i + n])
        if g in seen and g not in rep:
            rep.append(g)
        seen.add(g)
    # immediate word doubling ("the the")
    if _script_spaced(lang):
        rep += [(a,) for a, b in zip(toks, toks[1:]) if a == b and len(a) > 1]
    return [" ".join(g) if _script_spaced(lang) else "".join(g) for g in rep]


def _finding(check: str, severity: str, detail: str, **extra) -> dict:
    return {"check": check, "severity": severity, "detail": detail, **extra}


# ------------------------------------------------------------------ per-line QA
def check_line(src: str, tgt: str, lang: str, glossary: dict | None = None, back: str | None = None,
               protected: list[str] | None = None) -> dict:
    """QA of one translated line. `glossary` = {source term: target term} for this language; `protected` = names
    that must survive verbatim (brands, people) in Latin-script targets; `back` = back-translation into English."""
    f: list[dict] = []
    if not tgt.strip():
        f.append(_finding("empty", "critical", "target line is empty"))
        return _score(src, tgt, lang, f, back)
    ss, ts = sentences(src), sentences(tgt)
    if len(ts) < len(ss):
        f.append(_finding("missing_sentence", "major", f"{len(ss)} source sentences, {len(ts)} in the target - a sentence was dropped or merged"))
    elif len(ts) > len(ss):
        f.append(_finding("extra_sentence", "major", f"{len(ss)} source sentences, {len(ts)} in the target - possible added sentence"))
    lo, hi = LENGTH_BAND.get(lang.split("-")[0], DEFAULT_BAND)
    r = len(tgt) / max(1, len(src))
    if r < lo:
        f.append(_finding("too_short", "major", f"target is {r:.2f}x the source length (expected >= {lo}) - content probably missing"))
    elif r > hi:
        f.append(_finding("too_long", "major", f"target is {r:.2f}x the source length (expected <= {hi}) - content probably added"))
    sn, tn = numbers(src), numbers(tgt)
    missing = [n for n in dict.fromkeys(sn) if sn.count(n) > tn.count(n)]
    extra = [n for n in dict.fromkeys(tn) if tn.count(n) > sn.count(n)]
    if missing:
        f.append(_finding("number_missing", "major", f"numbers missing or changed: {missing} (spelled-out numbers are not recognised - verify)", items=missing))
    if extra:
        f.append(_finding("number_added", "major", f"numbers in the target that the source does not have: {extra}", items=extra))
    for s_term, t_term in (glossary or {}).items():
        if s_term.lower() in src.lower() and t_term.lower() not in tgt.lower():
            f.append(_finding("glossary_term_missing", "major", f"glossary: '{s_term}' must be rendered as '{t_term}'", term=s_term))
    latin_target = bool(re.search(r"[A-Za-z]", tgt)) and lang.split("-")[0] in ("de", "fr", "es", "pt", "it", "tr", "en")
    gl_sources = {k.lower() for k in (glossary or {})}
    for nm in list(dict.fromkeys((protected or []) + names(src))):
        if nm.lower() in gl_sources or nm.lower() not in src.lower():
            continue
        if latin_target or (protected and nm in protected and re.search(r"[A-Za-z]", tgt)):
            if nm.lower() not in tgt.lower():
                f.append(_finding("name_missing", "major", f"name '{nm}' does not appear in the target", term=nm))
        elif back is not None:
            if not all(_stem(w.lower()) in back.lower() for w in nm.split()):
                f.append(_finding("name_unconfirmed", "minor", f"name '{nm}' is not recognisable in the back-translation", term=nm))
        else:
            f.append(_finding("name_unverifiable", "info", f"'{nm}' is transliterated in this script; no glossary entry and no back-translation to confirm it", term=nm))
    reps = repeated_ngrams(tgt, lang)
    if reps:
        f.append(_finding("repetition", "major", f"repeated phrases in the target: {reps[:4]}", items=reps[:4]))
    return _score(src, tgt, lang, f, back)


def _score(src: str, tgt: str, lang: str, f: list[dict], back: str | None) -> dict:
    coverage = None
    if back is not None and not back.strip():
        back = None
    if back is not None and tgt.strip():
        sw, bw = content_words(src), content_words(back)
        s_st, b_st = {_stem(w) for w in sw}, {_stem(w) for w in bw}
        unsupported = [w for w in dict.fromkeys(bw) if _stem(w) not in s_st]
        lost = [w for w in dict.fromkeys(sw) if _stem(w) not in b_st]
        coverage = round(1 - len(lost) / max(1, len(set(sw))), 3)
        # paraphrase in a back-translation is normal: only flag when it is substantial
        if len(unsupported) >= 3 and len(unsupported) > 0.35 * max(1, len(set(sw))):
            f.append(_finding("unsupported_content", "major", f"the back-translation contains ideas the source never said: {unsupported[:8]}", items=unsupported[:8]))
        elif unsupported:
            f.append(_finding("unsupported_content", "info", f"back-translation words not in the source (likely paraphrase): {unsupported[:8]}", items=unsupported[:8]))
        # A later clause of the read-back that brings no source content of its own but has unsupported words is an
        # added clause ("..., and they helped in completing it" - ar-EG demo line 5).
        clauses = [c for c in re.split(r",\s*(?:and|but|while|which|so)\s+|;\s*", back) if c.strip()]
        used: set[str] = set()
        for k, cl in enumerate(clauses):
            cw = {_stem(w) for w in content_words(cl)}
            fresh = (cw & s_st) - used
            extra = [w for w in content_words(cl) if _stem(w) not in s_st]
            if k and cw and not fresh and extra:
                f.append(_finding("added_clause", "major", f"read-back clause adds nothing from the source but says {extra[:5]}: '{cl.strip()[:80]}'", items=extra[:5]))
            used |= cw & s_st
        if coverage < 0.6:
            f.append(_finding("omission_backcheck", "major", f"only {coverage:.0%} of the source content words come back: missing {lost[:8]}", items=lost[:8]))
        elif lost:
            f.append(_finding("omission_backcheck", "info", f"{coverage:.0%} source content words come back; not found: {lost[:8]}", items=lost[:8]))
    conf = 1.0
    for x in f:
        conf -= SEVERITY_COST[x["severity"]]
    if back is None:
        conf -= 0.15  # meaning not cross-checked
    if coverage is not None:
        conf = min(conf, 0.4 + 0.6 * coverage)
    return {"source": src, "target": tgt, "lang": lang, "back": back, "coverage": coverage, "findings": f,
            "confidence": round(max(0.0, min(1.0, conf)), 3),
            "status": "needs_fix" if any(x["severity"] in ("critical", "major") for x in f) else "machine_ok"}


def check_lines(source: list[str], target: list[str], lang: str, glossary: dict | None = None,
                back: list[str] | None = None, protected: list[str] | None = None) -> dict:
    """QA of a whole document: per-line results + document-level findings (line count, duplicated lines)."""
    doc: list[dict] = []
    if len(source) != len(target):
        doc.append(_finding("line_alignment", "critical", f"{len(target)} target lines for {len(source)} source lines"))
    lines = [check_line(s, t, lang, glossary, back[i] if back and i < len(back) else None, protected)
             for i, (s, t) in enumerate(zip(source, target))]
    seen: dict[str, int] = {}
    for i, ln in enumerate(lines):
        key = ln["target"].strip()
        if key and key in seen:
            ln["findings"].append(_finding("duplicate_line", "major", f"identical to line {seen[key] + 1}"))
            ln["confidence"] = round(max(0.0, ln["confidence"] - SEVERITY_COST["major"]), 3)
            ln["status"] = "needs_fix"
        seen.setdefault(key, i)
    counts: dict[str, int] = {}
    for ln in lines:
        for x in ln["findings"]:
            if x["severity"] != "info":
                counts[x["check"]] = counts.get(x["check"], 0) + 1
    return {"lang": lang, "lines": lines, "document": doc, "counts": counts,
            "mean_confidence": round(sum(x["confidence"] for x in lines) / max(1, len(lines)), 3),
            "lines_needing_fix": [i for i, x in enumerate(lines) if x["status"] == "needs_fix"],
            "back_translation": "run" if back else "not run"}


# ------------------------------------------------------------------ back-translation through the router
LANG_NAMES = {"de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese", "it": "Italian", "tr": "Turkish",
              "ru": "Russian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "hi": "Hindi", "ar": "Arabic",
              "ar-EG": "Egyptian Arabic", "ar-Gulf": "Gulf Arabic", "en": "English"}
EN_FUNCTION = set("the a an of to in on is are was were and it its that which for from with by as at be".split())


def is_english(text: str, target: str) -> bool:
    """A back-translation line is usable only if it is English and not a copy of the target line."""
    words = re.findall(r"[A-Za-z']+", text.lower())
    letters = [c for c in text if c.isalpha()]
    if not words or (letters and sum(c.isascii() for c in letters) / len(letters) < 0.9):
        return False
    if text.strip() == target.strip():
        return False
    hits = sum(w in EN_FUNCTION for w in words)
    return (hits >= 1 and hits / len(words) >= 0.08) or len(words) <= 2


def _bt_line(line: str, name: str, privacy: str, complete) -> str | None:
    r = complete("translation", f"{name} sentence:\n{line}\n\nWrite its literal English translation. Output only the English sentence.",
                 system="You translate into English, literally, for quality control.", privacy=privacy, max_tokens=300, temperature=0.1)
    txt = re.sub(r"<think>.*?</think>", "", r.text or "", flags=re.S).strip().strip('"').splitlines()
    txt = next((t.strip() for t in txt if t.strip()), "")
    return txt if is_english(txt, line) else None


def back_translate(lines: list[str], lang: str, privacy: str = "internal") -> tuple[list[str] | None, str]:
    """Literal English back-translation of `lines` via the model router (task 'translation').
    Batch call first; lines that come back untranslated (a small model sometimes copies the input) are retried one by
    one; lines that still fail are returned as "" and listed in the note. Returns (lines or None, note)."""
    from ..model_router import NoProviderAvailable, complete
    name = LANG_NAMES.get(lang, lang)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines))
    prompt = (f"Translate each numbered {name} line into ENGLISH as literally as possible. Do not fix, complete or "
              "improve anything: if a line says something odd or omits something, your English must show that. Keep every "
              f"number as digits. Return JSON {{\"lines\": [...]}} with exactly {len(lines)} English strings, in order.\n\n{numbered}")
    schema = {"type": "object", "required": ["lines"], "properties": {"lines": {"type": "array", "minItems": len(lines)}}}
    try:
        r = complete("translation", prompt, system=f"You are a literal {name}-to-English back-translator for translation quality control.",
                     schema=schema, privacy=privacy, max_tokens=1600, temperature=0.1)
        out = [re.sub(r"^\s*\d+[.)]\s*", "", str(x)).strip() for x in (r.data or {}).get("lines", [])][:len(lines)]
        out += [""] * (len(lines) - len(out))
        retried, failed = [], []
        for i, (src, b) in enumerate(zip(lines, out)):
            if not is_english(b, src):
                retried.append(i + 1)
                out[i] = _bt_line(src, name, privacy, complete) or ""
                if not out[i]:
                    failed.append(i + 1)
    except NoProviderAvailable as e:
        return None, f"back-translation not run: {e}"
    note = f"{r.provider}/{r.model}" + (" (cached)" if r.cached else "")
    if retried:
        note += f"; batch output not English for lines {retried}, retried one by one"
    if failed:
        note += f"; no usable back-translation for lines {failed} (meaning not cross-checked there)"
    return out, note


# ------------------------------------------------------------------ LocalizationManifest + approval workflow
APPROVAL = ("machine_draft", "in_review", "approved", "rejected")


def build_manifest(project_id: str, source_lang: str, source: list[str], results: dict[str, dict],
                   glossary: dict | None = None, voices: list | None = None) -> dict:
    """LocalizationManifest (contracts v1). `results` = {lang: check_lines(...) output}."""
    lines = []
    for i, s in enumerate(source):
        tg = {}
        for lang, res in results.items():
            if i < len(res["lines"]):
                ln = res["lines"][i]
                tg[lang] = {"machine": ln["target"], "text": ln["target"], "confidence": ln["confidence"],
                            "findings": ln["findings"], "status": "machine_draft" if ln["status"] == "machine_ok" else "needs_fix",
                            "review": []}
        lines.append({"idx": i, "source": s, "targets": tg})
    qa = {lang: {k: res[k] for k in ("counts", "mean_confidence", "lines_needing_fix", "back_translation", "document")}
          for lang, res in results.items()}
    return make("LocalizationManifest", id=new_id("loc"), project_id=project_id, source_lang=source_lang,
                targets=sorted(results), glossary=glossary or {}, lines=lines, qa=qa, voices=voices or [], approval="machine_draft")


def review_line(manifest: dict, lang: str, idx: int, reviewer: str, decision: str, text: str | None = None,
                note: str = "") -> dict:
    """Record a human decision on one line: decision = approve | correct | reject. The machine text is kept."""
    if decision not in ("approve", "correct", "reject"):
        raise ValueError("decision must be approve, correct or reject")
    if not reviewer.strip():
        raise ValueError("a named reviewer is required")
    t = manifest["lines"][idx]["targets"][lang]
    if decision == "correct":
        if not text or not text.strip():
            raise ValueError("a correction needs the corrected text")
        t["text"] = text.strip()
    t["status"] = {"approve": "approved", "correct": "approved", "reject": "rejected"}[decision]
    t["review"].append({"reviewer": reviewer, "decision": decision, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "note": note, **({"text": text} if text else {})})
    manifest["approval"] = approval_state(manifest)
    return manifest


def approval_state(manifest: dict, lang: str | None = None) -> str:
    states = [ln["targets"][lg]["status"] for ln in manifest["lines"] for lg in ln["targets"] if lang in (None, lg)]
    if not states or all(s in ("machine_draft", "needs_fix") for s in states):
        return "machine_draft"
    if any(s == "rejected" for s in states):
        return "rejected"
    return "approved" if all(s == "approved" for s in states) else "in_review"


def commercially_ready(manifest: dict, lang: str) -> tuple[bool, list[str]]:
    """A language is ready only when every line was approved by a named human (machine QA alone never suffices)."""
    why = [f"line {ln['idx'] + 1}: {ln['targets'][lang]['status']}" for ln in manifest["lines"]
           if lang in ln["targets"] and ln["targets"][lang]["status"] != "approved"]
    return not why, why


def save(manifest: dict, path) -> None:
    errs = validate(manifest)
    if errs:
        raise ContractError("; ".join(errs))
    from pathlib import Path
    Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
