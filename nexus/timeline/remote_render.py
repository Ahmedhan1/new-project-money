"""Account 2 work on Account 1's remote workers (nexus/cloud/compute, PR #4).

Account 1's worker is a single self-contained script (standard library + FFmpeg, torch on GPU images): it cannot import
the NEXUS renderer. So Account 2 compiles what a worker should do into plain FFmpeg steps on the dispatcher side and
ships them as the params of its own registered task:

    a2_render_steps   CPU. A list of FFmpeg argument lists; `{in:<artifact name>}` is replaced by the worker's verified
                      local copy of that input, `{work}` by its scratch folder. One checkpoint per step (resumable).
                      Output: one `master` artifact (SHA-256 recorded by the worker).
    a2_wan_video      GPU required (>= 8.19 GB VRAM). Wan 2.1 T2V-1.3B / Wan 2.2 TI2V-5B through diffusers on a GPU worker.
                      GPU-READY, UNVERIFIED: never executed here (no GPU); a CPU worker is never chosen for it.

worker_steps(tl)              -> (steps, inputs, expectation) for timelines of plain cuts, or raises NotRemoteRenderable
                                 with the reason (anything richer renders locally with timeline.render)
production_manifest(...)      -> dict accepted by nexus.contracts.cloud.ProductionJobManifest (no import of Account 1)
register_account1_tasks()     -> registers both TaskSpecs with Account 1's dispatcher when its package is present
"""
from __future__ import annotations

from pathlib import Path

from .. import config

TASK_RENDER = "a2_render_steps"
TASK_WAN = "a2_wan_video"
WAN_MIN_VRAM_GB = 8.19  # Wan 2.1 T2V-1.3B, diffusers offload (docs/results/a2_demos/demo5_generation.json)


class NotRemoteRenderable(ValueError):
    pass


_RICH = ("effects", "keyframes", "mask", "pip", "split", "freeze", "credits", "chroma", "perspective", "titles", "text", "audio_fx")


def worker_steps(tl: dict) -> tuple[list[list[str]], dict[str, str], dict]:
    """FFmpeg steps that reproduce a timeline of plain cuts (video clips with in/out, speed 1, no layers/effects, hard
    cuts or crossfades are refused). Audio: each clip's own audio, 48 kHz stereo, silence when a source has none."""
    from ..media import ff
    W, H, fps = int(tl["width"]), int(tl["height"]), int(tl.get("fps", config.FPS))
    if tl.get("tracks") or tl.get("subtitles") and tl.get("burn_subtitles", True) or tl.get("music") or tl.get("audio"):
        raise NotRemoteRenderable("layers, burned captions, music and audio items render locally (timeline.render)")
    inputs: dict[str, str] = {}
    steps: list[list[str]] = []
    durs = []
    for k, c in enumerate(tl["clips"]):
        if c.get("type") != "video":
            raise NotRemoteRenderable(f"clip {k}: {c.get('type')} clips render locally")
        rich = [x for x in _RICH if c.get(x)]
        if rich or float(c.get("speed", 1.0)) != 1.0 or c.get("transition") == "crossfade":
            raise NotRemoteRenderable(f"clip {k}: {rich or 'speed/crossfade'} needs the full renderer")
        name = f"src_{len(inputs):02d}{Path(c['src']).suffix or '.mp4'}"
        name = next((n for n, p in inputs.items() if p == str(c["src"])), name)
        inputs[name] = str(c["src"])
        a, b = float(c["in"]), float(c["out"])
        n = max(1, round((b - a) * fps))
        durs.append(n / fps)
        has_audio = any(s.get("codec_type") == "audio" for s in ff.probe_raw(c["src"]).get("streams", []))
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
              f"format=yuv420p")
        if has_audio:
            src_args = ["-ss", f"{a:.6f}", "-i", f"{{in:{name}}}", "-map", "0:v:0", "-map", "0:a:0",
                        "-af", f"aresample=48000,apad,atrim=0:{n / fps:.6f}"]
        else:
            src_args = ["-ss", f"{a:.6f}", "-i", f"{{in:{name}}}", "-f", "lavfi", "-t", f"{n / fps:.6f}", "-i",
                        "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a"]
        steps.append([*src_args, "-vf", vf, "-frames:v", str(n), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                      "-c:a", "pcm_s16le", "-ac", "2", "-ar", "48000", f"{{work}}/seg_{k:03d}.mkv"])
    segs = [f"{{work}}/seg_{k:03d}.mkv" for k in range(len(tl["clips"]))]
    fc = "".join(f"[{k}:v][{k}:a]" for k in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=1[v][a]"
    steps.append([*sum([["-i", s] for s in segs], []), "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                  "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                  "{work}/master.mp4"])
    expect = {"width": W, "height": H, "fps": fps, "duration": round(sum(durs), 3)}
    return steps, inputs, expect


def production_manifest(tl: dict, input_artifacts: dict[str, str], project_id: str | None = None, *, priority: int = 5,
                        idempotency_key: str | None = None, output_destination: str | None = None) -> dict:
    """ProductionJobManifest/v1 fields for an a2_render_steps job. `input_artifacts` maps worker input names (from
    worker_steps) to Account 1 artifact ids; the worker receives each input under its registered artifact name, so the
    artifacts must be registered with those names."""
    steps, inputs, expect = worker_steps(tl)
    missing = set(inputs) - set(input_artifacts)
    if missing:
        raise ValueError(f"no artifact id for inputs {sorted(missing)}")
    return {"contract": "ProductionJobManifest/v1", "task": TASK_RENDER, "owner_account": "account2", "project_id": project_id,
            "params": {"steps": steps, "output": "master.mp4", "expect": expect}, "inputs": [input_artifacts[n] for n in inputs],
            "requires": {"gpu": False}, "priority": priority, "max_retries": 2, "idempotency_key": idempotency_key,
            "output_destination": output_destination}


def wan_manifest(job: dict, project_id: str | None = None) -> dict:
    """ProductionJobManifest/v1 fields for a generation RenderJobManifest (nexus.generate.remote.job_manifest); GPU required."""
    st, req = job.get("settings") or {}, job.get("requirements") or {}
    settings = {k: v for k, v in st.items() if k not in ("task", "model", "prompt", "licence")}
    return {"contract": "ProductionJobManifest/v1", "task": TASK_WAN, "owner_account": "account2", "project_id": project_id,
            "params": {"model": st.get("model"), "task": st.get("task"), "prompt": st.get("prompt"), "settings": settings,
                       "licence": st.get("licence"), "a2_job_id": job.get("job_id"),
                       "classification_rule": "generated-video only when this task's model output is returned from a named GPU"},
            "inputs": [], "requires": {"gpu": True, "vram_gb": max(WAN_MIN_VRAM_GB, float(req.get("vram_gb") or 0))},
            "max_runtime_s": 3600, "max_retries": 1}


def register_account1_tasks() -> bool:
    """Register Account 2's worker tasks with Account 1's dispatcher (no-op without Account 1's package)."""
    try:
        from ..cloud.compute import tasks as T
    except ImportError:
        return False
    T.register(TASK_RENDER, T.TaskSpec(gpu="no", resumable=True, description="Account 2: compiled FFmpeg render steps -> master.mp4"))
    T.register(TASK_WAN, T.TaskSpec(gpu="required", min_vram_gb=WAN_MIN_VRAM_GB, internet=True,
                                    description="Account 2: Wan text/image-to-video on a GPU worker (GPU-READY, UNVERIFIED)"))
    return True
