"""NEXUS remote worker (standalone; Python 3.10+ standard library, optional torch / psutil).

This one file runs a NEXUS ProductionJobManifest on any worker: a Kaggle kernel (pushed by the dispatcher with the job
bundle embedded below), the local CPU worker, the owner's own GPU PC or a Colab notebook (pull protocol). It must stay
importable without the `nexus` package: Kaggle receives only this file.

Job bundle (JSON):
    {"job_id", "task", "params", "attempt", "provider", "connection_id", "max_runtime_s", "worker_version",
     "inputs": [{"name", "sha256"?, "size"?, "b64"? | "url"?}],   # small inputs inline (base64), larger ones by URL
     "checkpoint": JobCheckpoint/v1 dict or null}                     # resume point from an earlier lost/failed run

Output directory layout (what the dispatcher downloads and verifies):
    result.json                 WorkerExecutionResult/v1 fields; artifacts carry size + SHA-256
    checkpoints/ckpt_0001.json  JobCheckpoint/v1, one per completed step (seq continues across attempts)
    artifacts/<name>            every produced file

Adding a task (Accounts 2/3): write `@task("my_task")\ndef my_task(ctx): ...` in a file under worker_kit/tasks/
(it is appended to this file when the dispatcher builds the kernel script) and register its routing needs in
nexus/cloud/compute/tasks.py (TaskSpec). A task uses only `ctx` (see Context) and returns a dict of extra metrics.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
from pathlib import Path

WORKER_VERSION = "1.0.0"
_EMBEDDED_BUNDLE_B64 = ""  # replaced by the dispatcher in the pushed Kaggle script; never contains secrets

TASKS: dict = {}


def task(name: str):
    def deco(fn):
        TASKS[name] = fn
        return fn
    return deco


class SimulatedWorkerLoss(RuntimeError):
    """Raised by a task when params ask it to stop mid-way (resume demonstrations and tests)."""


# ---------------------------------------------------------------- helpers
def sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _run(cmd, timeout=None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    return {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "json": "application/json", "txt": "text/plain",
            "png": "image/png", "jpg": "image/jpeg", "wav": "audio/wav", "mp3": "audio/mpeg", "log": "text/plain"}.get(ext, "application/octet-stream")


def gpu_info() -> list:
    if not shutil.which("nvidia-smi"):
        return []
    r = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"], timeout=20)
    out = []
    for line in r.stdout.strip().splitlines() if r.returncode == 0 else []:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3:
            out.append({"name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 2), "driver": parts[2]})
    return out


def _ram() -> dict:
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {"ram_total_gb": round(vm.total / 2 ** 30, 2), "ram_available_gb": round(vm.available / 2 ** 30, 2)}
    except Exception:  # noqa: BLE001
        info = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
            return {"ram_total_gb": round(info["MemTotal"] / 2 ** 30, 2), "ram_available_gb": round(info.get("MemAvailable", 0) / 2 ** 30, 2)}
        except Exception:  # noqa: BLE001
            return {}


def _cpu_times():
    try:
        vals = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        return sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0)
    except Exception:  # noqa: BLE001
        return None


class Sampler(threading.Thread):
    """Samples GPU utilisation / memory (nvidia-smi) and whole-machine CPU % while the task runs."""

    def __init__(self, every: float = 0.5):
        super().__init__(daemon=True)
        self.every, self.halt = every, threading.Event()
        self.gpu: list = []
        self.cpu: list = []
        self.has_smi = bool(shutil.which("nvidia-smi"))

    def run(self):
        last = _cpu_times()
        while not self.halt.wait(self.every):
            if self.has_smi:
                try:
                    r = _run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"], timeout=5)
                    row = [[float(x) for x in line.split(",")] for line in r.stdout.strip().splitlines() if line.strip()]
                    if row:
                        self.gpu.append({"t": round(time.time(), 2), "util": [x[0] for x in row], "mem_mb": [x[1] for x in row]})
                except Exception:  # noqa: BLE001
                    pass
            now = _cpu_times()
            if now and last and now[0] > last[0]:
                self.cpu.append(round(100.0 * (1 - (now[1] - last[1]) / (now[0] - last[0])), 1))
            last = now

    def summary(self) -> dict:
        utils = [u for s in self.gpu for u in s["util"]]
        mems = [m for s in self.gpu for m in s["mem_mb"]]
        return {"gpu_util_samples": self.gpu[-120:], "gpu_util_mean": round(sum(utils) / len(utils), 1) if utils else None,
                "gpu_util_max": max(utils) if utils else None, "gpu_mem_used_max_mb": max(mems) if mems else None,
                "cpu_percent_mean": round(sum(self.cpu) / len(self.cpu), 1) if self.cpu else None,
                "cpu_percent_max": max(self.cpu) if self.cpu else None, "samples": len(self.cpu)}


def nvenc_available() -> bool:
    if not (shutil.which("ffmpeg") and gpu_info()):
        return False
    r = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=256x256:rate=10", "-t", "1",
              "-c:v", "h264_nvenc", "-f", "null", "-"], timeout=60)
    return r.returncode == 0


def ffprobe(p) -> dict:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size,bit_rate:stream=codec_name,codec_type,width,height,nb_frames,r_frame_rate",
              "-of", "json", str(p)], timeout=60)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": r.stderr[-300:]}


# ---------------------------------------------------------------- context
class Context:
    def __init__(self, bundle: dict, out: Path, work: Path):
        self.bundle = bundle
        self.job_id = bundle["job_id"]
        self.params = bundle.get("params") or {}
        self.attempt = int(bundle.get("attempt") or 1)
        self.out, self.work = out, work
        self.artifacts: list = []
        self.metrics: dict = {}
        self.inputs: dict = {}
        cp = bundle.get("checkpoint") or {}
        self.resume_state: dict = dict(cp.get("state") or {})
        self.seq = int(cp.get("seq", 0)) if cp else 0
        self.last_checkpoint: dict | None = cp or None
        self._log: list = []
        (out / "artifacts").mkdir(parents=True, exist_ok=True)
        (out / "checkpoints").mkdir(parents=True, exist_ok=True)

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self._log.append(line)
        print(line, flush=True)

    def artifact(self, path, kind: str = "other", name: str | None = None, **provenance) -> dict:
        src = Path(path)
        name = name or src.name
        dst = self.out / "artifacts" / name
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        a = {"contract": "ArtifactManifest/v1", "artifact_id": "art_" + uuid.uuid4().hex[:12], "kind": kind, "name": name,
             "mime": _mime(name), "size": dst.stat().st_size, "sha256": sha256_file(dst), "produced_by_job": self.job_id,
             "provenance": {"worker_version": WORKER_VERSION, "task": self.bundle["task"], "attempt": self.attempt, **provenance},
             "created_at": time.time()}
        self.artifacts.append(a)
        return a

    def write_json_artifact(self, name: str, data: dict, kind: str = "analysis") -> dict:
        p = self.work / name
        p.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        return self.artifact(p, kind, name)

    def checkpoint(self, step: str, state: dict) -> dict:
        self.seq += 1
        cp = {"contract": "JobCheckpoint/v1", "job_id": self.job_id, "step": step, "seq": self.seq, "state": state,
              "artifacts": [a["artifact_id"] for a in self.artifacts], "created_at": time.time()}
        (self.out / "checkpoints" / f"ckpt_{self.seq:04d}.json").write_text(json.dumps(cp, indent=1), encoding="utf-8")
        self.last_checkpoint = cp
        self.log(f"checkpoint {self.seq}: {step}")
        return cp

    def materialize_inputs(self) -> None:
        d = self.work / "inputs"
        d.mkdir(parents=True, exist_ok=True)
        t0, total = time.time(), 0
        for i in self.bundle.get("inputs") or []:
            p = d / Path(i["name"]).name
            if i.get("b64"):
                p.write_bytes(base64.b64decode(i["b64"]))
            elif i.get("url"):
                download(i["url"], p, int(i.get("max_bytes") or 2 * 2 ** 30))
            else:
                raise ValueError(f"input {i['name']} has neither b64 nor url")
            if i.get("sha256") and sha256_file(p) != i["sha256"]:
                raise ValueError(f"input {i['name']} checksum mismatch")
            total += p.stat().st_size
            self.inputs[i["name"]] = p
        self.metrics["input_bytes"] = total
        self.metrics["input_fetch_s"] = round(time.time() - t0, 3)


def download(url: str, dest: Path, max_bytes: int = 2 * 2 ** 30) -> Path:
    if not url.startswith("https://"):
        raise ValueError("only https:// inputs are fetched")
    req = urllib.request.Request(url, headers={"User-Agent": f"nexus-worker/{WORKER_VERSION}"})
    n = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:  # noqa: S310 - https only, size-capped
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            n += len(b)
            if n > max_bytes:
                raise ValueError(f"input exceeds {max_bytes} bytes")
            f.write(b)
    return dest


def _torch():
    try:
        import torch
        return torch
    except Exception:  # noqa: BLE001
        return None


def hardware() -> dict:
    t = _torch()
    hw = {"python": platform.python_version(), "platform": platform.platform(), "cpus": os.cpu_count(), **_ram(), "gpus": gpu_info(),
          "torch": getattr(t, "__version__", None), "cuda": None, "ffmpeg": None}
    if t is not None and t.cuda.is_available():
        hw["cuda"] = t.version.cuda
        hw["torch_devices"] = [{"name": t.cuda.get_device_name(i), "vram_gb": round(t.cuda.get_device_properties(i).total_memory / 2 ** 30, 2)}
                               for i in range(t.cuda.device_count())]
    if shutil.which("ffmpeg"):
        hw["ffmpeg"] = (_run(["ffmpeg", "-version"], timeout=20).stdout.splitlines() or [""])[0][:120]
    return hw


def accelerator_label(hw: dict) -> str | None:
    g = hw.get("gpus") or []
    if not g:
        return None
    return f"{g[0]['name']} x{len(g)}" if len(g) > 1 else g[0]["name"]


# ---------------------------------------------------------------- built-in tasks
def _matmul(t, n: int, dtype_name: str, device: str, iters: int = 10) -> dict:
    dtype = getattr(t, dtype_name)
    a = t.randn(n, n, device=device, dtype=dtype)
    b = t.randn(n, n, device=device, dtype=dtype)
    (a @ b)
    if device == "cuda":
        t.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        (a @ b)
    if device == "cuda":
        t.cuda.synchronize()
    el = time.perf_counter() - t0
    return {"n": n, "dtype": dtype_name, "device": device, "iters": iters, "seconds": round(el, 4), "tflops": round(2 * n ** 3 * iters / el / 1e12, 3)}


@task("probe")
def task_probe(ctx: Context) -> dict:
    hw = hardware()
    t = _torch()
    bench = []
    if t is not None:
        if t.cuda.is_available():
            bench.append(_matmul(t, 4096, "float16", "cuda", 20))
            bench.append(_matmul(t, 4096, "float32", "cuda", 5))
        bench.append(_matmul(t, 1024, "float32", "cpu", 3))
    hw["nvenc"] = nvenc_available()
    try:
        urllib.request.urlopen("https://www.google.com", timeout=5).close()  # noqa: S310
        hw["internet"] = True
    except Exception:  # noqa: BLE001
        hw["internet"] = False
    ctx.write_json_artifact("probe.json", {"hardware": hw, "matmul": bench, "worker_version": WORKER_VERSION})
    ctx.metrics["hardware"] = hw
    return {"matmul": bench}


@task("torch_bench")
def task_torch_bench(ctx: Context) -> dict:
    t = _torch()
    if t is None:
        raise RuntimeError("torch is not installed on this worker")
    dev = "cuda" if t.cuda.is_available() else "cpu"
    sizes = ctx.params.get("sizes") or ([1024, 2048, 4096] if dev == "cuda" else [512, 1024])
    rows = []
    for n in sizes:
        for dt in (["float32", "float16"] if dev == "cuda" else ["float32"]):
            rows.append(_matmul(t, int(n), dt, dev, int(ctx.params.get("iters", 10))))
    ctx.write_json_artifact("torch_bench.json", {"device": dev, "rows": rows})
    return {"matmul": rows}


def _encode_args(encoder: str) -> list:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]


def _pick_encoder(ctx: Context) -> str:
    want = ctx.params.get("encoder", "auto")
    if want == "x264":
        return "libx264"
    ok = nvenc_available()
    ctx.metrics["nvenc_available"] = ok
    if want == "nvenc" and not ok:
        raise RuntimeError("NVENC requested but not available on this worker")
    return "h264_nvenc" if ok else "libx264"


def _input_video(ctx: Context) -> Path:
    if ctx.inputs:
        return next(iter(ctx.inputs.values()))
    url = ctx.params.get("input_url")
    if not url:
        raise ValueError("ffmpeg tasks need an input artifact or params.input_url")
    p = ctx.work / "inputs" / (Path(url.split("?")[0]).name or "input.mp4")
    p.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    download(url, p)
    ctx.metrics.update(input_bytes=p.stat().st_size, input_fetch_s=round(time.time() - t0, 3), input_url=url)
    return p


def _ffmpeg(cmd: list, ctx: Context, timeout: float) -> float:
    t0 = time.time()
    r = _run(cmd, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-800:]}")
    return time.time() - t0


@task("ffmpeg_transcode")
def task_ffmpeg_transcode(ctx: Context) -> dict:
    src = _input_video(ctx)
    enc = _pick_encoder(ctx)
    out = ctx.work / ctx.params.get("output_name", "output.mp4")
    vf = ["-vf", f"scale={ctx.params['scale']}"] if ctx.params.get("scale") else []
    lim = ["-t", str(ctx.params["max_seconds"])] if ctx.params.get("max_seconds") else []
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(src), *lim, *vf, *_encode_args(enc), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    el = _ffmpeg(cmd, ctx, ctx.bundle.get("max_runtime_s") or 3600)
    info = ffprobe(out)
    dur = float((info.get("format") or {}).get("duration") or 0)
    ctx.artifact(out, "proxy", encoder=enc, source=ctx.params.get("input_url") or src.name)
    return {"encoder": enc, "gpu_encode": enc == "h264_nvenc", "encode_s": round(el, 3), "output_duration_s": dur,
            "realtime_factor": round(dur / el, 2) if el else None, "output_probe": info, "input_probe": ffprobe(src)}


@task("chunked_transcode")
def task_chunked_transcode(ctx: Context) -> dict:
    """Transcode N time-chunks of the input; checkpoint after each chunk. A resumed run skips finished chunks.

    params: chunks (default 4), encoder, scale, simulate_loss_after (int: on attempt 1 only, stop after that many chunks)."""
    src = _input_video(ctx)
    enc = _pick_encoder(ctx)
    n = int(ctx.params.get("chunks", 4))
    dur = float((ffprobe(src).get("format") or {}).get("duration") or 0)
    if dur <= 0:
        raise ValueError("input has no duration")
    state = {"chunks_total": n, "done": [], "chunk_files": {}, **ctx.resume_state}
    done = set(int(i) for i in state["done"])
    skipped = sorted(done)
    ran = []
    stop_after = ctx.params.get("simulate_loss_after")
    vf = ["-vf", f"scale={ctx.params['scale']}"] if ctx.params.get("scale") else []
    for i in range(n):
        if i in done:
            ctx.log(f"chunk {i}: already done in an earlier attempt (checkpoint) - skipped")
            continue
        start, length = dur * i / n, dur / n
        out = ctx.work / f"chunk_{i:03d}.mp4"
        el = _ffmpeg(["ffmpeg", "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}", *vf,
                      *_encode_args(enc), "-pix_fmt", "yuv420p", "-an", str(out)], ctx, ctx.bundle.get("max_runtime_s") or 3600)
        a = ctx.artifact(out, "proxy", chunk=i, encoder=enc)
        ran.append({"chunk": i, "encode_s": round(el, 3), "sha256": a["sha256"]})
        state["done"] = sorted(done | {i} | {r["chunk"] for r in ran})
        state["chunk_files"][str(i)] = {"name": a["name"], "sha256": a["sha256"], "attempt": ctx.attempt}
        ctx.checkpoint(f"chunk_{i}", json.loads(json.dumps(state)))
        if stop_after is not None and ctx.attempt == 1 and len(ran) >= int(stop_after) and len(state["done"]) < n:
            ctx.metrics.update(chunks_run_this_attempt=[r["chunk"] for r in ran], chunks_skipped=skipped, encoder=enc)
            raise SimulatedWorkerLoss(f"simulated worker loss after {len(ran)} chunk(s) (params.simulate_loss_after)")
    return {"encoder": enc, "chunks_total": n, "chunks_run_this_attempt": [r["chunk"] for r in ran], "chunks_skipped": skipped,
            "chunk_runs": ran, "input_duration_s": dur}


# ---------------------------------------------------------------- runner
def run(bundle: dict, out_dir) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="nexus_work_"))
    ctx = Context(bundle, out, work)
    sampler = Sampler()
    t = _torch()
    hw = {"gpus": gpu_info()}
    started = time.time()
    status, error = "succeeded", None
    sampler.start()
    try:
        fn = TASKS.get(bundle["task"])
        if fn is None:
            raise LookupError(f"task '{bundle['task']}' is not registered in worker {WORKER_VERSION} (have: {sorted(TASKS)})")
        ctx.log(f"NEXUS worker {WORKER_VERSION}: job {ctx.job_id} task {bundle['task']} attempt {ctx.attempt}")
        ctx.materialize_inputs()
        if t is not None and t.cuda.is_available():
            t.cuda.reset_peak_memory_stats()
        extra = fn(ctx) or {}
        ctx.metrics.update(extra)
    except SimulatedWorkerLoss as e:
        status, error = "lost", str(e)
        ctx.log(error)
    except Exception as e:  # noqa: BLE001 - every failure is reported in result.json
        status, error = "failed", f"{type(e).__name__}: {e}"
        ctx.log(traceback.format_exc()[-2000:])
    finally:
        sampler.halt.set()
        sampler.join(timeout=3)
    finished = time.time()
    m = ctx.metrics
    m.update(sampler.summary())
    m.update(_ram())
    try:
        import resource
        m["peak_rss_mb"] = round(max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) / 1024, 1)
    except Exception:  # noqa: BLE001
        pass
    if t is not None and t.cuda.is_available():
        m["peak_vram_allocated_mb"] = round(max(t.cuda.max_memory_allocated(i) for i in range(t.cuda.device_count())) / 2 ** 20, 1)
    m.setdefault("model_download_bytes", 0)
    m["gpus"] = hw["gpus"]
    m["output_bytes"] = sum(a["size"] for a in ctx.artifacts)
    m["worker_version"] = WORKER_VERSION
    m["task_s"] = round(finished - started, 3)
    res = {"contract": "WorkerExecutionResult/v1", "job_id": ctx.job_id, "provider": bundle.get("provider", "local"),
           "connection_id": bundle.get("connection_id", "local"), "status": status, "started_at": started, "finished_at": finished,
           "duration_s": round(finished - started, 3), "accelerator": accelerator_label(hw), "metrics": m, "artifacts": ctx.artifacts,
           "log_tail": "\n".join(ctx._log)[-4000:], "error": error, "checkpoint": ctx.last_checkpoint}
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return res


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="NEXUS remote worker")
    ap.add_argument("--bundle", help="job bundle JSON file (default: the bundle embedded in this script)")
    ap.add_argument("--out", help="output directory (default /kaggle/working on Kaggle, else ./nexus_out)")
    a = ap.parse_args(argv)
    if a.bundle:
        bundle = json.loads(Path(a.bundle).read_text(encoding="utf-8"))
    elif _EMBEDDED_BUNDLE_B64:
        bundle = json.loads(base64.b64decode(_EMBEDDED_BUNDLE_B64))
    else:
        ap.error("no --bundle and no embedded bundle")
    out = a.out or ("/kaggle/working" if Path("/kaggle/working").is_dir() else "nexus_out")
    res = run(bundle, out)
    print(json.dumps({"status": res["status"], "duration_s": res["duration_s"], "artifacts": len(res["artifacts"])}))
    return 0  # result.json carries the status; exit 0 so the outputs are always kept and retrievable


# NEXUS_EXTRA_TASKS
if __name__ == "__main__":
    sys.exit(main())
