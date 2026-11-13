"""Per-language readiness: eight measured dimensions -> one score and an honest production level.

Each dimension has a state backed by evidence (never by assumption):
  verified     measured on this system and passed (OCR read-back, a rendered voice-over, native reviewers' verdicts)
  available    the capability exists with a cleared licence but has not been validated end to end for this language
  partial      works with known defects (dialect output classifies as MSA, voice not cleared for commercial use, ...)
  unavailable  missing, not licensed, or blocked
  n/a          not needed (translation into the source language)

Dimensions and weights (sum 100): transcription 10, translation 15, subtitle_rendering 15, human_reviewed 15, tts 10,
commercial_dubbing_licensed 10, lip_sync 5, end_to_end_validated 20.

Production level derived from the states, most capable first:
  dubbed_publishable      translation + subtitles + licensed voice + native review all verified
  subtitles_publishable   translation available, subtitles verified, native review verified
  draft_needs_native_review  machine output only - may be shown to a reviewer, never published as-is
  not_supported"""
from __future__ import annotations

import json
from pathlib import Path

from ... import config
from . import matrix, review, terminology

WEIGHTS = {"transcription": 10, "translation": 15, "subtitle_rendering": 15, "human_reviewed": 15, "tts": 10,
           "commercial_dubbing_licensed": 10, "lip_sync": 5, "end_to_end_validated": 20}
VALUE = {"verified": 1.0, "available": 0.6, "partial": 0.3, "unavailable": 0.0, "n/a": 1.0}
REVIEW_MIN_LINES = 20
REVIEW_MIN_ACCEPT = 0.9

# One natural sentence per language, used to render and read back subtitles (glyph coverage + shaping).
SAMPLES = {
    "en": "The Aswan High Dam is 111 metres high.", "de": "Der Assuan-Staudamm ist 111 Meter hoch, größer als üblich.",
    "fr": "Le haut barrage d'Assouan mesure 111 mètres de haut.", "ar": "يبلغ ارتفاع السد العالي في أسوان ١١١ مترا.",
    "ar-EG": "السد العالي في أسوان طوله ١١١ متر، حاجة كبيرة قوي.", "ar-Gulf": "السد العالي في أسوان طوله ١١١ متر، شي واجد كبير.",
    "zh": "阿斯旺高坝高一百一十一米。", "ru": "Высота Асуанской плотины составляет 111 метров.",
    "hi": "असवान हाई डैम की ऊँचाई 111 मीटर है।", "es": "La presa de Asuán mide 111 metros de altura.",
    "pt": "A barragem de Assuã tem 111 metros de altura.", "tr": "Asvan Barajı 111 metre yüksekliğindedir.",
    "ja": "アスワン・ハイ・ダムの高さは111メートルです。", "ko": "아스완 하이 댐의 높이는 111미터입니다.",
    "it": "La diga di Assuan è alta 111 metri.",
}
OCR_LANG = {"ar-EG": "ar", "ar-Gulf": "ar"}


def _evidence() -> dict:
    p = config.ROOT / "docs" / "results" / "account3" / "language_demo.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _subtitles(tag: str, ocr: bool) -> tuple[str, str]:
    from ..quality import typography
    text = SAMPLES.get(tag)
    if not text:
        return "unavailable", "no sample text for this language"
    base = OCR_LANG.get(tag, tag)
    r = typography.check_text(text, base)
    if not r["ok"]:
        return "unavailable", r["finding"]
    if ocr and typography.ocr_available(base):
        rc = typography.render_check(text, base)
        if rc.get("recall") is not None:
            ok = rc["recall"] >= 0.6
            return ("verified" if ok else "partial"), f"rendered and read back by OCR: recall {rc['recall']}"
    return "available", "every character has a glyph; no OCR read-back on this machine"


