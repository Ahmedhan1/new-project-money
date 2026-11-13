"""A running job says what it is doing: a first-use model download shows MB so far in the Jobs table, instead of
sitting on "running" with no sign of life (owner report, 2026-11-13: analyze looked frozen while Whisper downloaded)."""
import json

from nexus import db, jobs, models


def test_model_download_progress_is_visible_on_the_running_job(home, tmp_path, monkeypatch):
    src = tmp_path / "weights.bin"
    src.write_bytes(b"\0" * 3_000_000)
    monkeypatch.setattr(models.costguard, "check_host", lambda url: None)
    seen = []

    @jobs.handler("fetchy")
    def fetchy(job):
        def run():
            orig = job.note

            def spy(msg, progress=None):
                orig(msg, progress)
                st = json.loads(db.query("SELECT stages FROM jobs WHERE id=?", (job.id,))[0]["stages"])
                seen.append(st["get-model"].get("note"))
            job.note = spy
            models._fetch(src.as_uri(), tmp_path / "out" / "weights.bin", "model test-weights", 3)
            return (tmp_path / "out" / "weights.bin").stat().st_size
        return job.stage("get-model", run)

    jid = jobs.submit("fetchy")["job"]
    assert jobs.run_job(jobs.claim("w1")) == "done"
    assert seen and seen[-1] == "downloading model test-weights: done"
    assert jobs.get(jid)["result"] in (3_000_000, "3000000")
    assert "downloading model test-weights" in (jobs.config.LOGS / "jobs" / f"{jid}.log").read_text(encoding="utf-8")


def test_note_outside_a_job_is_harmless():
    jobs.note("nothing is running")  # library code calls this from the CLI too


def test_api_exposes_the_running_stage_note(home):
    from fastapi.testclient import TestClient
    from nexus import api

    jid = jobs.submit("fetchy-ui")["job"]
    job = jobs.claim("w1")
    job.stage("prepare", lambda: 1)
    job._stages["download"] = {"status": "running", "started": 0, "runs": 1}
    job._cur = "download"
    job.note("downloading model whisper-small: 120 / 490 MB (first use only)", 0.24)
    row = next(r for r in TestClient(api.app).get("/api/jobs").json() if r["id"] == jid)
    assert row["status"] == "running" and row["note"].startswith("downloading model whisper-small: 120 / 490 MB")
    assert abs(row["progress"] - 0.24) < 1e-6
