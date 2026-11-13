"""batch_llm worker task (Account 3 request A1-3). No network, no GPU, no model download.

The end-to-end tests go through the fake Kaggle CLI (the real worker script, embedded bundle, checksum verification and
checkpoint resume all run) with the deterministic `echo` engine. They prove the plumbing, NOT model quality: a real
model run is recorded separately in docs/ACCOUNT1_FINAL_RELEASE_AUDIT.md as live evidence.
"""
import json

import pytest

from nexus import vault
from nexus.cloud import worker_kit
from nexus.cloud.compute import dispatcher as dsp
from nexus.cloud.compute import tasks as task_registry
from nexus.cloud.compute.kaggle import KaggleProvider
from nexus.cloud.compute.local import LocalProvider
from nexus.contracts import ProductionJobManifest

from test_cloud_compute import FAKE_TOKEN, FakeKaggleCLI


@pytest.fixture()
def env(home, tmp_path):
    fake = FakeKaggleCLI(tmp_path / "kaggle_remote")
    d = dsp.Dispatcher({"kaggle": KaggleProvider(runner=fake), "local": LocalProvider()}, poll_min=0, poll_max=0)
    dsp.set_dispatcher(d)
    vault.put("kaggle_primary", FAKE_TOKEN)
    yield d, fake
    dsp.set_dispatcher(None)


def _items(n, words=6):
    return [{"id": f"q{i}", "task": "summarization", "system": "Be brief.", "prompt": " ".join(f"w{i}_{k}" for k in range(words)),
             "max_tokens": 4} for i in range(n)]


def _worker_ns():
    ns = {"__name__": "nexus_worker_under_test"}
    exec(compile(worker_kit.build_script(), "nexus_job.py", "exec"), ns)  # noqa: S102 - our own worker source
    return ns


