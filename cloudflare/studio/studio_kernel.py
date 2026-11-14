"""NEXUS Studio session on Kaggle: runs the FULL NEXUS app (the same UI and features as on Windows) for the owner.

Pushed as a private Kaggle script kernel by the Cloudflare control plane (cloudflare/src/studio.js) with its config
embedded below. Steps:
  1. restore the previous session's NEXUS_HOME (projects, database, settings) from the mounted output of the last
     studio kernel, if the control plane attached one;
  2. install the system/Python dependencies the app needs (Kaggle images already ship torch, ffmpeg, numpy...);
  3. clone the NEXUS repository at the configured ref and start `python -m nexus serve` on 127.0.0.1 with
     NEXUS_API_TOKEN set (hosted mode: every request needs that bearer token; only the Cloudflare gateway has it);
  4. open a Cloudflare quick tunnel to it and report the URL to the control plane, which proxies the owner's browser
     to it after its own login check;
  5. heartbeat every 30 s; stop when the owner presses Stop, when the UI has been idle too long, or before Kaggle's
     session limit; then shut the app down cleanly and save NEXUS_HOME (without models and caches) to
     /kaggle/working so the next session continues where this one stopped.
Standard library only. Never prints the token or the callback secret.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_CONFIG_B64 = ""  # replaced by the control plane: repo, ref, token, callback, secret, max_s

CFG = json.loads(base64.b64decode(_CONFIG_B64)) if _CONFIG_B64 else {}
T0 = time.time()
WORK = Path("/kaggle/working") if Path("/kaggle/working").is_dir() else Path("studio_out")
HOME = Path("/tmp/nexus_home")
SRC = Path("/tmp/nexus_src")
SAVE = WORK / "nexus_home"
# re-creatable; models are re-downloaded on first use. "backups": the app's local backup copies duplicate every
# project file - the saved session output IS the backup here, so keeping them would double its size.
SKIP_SAVE = {"models", "cache", "tmp", "logs", "run", "backups"}
LOG: list[str] = []
STATE = {"restored": False, "saved": False, "safe_to_save": False}
SECRETS: dict = {}  # session-only secrets (never written to disk or the output)


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    for secret in (CFG.get("token"), CFG.get("secret"), *SECRETS.values()):
        if secret:
            line = line.replace(secret, "***")
    LOG.append(line)
    print(line, flush=True)


def report(state: str, **extra) -> dict:
    """POST to the control plane. Returns its reply ({"stop": bool}); never raises."""
    body = json.dumps({"secret": CFG["secret"], "session": CFG["session"], "state": state, "log": LOG[-25:], **extra}).encode()
    req = urllib.request.Request(CFG["callback"], data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "nexus-studio/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - https control plane URL from the config
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 403:  # a newer session replaced this one: stop at once, never hold a GPU nobody can reach
            print("this session was replaced by a newer one: stopping", flush=True)
            return {"stop": True, "superseded": True}
        print(f"report {state} failed: HTTP {e.code}", flush=True)
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"report {state} failed: {type(e).__name__}", flush=True)
        return {}


def sh(cmd: list[str], timeout: float = 1800, check: bool = False) -> int:
    log("$ " + " ".join(cmd)[:160])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"  timed out after {timeout:.0f}s")
        return 124
    if r.returncode != 0:
        log(f"  exit {r.returncode}: {(r.stderr or r.stdout)[-600:]}")
        if check:
            raise RuntimeError(f"{cmd[0]} failed")
    return r.returncode


# ---------------------------------------------------------------- 1. restore
def find_saved() -> Path | None:
    """The previous session's saved NEXUS_HOME. Kaggle mounts a notebook's output at
    /kaggle/input/notebooks/<owner>/<slug>/ (seen live 2026-11-14); older layouts used /kaggle/input/<slug>/."""
    root = Path("/kaggle/input")
    if not root.is_dir():
        return None
    want = (CFG.get("restore_from") or "").split("/")[-1]
    hits = [p for p in root.rglob("nexus_home") if p.is_dir() and len(p.relative_to(root).parts) <= 5]
    hits.sort(key=lambda p: (want not in p.parts, len(p.parts)))
    return hits[0] if hits else None


def restore() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    src = find_saved()
    if src is None:
        if CFG.get("restore_from"):
            # never start empty when work was expected: saving later would hide the real saved work
            raise RuntimeError(f"previous studio output {CFG['restore_from']} was attached but not found under /kaggle/input")
        log("no previous studio state attached: starting with an empty NEXUS_HOME")
        return
    shutil.copytree(src, HOME, dirs_exist_ok=True)
    n = sum(1 for _ in HOME.rglob("*"))
    log(f"restored previous session from {src.parent.name} ({n} files)")
    STATE["restored"] = True


# ---------------------------------------------------------------- 2+3. install and start
APT = ["espeak-ng", "fonts-noto-core", "fonts-noto-cjk", "fonts-dejavu-core", "tesseract-ocr", "tesseract-ocr-ara",
       "libcairo2-dev", "libpango1.0-dev", "pkg-config"]
OPTIONAL_PIP = {"manim", "llama-cpp-python", "pytest"}  # slow to build / not needed to serve; installed in the background


def install() -> None:
    if not shutil.which("ffmpeg"):
        APT.insert(0, "ffmpeg")
    sh(["apt-get", "update", "-qq"], 600)
    sh(["apt-get", "install", "-y", "-qq", "--no-install-recommends", *APT], 1200)
    sh(["git", "clone", "--depth", "1", "-b", CFG.get("ref", "main"), CFG["repo"], str(SRC)], 600, check=True)
    reqs = []
    for line in (SRC / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name = line.split("#")[0].strip()
        if name and re.split(r"[<>=\[ ]", name)[0].lower() not in OPTIONAL_PIP:
            reqs.append(name)
    sh([sys.executable, "-m", "pip", "install", "-q", *reqs], 2400, check=True)

    install_llama({**os.environ, "LD_LIBRARY_PATH": cuda_lib_path()})

    def background() -> None:
        sh([sys.executable, "-m", "pip", "install", "-q", "manim"], 2400)
        log("optional package manim done")
        if shutil.which("nvidia-smi"):  # natural Arabic/English dubbing voice (GPU only; ~87 s per line on CPU)
            ok = sh([sys.executable, "-m", "pip", "install", "-q", "silma-tts"], 2400) == 0
            log("SILMA TTS (Arabic voice) ready" if ok else "SILMA TTS not installed: dubbing uses the Piper voice")
            ok = sh([sys.executable, "-m", "pip", "install", "-q", "demucs"], 2400) == 0  # keeps music/effects under a dub
            log("Demucs (voice/background separation) ready" if ok else "Demucs not installed: dubbed scenes lose their background")

    threading.Thread(target=background, daemon=True).start()


def fetch_secrets() -> dict:
    """Session secrets (YouTube channel keys) from the control plane; kept in memory, passed to the app as env vars."""
    url = CFG["callback"].rsplit("/", 1)[0] + "/secrets"
    body = json.dumps({"secret": CFG["secret"], "session": CFG["session"]}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "User-Agent": "nexus-studio/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            got = json.loads(r.read() or b"{}").get("env") or {}
    except Exception as e:  # noqa: BLE001
        log(f"could not fetch session secrets ({type(e).__name__}); YouTube stays unconnected this session")
        return {}
    ok = {k: str(v) for k, v in got.items() if re.fullmatch(r"NEXUS_PUB_[A-Z0-9_]{1,60}", k)}
    if ok:
        log("YouTube channel keys received for this session")
    return ok


def register_youtube() -> None:
    code = ("from nexus.intelligence.publishing import engine, youtube_oauth as y; "
            "engine.connect_account('youtube', 'main', secret_ref=y.names('main')['refresh_token'], scopes=y.SCOPES, "
            "note='connected from the Cloudflare panel')")
    rc = sh([sys.executable, "-c", code], 120)
    log("YouTube channel registered in NEXUS (account 'main')" if rc == 0 else "YouTube account registration failed (see log)")



def cuda_lib_path() -> str:
    """Kaggle ships CUDA 13 system-wide, but the CUDA 12 runtime/cuBLAS that the llama.cpp cu12 wheel needs are in pip
    packages (site-packages/nvidia/<lib>/lib, seen live 2026-11-14) that are not on LD_LIBRARY_PATH."""
    import glob
    dirs = sorted({str(Path(p).parent) for pat in ("nvidia/*/lib/libcudart.so.12", "nvidia/*/lib/libcublas*.so.12", "nvidia/cudnn/lib/libcudnn.so.9")
                   for base in sys.path if base.endswith(("site-packages", "dist-packages")) for p in glob.glob(os.path.join(base, pat))})
    return ":".join(dirs + [x for x in os.environ.get("LD_LIBRARY_PATH", "").split(":") if x])


def llama_ok(env: dict) -> bool:
    code = "import llama_cpp, sys; llama_cpp.llama_backend_init(); print('llama ok', llama_cpp.__version__)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        log(f"llama.cpp check failed: {(r.stderr or r.stdout)[-300:]}")
    return r.returncode == 0


def install_llama(env: dict) -> None:
    """The local LLM (scripts, titles, translation checks). Order: CUDA 12 wheel -> CPU wheel -> build from source."""
    cuda = "https://abetlen.github.io/llama-cpp-python/whl/cu124"
    cpu = "https://abetlen.github.io/llama-cpp-python/whl/cpu"
    sh([sys.executable, "-m", "pip", "install", "-q", "diskcache>=5.6.1", "jinja2>=2.11.3", "typing-extensions>=4.5.0"], 600)
    for extra in (cuda, cpu):
        sh([sys.executable, "-m", "pip", "install", "-q", "--force-reinstall", "--no-deps", "--only-binary=:all:", "llama-cpp-python",
            "--extra-index-url", extra], 1800)
        if llama_ok(env):
            log(f"llama.cpp ready ({'GPU' if extra == cuda else 'CPU'})")
            return
    sh([sys.executable, "-m", "pip", "install", "-q", "--force-reinstall", "--no-binary=llama-cpp-python", "llama-cpp-python"], 3600)
    log("llama.cpp ready (built from source)" if llama_ok(env) else "llama.cpp NOT available: scripts use the fallback writer")


def start_app() -> subprocess.Popen:
    env = {**os.environ, **SECRETS, "LD_LIBRARY_PATH": cuda_lib_path(), "NEXUS_HOME": str(HOME), "NEXUS_API_TOKEN": CFG["token"], "NEXUS_PORT": "8765",
           "PYTHONUNBUFFERED": "1", "NEXUS_EMBEDDED_WORKER": "1"}
    out = open(WORK / "nexus_app.log", "a", encoding="utf-8")  # noqa: SIM115 - kept open for the child
    p = subprocess.Popen([sys.executable, "-m", "nexus", "serve", "--host", "127.0.0.1", "--port", "8765"],
                         cwd=SRC, env=env, stdout=out, stderr=subprocess.STDOUT)
    for _ in range(240):
        if p.poll() is not None:
            raise RuntimeError(f"the NEXUS app exited with code {p.returncode}: see nexus_app.log")
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/api/v1/health/live", timeout=3):  # noqa: S310
                log("NEXUS app is up on 127.0.0.1:8765")
                return p
        except Exception:  # noqa: BLE001 - not up yet (or 401: still means it is serving)
            try:
                req = urllib.request.Request("http://127.0.0.1:8765/api/health", headers={"Authorization": f"Bearer {CFG['token']}"})
                with urllib.request.urlopen(req, timeout=3):  # noqa: S310
                    log("NEXUS app is up on 127.0.0.1:8765")
                    return p
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
    raise RuntimeError("the NEXUS app did not become healthy within 8 minutes")


# ---------------------------------------------------------------- 4. tunnel
def tunnel() -> tuple[subprocess.Popen, str]:
    exe = Path("/tmp/cloudflared")
    if not exe.exists():
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
        with urllib.request.urlopen(url, timeout=120) as r, open(exe, "wb") as f:  # noqa: S310
            shutil.copyfileobj(r, f)
        exe.chmod(0o755)
    p = subprocess.Popen([str(exe), "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8765"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    found: list[str] = []

    def reader() -> None:
        for line in p.stdout:  # type: ignore[union-attr]
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not found:
                found.append(m.group(0))

    threading.Thread(target=reader, daemon=True).start()
    for _ in range(90):
        if found:
            log(f"tunnel ready ({found[0].split('//')[1].split('.')[0][:6]}…)")
            return p, found[0]
        if p.poll() is not None:
            break
        time.sleep(1)
    raise RuntimeError("cloudflared did not open a tunnel")


# ---------------------------------------------------------------- 5. save
def save(app: subprocess.Popen | None) -> None:
    if not STATE["safe_to_save"]:
        log("nothing saved: the session failed before your previous work was loaded (it is still kept on Kaggle)")
        return
    if app and app.poll() is None:
        app.send_signal(signal.SIGTERM)
        try:
            app.wait(45)
        except subprocess.TimeoutExpired:
            app.kill()
    if not HOME.exists():
        return
    shutil.rmtree(SAVE, ignore_errors=True)
    shutil.copytree(HOME, SAVE, ignore=lambda d, names: [n for n in names if Path(d) == HOME and n in SKIP_SAVE])
    size = sum(f.stat().st_size for f in SAVE.rglob("*") if f.is_file())
    STATE["saved"] = True
    log(f"saved NEXUS_HOME for the next session ({size / 2**20:.0f} MB, models and caches excluded)")


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    max_s = float(CFG.get("max_s", 6 * 3600))
    app = cf = None
    try:
        if report("installing").get("superseded"):
            return 0  # replaced before it started: exit without touching the saved work
        restore()
        STATE["safe_to_save"] = True
        install()
        if report("starting").get("superseded"):
            return 0
        SECRETS.update(fetch_secrets())
        app = start_app()
        if "NEXUS_PUB_YOUTUBE_MAIN_REFRESH_TOKEN" in SECRETS:
            os.environ.update({"NEXUS_HOME": str(HOME)})
            os.chdir(SRC)
            register_youtube()
        cf, url = tunnel()
        reply = report("ready", url=url)
        if reply.get("superseded"):
            reply = {"stop": True}
        while not reply.get("stop"):
            if time.time() - T0 > max_s - 20 * 60:
                log("Kaggle session limit is near: stopping to save your work")
                break
            if app.poll() is not None:
                log(f"the NEXUS app exited (code {app.returncode}); see nexus_app.log in the output")
                break
            if cf.poll() is not None:
                log("tunnel dropped: reopening")
                cf, url = tunnel()
                report("ready", url=url)
            time.sleep(30)
            reply = report("ready", url=url)
        report("saving")
        save(app)
        report("stopped", saved=STATE["saved"])
        return 0
    except Exception as e:  # noqa: BLE001 - always try to keep the owner's work
        log(f"ERROR: {type(e).__name__}: {e}")
        try:
            save(app)
        except Exception as e2:  # noqa: BLE001
            log(f"save failed: {e2}")
        report("error", error=f"{type(e).__name__}: {e}", saved=STATE["saved"])
        return 0  # exit 0 so Kaggle keeps the saved output for the next session
    finally:
        if cf and cf.poll() is None:
            cf.terminate()
        (WORK / "studio_log.txt").write_text("\n".join(LOG), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
