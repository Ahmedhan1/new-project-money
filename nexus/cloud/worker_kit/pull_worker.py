"""NEXUS pull worker (standalone, standard library only). Run it on your own GPU PC ("user_gpu") or in Colab ("colab").

    NEXUS_WORKER_TOKEN=<worker_id>.<token> python pull_worker.py --api https://your-nexus.example --kind user_gpu

It polls GET /api/v1/compute/workers/next, runs each job with the worker script the server sends (a subprocess, so a
crashing job never kills the loop), and uploads result.json + artifacts + checkpoints. The token comes from the
environment (or a prompt), is sent only to --api over HTTPS, and is never written to disk.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def _req(method: str, url: str, token: str, body: bytes | None = None, ctype: str | None = None, timeout: float = 60):
    h = {"Authorization": f"Bearer {token}", "User-Agent": "nexus-pull-worker/1"}
    if ctype:
        h["Content-Type"] = ctype
    return urllib.request.urlopen(urllib.request.Request(url, data=body, method=method, headers=h), timeout=timeout)  # noqa: S310


def _multipart(fields: dict, files: list) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for name, path in files:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + Path(path).read_bytes() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def hardware() -> dict:
    hw = {"cpus": os.cpu_count(), "gpus": []}
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20)
        hw["gpus"] = [{"name": x.split(",")[0].strip(), "vram_gb": round(float(x.split(",")[1]) / 1024, 2)} for x in r.stdout.strip().splitlines() if "," in x]
    except Exception:  # noqa: BLE001
        pass
    try:
        import torch  # noqa: F401
        hw["torch"] = True
    except Exception:  # noqa: BLE001
        hw["torch"] = False
    try:
        hw["ffmpeg"] = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        hw["ffmpeg"] = False
    return hw


def run_once(api: str, token: str, hw: dict) -> bool:
    q = urllib.parse.quote(json.dumps(hw))
    try:
        r = _req("GET", f"{api}/api/v1/compute/workers/next?hardware={q}", token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit("the server rejected this worker token (revoked or wrong)") from e
        raise
    if r.status == 204:
        return False
    job = json.loads(r.read())
    wid = token.split(".", 1)[0]
    with tempfile.TemporaryDirectory(prefix="nexus_pull_") as td:
        d = Path(td)
        (d / "worker.py").write_text(job["script"], encoding="utf-8")
        (d / "bundle.json").write_text(json.dumps(job["bundle"]), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "NEXUS_WORKER_TOKEN"}
        print(f"running {job['job_id']} ({job['bundle']['task']})", flush=True)
        subprocess.run([sys.executable, str(d / "worker.py"), "--bundle", str(d / "bundle.json"), "--out", str(d / "out")], env=env)
        out = d / "out"
        res = (out / "result.json").read_text(encoding="utf-8") if (out / "result.json").exists() else json.dumps(
            {"contract": "WorkerExecutionResult/v1", "job_id": job["job_id"], "provider": job["bundle"].get("provider"),
             "connection_id": job["bundle"].get("connection_id"), "status": "failed", "error": "worker produced no result.json"})
        files = [(f"{sub}/{p.name}", p) for sub in ("artifacts", "checkpoints") for p in sorted((out / sub).glob("*")) if p.is_file()]
        body, ctype = _multipart({"result": res, "ref": job["ref"]}, files)
        _req("POST", f"{api}/api/v1/compute/workers/{wid}/result", token, body, ctype, timeout=3600).read()
        print(f"uploaded {len(files)} file(s) for {job['job_id']}", flush=True)
    return True


def main(argv=None) -> int:
    import getpass
    ap = argparse.ArgumentParser(description="NEXUS pull worker")
    ap.add_argument("--api", required=True, help="base URL of your NEXUS server (https://...)")
    ap.add_argument("--kind", default="user_gpu", choices=["user_gpu", "colab"])
    ap.add_argument("--poll", type=float, default=10.0)
    ap.add_argument("--once", action="store_true", help="handle at most one job, then exit")
    a = ap.parse_args(argv)
    api = a.api.rstrip("/")
    if not (api.startswith("https://") or api.startswith("http://127.0.0.1") or api.startswith("http://localhost")):
        raise SystemExit("--api must be https:// (plain http only for localhost)")
    token = os.environ.get("NEXUS_WORKER_TOKEN") or getpass.getpass("NEXUS worker token: ")
    hw = hardware()
    while True:
        did = run_once(api, token, hw)
        if a.once:
            return 0
        if not did:
            time.sleep(a.poll)


if __name__ == "__main__":
    sys.exit(main())
