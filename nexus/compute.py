"""NEXUS Free Resource Orchestrator: provider-independent workers, task routing, checkpointed failover.

Policy (enforced in code, not just documented):
  * local resources first; remote free GPUs are optional accelerators, never a dependency
  * a provider is used only through its official, permitted interface
  * one account per provider; quotas are read and respected, never worked around
  * when nothing eligible is available the job is DEFERRED with a reason - it is never sent to a paid service
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import psutil

from . import config, costguard, db


class WorkerLost(RuntimeError):
    """The worker disappeared mid-job (session ended, quota exhausted, network gone)."""


# task type -> (needs_gpu, preferred providers in order)
ROUTES = {
    "encode": (False, ["local"]),
    "audio": (False, ["local"]),
    "transcribe": (False, ["local", "kaggle"]),
    "translate": (False, ["local"]),
    "llm": (False, ["local", "kaggle"]),
    "animation-2d": (False, ["local"]),
    "animation-3d": (False, ["local", "kaggle"]),
    "image-generation": (True, ["kaggle", "local"]),
    "video-generation": (True, ["kaggle", "local"]),
    "upscale": (True, ["kaggle", "local"]),
    "analysis": (False, ["local"]),
    "packaging": (False, ["local"]),
}


def detect_hardware() -> dict:
    gpu = None
    if shutil.which("nvidia-smi"):
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            name, mem, drv = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            gpu = {"name": name, "vram_gb": round(int(mem) / 1024, 1), "driver": drv, "cuda": True}
    cpu = ""
    try:
        cpu = next(l.split(":", 1)[1].strip() for l in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines() if l.startswith("model name"))
    except (OSError, StopIteration):
        pass
    vm, du = psutil.virtual_memory(), shutil.disk_usage(config.HOME if config.HOME.exists() else "/")
    hw = {"cpu": cpu, "cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count(), "ram_gb": round(vm.total / 2 ** 30, 1),
          "ram_free_gb": round(vm.available / 2 ** 30, 1), "gpu": gpu, "disk_free_gb": round(du.free / 2 ** 30, 1),
          "acceleration": "cuda" if gpu else "cpu (int8/quantised models, Cycles CPU)"}
    # model sizing follows the hardware
    hw["recommended"] = {"asr": "whisper-small int8" if hw["ram_gb"] >= 8 else "whisper-base int8",
                         "llm": "Qwen3-4B Q4" if hw["ram_gb"] >= 12 else "Qwen3-1.7B Q4",
                         "mt": "MADLAD-400 3B int8" if hw["ram_gb"] >= 12 else "LLM translation only",
                         "video_generation": "local" if gpu and gpu["vram_gb"] >= 12 else "not feasible locally - remote free GPU or stills/motion graphics",
                         "max_parallel_heavy_jobs": 1 if hw["ram_gb"] < 24 else 2}
    return hw


class Worker:
    provider = "base"
    automated = True

    def __init__(self, wid: str):
        self.id = wid
        self.lost = False

    def describe(self) -> dict:
        raise NotImplementedError

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def can_run(self, task: str) -> tuple[bool, str]:
        ok, why = self.available()
        if not ok:
            return False, why
        needs_gpu, _ = ROUTES[task]
        if needs_gpu and not self.describe().get("gpu"):
            return False, "no GPU on this worker"
        return True, "ok"

    def run_step(self, fn, *args):
        return fn(*args)

    def cancel(self, job_id: str) -> None:
        pass


class LocalWorker(Worker):
    provider = "local"

    def describe(self) -> dict:
        hw = detect_hardware()
        return {"id": self.id, "provider": "local", **hw, "frameworks": ["ffmpeg", "ctranslate2", "llama.cpp", "onnxruntime", "manim", "blender"],
                "max_job_hours": None, "quota": "unlimited (your own machine)", "network": "as configured", "policy": "none"}

    def available(self) -> tuple[bool, str]:
        if psutil.virtual_memory().available < 1.5 * 2 ** 30:
            return False, "less than 1.5 GB RAM free"
        return True, "ok"


class KaggleWorker(Worker):
    """Batch GPU through the OFFICIAL Kaggle API ('kaggle kernels push' = Save & Run All).

    Requires the user's own single Kaggle account + API token (free, no card). NOT TESTED in this build:
    no credentials were available. It refuses to run without credentials rather than pretending.
    """
    provider = "kaggle"

    def _creds(self) -> bool:
        return bool(os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")) or (Path.home() / ".kaggle" / "kaggle.json").exists()

    def describe(self) -> dict:
        c = costguard.manifest()["kaggle-notebooks"]
        return {"id": self.id, "provider": "kaggle", "gpu": {"name": "T4 x2 or P100 (assigned by Kaggle)", "vram_gb": 16}, "ram_gb": 30,
                "max_job_hours": 12, "quota": c["quotas"], "policy": c["usage_restrictions"], "tested": False,
                "frameworks": ["pytorch", "diffusers"], "network": "internet toggle requires phone-verified account"}

    def available(self) -> tuple[bool, str]:
        if self.lost:
            return False, "worker marked lost"
        if not shutil.which("kaggle"):
            return False, "kaggle CLI not installed (pip install kaggle)"
        if not self._creds():
            return False, "no Kaggle API token configured (~/.kaggle/kaggle.json)"
        return True, "ok"

    def submit_script(self, job_id: str, script: str, gpu: bool = True) -> str:
        costguard.authorize("kaggle-notebooks", "submit")
        ok, why = self.available()
        if not ok:
            raise WorkerLost(why)
        user = os.environ.get("KAGGLE_USERNAME") or json.loads((Path.home() / ".kaggle" / "kaggle.json").read_text(encoding="utf-8"))["username"]
        d = config.CACHE / "kaggle" / job_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.py").write_text(script)
        slug = f"{user}/nexus-{job_id}".replace("_", "-")
        (d / "kernel-metadata.json").write_text(json.dumps({"id": slug, "title": f"nexus-{job_id}", "code_file": "job.py", "language": "python",
                                                            "kernel_type": "script", "is_private": True, "enable_gpu": gpu, "enable_internet": True}))
        r = subprocess.run(["kaggle", "kernels", "push", "-p", str(d)], capture_output=True, text=True)
        if r.returncode != 0:
            raise WorkerLost(f"kaggle push failed: {r.stderr[-300:]}")  # includes quota exhausted -> caller falls back / defers
        return slug

    def status(self, slug: str) -> str:
        r = subprocess.run(["kaggle", "kernels", "status", slug], capture_output=True, text=True)
        return r.stdout.strip()

    def fetch(self, slug: str, dest: Path) -> Path:
        subprocess.run(["kaggle", "kernels", "output", slug, "-p", str(dest)], check=True, capture_output=True)
        return dest


class ColabHandoff(Worker):
    """Colab free tier forbids unattended/automated use, so this is NOT an automated worker.
    NEXUS only prepares a notebook; the user opens it and runs it interactively, then imports the result."""
    provider = "colab"
    automated = False

    def describe(self) -> dict:
        c = costguard.manifest()["colab-free"]
        return {"id": self.id, "provider": "colab", "gpu": {"name": "T4 (when granted)", "vram_gb": 16}, "quota": c["quotas"],
                "policy": c["usage_restrictions"], "mode": "manual hand-off only"}

    def available(self) -> tuple[bool, str]:
        return False, "manual hand-off only (automation would violate Colab's free-tier terms)"

    def prepare_notebook(self, job_id: str, code: str) -> Path:
        costguard.authorize("colab-free", "prepare-notebook")
        nb = {"cells": [{"cell_type": "markdown", "metadata": {}, "source": [f"# NEXUS job {job_id}\nRun the cells by hand, keep the tab open, then download the output."]},
                        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": code.splitlines(keepends=True)}],
              "metadata": {"accelerator": "GPU", "colab": {"gpuType": "T4"}}, "nbformat": 4, "nbformat_minor": 5}
        out = config.CACHE / "colab" / f"{job_id}.ipynb"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(nb, indent=1))
        return out


class SimulatedRemoteGPU(Worker):
    """Test double for a remote free GPU session that dies after `fail_after` steps (used by acceptance test I)."""
    provider = "kaggle"

    def __init__(self, wid: str, fail_after: int):
        super().__init__(wid)
        self.fail_after, self.done = fail_after, 0

    def describe(self) -> dict:
        return {"id": self.id, "provider": "kaggle (simulated)", "gpu": {"name": "simulated T4", "vram_gb": 16}, "quota": "simulated", "simulated": True}

    def available(self) -> tuple[bool, str]:
        return (not self.lost), ("ok" if not self.lost else "session ended")

    def run_step(self, fn, *args):
        if self.done >= self.fail_after:
            self.lost = True
            raise WorkerLost("remote GPU session terminated by provider")
        self.done += 1
        return fn(*args)


class Federation:
    def __init__(self, workers: list[Worker] | None = None):
        self.workers = workers if workers is not None else [LocalWorker("local-0"), KaggleWorker("kaggle-0"), ColabHandoff("colab-0")]

    def registry(self) -> list[dict]:
        out = []
        for w in self.workers:
            ok, why = w.available()
            out.append({**w.describe(), "available": ok, "status": why, "automated": w.automated})
        return out

    def route(self, task: str, exclude: set[str] | None = None) -> tuple[Worker | None, list[str]]:
        """Pick the best eligible worker for a task. Returns (worker or None, reasons for every rejected worker)."""
        needs_gpu, prefs = ROUTES[task]
        reasons = []
        for prov in prefs:
            for w in self.workers:
                if w.provider != prov or not w.automated or (exclude and w.id in exclude):
                    continue
                ok, why = w.can_run(task)
                if ok:
                    return w, reasons
                reasons.append(f"{w.id}: {why}")
        return None, reasons

    def run_job(self, kind: str, task: str, steps: list[tuple[str, callable]], project_id: str = "", job_id: str | None = None,
                cpu_fallback_ok: bool = True) -> dict:
        """Run `steps` [(name, fn(state) -> artefact)] with a checkpoint after every step.
        If the worker is lost, progress is kept and the job continues on the next eligible worker."""
        job_id = job_id or db.new_id("job")
        rows = db.query("SELECT checkpoint FROM jobs WHERE id=?", (job_id,))
        state = json.loads(rows[0]["checkpoint"]) if rows and rows[0]["checkpoint"] else {"done": {}, "events": []}
        if not rows:
            db.execute("INSERT INTO jobs(id,project_id,kind,status,progress,created,params) VALUES(?,?,?,?,?,?,?)",
                       (job_id, project_id, kind, "queued", 0.0, time.time(), json.dumps({"task": task, "steps": [n for n, _ in steps]})))
        tried: set[str] = set()
        t0 = time.time()
        while True:
            worker, reasons = self.route(task, tried)
            if worker is None:
                needs_gpu, _ = ROUTES[task]
                local = next((w for w in self.workers if w.provider == "local" and w.id not in tried), None)
                if needs_gpu and cpu_fallback_ok and local and local.available()[0]:
                    worker = local  # explicit, slower CPU path - recorded as a limitation
                    state["events"].append({"t": time.time(), "event": "cpu-fallback", "detail": "no GPU worker available; continuing on CPU (slower)"})
                else:
                    state["events"].append({"t": time.time(), "event": "deferred", "detail": reasons})
                    self._save(job_id, "deferred", state, steps, None, f"no eligible free worker: {reasons}")
                    return {"job": job_id, "status": "deferred", "reasons": reasons, "state": state}
            state["events"].append({"t": time.time(), "event": "assigned", "worker": worker.id})
            self._save(job_id, "running", state, steps, worker.id)
            try:
                for name, fn in steps:
                    if name in state["done"]:
                        continue  # resume: finished steps are never redone
                    state["done"][name] = worker.run_step(fn, state)
                    self._save(job_id, "running", state, steps, worker.id)
                self._save(job_id, "done", state, steps, worker.id)
                db.metric("job_seconds", time.time() - t0, kind=kind)
                return {"job": job_id, "status": "done", "worker": worker.id, "state": state}
            except WorkerLost as e:
                tried.add(worker.id)
                state["events"].append({"t": time.time(), "event": "worker-lost", "worker": worker.id, "detail": str(e),
                                        "progress_saved": list(state["done"])})
                self._save(job_id, "recovering", state, steps, worker.id, str(e))

    @staticmethod
    def _save(job_id: str, status: str, state: dict, steps: list, worker: str | None, error: str | None = None) -> None:
        prog = len(state["done"]) / max(len(steps), 1)
        db.execute("UPDATE jobs SET status=?, worker=?, progress=?, checkpoint=?, error=?, started=COALESCE(started,?), finished=? WHERE id=?",
                   (status, worker, prog, json.dumps(state, default=str), error, time.time(), time.time() if status in ("done", "deferred") else None, job_id))
