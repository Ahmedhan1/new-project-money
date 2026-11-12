"""D: autonomous production scheduler - targets, priority, limits, pause/resume/stop, retries, ETA, persistence."""
import time

import pytest

DAY0 = 1_790_000_000.0 - (1_790_000_000.0 % 86400) + 3600  # 01:00 UTC on some day


class FakeCompute:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.submitted = []
        self.cancelled = []
        self.quotas: list[dict] = []
        self.storage = {"used_bytes": 0, "free_disk_bytes": 10 ** 12, "exhausted_connections": []}
        self.fail_submit = None

    def submit(self, m):
        if self.fail_submit:
            raise self.fail_submit
        self.submitted.append(m)
        self.jobs[m.job_id] = {"status": "queued", "task": m.task}
        return m.job_id

    def cancel(self, jid):
        self.cancelled.append(jid)
        self.jobs[jid]["status"] = "cancelled"
        return True

    def status(self, jid):
        return self.jobs.get(jid, {"status": "unknown"})

    def finish(self, jid, status="succeeded", started=None, finished=None, error=None):
        self.jobs[jid].update(status=status, started=started, finished=finished, error=error)

    def active(self):
        return [m for m in self.submitted if self.jobs[m.job_id]["status"] in ("queued", "running")]


@pytest.fixture()
def fc(home):
    from nexus.cloud import scheduler as sch
    f = FakeCompute()
    f.sched = sch.Scheduler(sch.Adapter(submit_fn=f.submit, cancel_fn=f.cancel, quota_fn=lambda: f.quotas,
                                        status_fn=f.status, storage_fn=lambda: f.storage), clock=lambda: f.now)
    f.now = DAY0
    f.sch = sch
    return f


def _drain(fc, now, status="succeeded"):
    """Finish every active job, tick; repeat until nothing new is submitted."""
    for _ in range(50):
        act = fc.active()
        if not act:
            fc.sched.tick(now)
            if not fc.active():
                return
        for m in fc.active():
            fc.finish(m.job_id, status, started=now - 10, finished=now)
        fc.sched.tick(now)


def test_daily_target_counting_and_next_day(fc):
    fc.sch.set_config({"daily_masters": 2, "shorts_per_master": 2, "languages": ["en"], "max_active_jobs": 3,
                       "plan": {"master": ["probe"], "short": ["ffmpeg_transcode"]}})
    fc.sched.start()
    fc.sched.tick(DAY0)
    assert [m.task for m in fc.submitted] == ["probe", "probe"]  # shorts wait for their master
    _drain(fc, DAY0 + 60)
    tasks = [m.task for m in fc.submitted]
    assert tasks.count("probe") == 2 and tasks.count("ffmpeg_transcode") == 4
    snap = fc.sched.snapshot(DAY0 + 60)
    assert snap["today"] == {"master": {"target": 2, "done": 2}, "short": {"target": 4, "done": 4}}
    n = len(fc.submitted)
    fc.sched.tick(DAY0 + 120)
    fc.sched.tick(DAY0 + 120)
    assert len(fc.submitted) == n  # target met: idempotent ticks submit nothing
    fc.sched.tick(DAY0 + 86400)
    assert len(fc.submitted) == n + 2  # the next UTC day plans a new batch
    m = fc.submitted[-1]
    assert m.params["language"] == "en" and m.idempotency_key.startswith("sched:")


