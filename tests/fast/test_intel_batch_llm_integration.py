"""Account 3 batch_llm jobs through Account 1's REAL dispatcher and worker script (merged tree only).

Skipped on the Account 3 branch alone (Account 1's nexus.cloud is not there). On the merged integration tree it uses
Account 1's own test transport (FakeKaggleCLI: the real worker script, embedded bundle, checksum verification and
checkpoints run) and the `echo` engine, which loads no model. It proves the contract and plumbing, NOT model quality;
results are labelled simulated end to end.
"""
import json

import pytest

dsp = pytest.importorskip("nexus.cloud.compute.dispatcher", reason="Account 1 dispatcher: merged integration tree only")

from nexus import vault  # noqa: E402
from nexus.cloud.compute.kaggle import KaggleProvider  # noqa: E402
from nexus.cloud.compute.local import LocalProvider  # noqa: E402
from nexus.contracts import ProductionJobManifest  # noqa: E402
from nexus.intelligence.model_router import batch_jobs as bj  # noqa: E402
from nexus.intelligence.model_router import bench_v2, bench_v3  # noqa: E402

test_cloud_compute = pytest.importorskip("test_cloud_compute")


@pytest.fixture()
def env(home, tmp_path):
    fake = test_cloud_compute.FakeKaggleCLI(tmp_path / "kaggle_remote")
    d = dsp.Dispatcher({"kaggle": KaggleProvider(runner=fake), "local": LocalProvider()}, poll_min=0, poll_max=0)
    dsp.set_dispatcher(d)
    vault.put("kaggle_primary", test_cloud_compute.FAKE_TOKEN)
    yield d, fake
    dsp.set_dispatcher(None)


def _items(n=12):
    return bench_v3.taskset()[:n]


def _results(row):
    a = next(x for x in dsp.job_artifacts(row["id"]) if x["name"].startswith("batch_llm_results."))
    p = dsp.artifact_path(a)
    assert dsp._sha(p) == a["sha256"]
    return json.loads(row["result"]), [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_generated_manifest_validates_against_account1_contract():
    m = bench_v2.account1_manifest("int-1", ["qwen3-4b"], _items())[0]
    pm = ProductionJobManifest.model_validate(m)
    assert pm.task == "batch_llm" and pm.params["model"] == "Qwen/Qwen3-4B" and isinstance(pm.params["items"], list)


def test_manifest_runs_on_the_real_dispatcher_and_returns_every_item(env):
    d, fake = env
    items = _items()
    m = bj.manifest_for_model("int-2", "qwen3-4b", items, engine="echo", part_size=5, batch_size=4, max_runtime_s=300)
    row, created = d.submit(ProductionJobManifest.model_validate(m))
    assert created
    r = d.run_until_done(row["id"], timeout=180, interval=0.05)
    assert r["status"] == "succeeded", r["error"]
    assert fake.last_meta["enable_gpu"] is True and fake.last_meta["is_private"] is True
    res, rows = _results(r)
    assert [x["id"] for x in rows] == [it["id"] for it in items]
    assert all(x["error"] is None and x["text"] for x in rows)
    assert res["postprocess"]["items"] == len(items) and res["postprocess"]["items_error"] == 0
    assert res["postprocess"]["parts"] == 3                                    # 12 items / part_size 5
    summ = next(x for x in dsp.job_artifacts(row["id"]) if x["name"] == "batch_llm_summary.json")
    s = json.loads(dsp.artifact_path(summ).read_text(encoding="utf-8"))
    assert s["simulated"] is True and s["engine"] == "echo" and s["persistent_endpoint"] is False
    assert s["model"] == "Qwen/Qwen3-4B" and s["items_total"] == len(items)
    raw = bj.results_to_bench_raw("qwen3-4b", rows, simulated=s["simulated"])
    assert raw["simulated"] is True and len(bench_v3.score_results(raw)["qwen3-4b"]["rows"]) == len(items)


def test_resubmitting_the_same_manifest_is_idempotent(env):
    d, _ = env
    m = ProductionJobManifest.model_validate(bj.manifest_for_model("int-3", "qwen3-4b", _items(3), engine="echo"))
    row, created = d.submit(m)
    d.run_until_done(row["id"], timeout=120, interval=0.05)
    again, created2 = d.submit(ProductionJobManifest.model_validate(bj.manifest_for_model("int-3", "qwen3-4b", _items(3), engine="echo")))
    assert created and not created2 and again["id"] == row["id"]


def test_the_old_shape_is_refused_before_submission_and_fails_on_the_worker(env):
    d, _ = env
    old = {"contract": "ProductionJobManifest/v1", "task": "batch_llm", "owner_account": "account3",
           "params": {"models": ["qwen3-4b"], "items": 36, "runtime": "ollama", "engine": "echo"},
           "preferred_providers": ["kaggle"], "max_retries": 0, "idempotency_key": "a3:old-shape"}
    with pytest.raises(bj.BatchManifestError, match="runtime"):
        bj.validate_manifest(old)
    # Account 1 still refuses it on the worker (the job fails; nothing is retried) - our check names the cause earlier.
    r = d.run_until_done(d.submit(ProductionJobManifest.model_validate(old))[0]["id"], timeout=120, interval=0.05)
    assert r["status"] == "failed"
