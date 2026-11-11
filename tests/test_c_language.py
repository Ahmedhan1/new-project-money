"""Test C - one authorised video -> English, Arabic (MSA), Egyptian Arabic, German: subtitles + dubbing + language QA."""
import json

import fixtures
from nexus import edit, localize, project, qa
from nexus.media import analyze, ff


def test_c_language_versions(fx, results):
    src = fixtures.make_talk(fx / "solar_talk.mp4", fixtures.SOLAR, pause_after=1, repeat=2)
    p = project.Project.create("Test C - languages", "localize")
    media = p.import_media(src)
    an = analyze.analyze(media["path"])
    tl = edit.build_timeline(media["path"], an, edit.propose(an, "tighten"), title="Why Solar Won")
    p.save_timeline(tl, "english master")
    summary = {}
    for lang in ["en", "ar", "ar-EG", "de"]:
        ltl, review = localize.localize(p, tl, lang, {media["path"]: an}, context="solar power explainer")
        p.save_timeline(ltl, f"{lang} version", branch=lang)
        stats, report = qa.render_with_qa(p, ltl, f"solar_{lang}", {media["path"]: an} if lang == "en" else None,
                                          extra_stages=[review], extra={"language": lang})
        out = ff.probe(stats["output"])
        assert out["video"] and out["audio"] and ltl["subtitles"]
        tech = next(s for s in report["stages"] if s["stage"] == "5-technical")
        assert tech["passed"], [c for c in tech["checks"] if not c["ok"]]
        summary[lang] = {
            "status": report["status"], "duration": out["duration"], "voice": review.get("voice"),
            "lines": review["lines"],
            "language_checks": [f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for c in review["checks"]],
            "known_issues": report["known_issues"]}
        dubs = [c for c in review["checks"] if c["check"].startswith("dub intelligibility")]
        summary[lang]["dub_pass_rate"] = round(sum(c["ok"] for c in dubs) / len(dubs), 2) if dubs else None
        if lang != "en":
            ff.run(["-ss", "5", "-i", stats["output"], "-frames:v", "1", results / f"C_frame_{lang}.png"])
    (results / "C_language.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    # English master passes every automated gate.
    assert summary["en"]["status"] == "PASSED_AUTOMATED_CHECKS"
    # MSA Arabic and German: the dub must be independently intelligible (Whisper hears the script back).
    for lang in ("ar", "de"):
        assert summary[lang]["dub_pass_rate"] >= 0.7, summary[lang]["language_checks"]
        assert any("native-speaker review" in i for i in summary[lang]["known_issues"])
    # Egyptian Arabic: must really be Egyptian (dialect markers) and independently intelligible.
    eg = summary["ar-EG"]
    assert any(c.startswith("PASS Egyptian dialect markers") for c in eg["language_checks"]), eg["language_checks"]
    assert eg["dub_pass_rate"] >= 0.7
    # no version may contain content the source did not have
    for lang in ("ar", "ar-EG", "de"):
        assert any(c.startswith("PASS no added/dropped content") for c in summary[lang]["language_checks"]), summary[lang]["lines"]
    # a voice without a verified commercial licence must show up in the report
    for lang in ("ar", "ar-EG"):
        assert summary[lang]["status"] != "PASSED_AUTOMATED_CHECKS" and any("voice licence" in i for i in summary[lang]["known_issues"])