def _results(row):
    res = json.loads(row["result"])
    a = next(x for x in dsp.job_artifacts(row["id"]) if x["name"].startswith("batch_llm_results."))
    p = dsp.artifact_path(a)
    assert dsp._sha(p) == a["sha256"]
    return res, [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_batch_llm_is_registered_kaggle_only_and_never_routed_to_the_owner_pc(home):
    assert "batch_llm" in worker_kit.registered_worker_tasks()
    s = task_registry.spec("batch_llm")
    assert s.gpu == "required" and s.providers == ("kaggle",) and s.resumable and s.internet
    d = dsp.Dispatcher({"local": LocalProvider()}, poll_min=0, poll_max=0)
    m = ProductionJobManifest(task="batch_llm", owner_account="account3", params={"engine": "echo", "items": _items(1)},
                              preferred_providers=["local", "user_gpu"])
    cands, why = d.candidates(m)
    assert cands == [] and any(w.startswith("kaggle") for w in why)


def test_batch_llm_end_to_end_through_fake_kaggle_with_parts_and_join(env):
    d, fake = env
    m = ProductionJobManifest(task="batch_llm", owner_account="account3", max_runtime_s=300,
                              params={"engine": "echo", "model": "example/tiny", "items": _items(5), "part_size": 2, "batch_size": 2},
                              idempotency_key="a3:batch:test-1")
    row, created = d.submit(m)
    r = d.run_until_done(row["id"], timeout=120, interval=0.05)
    assert r["status"] == "succeeded", r["error"]
    assert fake.last_meta["enable_gpu"] is True and fake.last_meta["is_private"] is True
    assert FAKE_TOKEN not in fake.last_script
    res, rows = _results(r)
    assert [x["id"] for x in rows] == [f"q{i}" for i in range(5)]
    assert all(x["error"] is None and x["text"].startswith("echo[summarization]: ") for x in rows)
    assert rows[0]["usage"] == {"prompt_tokens": 8, "completion_tokens": 4} and rows[0]["finish_reason"] == "length"
    assert res["postprocess"] == {**res["postprocess"], "join": "ok", "items": 5, "items_error": 0, "parts": 3}
    assert [c["seq"] for c in dsp.checkpoints(row["id"])] == [1, 2, 3]
    summ = next(x for x in dsp.job_artifacts(row["id"]) if x["name"] == "batch_llm_summary.json")
    s = json.loads(dsp.artifact_path(summ).read_text())
    assert s["simulated"] is True and s["engine"] == "echo" and s["persistent_endpoint"] is False
    assert d.submit(m)[1] is False                                     # idempotent: no second run


def test_batch_llm_resumes_from_checkpoint_after_worker_loss(env):
    d, _ = env
    m = ProductionJobManifest(task="batch_llm", max_retries=2,
                              params={"engine": "echo", "items": _items(6), "part_size": 2, "simulate_loss_after_parts": 1})
    row, _ = d.submit(m)
    r = d.run_until_done(row["id"], timeout=180, interval=0.05)
    assert r["status"] == "succeeded", r["error"]
    res, rows = _results(r)
    a1, a2 = res["attempts"][0], res["attempts"][1]
    assert a1["outcome"] == "lost" and a1["metrics"]["parts_run_this_attempt"] == [0]
    assert a2["outcome"] == "succeeded" and a2["metrics"]["parts_skipped"] == [0] and a2["metrics"]["parts_run_this_attempt"] == [1, 2]
    assert [x["id"] for x in rows] == [f"q{i}" for i in range(6)]      # part 0 from attempt 1, parts 1-2 from attempt 2
    assert [c["seq"] for c in dsp.checkpoints(row["id"])] == [1, 2, 3]


def test_batch_llm_limits_and_per_item_errors(env, tmp_path):
    d, _ = env
    too_many = ProductionJobManifest(task="batch_llm", max_retries=0, params={"engine": "echo", "items": _items(4), "max_items": 3})
    r = d.run_until_done(d.submit(too_many)[0]["id"], timeout=120, interval=0.05)
    assert r["status"] == "failed" and "max_items 3" in r["error"]
    items = _items(2) + [{"id": "long", "prompt": "x" * 50}]
    ok = ProductionJobManifest(task="batch_llm", params={"engine": "echo", "items": items, "max_prompt_chars": 40, "max_new_tokens": 2})
    r = d.run_until_done(d.submit(ok)[0]["id"], timeout=120, interval=0.05)
    assert r["status"] == "succeeded", r["error"]
    res, rows = _results(r)
    assert rows[2]["error"].startswith("prompt longer than max_prompt_chars") and rows[2]["text"] is None
    assert rows[0]["usage"]["completion_tokens"] == 2                     # item max_tokens 4 clipped to max_new_tokens 2
    assert res["postprocess"]["items_error"] == 1


def test_batch_llm_reads_a_jsonl_input_artifact(env, tmp_path):
    d, _ = env
    f = tmp_path / "prompts.jsonl"
    f.write_text("".join(json.dumps(x) + "\n" for x in _items(3)), encoding="utf-8")
    art = dsp.register_local_file(f, "source")
    m = ProductionJobManifest(task="batch_llm", inputs=[art.artifact_id], params={"engine": "echo", "output_format": "json"})
    r = d.run_until_done(d.submit(m)[0]["id"], timeout=120, interval=0.05)
    assert r["status"] == "succeeded", r["error"]
    a = next(x for x in dsp.job_artifacts(r["id"]) if x["name"] == "batch_llm_results.json")
    assert [x["id"] for x in json.loads(dsp.artifact_path(a).read_text())] == ["q0", "q1", "q2"]


def test_batch_llm_refuses_model_download_outside_kaggle_and_oversized_models(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS_BATCH_LLM_ALLOW_HERE", raising=False)
    monkeypatch.delenv("KAGGLE_KERNEL_RUN_TYPE", raising=False)
    ns = _worker_ns()
    if ns["_bl_on_kaggle"]():
        pytest.skip("running inside a Kaggle kernel")
    b = {"job_id": "rjob_t1", "task": "batch_llm", "params": {"engine": "transformers", "model": "Qwen/Qwen2.5-0.5B-Instruct", "items": _items(1)}}
    r = ns["run"](b, tmp_path / "o1")
    assert r["status"] == "failed" and "refusing to download model weights outside a Kaggle kernel" in r["error"]
    monkeypatch.setenv("NEXUS_BATCH_LLM_ALLOW_HERE", "1")
    ns["_bl_model_bytes"] = lambda model, rev: 20 * 2 ** 30                 # metadata says 20 GiB: refused before download
    r = ns["run"]({**b, "job_id": "rjob_t2"}, tmp_path / "o2")
    assert r["status"] == "failed" and "max_model_gb 8.0" in r["error"]
    ns["_bl_model_bytes"] = lambda model, rev: None                         # size unknown: refused too
    r = ns["run"]({**b, "job_id": "rjob_t3"}, tmp_path / "o3")
    assert r["status"] == "failed" and "cannot determine the weight size" in r["error"]
    r = ns["run"]({**b, "job_id": "rjob_t4", "params": {**b["params"], "model": "not a repo"}}, tmp_path / "o4")
    assert r["status"] == "failed" and "repo id" in r["error"]


def test_batch_llm_time_budget_reports_lost_for_a_bounded_resume(tmp_path):
    ns = _worker_ns()
    clock = iter(range(0, 10_000, 50))
    real = ns["time"]

    class FakeTime:
        def __getattr__(self, k):
            return getattr(real, k)

        def time(self):
            return float(next(clock))

    ns["time"] = FakeTime()
    b = {"job_id": "rjob_tb", "task": "batch_llm", "max_runtime_s": 100,
         "params": {"engine": "echo", "items": _items(6), "part_size": 2, "time_budget_s": 60}}
    r = ns["run"](b, tmp_path / "o")
    assert r["status"] == "lost" and "time budget" in r["error"]
    assert r["checkpoint"]["state"]["done"] and len(r["checkpoint"]["state"]["done"]) < 3


def test_batch_llm_waits_in_safe_mode_without_an_authorised_kaggle_worker(home):
    d = dsp.Dispatcher({"local": LocalProvider()}, poll_min=0, poll_max=0)
    dsp.set_dispatcher(d)
    try:
        r = dsp.submit(ProductionJobManifest(task="batch_llm", params={"engine": "echo", "items": _items(1)}))
        for _ in range(3):
            d.tick()
        s = dsp.status(r["job_id"])
        assert s["status"] == "queued" and s["attempts"] == 0 and "kaggle" in s["error"]
    finally:
        dsp.set_dispatcher(None)
