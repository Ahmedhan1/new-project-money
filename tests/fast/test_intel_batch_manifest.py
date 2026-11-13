"""Account 3 -> Account 1 batch_llm manifests: shape rules (no network, no Account 1 code needed).
The dispatcher round trip is in test_intel_batch_llm_integration.py and runs on the merged tree."""
import pytest

from nexus.intelligence.model_router import batch_jobs as bj
from nexus.intelligence.model_router import bench_v2, bench_v3


def test_one_job_per_model_with_the_actual_prompts():
    items = bench_v3.taskset()
    jobs = bench_v2.account1_manifest("run1", ["qwen3-4b", "qwen3.5-9b"], items)
    assert len(jobs) == 2
    for job, key in zip(jobs, ["qwen3-4b", "qwen3.5-9b"]):
        p = job["params"]
        assert job["contract"] == "ProductionJobManifest/v1" and job["task"] == "batch_llm" and job["owner_account"] == "account3"
        assert p["model"] == bench_v2.MODELS[key]["repo"] and "models" not in p and "runtime" not in p and "runtime" not in job
        assert isinstance(p["items"], list) and len(p["items"]) == len(items) == p["max_items"]
        assert [r["prompt"] for r in p["items"]] == [it["prompt"] for it in items]       # prompts verbatim, same order
        assert {r["id"] for r in p["items"]} == {it["id"] for it in items}
        assert p["engine"] == "transformers" and job["preferred_providers"] == ["kaggle"]
        assert p["max_prompt_chars"] >= max(len(it["prompt"]) for it in items)
        assert job["idempotency_key"].startswith(f"a3:batch:run1:{key}:")
    assert jobs[0]["idempotency_key"] != jobs[1]["idempotency_key"]


def test_idempotency_key_is_stable_and_changes_with_the_prompts():
    items = bench_v3.taskset()[:5]
    a = bj.manifest_for_model("r", "qwen3-4b", items)
    assert a["idempotency_key"] == bj.manifest_for_model("r", "qwen3-4b", items)["idempotency_key"]
    changed = [dict(items[0], prompt=items[0]["prompt"] + " ")] + items[1:]
    assert a["idempotency_key"] != bj.manifest_for_model("r", "qwen3-4b", changed)["idempotency_key"]


def test_thinking_flag_and_licence_travel_with_the_job():
    p = bj.manifest_for_model("r", "qwen3-4b-think", bench_v3.taskset()[:2])["params"]
    assert p["chat_template_kwargs"] == {"enable_thinking": True} and p["model_licence"] == "Apache-2.0"


def test_models_too_large_for_full_precision_on_two_t4s_are_reported_not_dropped():
    plan = bench_v2.account1_manifest_plan("r", ["qwen3-4b", "glm-4.7-flash", "ministral-3-14b"], bench_v3.taskset()[:3])
    assert [j["params"]["model"] for j in plan["jobs"]] == ["Qwen/Qwen3-4B"]
    assert set(plan["not_eligible"]) == {"glm-4.7-flash", "ministral-3-14b"}
    assert "fp16" in plan["not_eligible"]["glm-4.7-flash"]
    with pytest.raises(bj.BatchManifestError, match="fp16"):
        bench_v2.account1_manifest("r", ["glm-4.7-flash"], bench_v3.taskset()[:3])


OLD_SHAPE = {"contract": "ProductionJobManifest/v1", "job_id": "x", "task": "batch_llm", "owner_account": "account3",
             "params": {"models": ["qwen3-4b"], "items": 36, "runtime": "ollama"}, "preferred_providers": ["kaggle"],
             "idempotency_key": "x"}


@pytest.mark.parametrize("mutate, message", [
    (lambda m: m, "runtime"),
    (lambda m: {**m, "params": {"models": ["a/b"], "items": []}}, "params.models"),
    (lambda m: {**m, "params": {"model": "a/b", "items": 36}}, "LIST"),
    (lambda m: {**m, "params": {"model": "a/b", "engine": "ollama", "items": [{"id": 1, "prompt": "p"}]}}, "engine"),
    (lambda m: {**m, "params": {"model": "qwen3:4b", "items": [{"id": 1, "prompt": "p"}]}}, "repo id"),
    (lambda m: {**m, "params": {"model": "a/b"}}, "params.items"),
    (lambda m: {**m, "params": {"model": "a/b", "items": [{"id": 1, "prompt": "p"}, {"id": 1, "prompt": "q"}]}}, "duplicate"),
    (lambda m: {**m, "params": {"model": "a/b", "items": [{"id": 1}]}}, "'prompt'"),
    (lambda m: {**m, "params": {"model": "a/b", "items": [{"id": i, "prompt": "p"} for i in range(4)], "max_items": 3}}, "max_items 3"),
    (lambda m: {**m, "params": {"model": "a/b", "items": [{"id": 1, "prompt": "x" * 20}], "max_prompt_chars": 10}}, "max_prompt_chars"),
    (lambda m: {**m, "params": {"model": "a/b", "items": [{"id": 1, "prompt": "p"}], "max_new_tokens": 9999}}, "hard cap"),
    (lambda m: {**m, "params": {"model": "a/b", "items": [{"id": 1, "prompt": "p"}], "num_ctx": 8192}}, "unsupported"),
    (lambda m: {**m, "idempotency_key": None, "params": {"model": "a/b", "items": [{"id": 1, "prompt": "p"}]}}, "idempotency_key"),
    (lambda m: {**m, "preferred_providers": ["local"], "params": {"model": "a/b", "items": [{"id": 1, "prompt": "p"}]}}, "Kaggle"),
    (lambda m: {**m, "task": "llm", "params": {"model": "a/b", "items": [{"id": 1, "prompt": "p"}]}}, "task"),
])
def test_invalid_manifests_fail_with_a_message_naming_the_cause(mutate, message):
    m = {k: v for k, v in mutate(dict(OLD_SHAPE)).items()}
    if message != "runtime":
        m.pop("runtime", None)
    with pytest.raises(bj.BatchManifestError, match=message):
        bj.validate_manifest(m)


def test_the_kernel_cli_writes_the_batch_plan_not_the_old_shape(tmp_path):
    bench_v2.main(["kernel", "--models", "qwen3-4b", "--out", str(tmp_path), "--slug", "s"])
    import json
    plan = json.loads((tmp_path / "account1_manifest.json").read_text(encoding="utf-8"))
    assert plan["schema"] == "nexus.Account1BatchPlan/1" and len(plan["jobs"]) == 1
    bj.validate_manifest(plan["jobs"][0])


def test_results_convert_to_the_bench_format_and_keep_the_simulated_label():
    items = bench_v3.taskset()[:3]
    rows = [{"id": it["id"], "text": "x", "usage": {"prompt_tokens": 3, "completion_tokens": 2}, "latency_s": 0.5, "error": None}
            for it in items]
    raw = bj.results_to_bench_raw("qwen3-4b", rows, simulated=True)
    assert raw["simulated"] is True and raw["source"] == "account1.batch_llm"
    scored = bench_v3.score_results(raw)
    assert set(scored["qwen3-4b"]["capabilities"]) <= set(bench_v3.CAPS + ["dialect"])