def test_priority_and_max_active(fc):
    fc.sch.set_config({"daily_masters": 1, "shorts_per_master": 0, "languages": ["en", "de", "fr"], "max_active_jobs": 2,
                       "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.sched.start()
    fc.sched.tick(DAY0)
    assert len(fc.submitted) == 2 and len(fc.active()) == 2
    fc.sched.tick(DAY0 + 1)
    assert len(fc.submitted) == 2
    assert fc.sched.snapshot(DAY0)["blocked"] == "max active jobs reached"
    # shorts more urgent than masters: after the masters finish, shorts go first
    fc.sch.set_config({"priority": {"master": 6, "short": 1}, "shorts_per_master": 1, "max_active_jobs": 1})
    for m in fc.active():
        fc.finish(m.job_id, started=DAY0, finished=DAY0 + 5)
    fc.sched.tick(DAY0 + 10)
    last = fc.submitted[-1]
    assert last.params["kind"] == "short" and last.priority == 1


def test_pause_resume_stop_after_current(fc):
    fc.sch.set_config({"daily_masters": 3, "shorts_per_master": 0, "max_active_jobs": 1, "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.sched.start()
    fc.sched.tick(DAY0)
    assert len(fc.submitted) == 1
    fc.sched.pause()
    fc.finish(fc.submitted[0].job_id, started=DAY0, finished=DAY0 + 3)
    fc.sched.tick(DAY0 + 5)
    assert len(fc.submitted) == 1 and fc.sch.get_state()["status"] == "paused"
    assert fc.sched.snapshot(DAY0 + 5)["today"]["master"]["done"] == 1  # results still collected while paused
    fc.sched.resume()
    fc.sched.tick(DAY0 + 6)
    assert len(fc.submitted) == 2
    fc.sched.stop_after_current()
    fc.sched.tick(DAY0 + 7)
    assert fc.sch.get_state()["status"] == "stopping" and len(fc.submitted) == 2
    fc.finish(fc.submitted[1].job_id, started=DAY0 + 6, finished=DAY0 + 9)
    fc.sched.tick(DAY0 + 10)
    assert fc.sch.get_state()["status"] == "stopped" and len(fc.submitted) == 2
    from nexus.cloud import events
    assert any(e["type"] == "scheduler.stopped" for e in events.events())


def test_stop_cancels_safely_and_restart_replans(fc):
    fc.sch.set_config({"daily_masters": 2, "shorts_per_master": 0, "max_active_jobs": 2, "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.sched.start()
    fc.sched.tick(DAY0)
    fc.sched.stop()
    assert sorted(fc.cancelled) == sorted(m.job_id for m in fc.submitted)
    fc.sched.tick(DAY0 + 1)
    assert len(fc.submitted) == 2
    fc.sched.start()
    fc.sched.tick(DAY0 + 2)
    assert len(fc.submitted) == 4
    assert fc.submitted[2].idempotency_key != fc.submitted[0].idempotency_key


def test_quota_exhaustion_pauses_and_never_rotates(fc):
    fc.sch.set_config({"daily_masters": 2, "shorts_per_master": 0, "max_active_jobs": 1, "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.sched.start()
    fc.sched.tick(DAY0)
    fc.quotas = [{"provider": "kaggle", "connection_id": "kaggle", "resource": "gpu_seconds_week", "limit": 108000,
                  "used": 108000, "exhausted": True}]
    fc.finish(fc.submitted[0].job_id, started=DAY0, finished=DAY0 + 4)
    fc.sched.tick(DAY0 + 5)
    st = fc.sch.get_state()
    assert st["status"] == "paused" and st["pause_reason"] == "quota" and len(fc.submitted) == 1
    from nexus.cloud import events
    assert any("quota" in n["title"].lower() or "paused" in n["title"].lower() for n in events.notifications())
    assert all(not m.preferred_providers for m in fc.submitted)  # no account switching
    fc.sched.tick(DAY0 + 6)
    assert len([n for n in events.notifications() if n["title"] == "Scheduler paused"]) == 1  # not re-notified each tick
    # a job that reports quota_exhausted itself: back to pending, scheduler paused
    fc.quotas = []
    fc.sched.resume()
    fc.sched.tick(DAY0 + 7)
    fc.finish(fc.submitted[1].job_id, status="quota_exhausted")
    fc.sched.tick(DAY0 + 8)
    assert fc.sch.get_state()["pause_reason"] == "quota"
    fc.sched.resume()
    fc.sched.tick(DAY0 + 9)
    assert len(fc.submitted) == 3 and fc.submitted[2].idempotency_key != fc.submitted[1].idempotency_key


def test_storage_exhaustion_pauses(fc):
    fc.sch.set_config({"max_storage_bytes": 1000, "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.storage["used_bytes"] = 1500
    fc.sched.start()
    fc.sched.tick(DAY0)
    st = fc.sch.get_state()
    assert st["status"] == "paused" and st["pause_reason"] == "storage" and not fc.submitted
    fc.storage = {"used_bytes": 0, "free_disk_bytes": 10, "exhausted_connections": []}
    fc.sch.set_config({"max_storage_bytes": None})
    fc.sched.resume()
    fc.sched.tick(DAY0 + 1)
    assert fc.sch.get_state()["pause_reason"] == "storage"  # disk floor
    fc.storage = {"used_bytes": 0, "free_disk_bytes": 10 ** 12, "exhausted_connections": ["drive_b"]}
    fc.sched.resume()
    fc.sched.tick(DAY0 + 2)
    assert fc.sch.get_state()["pause_reason"] == "storage"


def test_retry_with_backoff_respects_max_retries(fc):
    fc.sch.set_config({"daily_masters": 1, "shorts_per_master": 1, "max_retries": 2, "retry_backoff_s": 100,
                       "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.sched.start()
    fc.sched.tick(DAY0)
    fc.finish(fc.submitted[0].job_id, "failed", error="worker crashed")
    fc.sched.tick(DAY0 + 1)
    assert len(fc.submitted) == 1
    fc.sched.tick(DAY0 + 99)
    assert len(fc.submitted) == 1  # backoff 100 s
    fc.sched.tick(DAY0 + 102)
    assert len(fc.submitted) == 2 and fc.submitted[1].max_retries == 2
    fc.finish(fc.submitted[1].job_id, "lost")
    fc.sched.tick(DAY0 + 103)
    fc.sched.tick(DAY0 + 103 + 199)
    assert len(fc.submitted) == 2  # second backoff doubles to 200 s
    fc.sched.tick(DAY0 + 103 + 201)
    assert len(fc.submitted) == 3
    fc.finish(fc.submitted[2].job_id, "failed", error="again")
    fc.sched.tick(DAY0 + 400)
    fc.sched.tick(DAY0 + 10_000)
    assert len(fc.submitted) == 3  # 1 + max_retries attempts, then give up
    snap = fc.sched.snapshot(DAY0 + 10_000)
    assert snap["by_status"] == {"failed": 1, "skipped": 1}  # dependent short skipped
    from nexus.cloud import events
    assert any(n["level"] == "error" and "failed" in n["title"] for n in events.notifications())
    assert sum(1 for e in events.events() if e["type"] == "job.retrying") == 2


def test_eta_from_measured_history(fc):
    from nexus import db
    fc.sch.set_config({"daily_masters": 1, "shorts_per_master": 2, "max_active_jobs": 1, "plan": {"master": ["probe"], "short": ["ffmpeg_transcode"]}})
    eta = fc.sched.snapshot(DAY0)["eta"]
    assert eta["remaining_s"] is None and "no history yet" in eta["note"]
    for i, (task, d) in enumerate([("probe", 10), ("probe", 30), ("probe", 20), ("ffmpeg_transcode", 50), ("ffmpeg_transcode", 70)]):
        db.execute("INSERT INTO remote_jobs(id, task, status, started, finished, created) VALUES(?,?,?,?,?,?)",
                   (f"rjob_h{i}", task, "succeeded", 1000.0, 1000.0 + d, 1000.0))
    db.execute("INSERT INTO remote_jobs(id, task, status, started, finished, created) VALUES('rjob_x','probe','failed',0,9999,0)")
    eta = fc.sched.snapshot(DAY0)["eta"]
    assert eta["tasks"]["probe"]["median_s"] == 20 and eta["tasks"]["ffmpeg_transcode"]["median_s"] == 60
    assert eta["remaining_s"] == 20 + 2 * 60


def test_restart_persistence(fc):
    fc.sch.set_config({"daily_masters": 1, "shorts_per_master": 1, "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.sched.start()
    fc.sched.tick(DAY0)
    jid = fc.submitted[0].job_id
    # "restart": a brand-new Scheduler object reads everything from SQLite
    s2 = fc.sch.Scheduler(fc.sched.adapter, clock=lambda: DAY0)
    assert fc.sch.get_state()["status"] == "running"
    s2.tick(DAY0 + 1)
    assert len(fc.submitted) == 1  # the in-flight job is tracked, not resubmitted
    fc.finish(jid, started=DAY0, finished=DAY0 + 2)
    s2.tick(DAY0 + 3)
    assert len(fc.submitted) == 2 and fc.submitted[1].params["kind"] == "short"


def test_gpu_budget_and_stop_conditions(fc):
    fc.sch.set_config({"daily_masters": 5, "shorts_per_master": 0, "max_gpu_seconds_day": 15, "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.sched.start()
    fc.sched.tick(DAY0)
    fc.finish(fc.submitted[0].job_id, started=DAY0, finished=DAY0 + 20)
    fc.sched.tick(DAY0 + 21)
    assert len(fc.submitted) == 1 and "GPU" in fc.sch.get_state()["blocked"]
    fc.sch.set_config({"max_gpu_seconds_day": None, "stop": {"max_items": 2}})
    fc.sched.tick(DAY0 + 22)
    fc.finish(fc.submitted[1].job_id, started=DAY0 + 22, finished=DAY0 + 23)
    fc.sched.tick(DAY0 + 24)
    fc.sched.tick(DAY0 + 25)
    assert fc.sch.get_state()["status"] == "stopped" and len(fc.submitted) == 2
    fc.sch.set_config({"stop": {"max_items": None, "until": DAY0 + 100}})
    fc.sched.start()
    fc.sched.tick(DAY0 + 101)
    assert fc.sch.get_state()["status"] == "stopped"


def test_compute_unavailable_is_reported_not_crashing(fc):
    fc.fail_submit = fc.sch.ComputeUnavailable("compute service not installed in this build")
    fc.sched.start()
    fc.sched.tick(DAY0)
    st = fc.sch.get_state()
    assert st["status"] == "running" and "not installed" in st["blocked"]


def test_config_validation(home):
    from nexus.cloud import scheduler as sch
    with pytest.raises(ValueError):
        sch.set_config({"max_active_jobs": 0})
    with pytest.raises(ValueError):
        sch.set_config({"plan": {"master": ["Bad Task"], "short": ["probe"]}})
    with pytest.raises(ValueError):
        sch.set_config({"nope": 1})
    c = sch.set_config({"languages": ["en", "de"], "stop": {"max_items": 4}})
    assert c["stop"] == {"max_items": 4, "until": None} and sch.get_config()["languages"] == ["en", "de"]


def test_background_thread_ticks(fc):
    fc.sch.set_config({"tick_interval_s": 5, "plan": {"master": ["probe"], "short": ["probe"]}})
    fc.sched.start()
    fc.sched.start_thread()
    try:
        for _ in range(50):
            if fc.submitted:
                break
            time.sleep(0.05)
        assert fc.submitted and fc.sched.snapshot()["thread_alive"]
    finally:
        fc.sched.stop_thread()
