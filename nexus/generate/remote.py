"""Remote-execution adapter for generation jobs (docs/ACCOUNT2_CONTRACTS.md, execution model).

Account 2 never talks to a GPU provider itself. It describes the job; Account 1's scheduler picks a worker:

    m   = job_manifest("text_to_video", "wan21-t2v-1.3b", prompt="...", settings={...})   # RenderJobManifest
          m["requirements"] = {"gpu": True, "vram_gb": 8.19, "ram_gb": .., "disk_gb": .., "models": [...], "est_s": ..}
    ok, why = can_run_here(m)                       # this machine? (it is never true for video here: no GPU)
    req = local_request(m)                          # RenderExecutionRequest for the local path (same code path)
    res = execute(req)                              # RenderExecutionResult: done / deferred (requirements unmet) / failed
    out = consume(res, m)                           # validates, builds GeneratedMediaManifests through classify.py,
                                                    # returns {"status", "generated", "retry_manifest"}

A worker runs `python -m nexus.generate.remote request.json [result.json]`. Interrupted work returns status failed +
error.retryable + checkpoint; consume() puts that checkpoint into the retry manifest so finished items are skipped.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .. import contracts
from . import catalog, classify, hardware

TASK_KIND = {"image": "generate.image", "text_to_video": "generate.t2v", "image_to_video": "generate.i2v",
             "video_to_video": "generate.v2v", "upscale": "enhance.upscale", "interpolate": "enhance.interpolate"}


def _ref(x) -> dict:
    if isinstance(x, dict):
        return x
    return contracts.artifact(x, role="input", hash_it=Path(x).exists())


def job_manifest(task: str, model_id: str, *, prompt: str | None = None, inputs: list | None = None,
                 settings: dict | None = None, outputs: list[str] | None = None, job_id: str | None = None,
                 checkpoint: dict | None = None, accept: tuple[str, ...] = ("foss",)) -> dict:
    """RenderJobManifest for one generation job. Refuses models whose weights licence class is not accepted."""
    m = catalog.model(model_id)
    if task not in (m.get("tasks") or []):
        raise ValueError(f"{model_id} does not do {task} (tasks: {m.get('tasks')})")
    lic = catalog.allowed(m, accept)
    jid = job_id or contracts.new_id("gen")
    st = {"task": task, "model": model_id, "prompt": prompt, "licence": lic, **(settings or {})}
    outs = [{"uri": o, "role": "output"} for o in (outputs or [f"{jid}.{'png' if task == 'image' else 'mp4'}"])]
    doc = {"job_id": jid, "kind": TASK_KIND.get(task, f"generate.{task}"), "inputs": [_ref(i) for i in inputs or []],
           "settings": st, "outputs": outs, "requirements": catalog.requirements(m, task)}
    if checkpoint:
        doc["checkpoint"] = checkpoint
    return contracts.make("RenderJobManifest", **doc)


def can_run_here(manifest: dict, hw: dict | None = None) -> tuple[bool, list[str]]:
    return hardware.satisfies(manifest["requirements"], hw or hardware.detect())


def local_request(manifest: dict, hw: dict | None = None, attempt: int = 1) -> dict:
    hw = hw or hardware.detect()
    env = {"provider": "local", "gpu": (hw["gpus"][0]["name"] if hw["gpus"] else None), "vram_gb": hw.get("best_vram_gb"),
           "cpu": hw.get("cpu_count"), "ram_gb": hw.get("ram_gb"), "workdir": "", "storage": "local"}
    return contracts.make("RenderExecutionRequest", request_id=contracts.new_id("req"), manifest=manifest, environment=env,
                          attempt=attempt)


def result(request_id: str, status: str, outputs: list | None = None, metrics: dict | None = None,
           error: dict | None = None, checkpoint: dict | None = None) -> dict:
    doc = {"request_id": request_id, "status": status, "outputs": outputs or [], "metrics": metrics or {}}
    if error:
        doc["error"] = error
    if checkpoint:
        doc["checkpoint"] = checkpoint
    return contracts.make("RenderExecutionResult", **doc)


# Executors that really run on this machine. Video generation has none here (no GPU): execute() defers instead.
EXECUTORS: dict[str, object] = {}


def register(kind: str):
    def deco(fn):
        EXECUTORS[kind] = fn
        return fn
    return deco


@register("generate.image")
def _exec_image(m: dict) -> tuple[list, dict]:
    """Generated still on this machine through the optional diffusers environment (LocalDiffusers)."""
    from . import image
    s = m["settings"]
    out = m["outputs"][0]["uri"]
    out = out[7:] if out.startswith("file://") else out
    r = image(s.get("prompt") or "", out, size=tuple(s.get("size") or (512, 512)), seed=int(s.get("seed", 1)),
              steps=s.get("steps"), model_id=s["model"], allow_noncommercial=s.get("licence", {}).get("class") != "foss")
    return [{**contracts.artifact(r["path"], role="output", media_type="image/png"), "provenance": r["provenance"]}], \
        {"seconds": r["provenance"].get("seconds_taken"), "provenance": {"provider": r["provenance"].get("provider")}}


def execute(request: dict, hw: dict | None = None) -> dict:
    contracts.check(request, "RenderExecutionRequest")
    m = request["manifest"]
    ok, why = can_run_here(m, hw)
    if not ok:
        return result(request["request_id"], "deferred", error={"code": "RESOURCE_EXHAUSTED", "message": "; ".join(why),
                                                                  "retryable": True}, checkpoint=m.get("checkpoint"))
    fn = EXECUTORS.get(m["kind"])
    if fn is None:
        return result(request["request_id"], "deferred", error={"code": "MODEL_UNAVAILABLE", "retryable": True,
                                                                  "message": f"no local executor for {m['kind']}"})
    t0 = time.time()
    try:
        outs, metrics = fn(m)  # type: ignore[operator]
    except Exception as e:  # noqa: BLE001 - every failure becomes a structured result
        return result(request["request_id"], "failed", error={"code": "INTERNAL", "message": str(e)[:500], "retryable": False},
                      checkpoint=m.get("checkpoint"))
    return result(request["request_id"], "done", outputs=outs, metrics={**metrics, "elapsed_s": round(time.time() - t0, 2)})


def consume(res: dict, manifest: dict, local_root: str | Path | None = None) -> dict:
    """Turn a worker's RenderExecutionResult into GeneratedMediaManifests (classification by classify.py)."""
    contracts.check(res, "RenderExecutionResult")
    st = res["status"]
    out = {"status": st, "generated": [], "retry_manifest": None, "error": res.get("error")}
    if st in ("failed", "deferred") and (res.get("error") or {}).get("retryable", st == "deferred"):
        retry = dict(manifest)
        retry.pop("schema", None)
        if res.get("checkpoint"):
            retry["checkpoint"] = res["checkpoint"]
        out["retry_manifest"] = contracts.make("RenderJobManifest", **retry)
        return out
    if st != "done":
        return out
    s = manifest["settings"]
    for o in res["outputs"]:
        uri = o.get("uri", "")
        path = Path(uri[7:]) if uri.startswith("file://") else (Path(local_root) / Path(uri).name if local_root else None)
        prov = {"model": s["model"], "task": s["task"], **(o.get("provenance") or {}), **(res["metrics"].get("provenance") or {})}
        if path is None or not path.exists():
            # a remote artefact we cannot inspect yet: record it, but never as generated video without seeing frames
            prov["unverified_remote"] = True
        try:
            gm = classify.manifest(s["task"], prov, path if path is not None and path.exists() else Path(uri),
                                   inputs=manifest["inputs"], prompt=s.get("prompt"), seconds=res["metrics"].get("elapsed_s"),
                                   compute={k: v for k, v in res["metrics"].items() if k != "provenance"})
        except classify.ClassificationError as e:
            out["generated"].append({"output": o, "refused": str(e)})
            continue
        out["generated"].append(gm)
    return out


def main(argv: list[str]) -> int:
    req = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    res = execute(req)
    dest = Path(argv[1]) if len(argv) > 1 else Path(argv[0]).with_suffix(".result.json")
    dest.write_text(contracts.dumps(res), encoding="utf-8")
    print(res["status"], dest)
    return 0 if res["status"] in ("done", "deferred") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
