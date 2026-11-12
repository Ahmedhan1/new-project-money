"""compare(profiles) -> report and style_from(profiles) -> CreativeStyleProfile.

Every statement is {"text", "cites": ["<profile id>:<field path>", ...]}. The report says plainly that shared patterns
are correlations of structure across a handful of references, not proof that they cause any audience outcome.
"""
from __future__ import annotations

import statistics
import time

from .. import contracts as C
from . import library

DISCLAIMER = ("These are structural patterns observed in a small set of references. Patterns are not proof of causation: "
              "nothing here shows that a pattern produced views, retention or any other outcome.")

METRICS = {  # field path -> (label, higher_is: 'faster'/'more'...)
    "measured.cuts_per_min": "cuts per minute",
    "measured.median_shot_s": "median shot length (s)",
    "measured.speech.speech_share": "speech share",
    "measured.speech.wpm_speaking": "words per speaking minute",
    "measured.speech.time_to_first_speech_s": "time to first speech (s)",
    "measured.opening.first_5s.cuts": "cuts in first 5 s",
    "measured.audio.silence_share": "silence share",
    "measured.loudness_jump_db": "opening loudness jump (dB)",
}
# Strongest opening: explicit, measurable criteria. Weight 1 each. Direction: +1 higher is better, -1 lower is better.
OPENING_CRITERIA = [
    ("measured.opening.first_3s.cuts", +1, "visual change in first 3 s"),
    ("measured.opening.first_5s.words_per_s", +1, "words per second in first 5 s"),
    ("measured.speech.time_to_first_speech_s", -1, "earlier speech onset"),
    ("measured.opening.first_15s.question_words", +1, "question words in first 15 s"),
    ("measured.opening.first_15s.numbers", +1, "numbers in first 15 s"),
    ("measured.opening.first_15s.stakes_words", +1, "stakes words in first 15 s"),
    ("measured.loudness_jump_db", +1, "opening louder than the rest"),
]


def get(p: dict, path: str):
    cur = p
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def usable(p: dict, path: str):
    """Field value, or None when it rests on captions that do not match the media (flagged in the profile)."""
    if get(p, "measured.speech.caption_mismatch") and (".speech." in path or ".opening." in path):
        return None
    return get(p, path)


def _st(text: str, cites: list[str], **kw) -> dict:
    return {"text": text, "cites": cites, **kw}


def _label(p: dict) -> str:
    t = (p.get("packaging") or {}).get("title", {})
    return f"{p['id']} ({t.get('value') if isinstance(t, dict) else p.get('format')})"


def opening_ranking(profiles: list[dict]) -> list[dict]:
    rows = []
    for path, direction, name in OPENING_CRITERIA:
        vals = {p["id"]: usable(p, path) for p in profiles}
        known = {k: v for k, v in vals.items() if v is not None}
        for pid in vals:
            rows.append((pid, path, name, vals[pid], known, direction))
    score = {p["id"]: 0.0 for p in profiles}
    detail = {p["id"]: [] for p in profiles}
    for pid, path, name, v, known, d in rows:
        if v is None or len(known) < 2:
            pts = 0.0
        else:
            lo, hi = min(known.values()), max(known.values())
            pts = 0.5 if hi == lo else ((v - lo) / (hi - lo) if d > 0 else (hi - v) / (hi - lo))
        score[pid] += pts
        detail[pid].append({"criterion": name, "field": path, "value": v, "points": round(pts, 3)})
    return sorted(({"id": pid, "score": round(score[pid], 3), "criteria": detail[pid]} for pid in score),
                  key=lambda r: -r["score"])


