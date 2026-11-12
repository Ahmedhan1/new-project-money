"""Image/video generation behind one honest interface (GEN-0).

What NEXUS can produce is reported from REAL runtime detection, never from configuration alone, and every produced asset
carries provenance with one of these kinds - they are different capabilities and are never conflated:

    generated        pixels synthesised by a generative model (e.g. SD 1.5 + LCM on CPU)
    animated_still   motion added to a still (Ken Burns / parallax) - the still may itself be generated or licensed
    motion_graphics  designed graphics/typography animation
    footage          camera footage (the user's or licensed)
    slideshow        stills shown in sequence

    providers() / capabilities()                  what exists and what is usable on this machine, with reasons
    image(prompt, ...) -> {"path", "provenance"}  generated still (raises Unavailable with what would enable it)
    video(prompt=None, image=None, ...)           generated video when a provider exists, else Unavailable
    animate_still(image, out, seconds)            honest fallback: an animated still, labelled animated_still
    label(tl) / guard_claim(text, tl)             share of programme time per kind; refuses "AI-generated video" claims
                                                  for a programme with no generated video

The heavy stack (torch + diffusers, multi-GB weights) is optional and runs in a subprocess (nexus/generate/worker.py)
pointed at by settings.json "generation": {"python", "pythonpath", "hf_home"} or env NEXUS_GEN_PYTHONPATH/NEXUS_GEN_HF_HOME.
Model choices, licences and hardware tiers: nexus/registry/generation.yaml and docs/GENERATION.md.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .. import config

KINDS = ("generated", "animated_still", "motion_graphics", "footage", "slideshow")
TASKS = ("image", "image_to_video", "text_to_video", "video_to_video", "inpaint", "outpaint", "upscale", "interpolate")
CLAIM_WORDS = ("ai-generated video", "ai generated video", "generated video", "text-to-video", "text to video", "cinematic generated")


class Unavailable(RuntimeError):
    def __init__(self, reason: str, enable: str):
        super().__init__(f"{reason}. To enable: {enable}")
        self.reason, self.enable = reason, enable


@functools.lru_cache(maxsize=1)
def registry() -> dict:
    return yaml.safe_load((config.REGISTRY_DIR / "generation.yaml").read_text(encoding="utf-8"))


def model(mid: str) -> dict:
    m = next((x for x in registry()["models"] if x["id"] == mid), None)
    if m is None:
        raise KeyError(f"unknown generation model {mid}")
    return m


def _settings() -> dict:
    s = (config.settings() or {}).get("generation") or {}
    return {"python": os.environ.get("NEXUS_GEN_PYTHON") or s.get("python") or sys.executable,
            "pythonpath": os.environ.get("NEXUS_GEN_PYTHONPATH") or s.get("pythonpath") or "",
            "hf_home": os.environ.get("NEXUS_GEN_HF_HOME") or s.get("hf_home") or str(config.MODELS / "hf")}


@functools.lru_cache(maxsize=4)
def _probe_env(python: str, pythonpath: str) -> dict:
    """Ask the generation environment what it has (torch, diffusers, CUDA/MPS, VRAM). One subprocess, cached."""
    code = ("import json,importlib.util as u\nr={'torch':False,'diffusers':False,'cuda':False,'vram_gb':None,'mps':False}\n"
            "if u.find_spec('torch'):\n import torch\n r['torch']=torch.__version__\n r['cuda']=torch.cuda.is_available()\n"
            " r['vram_gb']=round(torch.cuda.get_device_properties(0).total_memory/2**30,1) if r['cuda'] else None\n"
            " r['mps']=bool(getattr(torch.backends,'mps',None) and torch.backends.mps.is_available())\n"
            "r['diffusers']=bool(u.find_spec('diffusers'))\nprint(json.dumps(r))")
    env = {**os.environ, "PYTHONPATH": pythonpath} if pythonpath else dict(os.environ)
    try:
        p = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=180, env=env)
        return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else {"error": p.stderr[-300:]}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"error": str(e)}


def _weights_present(m: dict, hf_home: str) -> bool:
    hub = Path(hf_home) / "hub"
    for f in m.get("fetch") or []:
        d = hub / ("models--" + f["repo"].replace("/", "--")) / "snapshots"
        if not d.exists() or not any(d.iterdir()):
            return False
    return bool(m.get("fetch"))


def detect(refresh: bool = False) -> dict:
    s = _settings()
    if refresh:
        _probe_env.cache_clear()
    env = _probe_env(s["python"], s["pythonpath"])
    return {"settings": s, "env": env, "ram_gb": _ram_gb(), "disk_free_gb": round(shutil.disk_usage(config.HOME if config.HOME.exists() else ".").free / 2 ** 30, 1)}


def _ram_gb() -> float | None:
    try:
        import psutil
        return round(psutil.virtual_memory().total / 2 ** 30, 1)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ providers
class Provider:
    name = "base"
    kind = "generated"
    tasks: tuple = ()

    def status(self, det: dict) -> tuple[bool, str]:
        return False, "not implemented"


class LocalDiffusers(Provider):
    """SD 1.5 + LCM-LoRA (or another registry model with a `diffusers` block) in the optional generation environment."""
    name, kind, tasks = "local-diffusers", "generated", ("image",)
    model_id = "sd15-lcm"

    def status(self, det: dict) -> tuple[bool, str]:
        env = det["env"]
        m = model(self.model_id)
        if env.get("error") or not env.get("torch"):
            return False, "torch is not importable in the generation environment"
        if not env.get("diffusers"):
            return False, "diffusers is not importable in the generation environment"
        if not _weights_present(m, det["settings"]["hf_home"]):
            return False, f"{m['name']} weights are not downloaded ({m['download_gb']} GB)"
        if (det.get("ram_gb") or 99) < float(m.get("min_ram_gb") or 0):
            return False, f"needs {m['min_ram_gb']} GB RAM, this machine has {det['ram_gb']} GB"
        dev = "CUDA GPU" if env.get("cuda") else "CPU"
        return True, f"{m['name']} on {dev} (torch {env['torch']})"

    def generate(self, prompts: list[dict], workdir: Path, allow_download: bool = False) -> dict:
        s = _settings()
        workdir.mkdir(parents=True, exist_ok=True)
        spec = {"model": model(self.model_id), "hf_home": s["hf_home"], "allow_download": allow_download,
                "images": prompts, "result": str(workdir / f"result_{int(time.time() * 1000)}.json")}
        sp = workdir / f"spec_{int(time.time() * 1000)}.json"
        sp.write_text(json.dumps(spec), encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(x for x in (s["pythonpath"], str(config.ROOT)) if x)}
        p = subprocess.run([s["python"], "-m", "nexus.generate.worker", str(sp)], capture_output=True, text=True, env=env,
                           timeout=3600, cwd=str(config.ROOT))
        if p.returncode != 0 or not Path(spec["result"]).exists():
            raise RuntimeError(f"generation worker failed: {p.stderr[-800:]}")
        return json.loads(Path(spec["result"]).read_text(encoding="utf-8"))


class RemoteFolder(Provider):
    """Owner's own GPU machine (or an authorised free GPU notebook) via a shared folder: NEXUS writes job specs to
    <folder>/outbox, the remote side writes artefacts + provenance to <folder>/inbox. Nothing is uploaded by NEXUS."""
    name, kind, tasks = "remote-folder", "generated", ("image", "image_to_video", "text_to_video")

    def status(self, det: dict) -> tuple[bool, str]:
        folder = ((config.settings() or {}).get("generation") or {}).get("remote_folder")
        if not folder:
            return False, "no remote GPU folder configured (settings.generation.remote_folder)"
        if not (Path(folder) / "inbox").exists():
            return False, f"remote folder {folder} has no inbox/ - the remote worker has not connected"
        return True, f"remote GPU folder {folder}"

    @staticmethod
    def submit(folder: Path, task: str, spec: dict) -> Path:
        (folder / "outbox").mkdir(parents=True, exist_ok=True)
        job = folder / "outbox" / f"{task}_{int(time.time() * 1000)}.json"
        job.write_text(json.dumps({"task": task, **spec, "requested": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        return job

    @staticmethod
    def collect(folder: Path, job: Path) -> dict | None:
        res = folder / "inbox" / (job.stem + ".result.json")
        if not res.exists():
            return None
        r = json.loads(res.read_text(encoding="utf-8"))
        if not r.get("provenance", {}).get("model") or r.get("provenance", {}).get("simulated"):
            raise ValueError("remote result without a real model in its provenance cannot be labelled generated")
        return r


class AnimatedStill(Provider):
    name, kind, tasks = "animated-still", "animated_still", ("image_to_video",)

    def status(self, det: dict) -> tuple[bool, str]:
        return (shutil.which("ffmpeg") is not None), "FFmpeg Ken Burns / pan on a still (not generated video)"


PROVIDERS: dict[str, Provider] = {p.name: p for p in (LocalDiffusers(), RemoteFolder(), AnimatedStill())}


def providers(det: dict | None = None) -> list[dict]:
    det = det or detect()
    out = []
    for p in PROVIDERS.values():
        ok, why = p.status(det)
        out.append({"name": p.name, "kind": p.kind, "tasks": list(p.tasks), "available": ok, "why": why})
    return out


ENABLE = {
    "image": "install the optional generation environment (torch CPU + diffusers) and download SD 1.5 + LCM-LoRA "
             "(2.3 GB, CreativeML OpenRAIL-M / OpenRAIL++): python -m nexus generation-setup",
    "image_to_video": "a GPU with >= 8 GB VRAM (Wan 2.1 1.3B, Apache-2.0) or an authorised remote GPU folder; on CPU only "
                      "animated stills are possible",
    "text_to_video": "a GPU with >= 8 GB VRAM (Wan 2.1 T2V 1.3B) or the owner's GPU machine / authorised free GPU notebook via "
                     "the remote folder - not feasible on this CPU",
    "video_to_video": "a GPU provider (not implemented)", "inpaint": "SD 1.5 inpainting in the generation environment (not wired yet)",
    "outpaint": "SD 1.5 inpainting in the generation environment (not wired yet)",
    "upscale": "Real-ESRGAN (BSD-3) in the generation environment (not wired yet)",
    "interpolate": "RIFE (MIT) in the generation environment (not wired yet); FFmpeg minterpolate exists as a non-AI fallback",
}


def capabilities(det: dict | None = None) -> dict:
    det = det or detect()
    ps = {p["name"]: p for p in providers(det)}
    out = {}
    for t in TASKS:
        cands = [p for p in ps.values() if t in p["tasks"] and p["kind"] == "generated"]
        live = [p for p in cands if p["available"]]
        out[t] = {"available": bool(live), "provider": live[0]["name"] if live else None,
                  "why": live[0]["why"] if live else "; ".join(f"{p['name']}: {p['why']}" for p in cands) or "no provider implements it",
                  "enable": None if live else ENABLE.get(t)}
    out["image_to_video"]["fallback"] = "animated_still (Ken Burns on a still) - labelled animated_still, never generated"
    out["text_to_video"]["fallback"] = "generated still + animated_still, or motion graphics - labelled as such"
    return out


def _licence_gate(m: dict, allow_noncommercial: bool) -> None:
    if m.get("commercial_use") != "yes" and not allow_noncommercial:
        raise Unavailable(f"{m['name']} weights are '{m.get('commercial_use')}' for commercial use ({m.get('weights_licence')})",
                          "choose a model whose weights allow commercial use, or pass allow_noncommercial=True for personal work")


def image(prompt: str, out: str | Path, *, size: tuple[int, int] = (512, 512), seed: int = 1, steps: int | None = None,
          negative: str = "", model_id: str = "sd15-lcm", allow_noncommercial: bool = False, allow_download: bool = False) -> dict:
    """One generated still with full provenance. Raises Unavailable (with what would enable it) when no provider works."""
    m = model(model_id)
    _licence_gate(m, allow_noncommercial)
    det = detect()
    prov = PROVIDERS["local-diffusers"]
    ok, why = prov.status(det)
    if not ok and not allow_download:
        raise Unavailable(why, ENABLE["image"])
    out = Path(out)
    w, h = (int(x) // 8 * 8 for x in size)
    res = prov.generate([{"prompt": prompt, "negative": negative, "seed": seed, "w": w, "h": h,
                          "steps": steps or (m.get("diffusers") or {}).get("steps", 4), "out": str(out)}],
                        config.CACHE / "generate", allow_download=allow_download)
    r = res["images"][0]
    return {"path": str(out), "provenance": {"kind": "generated", "model": m["id"], "model_name": m["name"],
                                             "licence": m["weights_licence"], "commercial_use": m["commercial_use"],
                                             "conditions": m.get("conditions"), "seed": seed, "prompt": prompt, "steps": steps,
                                             "size": [w, h], "provider": prov.name, "seconds_taken": r["seconds"],
                                             "load_seconds": res["load_seconds"], "peak_rss_gb": res.get("peak_rss_gb")}}


def video(prompt: str | None = None, image_path: str | None = None, *, seconds: float = 4.0, size=(832, 480)) -> dict:
    """Generated video through a provider that really exists. Never falls back silently: callers who accept an animated
    still call animate_still() themselves, so the label stays correct."""
    task = "image_to_video" if image_path else "text_to_video"
    cap = capabilities()[task]
    if not cap["available"]:
        raise Unavailable(cap["why"], cap["enable"])
    raise Unavailable(f"{cap['provider']} hand-off is asynchronous", "submit via RemoteFolder.submit and collect the result")


def animate_still(image_path: str | Path, out: str | Path, seconds: float = 5.0, size=(1280, 720), move: str = "in",
                  source_provenance: dict | None = None, fps: int = 30) -> dict:
    """Ken Burns motion on a still. The result is labelled animated_still; the still's own provenance is kept."""
    from ..media import ff
    W, H = size
    n = max(1, int(round(seconds * fps)))
    z0, z1 = (1.0, 1.18) if move == "in" else (1.18, 1.0)
    ff.run(["-loop", "1", "-i", str(image_path), "-vf",
            f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
            f"zoompan=z='{z0}+({z1}-{z0})*on/{n}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d={n}:s={W}x{H}:fps={fps},format=yuv420p",
            "-frames:v", n, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out)], op="animate-still")
    return {"path": str(out), "provenance": {"kind": "animated_still", "method": f"Ken Burns zoom {move} (FFmpeg zoompan)",
                                             "still": source_provenance or {"kind": "unknown"}}}


