"""Test C - one authorised video -> every language in registry/languages.yaml: subtitles + dubbing + language QA.

Languages marked `unavailable` are not rendered, but they are written to the results with their status and reason
(set NEXUS_LANG_EXPERIMENTAL=1 to run them anyway and record how they fail). Results: docs/results/C_language.json."""
import json
import os

import fixtures
from nexus import edit, localize, models, project, qa
from nexus.media import analyze, ff

FRAMES = {"de", "fr", "ar-EG", "zh", "ru", "hi"}           # one still per priority language / script (evidence, kept small)
MUST_PASS = ("script", "clean text", "no code-switching", "line alignment", "no invented numbers", "no added/dropped content")


def _checks(review: dict) -> list[str]:
    return [f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for c in review["checks"]]


def test_c_language_versions(fx, results):
    src = fixtures.make_talk(fx / "solar_talk.mp4", fixtures.SOLAR, pause_after=1, repeat=2)
    p = project.Project.create("Test C - languages", "localize")
    media = p.import_media(src)
    an = analyze.analyze(media["path"])
    tl = edit.build_timeline(media["path"], an, edit.propose(an, "tighten"), title="Why Solar Won")
    p.save_timeline(tl, "english master")
    experimental = os.environ.get("NEXUS_LANG_EXPERIMENTAL") == "1"
    summary = {}
    for lang, entry in models.languages().items():
        reg = {"registry_status": entry["status"], "reason": entry["reason"], "translation": entry["translation"], "madlad": entry.get("madlad")}
        if entry["status"] == "unavailable" and not experimental:
            summary[lang] = {**reg, "status": "NOT RUN - registry: unavailable"}
            continue
        ltl, review = localize.localize(p, tl, lang, {media["path"]: an}, context="solar power explainer", experimental=experimental)
        p.save_timeline(ltl, f"{lang} version", branch=lang)
        stats, report = qa.render_with_qa(p, ltl, f"solar_{lang}", {media["path"]: an} if lang == "en" else None,
                                          extra_stages=[review], extra={"language": lang})
        out = ff.probe(stats["output"])
        assert out["video"] and out["audio"] and ltl["subtitles"]
        tech = next(s for s in report["stages"] if s["stage"] == "5-technical")
        assert tech["passed"], (lang, [c for c in tech["checks"] if not c["ok"]])
        dubs = [c for c in review["checks"] if c["check"].startswith("dub intelligibility")]
        summary[lang] = {
            **reg, "status": report["status"], "duration": out["duration"], "voice": review.get("voice"),
            "lines": review["lines"], "language_checks": _checks(review), "known_issues": report["known_issues"],
            "dub_pass_rate": round(sum(c["ok"] for c in dubs) / len(dubs), 2) if dubs else None,
            "dub_similarity": [c["similarity"] for c in dubs], "dub_heard": [c["heard"] for c in dubs],
            "subtitles": [c["text"] for c in ltl["subtitles"]], "elapsed_sec": stats.get("elapsed")}
        if lang in FRAMES:
            ff.run(["-ss", "5", "-i", stats["output"], "-frames:v", "1", results / f"C_frame_{lang}.png"])
        (results / "C_language.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))   # partial results survive a failure
    (results / "C_language.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))

    # English master passes every automated gate.
    assert summary["en"]["status"] == "PASSED_AUTOMATED_CHECKS"
    langs = models.languages()
    for lang, s in summary.items():
        if lang == "en" or "language_checks" not in s:
            continue
        # every translated version: right script, aligned, nothing invented or dropped (incl. numbers)
        for name in MUST_PASS:
            assert any(c.startswith(f"PASS {name} [") for c in s["language_checks"]), (lang, name, s["lines"])
        assert any("native-speaker review" in i for i in s["known_issues"])        # MT is never published unreviewed
        # the registry status must match what was measured: 'available' needs a cleared voice and an intelligible dub
        if langs[lang]["status"] == "available":
            assert s["voice"]["commercial"] and s["voice"]["engine"] == "piper", (lang, s["voice"])
            assert s["dub_pass_rate"] >= 0.8, (lang, s["language_checks"])
        else:  # subtitles-only: the draft dub must be visible in the report, never a clean pass
            assert s["status"] != "PASSED_AUTOMATED_CHECKS", lang
            assert any("voice licence" in i or "dub voice quality" in i for i in s["known_issues"]), (lang, s["known_issues"])
        assert s["dub_pass_rate"] >= 0.6, (lang, s["language_checks"])           # even a draft dub must be understandable
    # Egyptian Arabic: must really be Egyptian (dialect markers).
    assert any(c.startswith("PASS Egyptian dialect markers") for c in summary["ar-EG"]["language_checks"]), summary["ar-EG"]["language_checks"]
    # A voice without a verified commercial licence must show up in the report.
    for lang in ("ar", "ar-EG"):
        assert any("voice licence" in i for i in summary[lang]["known_issues"])
    # Gulf Arabic is never shipped as MSA: either not run (unavailable) or its dialect check decides.
    g = summary["ar-GULF"]
    if "language_checks" in g:
        assert g["status"] != "PASSED_AUTOMATED_CHECKS" or any(c.startswith("PASS Gulf dialect markers") for c in g["language_checks"])
