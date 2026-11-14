"""Simple mode: request validation, language understanding, script parsing, and cancelling a running job."""
import pytest

from nexus import autopilot, jobs


def test_validate_rejects_platform_page_links_and_empty_requests():
    with pytest.raises(autopilot.AutopilotError, match="cannot be downloaded"):
        autopilot.validate({"mode": "edit", "url": "https://www.youtube.com/watch?v=abc"})
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
