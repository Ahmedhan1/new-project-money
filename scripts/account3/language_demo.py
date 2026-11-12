"""Account 3 / agent D demo: translate a fixed 8-sentence English source into de, fr, ar-EG, ar-Gulf, zh, ru, hi, es
with the existing MT (nexus.localize.translate_sentences: MADLAD-400 + local-LLM fallback + pack dialect rules), run the
dialect pass and the translation QA (back-translation through the model router), export SRT/VTT/ASS, build dubbing
manifests and render ONE voice-over line per language where a commercially cleared Piper voice exists.

Run:  NEXUS_HOME=/home/user/new-project-money/.nexus python scripts/account3/language_demo.py [--langs de,fr]
Writes docs/results/account3/language_demo.json (every QA finding, nothing filtered). Subtitle files and audio go to
$NEXUS_HOME/projects/account3_language_demo/ (not Git). Heavy work holds nexus.jobs.heavy_lock(), one language at a time.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from nexus import config, jobs, llm  # noqa: E402
from nexus.intelligence.language import dialect, dubbing, subtitles  # noqa: E402
from nexus.intelligence.language import translation_qa as tq  # noqa: E402

SOURCE = [
    "The Aswan High Dam on the Nile in Egypt was built between 1960 and 1970.",
    "It is 111 metres high and about 3,830 metres long.",
    "Behind it lies Lake Nasser, which can hold about 132 cubic kilometres of water.",
    "Its 12 turbines can generate up to 2,100 megawatts of electricity.",
    "Engineers from the Soviet Union helped to design and build the dam.",
    "The rising water threatened the temples of Abu Simbel, built for Ramesses II.",
    "Between 1964 and 1968, an international UNESCO campaign moved the temples to higher ground.",
    "Today the dam protects farms from floods, but it also traps the silt that once fertilised the Nile valley.",
]
SOURCE_NOTE = ("Facts are standard encyclopaedic figures (built 1960-1970, 111 m high, ~3,830 m long, Lake Nasser ~132 km3, "
               "12 x 175 MW turbines, Soviet assistance, UNESCO relocation of Abu Simbel 1964-1968); written by the agent, "
               "not re-verified against a primary source for this demo - the demo tests translation fidelity, not the facts.")
LANGS = ["de", "fr", "ar-EG", "ar-Gulf", "zh", "ru", "hi", "es"]
# Terminology the target MUST use (chosen by the agent from common usage, not by a native reviewer; substring match).
AR = {"Aswan": "أسوان", "Nile": "النيل", "Lake Nasser": "بحيرة ناصر", "Soviet Union": "السوفي", "Abu Simbel": "أبو سمبل",
      "Ramesses II": "رمسيس", "UNESCO": "اليونسكو", "Egypt": "مصر"}
GLOSSARY = {
    "de": {"Aswan": "Assuan", "Nile": "Nil", "Lake Nasser": "Nassersee", "Soviet Union": "Sowjetunion", "Abu Simbel": "Abu Simbel",
           "Ramesses II": "Ramses II", "UNESCO": "UNESCO", "Egypt": "Ägypten"},
    "fr": {"Aswan": "Assouan", "Nile": "Nil", "Lake Nasser": "lac Nasser", "Soviet Union": "Union soviétique", "Abu Simbel": "Abou Simbel",
           "Ramesses II": "Ramsès II", "UNESCO": "UNESCO", "Egypt": "Égypte"},
    "es": {"Aswan": "Asuán", "Nile": "Nilo", "Lake Nasser": "lago Nasser", "Soviet Union": "Unión Soviética", "Abu Simbel": "Abu Simbel",
           "Ramesses II": "Ramsés II", "UNESCO": "UNESCO", "Egypt": "Egipto"},
    "ru": {"Aswan": "Асуан", "Nile": "Нил", "Lake Nasser": "Насер", "Soviet Union": "Советск", "Abu Simbel": "Абу-Симбел",
           "Ramesses II": "Рамсес", "UNESCO": "ЮНЕСКО", "Egypt": "Египт"},
    "zh": {"Aswan": "阿斯旺", "Nile": "尼罗河", "Lake Nasser": "纳赛尔湖", "Soviet Union": "苏联", "Abu Simbel": "阿布辛贝",
           "Ramesses II": "拉美西斯二世", "UNESCO": "联合国教科文组织", "Egypt": "埃及"},
    "hi": {"Aswan": "असवान", "Nile": "नील", "Lake Nasser": "नासिर", "Soviet Union": "सोवियत", "Abu Simbel": "अबू सिम्बल",
           "Ramesses II": "रामसेस", "UNESCO": "यूनेस्को", "Egypt": "मिस्र"},
    "ar-EG": AR, "ar-Gulf": AR,
}
OUT = ROOT / "docs" / "results" / "account3" / "language_demo.json"


def timings() -> list[dict]:
    """A synthetic timeline: each English sentence gets the time an English narrator needs (2.6 words/s + 0.4 s)."""
    t, out = 0.5, []
    for s in SOURCE:
        d = round(len(s.split()) / 2.6 + 0.4, 2)
        out.append({"start": round(t, 2), "end": round(t + d, 2)})
        t += d + 0.3
    return out


def run_lang(lang: str, work: Path) -> dict:
    from nexus import localize
    from nexus.speech import mt
    rec: dict = {"lang": lang, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    t0 = time.time()
    with jobs.heavy_lock():
        try:
            mt_lines = localize.translate_sentences(SOURCE, lang, context="history and engineering of the Aswan High Dam")
            rec["mt_seconds"] = round(time.time() - t0, 1)
            rec["mt_output"] = mt_lines
            final = list(mt_lines)
            if lang in ("ar-EG", "ar-Gulf"):
                rec["dialect_before"] = [dialect.classify(x) for x in mt_lines]
                ad = dialect.adapt_lines(mt_lines, lang, privacy="internal", register="colloquial")
                final = [a["text"] for a in ad]
                rec["dialect_pass"] = [{k: a.get(k) for k in ("method", "changes", "router", "router_rejected", "forbidden_hits")}
                                       for a in ad]
                rec["dialect_after"] = [a["classifier"] for a in ad]
            rec["final"] = final
            t1 = time.time()
            back, note = tq.back_translate(final, lang, privacy="internal")
            rec["back_translation"] = {"lines": back, "note": note, "seconds": round(time.time() - t1, 1)}
        finally:
            llm.unload()
            mt.unload()
    rec["qa"] = tq.check_lines(SOURCE, final, lang, GLOSSARY[lang], back)
    tm = timings()
    cues = subtitles.build_cues([{"text": t, **tm[i]} for i, t in enumerate(final)], lang)
    exp = subtitles.export_all(cues, lang, work, "aswan")
    rec["subtitles"] = {"paths": exp["paths"], "qa_full": {k: exp["qa_full"][k] for k in ("cues", "findings", "passed")},
                        "qa_condensed": {k: exp["qa_condensed"][k] for k in ("cues", "findings", "passed")},
                        "condense_todo": exp["condense_todo"],
                        "srt_full": Path(exp["paths"]["full.srt"]).read_text(encoding="utf-8"),
                        "ass_style": [x for x in Path(exp["paths"]["full.ass"]).read_text(encoding="utf-8").splitlines() if x.startswith("Style:")]}
    man = dubbing.build_manifest([{"text": t, **tm[i]} for i, t in enumerate(final)], lang, commercial=True, project_id="account3_language_demo")
    rec["dubbing"] = {k: man[k] for k in ("voice", "blocked", "needs_shortening", "estimate_note")}
    rec["dubbing"]["lines"] = [{k: x[k] for k in ("idx", "voice", "licence", "timing_target", "estimated_s", "speed_needed", "action")} for x in man["lines"]]
    if man["voice"] and man["voice"].get("engine") == "piper":
        with jobs.heavy_lock():
            try:
                rec["voiceover_line"] = {"idx": 0, "text": final[0], **dubbing.render_demo_line(man, 0, work / f"vo_{lang}_0.wav")}
            except Exception as e:  # noqa: BLE001 - recorded, never hidden
                rec["voiceover_line"] = {"idx": 0, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    else:
        rec["voiceover_line"] = {"skipped": man["blocked"] or f"voice engine {man['voice'] and man['voice'].get('engine')} not integrated"}
    rec["seconds"] = round(time.time() - t0, 1)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", default=",".join(LANGS))
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--reqa", action="store_true", help="recompute QA from stored outputs only")
    a = ap.parse_args()
    work = config.HOME / "projects" / "account3_language_demo"
    work.mkdir(parents=True, exist_ok=True)
    doc = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() and not a.fresh else {}
    doc.update({"schema": "nexus.Account3LanguageDemo/1", "source": SOURCE, "source_note": SOURCE_NOTE, "glossary": GLOSSARY,
                "timings": timings(), "machine": "4 CPU / 15 GB shared; MADLAD-400 3B int8 (CTranslate2), Qwen3-4B Q4_K_M via model router"})
    doc.setdefault("languages", {})
    if a.reqa:  # recompute the deterministic QA from the stored translations + back-translations (no models)
        for lang, r in doc["languages"].items():
            if "final" in r:
                r["qa"] = tq.check_lines(SOURCE, r["final"], lang, GLOSSARY[lang], (r.get("back_translation") or {}).get("lines"))
                r["qa_recomputed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if "ar-Gulf" in doc["languages"] and "mt_output" in doc["languages"]["ar-Gulf"]:
            doc["dialect_contrast"] = dialect_contrast(doc["languages"]["ar-Gulf"]["mt_output"])
        doc["summary"] = summary(doc)
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return
    for lang in a.langs.split(","):
        print(f"[{time.strftime('%H:%M:%S')}] {lang} ...", flush=True)
        try:
            doc["languages"][lang] = run_lang(lang, work)
        except Exception as e:  # noqa: BLE001
            doc["languages"][lang] = {"lang": lang, "error": f"{type(e).__name__}: {str(e)[:400]}"}
        r = doc["languages"][lang]
        print(f"   done: {r.get('seconds')} s, qa counts {r.get('qa', {}).get('counts')}", flush=True)
        doc["summary"] = summary(doc)
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def dialect_contrast(msa_lines: list[str]) -> dict:
    """Same real MSA machine translation (MADLAD <2ar>, from the ar-Gulf run) through the EG and the Gulf lexical
    passes: do the outputs differ, and does the classifier tell the three apart? No model involved."""
    eg = dialect.adapt_lines(msa_lines, "ar-EG", use_router=False)
    gu = dialect.adapt_lines(msa_lines, "ar-Gulf", use_router=False)
    rows = []
    for m, e, g in zip(msa_lines, eg, gu):
        rows.append({"msa": m, "msa_label": dialect.classify(m)["label"], "eg": e["text"], "eg_label": e["classifier"]["label"],
                     "eg_changes": e["changes"], "gulf": g["text"], "gulf_label": g["classifier"]["label"], "gulf_changes": g["changes"],
                     "differ": e["text"] != g["text"]})
    whole = {k: dialect.classify(" ".join(r[k] for r in rows)) for k in ("msa", "eg", "gulf")}
    return {"rows": rows, "lines_that_differ": sum(r["differ"] for r in rows), "whole_text": whole,
            "note": "lexical substitution only changes function words present in the MT output; lines without such words stay MSA"}


def summary(doc: dict) -> dict:
    out = {}
    for lang, r in doc["languages"].items():
        if "error" in r:
            out[lang] = {"error": r["error"]}
            continue
        q = r["qa"]
        out[lang] = {"lines_needing_fix": [i + 1 for i in q["lines_needing_fix"]], "finding_counts": q["counts"],
                     "mean_confidence": q["mean_confidence"], "back_translation": r["back_translation"]["note"],
                     "subtitles_full_passed": r["subtitles"]["qa_full"]["passed"],
                     "subtitle_findings": len(r["subtitles"]["qa_full"]["findings"]),
                     "voice": (r["dubbing"]["voice"] or {}).get("id"), "voice_blocked": r["dubbing"]["blocked"],
                     "voiceover_line": {k: r["voiceover_line"].get(k) for k in ("voice", "duration", "error", "skipped") if r["voiceover_line"].get(k) is not None}}
        if "dialect_after" in r:
            out[lang]["dialect_labels_before"] = [x["label"] for x in r["dialect_before"]]
            out[lang]["dialect_labels_after"] = [x["label"] for x in r["dialect_after"]]
            out[lang]["dialect_methods"] = [x["method"] for x in r["dialect_pass"]]
    return out


if __name__ == "__main__":
    main()
