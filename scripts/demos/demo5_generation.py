"""Account 2 Demo 5: AI video generation - GPU-ready path, run on a machine WITHOUT a GPU.

What it proves here (no GPU): an image-to-video job for a licence-clean open model is described as a contract-valid
RenderJobManifest with real requirements, the local executor refuses to fake it (status "deferred",
RESOURCE_EXHAUSTED, retryable), the result consumer turns that into a retry manifest for a GPU worker, and the
classification guard refuses to label a Ken Burns still as generated video. The actual generation is UNVERIFIED until
Account 1's GPU worker runs scripts/gpu_video_bench.py.
Run: NEXUS_HOME=/tmp/a2-demo5 python scripts/demos/demo5_generation.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nexus import config, contracts  # noqa: E402
from nexus.generate import catalog, classify, hardware, remote  # noqa: E402

OUT = ROOT / "docs" / "results" / "a2_demos"


def main() -> None:
    config.ensure_dirs()
    hw = hardware.detect()
    still = config.HOME / "demo5_still.png"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "30", "-i",
                    str(ROOT / ".nexus" / "benchmarks" / "broll" / "source.mp4"), "-frames:v", "1", "-vf", "scale=832:480", str(still)], check=True)
    rows = []
    for task, model in (("image_to_video", "wan22-ti2v-5b"), ("text_to_video", "wan21-t2v-1.3b")):
        try:
            man = remote.job_manifest(task, model, prompt="Slow orbital pan over the cloud bands of Earth at dusk, cinematic",
                                      inputs=[str(still)] if task == "image_to_video" else [], settings={"frames": 81, "fps": 16, "steps": 30})
        except Exception as e:  # noqa: BLE001 - a refused model is a result too
            rows.append({"task": task, "model": model, "refused": f"{type(e).__name__}: {e}"})
            continue
        ok, why = remote.can_run_here(man, hw)
        req = remote.local_request(man, hw)
        res = remote.execute(req, hw)
        nxt = remote.consume(res, man)
        rows.append({"task": task, "model": model, "licence_class": catalog.licence_class(catalog.model(model)),
                     "requirements": man["requirements"], "runs_here": ok, "why_not": why,
                     "manifest_valid": not contracts.validate(man), "request_valid": not contracts.validate(req),
                     "result": {k: res.get(k) for k in ("status", "error")}, "result_valid": not contracts.validate(res),
                     "next": {k: nxt.get(k) for k in ("action", "retry_manifest") if k in nxt} if isinstance(nxt, dict) else str(nxt)})
    # classification guard on a Ken Burns 'animation' of the still claiming to be model output
    kb = config.HOME / "demo5_kenburns.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(still), "-t", "3", "-vf",
                    "zoompan=z='1+0.002*on':d=90:s=832x480:fps=30", "-pix_fmt", "yuv420p", str(kb)], check=True)
    # forged provenance: claims the Wan model made it, but the frames are one still under a zoom
    guard = classify.classify({"model": "wan22-ti2v-5b", "task": "image_to_video"}, path=kb)
    res = {"demo": "5 - AI video generation", "status": "GPU-READY, UNVERIFIED (no GPU in this container)", "hardware": hw,
           "jobs": rows, "classification_guard": guard,
           "protocol": "On an authorised GPU worker: python scripts/gpu_video_bench.py --model wan22-ti2v-5b --mode i2v --image <still> "
                       "--frames 81 --steps 30 --clip ; returns time, VRAM, temporal SSIM/flow-warp error, CLIP prompt score.",
           "never_claimed": "No generated video exists from this machine; CPU video generation produced noise (K-19)."}
    (OUT / "demo5_generation.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({"jobs": [{k: r.get(k) for k in ("task", "model", "runs_here", "result", "manifest_valid")} for r in rows],
                      "guard": {k: guard.get(k) for k in ("classification", "reasons") if isinstance(guard, dict)}}, indent=1, default=str))


if __name__ == "__main__":
    main()
