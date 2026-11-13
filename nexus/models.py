"""On-demand model cache: checks disk space, downloads through the Cost Guard, records provenance."""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from . import config, costguard

HF = "https://huggingface.co"


@functools.lru_cache(maxsize=1)
def catalog() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "models.yaml").read_text(encoding="utf-8"))


def _fetch(url: str, dest: Path, label: str | None = None, expected_mb: float | None = None) -> None:
    costguard.check_host(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    label = label or dest.name
    # curl gives resumable downloads (-C -) for multi-GB files. Progress is reported to the running job (if any), so the
    # Jobs table says "downloading model whisper-small: 120 / 490 MB" instead of sitting on "running" with no sign of life.
    from . import jobs
    proc = subprocess.Popen(["curl", "-sSL", "--fail", "--speed-limit", "1024", "--speed-time", "120", "-C", "-", "-A", config.USER_AGENT, "-o", str(tmp), url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    while True:
        try:
            proc.wait(timeout=2)
            break
        except subprocess.TimeoutExpired:
            pass
        mb = tmp.stat().st_size / 1e6 if tmp.exists() else 0.0
        if expected_mb:
            jobs.note(f"downloading {label}: {mb:.0f} / {expected_mb:.0f} MB (first use only)", min(0.99, mb / expected_mb))
        else:
            jobs.note(f"downloading {label}: {mb:.0f} MB (first use only)")
    err = proc.stderr.read() if proc.stderr else ""
    if proc.returncode != 0:
        jobs.note(f"downloading {label}: failed")
        raise RuntimeError(f"download failed for {url}: {err.strip()}")
    tmp.replace(dest)
    jobs.note(f"downloading {label}: done")


def _revision(repo: str) -> str:
    try:
        from . import net
        return net.get_json(f"{HF}/api/models/{repo}").get("sha", "unknown")
    except Exception:
        return "unknown"


def _record(name: str, info: dict) -> None:
    p = config.MODELS / "installed.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data[name] = info
    p.write_text(json.dumps(data, indent=2))


def installed() -> dict:
    p = config.MODELS / "installed.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def ensure_model(name: str) -> Path:
    m = catalog()["models"][name]
    d = config.MODELS / m["dir"]
    missing = [f for f in m["files"] if not (d / f).exists() or (d / f).stat().st_size == 0]
    if missing:
        config.ensure_dirs()  # fresh clone: NEXUS_HOME does not exist yet
        free_mb = shutil.disk_usage(config.HOME).free / 1e6
        if free_mb < m["size_mb"] * 1.5 + 2000:
            raise RuntimeError(f"not enough disk for {name}: need ~{m['size_mb']} MB, {free_mb:.0f} MB free")
        for f in missing:
            # the weights file is nearly all of size_mb; small config/tokenizer files just show MB so far
            share = m["size_mb"] if f.endswith((".bin", ".gguf", ".safetensors")) else None
            _fetch(f"{HF}/{m['repo']}/resolve/main/{f}", d / f, f"model {name} ({f})", share)
        _record(name, {"repo": m["repo"], "revision": _revision(m["repo"]), "license": m["license"],
                       "files": m["files"], "downloaded": time.strftime("%Y-%m-%d"), "path": str(d)})
    return d


def _pack_voice(name: str) -> dict | None:
    """A voice declared in a language pack (nexus/registry/languages.yaml) but not in the legacy catalog."""
    from .speech import packs
    for tag, p in packs.packs().items():
        for v in (p.get("tts") or {}).get("voices") or []:
            if v.get("id") == name and v.get("engine", "piper") == "piper":
                return {"lang": tag.split("-")[0], "path": v["path"], "gender": v.get("gender"), "license": v.get("licence"),
                        "commercial": v.get("commercial_use") == "yes", "attribution": v.get("attribution")}
    return None


def voice_meta(name: str) -> dict:
    v = catalog()["voices"].get(name) or _pack_voice(name)
    if v is None:
        raise KeyError(f"unknown voice {name}")
    return v


def ensure_voice(name: str) -> Path:
    v = voice_meta(name)
    config.ensure_dirs()
    d = config.MODELS / "piper"
    onnx = d / f"{name}.onnx"
    for suffix in (".onnx", ".onnx.json"):
        f = d / f"{name}{suffix}"
        if not f.exists() or f.stat().st_size == 0:
            _fetch(f"{HF}/rhasspy/piper-voices/resolve/main/{v['path']}/{name}{suffix}", f)
            _record(f"voice:{name}", {"repo": "rhasspy/piper-voices", "license": v["license"],
                                      "commercial": v["commercial"], "downloaded": time.strftime("%Y-%m-%d")})
    return onnx


def voice_for(lang: str, gender: str | None = None) -> str:
    """Pick a voice for a language, preferring commercially cleared ones."""
    base = lang.split("-")[0]
    cands = [(n, v) for n, v in catalog()["voices"].items() if v["lang"] == base]
    if not cands:
        raise KeyError(f"no voice for language {lang}")
    cands.sort(key=lambda nv: (not nv[1]["commercial"], nv[1].get("gender") != gender if gender else 0))
    return cands[0][0]


def fetch_all() -> None:
    for name in catalog()["models"]:
        print("model", name, "->", ensure_model(name))
    for name in catalog()["voices"]:
        print("voice", name, "->", ensure_voice(name))
