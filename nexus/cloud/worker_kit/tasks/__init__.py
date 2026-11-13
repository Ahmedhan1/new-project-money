"""Extra worker tasks (Accounts 2/3). Not imported as a package: every `*.py` here whose name does not start with "_"
is appended to nexus_worker.py when a worker script is built, so a task file may use the worker's globals directly
(`task`, `Context`, `ffprobe`, `download`, `sha256_file`, `_torch`, `SimulatedWorkerLoss`).

How to add a task:
1. Create `nexus/cloud/worker_kit/tasks/<name>.py` beginning with `# ruff: noqa: F821` and containing
       @task("render_timeline")
       def render_timeline(ctx):
           src = next(iter(ctx.inputs.values()))      # inputs arrive materialised and checksum-verified
           ...                                          # ctx.params, ctx.resume_state, ctx.checkpoint(step, state)
           ctx.artifact(out_path, "master")            # every output file goes through ctx.artifact (SHA-256 recorded)
           return {"my_metric": 1.0}                   # merged into result.metrics
2. Register its routing needs in nexus/cloud/compute/tasks.py:  TASKS["render_timeline"] = TaskSpec(gpu="required", ...)
3. Add a fast test that runs it through LocalProvider (see tests/fast/test_cloud_compute.py) when it can run on CPU.
Only standard library + what the worker image has (Kaggle: torch, ffmpeg, numpy...) may be imported; import inside the
function and record downloaded model sizes in ctx.metrics["model_download_bytes"].
"""
