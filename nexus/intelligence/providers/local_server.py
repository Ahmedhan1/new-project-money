"""Start/stop a local OpenAI-compatible llama.cpp server (llama-cpp-python[server]) so the router has a second real
provider without any hosted API. Used by the benchmark and the demo; an owner can run the same command permanently and
point settings.json ai_providers.owner-openai-compatible.base_url at it.

    python -m llama_cpp.server --model <gguf> --model_alias <id> --host 127.0.0.1 --port 8765 --n_ctx 4096 --n_threads 4
"""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path


def command(model_path: Path, alias: str, port: int = 8765, n_ctx: int = 4096, threads: int = 4) -> list[str]:
    return [sys.executable, "-m", "llama_cpp.server", "--model", str(model_path), "--model_alias", alias,
            "--host", "127.0.0.1", "--port", str(port), "--n_ctx", str(n_ctx), "--n_threads", str(threads)]


def wait_ready(base_url: str, timeout: float = 120) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(1)
    return False


@contextmanager
def serve(model_path: Path, alias: str, port: int = 8765, n_ctx: int = 4096, threads: int = 4, log: Path | None = None):
    """Run the server for the duration of the block; yields its base URL (http://127.0.0.1:<port>/v1)."""
    if not Path(model_path).exists():
        raise FileNotFoundError(model_path)
    out = open(log, "w") if log else subprocess.DEVNULL
    p = subprocess.Popen(command(Path(model_path), alias, port, n_ctx, threads), stdout=out, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}/v1"
    try:
        if not wait_ready(base):
            raise RuntimeError(f"llama.cpp server did not become ready on {base}")
        yield base
    finally:
        p.terminate()
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
        if log:
            out.close()
