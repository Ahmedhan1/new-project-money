"""Acceptance test C - interrupted production: a worker dies mid-job, the job is recovered and finished,
and stages that had completed are not executed again."""
import os
import subprocess
import sys
import time
from pathlib import Path

from nexus import db, jobs

HERE = Path(__file__).parent
WORKER = "import crash_handlers; from nexus import jobs; jobs.run_worker(once=True)"


def _env(home):
    return {**os.environ, "NEXUS_HOME": str(home), "PYTHONPATH": os.pathsep.join([str(HERE), str(HERE.parent.parent)])}


def test_failed_stage_retries_from_checkpoint(home):
    calls = {"a": 0, "b": 0}

    @jobs.handler("flaky")
    def flaky(job):
        def a():
            calls["a"] += 1
            return 1

        def b():
            calls["b"] += 1
            if calls["b"] == 1:
                raise RuntimeError("transient")
            return 2

        return job.stage("a", a) + job.stage("b", b)

    jid = jobs.submit("flaky")["job"]
    assert jobs.run_job(jobs.claim("w1")) == "queued"          # failed once -> re-queued, not lost
    assert jobs.get(jid)["retries"] == 1 and "transient" in jobs.get(jid)["error"]
    assert jobs.run_job(jobs.claim("w2")) == "done"
    j = jobs.get(jid)
    assert j["result"] == 3 and calls == {"a": 1, "b": 2}        # stage a answered from its checkpoint
    assert j["stages"]["a"]["runs"] == 1 and j["stages"]["b"]["runs"] == 2
    assert db.metric_avg("stage_seconds.flaky.a") is not None    # stage timings feed the capacity planner


def test_retry_limit_then_manual_retry(home):
    @jobs.handler("always-bad")
    def bad(job):
        raise ValueError("nope")

    jid = jobs.submit("always-bad", max_retries=1)["job"]
    assert jobs.run_job(jobs.claim("w")) == "queued"
    assert jobs.run_job(jobs.claim("w")) == "failed"
    assert jobs.claim("w") is None
    assert jobs.get(jid)["recovery"] and jobs.retry(jid) and jobs.get(jid)["status"] == "queued"


def test_unknown_kind_fails_cleanly_and_cancel(home):
    jid = jobs.submit("no-such-kind")["job"]
    assert jobs.run_job(jobs.claim("w")) == "failed" and "no handler" in jobs.get(jid)["error"]
    j2 = jobs.submit("no-such-kind")["job"]
    assert jobs.cancel(j2) and jobs.claim("w") is None


def test_worker_killed_mid_job_resumes_without_repeating_stages(home):
    jid = jobs.submit("crash-test", {"n": 1})["job"]
    p = subprocess.Popen([sys.executable, "-c", WORKER], env=_env(home))
    try:
        t0 = time.time()
        while not (home / "b_started").exists():
            assert time.time() - t0 < 60 and p.poll() is None, "worker never reached stage b"
            time.sleep(0.1)
    finally:
        p.kill()            # hard kill: no cleanup code runs, like a crash or power loss
        p.wait()
    j = jobs.get(jid)
    assert j["status"] == "running" and j["stage"] == "b" and j["stages"]["a"]["status"] == "done"
    # The heartbeat is still fresh, but the worker's process is gone: recovered at once, not after STALE_S (a fast app
    # restart used to leave a killed render 'running' until the next idle moment).
    rec = jobs.recover(stale_after=3600)
    assert rec["requeued"] == [jid]
    assert "lost at stage 'b'" in jobs.get(jid)["recovery"]
    subprocess.run([sys.executable, "-c", WORKER], env=_env(home), check=True, timeout=120)       # a brand-new worker process
    j = jobs.get(jid)
    assert j["status"] == "done" and j["result"] == {"answer": 42}
    ran = {n: len((home / f"ran_{n}").read_text()) for n in "abc"}
    assert ran == {"a": 1, "b": 2, "c": 1}, ran                 # a was NOT repeated; only the interrupted stage re-ran
    assert "restored from checkpoint" in Path(j["log"]).read_text()


