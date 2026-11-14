"""Simple mode: request validation, language understanding, script parsing, and cancelling a running job."""
import pytest

from nexus import autopilot, jobs


def test_validate_rejects_platform_page_links_and_empty_requests():
    for url in ("https://www.youtube.com/watch?v=abc", "https://www.pornhub.com/view_video.php?viewkey=x"):
        with pytest.raises(autopilot.AutopilotError, match="مش بتتحمل"):
            autopilot.validate({"mode": "edit", "url": url})
    with pytest.raises(autopilot.AutopilotError):
        autopilot.validate({"mode": "edit"})
    with pytest.raises(autopilot.AutopilotError):
        autopilot.validate({"mode": "edit", "project": "p", "goal": "scenes", "scenes": []})
    with pytest.raises(autopilot.AutopilotError):
        autopilot.validate({"mode": "idea", "idea": ""})
    v = autopilot.validate({"mode": "edit", "project": "p", "goal": "shorts", "shorts": 0, "platforms": ["youtube_shorts", "nope"]})
    assert v["shorts"] == 3 and v["platforms"] == ["youtube_shorts"]


def test_languages_understood_from_the_owners_words():
    assert autopilot.wanted_languages({}, "دبلجه عربي وترجمه ومونتاج شكله كويس") == (["ar"], [])
    assert autopilot.wanted_languages({}, "ترجمة انجليزي") == ([], ["en"])
    assert autopilot.wanted_languages({}, "قصّره بس") == ([], [])
    assert autopilot.wanted_languages({"dub": ["en"], "subtitles": ["ar"]}, "") == (["en"], ["ar"])
    v = autopilot.validate({"mode": "edit", "project": "p", "instructions": "عاوز دبلجة عربي"})
    assert v["dub"] == ["ar"]


def test_script_text_becomes_verbatim_scenes():
    s = autopilot.script_from_text("أول فقرة عن المريخ.\n\nتاني فقرة عن القمر. وجملة كمان.", "عنوان")
    assert s["title"] == "عنوان" and len(s["scenes"]) == 2
    assert s["scenes"][1]["narration"] == "تاني فقرة عن القمر. وجملة كمان."


def test_running_job_cancels_at_next_stage(home):
    seen = []

    @jobs.handler("two-stages")
    def two(job):
        job.stage("a", lambda: seen.append("a") or jobs.cancel(job.id))  # the owner cancels while stage a runs
        job.stage("b", lambda: seen.append("b"))
        return 1

    jid = jobs.submit("two-stages")["job"]
    assert jobs.run_job(jobs.claim("w1")) == "cancelled"
    assert seen == ["a"] and jobs.get(jid)["status"] == "cancelled"


def _an(dur, scenes, silences=()):
    return {"probe": {"duration": dur, "audio": True, "video": {"width": 1280, "height": 720}}, "transcript": None,
            "scenes": scenes, "silences": list(silences), "black": [], "repetitions": [], "loudness": {}, "quality": {}, "issues": []}


def _len(prop):
    return sum(r["out"] - r["in"] for r in prop["keep"])


def test_fit_to_target_hits_the_requested_length():
    an = _an(720.0, [(i * 30.0, (i + 1) * 30.0) for i in range(24)])  # a 12-minute video in 30 s scenes
    for target in (30, 95, 360, 600):
        prop = autopilot.fit_to_target(an, target)
        assert abs(_len(prop) - target) <= max(1.0, 0.02 * target), (target, _len(prop))
        assert all(a["out"] <= b["in"] + 1e-6 for a, b in zip(prop["keep"], prop["keep"][1:]))  # in order, no overlap


def test_fit_to_target_adds_context_back_when_clean_material_is_short():
    # 12 minutes, but only 2 x 20 s of usable scenes (the rest is silence): 6 minutes asked -> about 6 minutes delivered
    an = _an(720.0, [(100.0, 120.0), (500.0, 520.0)])
    prop = autopilot.fit_to_target(an, 360)
    assert abs(_len(prop) - 360) <= 8, _len(prop)


def test_fit_to_target_whole_video_when_target_is_its_length():
    an = _an(300.0, [(0.0, 150.0), (150.0, 300.0)])
    prop = autopilot.fit_to_target(an, 300)
    assert prop["keep"] == [{"in": 0.0, "out": 300.0, "score": 1.0, "segs": []}]
