"""Local worker: runs the same worker script in a subprocess on this machine (CPU, or a local NVIDIA GPU if present).

It is the failover target for CPU-capable tasks when Kaggle is paused or failing. The child gets a scrubbed
environment: no vault key, no API token, no Kaggle token.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ...contracts import ProviderHealth, RemoteWorkerCapability
from .. import worker_kit
from .base import Provider, RemoteStatus, compute_dir

SCRUB = ("KAGGLE_API_TOKEN", "KAGGLE_KEY", "KAGGLE_USERNAME", "NEXUS_API_TOKEN", "NEXUS_VAULT_KEY", "NEXUS_BACKUP_PASSPHRASE")


def scrubbed_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in SCRUB and not k.endswith(("_TOKEN", "_SECRET", "_PASSWORD"))}


class LocalProvider(Provider):
    provider = "local"
    mode = "push"

    def __init__(self, connection_id: str = "local"):
        super().__init__(connection_id)
        self._procs: dict[str, subprocess.Popen] = {}

    def declared_capability(self) -> RemoteWorkerCapability:
        import psutil
        gpu = None
        if shutil.which("nvidia-smi"):
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                n, m = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
                gpu = (n, round(float(m) / 1024, 1))
        fw = ["python"] + (["ffmpeg"] if shutil.which("ffmpeg") else [])
        try:
            import torch  # noqa: F401
            fw.append("torch")
        except Exception:  # noqa: BLE001
            pass
        return RemoteWorkerCapability(provider="local", connection_id=self.connection_id, accelerator=gpu[0] if gpu else None,
                                      vram_gb=gpu[1] if gpu else None, ram_gb=round(psutil.virtual_memory().total / 2 ** 30, 1),
                                      cpus=os.cpu_count(), frameworks=fw, internet=True, measured=False)

    def has_gpu(self) -> bool:
        return self.capability().accelerator is not None

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider="local", connection_id=self.connection_id, status="ok", detail="local subprocess worker",
                              last_success=time.time())

    def _dir(self, ref: str) -> Path:
        return compute_dir("local", ref)

    def submit(self, job_id: str, attempt: int, bundle: dict, gpu: bool, timeout_s: float) -> str:
        ref = f"{job_id}-a{attempt}"
        d = self._dir(ref)
        for sub in ("out",):
            shutil.rmtree(d / sub, ignore_errors=True)
        (d / "worker.py").write_text(worker_kit.build_script(None), encoding="utf-8")
        (d / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        log = open(d / "worker.log", "w", encoding="utf-8")  # noqa: SIM115 - closed by the child exit
        self._procs[ref] = subprocess.Popen([sys.executable, "-I", str(d / "worker.py"), "--bundle", str(d / "bundle.json"), "--out", str(d / "out")],
                                            cwd=d, stdout=log, stderr=subprocess.STDOUT, env=scrubbed_env())
        return ref

    def poll(self, ref: str) -> RemoteStatus:
        p = self._procs.get(ref)
        res = self._dir(ref) / "out" / "result.json"
        if p is not None and p.poll() is None:
            return RemoteStatus("running")
        if res.exists():
            return RemoteStatus("complete")
        if p is not None:
            return RemoteStatus("error", f"worker exited with code {p.returncode} without result.json")
        return RemoteStatus("lost", "no local process for this attempt (the server restarted?)")

    def wait(self, ref: str, timeout: float | None = None) -> None:
        p = self._procs.get(ref)
        if p is not None:
            p.wait(timeout)

    def fetch(self, ref: str, dest: Path) -> Path:
        src = self._dir(ref) / "out"
        if src.exists():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        lg = self._dir(ref) / "worker.log"
        if lg.exists():
            shutil.copyfile(lg, dest / "worker.log")
        return dest

    def cancel(self, ref: str) -> str:
        p = self._procs.get(ref)
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
            return "local worker process terminated"
        return "local worker was not running"

    def log_tail(self, ref: str) -> str:
        lg = self._dir(ref) / "worker.log"
        return lg.read_text(encoding="utf-8", errors="replace")[-4000:] if lg.exists() else ""