def compare(profiles: list[dict]) -> dict:
    for p in profiles:
        C.check(p)
    measured = [p for p in profiles if p.get("measured")]
    rep = {"kind": "nexus.CreatorComparison/1", "created": time.time(), "profiles": [_label(p) for p in profiles],
           "disclaimer": DISCLAIMER, "table": [], "shared_patterns": [], "differences": [], "strongest_opening": None,
           "adaptable_techniques": [], "nexus_should_differently": [], "original_angle": None,
           "not_compared": [p["id"] for p in profiles if not p.get("measured")]}
    for p in measured:
        rep["table"].append({"id": p["id"], "format": p["format"], **{lab: usable(p, f) for f, lab in METRICS.items()},
                             "caption_mismatch": bool(get(p, "measured.speech.caption_mismatch")),
                             "hook_type": get(p, "hook.type.value"), "pacing_shape": get(p, "story.pacing_shape.value")})
    if len(measured) < 2:
        rep["shared_patterns"].append(_st("Fewer than two measured profiles: nothing to compare.", []))
        return rep
    # shared / differing categorical readings
    for path, name in (("hook.type.value", "hook type"), ("story.pacing_shape.value", "pacing shape"),
                       ("editing.tempo.value", "edit tempo"), ("audio.mode.value", "audio mode")):
        vals = {p["id"]: get(p, path) for p in measured}
        cites = [f"{k}:{path}" for k in vals]
        if len(set(vals.values())) == 1:
            rep["shared_patterns"].append(_st(f"All references share the same {name}: {next(iter(vals.values()))} (interpreted).", cites))
        else:
            rep["differences"].append(_st(f"{name} differs: " + ", ".join(f"{k}={v}" for k, v in vals.items()) + " (interpreted).", cites))
    for path, lab in METRICS.items():
        vals = {p["id"]: usable(p, path) for p in measured if usable(p, path) is not None}
        if len(vals) < 2:
            continue
        lo_id, hi_id = min(vals, key=vals.get), max(vals, key=vals.get)
        lo, hi = vals[lo_id], vals[hi_id]
        cites = [f"{k}:{path}" for k in vals]
        spread = (hi - lo) / max(abs(hi), abs(lo), 1e-9)
        if spread <= 0.25:
            rep["shared_patterns"].append(_st(f"Similar {lab} across references ({lo} to {hi}).", cites))
        else:
            rep["differences"].append(_st(f"{lab} ranges from {lo} ({lo_id}) to {hi} ({hi_id}).", cites))
    ranking = opening_ranking(measured)
    top = ranking[0]
    rep["strongest_opening"] = _st(
        f"{top['id']} has the strongest opening by the stated measurable criteria (score {top['score']} of "
        f"{len(OPENING_CRITERIA)}). This ranks opening structure only; it says nothing about how audiences responded.",
        [f"{top['id']}:{c['field']}" for c in top["criteria"]], criteria=[c[2] for c in OPENING_CRITERIA], ranking=ranking)
    seen = set()
    for p in measured:
        for t in p.get("techniques", []) + p.get("engagement", []):
            key = t["text"].split("(")[0].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            rep["adaptable_techniques"].append(_st(f"Adapt as a pattern (not a copy): {t['text']}",
                                                   [f"{p['id']}:{b}" for b in t.get("based_on", [])]))
    ids = [p["id"] for p in measured]

    def allp(path, pred):
        vs = [usable(p, path) for p in measured]
        vs = [v for v in vs if v is not None] or [None]
        return all(v is not None and pred(v) for v in vs)
    if allp("measured.speech.time_to_first_speech_s", lambda v: v > 3):
        rep["nexus_should_differently"].append(_st("None of these references speaks within 3 s; NEXUS can test an earlier "
                                                   "speech onset as a hypothesis.", [f"{i}:measured.speech.time_to_first_speech_s" for i in ids]))
    if allp("measured.opening.first_15s.question_words", lambda v: v == 0):
        rep["nexus_should_differently"].append(_st("No reference asks a question in its first 15 s; a question hook is an "
                                                   "untested alternative.", [f"{i}:measured.opening.first_15s.question_words" for i in ids]))
    if all(not [c for c in p["story"]["pacing_curve"][3:] if c["cuts"] > 0] for p in measured):
        rep["nexus_should_differently"].append(_st("No reference changes shots after the first 30 % of runtime; NEXUS videos "
                                                   "can plan visual re-engagement beats later on.", [f"{i}:story.pacing_curve" for i in ids]))
    weak = [(p["id"], w) for p in measured for w in p.get("weaknesses", [])]
    for pid, w in weak[:4]:
        rep["nexus_should_differently"].append(_st(f"Avoid: {w['text']} (seen in {pid}).", [f"{pid}:{b}" for b in w["based_on"]]))
    if not rep["nexus_should_differently"]:
        rep["nexus_should_differently"].append(_st("No rule-based gap found across these references.", []))
    # original angle: an archetype none of the references represents, opened with the strongest measured hook type
    present = {p.get("category") for p in profiles} | {p.get("format") for p in profiles}
    missing = [a for a in library.archetypes() if a["id"] not in present]
    best = next(p for p in measured if p["id"] == top["id"])
    if missing:
        a = missing[0]
        rep["original_angle"] = _st(
            f"Combine the {get(best, 'hook.type.value')} opening structure of {best['id']} with a '{a['name']}' body "
            f"structure ({'; '.join(s['text'] for s in a['structure'][:2])}) on an original topic - no reference uses this mix.",
            [f"{best['id']}:hook.type.value", f"library:{a['id']}.structure"] + [f"{p['id']}:category" for p in profiles])
    return rep