# ------------------------------------------------------------------ provenance labelling
def _clip_kind(c: dict, generated_paths: dict) -> str:
    p = c.get("provenance") or generated_paths.get(c.get("src") or "") or {}
    if p.get("kind") in KINDS:
        return p["kind"]
    if c["type"] == "video":
        return "footage"
    if c["type"] == "image":
        return "animated_still" if c.get("kenburns", "in") != "none" else "slideshow"
    return "motion_graphics"


def label(tl: dict, generated_paths: dict | None = None) -> dict:
    """Share of programme time per provenance kind, and an honest one-line description."""
    from ..timeline.render import clip_duration
    gp = generated_paths or {}
    secs = {k: 0.0 for k in KINDS}
    for c in tl.get("clips", []):
        k = _clip_kind(c, gp)
        if k == "animated_still" and ((c.get("provenance") or gp.get(c.get("src") or "") or {}).get("still") or {}).get("kind") == "generated":
            k = "animated_still"  # a generated still with motion is still an animated still, not generated video
        secs[k] += clip_duration(c)
    total = sum(secs.values()) or 1.0
    share = {k: round(v / total, 3) for k, v in secs.items()}
    gen_stills = sum(1 for c in tl.get("clips", []) if ((c.get("provenance") or {}).get("still") or {}).get("kind") == "generated"
                     or (c.get("provenance") or {}).get("kind") == "generated" and c["type"] == "image")
    main = max(share, key=share.get)
    desc = {"generated": "AI-generated video", "animated_still": "animated stills", "motion_graphics": "motion graphics",
            "footage": "edited footage", "slideshow": "slideshow"}[main]
    return {"seconds": {k: round(v, 2) for k, v in secs.items()}, "share": share, "generated_stills": gen_stills,
            "describe_as": desc + (f" ({gen_stills} AI-generated still(s))" if gen_stills and main != "generated" else "")}


def guard_claim(text: str, tl: dict, generated_paths: dict | None = None) -> None:
    """Refuse marketing/description text that calls a programme generated video when no generated video is in it."""
    lab = label(tl, generated_paths)
    if lab["share"]["generated"] == 0 and any(w in (text or "").lower() for w in CLAIM_WORDS):
        raise ValueError(f"the programme contains no generated video (it is {lab['describe_as']}); do not describe it as "
                         "generated video")