def test_fresh_heartbeat_of_a_live_worker_is_left_alone(home):
    jid = jobs.submit("crash-test", {"n": 1})["job"]
    assert jobs.claim(jobs.worker_id()).id == jid                      # this (live) process holds it
    assert jobs.recover(stale_after=3600)["requeued"] == []
    db.execute("UPDATE jobs SET worker='worker-other-machine-123' WHERE id=?", (jid,))
    assert jobs.recover(stale_after=3600)["requeued"] == []           # remote worker: only the heartbeat can tell
    assert jobs.recover(stale_after=0)["requeued"] == [jid]


def test_a_killed_but_unreaped_worker_counts_as_dead(home):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    wid = f"worker-{jobs.socket.gethostname()}-{p.pid}"
    assert jobs._local_worker_dead(wid) is False
    p.kill()
    time.sleep(0.3)                  # killed, not yet waited for: a zombie, which psutil.pid_exists reports as alive
    try:
        assert jobs._local_worker_dead(wid) is True
    finally:
        p.wait()
    assert jobs._local_worker_dead(wid) is True


def test_watchdog_recovers_while_the_worker_is_busy(home, monkeypatch):
    import threading
    monkeypatch.setattr(jobs, "STALE_S", 0.2)
    dead = jobs.submit("crash-test", {"n": 1})["job"]
    db.execute("UPDATE jobs SET status='running', worker='worker-x-1', heartbeat=? WHERE id=?", (time.time() - 99, dead))
    gate, stop = threading.Event(), threading.Event()

    @jobs.handler("slow-test")
    def slow(job):
        gate.wait(10)
        return {}

    jobs.submit("slow-test", {})
    db.execute("UPDATE jobs SET status='done' WHERE id=?", (dead,))   # keep the dead job out of the first claim
    th = threading.Thread(target=jobs.run_worker, kwargs={"stop": stop, "poll": 0.05}, daemon=True)
    th.start()
    t0 = time.time()
    while (jobs.get(next(r["id"] for r in db.query("SELECT id FROM jobs WHERE kind='slow-test'")))["status"] != "running"):
        assert time.time() - t0 < 10
        time.sleep(0.05)
    db.execute("UPDATE jobs SET status='running' WHERE id=?", (dead,))   # now its worker dies while the other job runs
    t0 = time.time()
    while jobs.get(dead)["status"] == "running":
        assert time.time() - t0 < 5, "watchdog did not recover the dead job while the worker was busy"
        time.sleep(0.05)
    assert jobs.get(dead)["status"] in ("queued", "running") and "lost" in jobs.get(dead)["recovery"]
    stop.set()
    gate.set()
    th.join(10)


def test_in_process_jobs_of_a_dead_app_are_flagged_not_left_running(home):
    import time as t
    db.execute("INSERT INTO jobs(id,project_id,kind,status,worker,progress,created) VALUES('job_old','','remix','running','api-host-1',0,?)", (t.time(),))
    db.execute("INSERT INTO jobs(id,project_id,kind,status,worker,progress,created) VALUES('job_mine','','remix','queued','api-host-2',0,?)", (t.time(),))
    rec = jobs.recover(api_worker="api-host-2")
    assert rec["interrupted"] == ["job_old"]
    assert jobs.get("job_old")["status"] == "interrupted" and jobs.get("job_old")["recovery"]
    assert jobs.get("job_mine")["status"] == "queued"