def style_from(profiles: list[dict], name: str = "reference style") -> dict:
    """Abstract CreativeStyleProfile: numbers and patterns only, plus an explicit do_not_copy list."""
    for p in profiles:
        C.check(p)
    ms = [p for p in profiles if p.get("measured")]

    def med(path):
        vs = [get(p, path) for p in ms if get(p, path) is not None]
        return {"median": round(statistics.median(vs), 3), "min": min(vs), "max": max(vs), "n": len(vs)} if vs else None
    curve = None
    if ms:
        curve = [round(statistics.fmean(p["story"]["pacing_curve"][k]["cuts_per_min"] for p in ms), 2) for k in range(10)]
    pacing = {"cuts_per_min": med("measured.cuts_per_min"), "median_shot_s": med("measured.median_shot_s"),
              "speech_share": med("measured.speech.speech_share"), "wpm_speaking": med("measured.speech.wpm_speaking"),
              "time_to_first_speech_s": med("measured.speech.time_to_first_speech_s"),
              "cuts_first_5s": med("measured.opening.first_5s.cuts"), "curve_cuts_per_min_by_10pct": curve,
              "kind": "measured (aggregated)"}
    hooks = sorted({get(p, "hook.type.value") for p in ms if get(p, "hook.type.value")})
    shapes = sorted({get(p, "story.pacing_shape.value") for p in ms if get(p, "story.pacing_shape.value")})
    tempos = sorted({get(p, "editing.tempo.value") for p in ms if get(p, "editing.tempo.value")})
    modes = sorted({get(p, "audio.mode.value") for p in ms if get(p, "audio.mode.value")})
    structure = [f"pacing shape: {s}" for s in shapes]
    for p in profiles:
        a = library.get(p.get("category")) or library.get(p.get("format"))
        if a:
            structure += [f"{s['text']} [{a['id']}; source: {s['src']}]" for s in a["structure"]]
    dnc = ["Titles, thumbnails and exact wording of any reference", "Catchphrases, intros/outros, recurring segment names",
           "Logos, music, sound effects, characters, set designs and on-screen personas", "Footage, graphics or frames of any reference"]
    for p in profiles:
        pk = p.get("packaging") or {}
        for k in ("title", "channel"):
            if isinstance(pk.get(k), dict) and pk[k].get("value"):
                dnc.append(f"{k}: {pk[k]['value']}")
        if p.get("creator") and p["creator"] != "unknown":
            dnc.append(f"creator identity / branding: {p['creator']}")
        op = get(p, "measured.opening.opening_phrase")
        if op:
            dnc.append(f"opening line: {op}")
    # contracts.make() takes the contract name as its first positional `name`, which collides with this contract's own
    # `name` field, so the document is built directly and validated with contracts.check().
    return C.check(dict(schema=f"nexus.CreativeStyleProfile/{C.VERSION}", id=C.new_id("csp"), name=name,
                  from_references=[p["id"] for p in profiles],
                  pacing=pacing, hook_patterns=[f"{h} opening" for h in hooks], structure=structure,
                  visual_rules=[f"edit tempo: {t}" for t in tempos] +
                  ([f"cut within the first 5 s (references: median {pacing['cuts_first_5s']['median']})"]
                   if pacing["cuts_first_5s"] and pacing["cuts_first_5s"]["median"] >= 1 else []),
                  audio_rules=[f"audio mode: {m}" for m in modes], do_not_copy=list(dict.fromkeys(dnc)),
                  note="abstract structure only; use with original content"))


def to_account2_reference_style(style: dict, rights: str = "owned") -> dict:
    """Project a CreativeStyleProfile onto Account 2's ReferenceStyleProfile (name, source, metrics, structure, rights)."""
    return {"schema": "nexus.ReferenceStyleProfile/1", "name": style["name"],
            "source": {"kind": "account3.CreativeStyleProfile", "id": style["id"], "references": style["from_references"]},
            "metrics": style["pacing"], "structure": style["structure"] + style["hook_patterns"],
            "rights": {"basis": rights, "note": "structure only - no footage or script", "do_not_copy": style["do_not_copy"]}}