def assess(tag: str, ocr: bool = True) -> dict:
    lang = matrix.language(tag)
    ev = _evidence()
    run = (ev.get("summary") or {}).get(tag)
    dims: dict[str, dict] = {}

    def put(k, state, why):
        dims[k] = {"state": state, "evidence": why}

    asr = lang.get("asr")
    if not asr:
        put("transcription", "unavailable", "no ASR engine")
    elif tag in ("ar-EG", "ar-Gulf"):
        put("transcription", "partial", f"{asr}: one 'ar' code for all varieties - no dialect label, not measured on dialect speech")
    else:
        put("transcription", "available", f"{asr} installed; word error rate not measured for this language here")

    rv = review.stats().get(tag, {})
    reviewed_ok = rv.get("reviewed", 0) >= REVIEW_MIN_LINES and (rv.get("acceptance") or 0) >= REVIEW_MIN_ACCEPT
    if tag == "en":
        put("translation", "n/a", "source language")
    elif not lang.get("mt"):
        put("translation", "unavailable", "no MT engine")
    elif run:
        conf = run.get("mean_confidence") or 0
        state = "verified" if reviewed_ok else ("partial" if conf < 0.7 or tag == "ar-Gulf" else "available")
        put("translation", state, f"real MT run: QA confidence {conf}, lines needing fix {run.get('lines_needing_fix')}"
            + ("" if reviewed_ok else "; not native-reviewed") + ("; Gulf output still classifies as MSA (K-15)" if tag == "ar-Gulf" else ""))
    else:
        put("translation", "available", f"engines {lang['mt']}; no QA'd run for this language yet")

    put("subtitle_rendering", *_subtitles(tag, ocr))

    if reviewed_ok:
        put("human_reviewed", "verified", f"{rv['reviewed']} lines reviewed by {rv['reviewers']} reviewer(s), acceptance {rv['acceptance']}")
    elif rv.get("reviewed"):
        put("human_reviewed", "partial", f"{rv['reviewed']} lines reviewed (need {REVIEW_MIN_LINES} at >= {REVIEW_MIN_ACCEPT:.0%} acceptance)")
    else:
        put("human_reviewed", "unavailable" if tag != "en" else "n/a",
            "no native-speaker review recorded" if tag != "en" else "source language (owner reviews the script)")

    voices = lang.get("voices") or []
    cleared = [v for v in voices if v.get("commercial_use") == "yes"]
    vo = (run or {}).get("voiceover_line") or {}
    rendered = bool(vo.get("duration"))
    if rendered and cleared:
        put("tts", "verified", f"voice-over rendered with {run['voice']}")
    elif vo.get("error") or vo.get("skipped"):
        put("tts", "partial", f"cleared voice {run.get('voice')} but the voice-over failed: {vo.get('error') or vo.get('skipped')}")
    elif [v for v in cleared if v.get("installed")]:
        put("tts", "available", f"cleared voice installed: {[v['id'] for v in cleared if v.get('installed')]}")
    elif cleared:
        put("tts", "partial", f"cleared voice exists but is not installed: {[v['id'] for v in cleared]}")
    elif voices:
        put("tts", "partial", f"only voices with commercial use {sorted({v['commercial_use'] for v in voices})}: {[v['id'] for v in voices][:3]}")
    else:
        put("tts", "unavailable", "no voice")
    put("commercial_dubbing_licensed", "verified" if cleared else "unavailable",
        f"licence read from first-party source: {[(v['id'], v['licence']) for v in cleared][:2]}" if cleared
        else "no voice with a verified commercial licence (unverified / non-commercial only)")
    put("lip_sync", "unavailable", "no lip-sync model with a cleared commercial licence installed (Account 2 scope)")

    if run and reviewed_ok and dims["subtitle_rendering"]["state"] == "verified":
        put("end_to_end_validated", "verified", "machine pipeline + native review + rendered subtitles")
    elif run:
        put("end_to_end_validated", "partial", "translate -> QA -> subtitles" + (" -> voice-over" if rendered else "")
            + " ran on real text; not human-validated")
    elif tag == "en":
        put("end_to_end_validated", "verified" if Path(config.ROOT / "docs/results/account3/demo4_strategy_to_production.json").exists()
            else "unavailable", "research -> script -> render (Demo 4); frames inspected by eye")
    else:
        put("end_to_end_validated", "unavailable", "never run end to end for this language")

    score = round(100 * sum(WEIGHTS[k] * VALUE[d["state"]] for k, d in dims.items()) / sum(WEIGHTS.values()), 1)
    st = {k: d["state"] for k, d in dims.items()}
    hr_ok = st["human_reviewed"] in ("verified", "n/a")
    if st["translation"] in ("verified", "n/a") and st["subtitle_rendering"] == "verified" and hr_ok and st["tts"] == "verified" \
            and st["commercial_dubbing_licensed"] == "verified":
        level = "dubbed_publishable"
    elif st["translation"] in ("verified", "available", "n/a") and st["subtitle_rendering"] == "verified" and hr_ok:
        level = "subtitles_publishable"
    elif st["translation"] != "unavailable" and st["subtitle_rendering"] != "unavailable":
        level = "draft_needs_native_review"
    else:
        level = "not_supported"
    blockers = [f"{k}: {d['evidence']}" for k, d in dims.items() if d["state"] in ("unavailable", "partial") and k != "lip_sync"]
    return {"tag": tag, "name": lang["name"], "score": score, "level": level, "dimensions": dims, "blockers": blockers,
            "terminology": terminology.stats().get(tag, {"terms": 0, "native_review": 0}), "review": rv or None}


def report(ocr: bool = True) -> list[dict]:
    return [assess(t, ocr) for t in matrix.languages()]