def test_create_pipeline_runs_as_resumable_stages(home, monkeypatch):
    """The real create-from-idea workflow, with the model-backed functions stubbed: crash in render, resume."""
    from nexus import create, qa, workflows  # noqa: F401
    n = {"research": 0, "script": 0, "produce": 0, "render": 0}

    def research(topic, notes=None, **_):
        n["research"] += 1
        return {"topic": topic, "sources": [{"title": "t", "url": "https://example.org"}], "text": "facts", "grounded": True}

    def script_with_review(topic, res, scenes, **_):
        n["script"] += 1
        return {"title": "T", "topic": topic, "scenes": [{"narration": "n", "on_screen_text": "o", "visual_query": "q"}]}, {"stage": "1-pre-production", "checks": [], "passed": True}

    def produce(project, script, **_):
        n["produce"] += 1
        return {"clips": [], "width": 1280, "height": 720}

    def render_with_qa(project, tl, name, **_):
        n["render"] += 1
        if n["render"] == 1:
            raise RuntimeError("encoder crashed")
        return {"output": str(project.dir / "renders" / "master.mp4"), "elapsed": 1.0}, {"status": "PASSED_AUTOMATED_CHECKS", "known_issues": []}

    monkeypatch.setattr(create, "research", research)
    monkeypatch.setattr(create, "script_with_review", script_with_review)
    monkeypatch.setattr(create, "produce", produce)
    monkeypatch.setattr(qa, "render_with_qa", render_with_qa)
    jid = jobs.submit("create-from-idea", {"topic": "Solar power", "scenes": 1})["job"]
    assert jobs.run_job(jobs.claim("w")) == "queued"
    assert jobs.run_job(jobs.claim("w")) == "done"
    assert n == {"research": 1, "script": 1, "produce": 1, "render": 2}
    j = jobs.get(jid)
    assert j["project_id"].startswith("prj_") and j["result"]["status"] == "PASSED_AUTOMATED_CHECKS"
    assert [k for k in j["stages"]] == ["project", "research", "script", "assemble", "render-qa"]


def test_series_episode_is_not_written_twice_when_production_crashes(home, monkeypatch):
    """Writing an episode appends it to the series canon; a crash during production must not append it again."""
    from nexus import series, workflows  # noqa: F401
    n = {"write": 0, "produce": 0}

    def next_episode(sid):
        n["write"] += 1
        return {"id": sid, "episodes": [{"number": n["write"]}]}, {"number": n["write"], "scenes": []}, {"checks": [], "attempts": 1}

    def produce_episode(b, ep, review):
        n["produce"] += 1
        if n["produce"] == 1:
            raise RuntimeError("render crashed")
        return {"project": "prj_x", "report": {"status": "PASSED_AUTOMATED_CHECKS", "known_issues": []}}

    monkeypatch.setattr(series, "next_episode", next_episode)
    monkeypatch.setattr(series, "produce_episode", produce_episode)
    jid = jobs.submit("series-episode", {"series": "series_1"})["job"]
    assert jobs.run_job(jobs.claim("w")) == "queued"
    assert jobs.run_job(jobs.claim("w")) == "done"
    j = jobs.get(jid)
    assert n == {"write": 1, "produce": 2} and j["result"]["episode"]["number"] == 1 and j["project_id"] == "prj_x"


def test_backups_are_scheduled_automatically(home):
    import json
    import time as t
    from nexus import project as prj
    assert jobs.schedule_backup() is None                       # nothing to protect yet
    prj.Project.create("p", "edit")
    jid = jobs.schedule_backup()
    assert jid and jobs.get(jid)["kind"] == "backup"            # never backed up -> queued
    assert jobs.schedule_backup() is None                       # one pending backup is enough
    from nexus import workflows  # noqa: F401
    assert jobs.run_job(jobs.claim("w")) == "done" and (home / "last_backup.json").exists()
    assert jobs.schedule_backup() is None                       # fresh backup -> nothing to do
    assert jobs.schedule_backup(now=t.time() + 25 * 3600)       # a day later -> queued again
    (home / "settings.json").write_text(json.dumps({"backup_every_hours": 0}))
    db.execute("DELETE FROM jobs")
    assert jobs.schedule_backup(now=t.time() + 99 * 3600) is None   # user switched it off



def test_heavy_lock_is_reentrant_in_a_thread_and_exclusive_across_threads(home):
    """A job handler holds the lock; code it calls that guards itself again must not deadlock (found on a real
    research job: the worker waited on its own lock for 18 minutes). Another thread must still wait."""
    import threading
    import time as t
    order = []
    with jobs.heavy_lock():
        with jobs.heavy_lock():          # same thread: immediate
            order.append("nested")

        def other():
            with jobs.heavy_lock():
                order.append("other")
        th = threading.Thread(target=other)
        th.start()
        t.sleep(0.3)
        order.append("outer-still-held")
    th.join(5)
    assert order == ["nested", "outer-still-held", "other"]
