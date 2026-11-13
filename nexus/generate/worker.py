"""Runs inside the (optional) generation environment as a subprocess: `python -m nexus.generate.worker spec.json`.
Reads a job spec, generates, writes the image and a result JSON next to it. Only diffusers models that the registry
declares are loaded; nothing is downloaded unless the spec says `allow_download`."""
from __future__ import annotations

import json
import os
import resource
import sys
import time


def main(spec_path: str) -> None:
    spec = json.load(open(spec_path, encoding="utf-8"))
    os.environ.setdefault("HF_HUB_OFFLINE", "0" if spec.get("allow_download") else "1")
    if spec.get("hf_home"):
        os.environ["HF_HOME"] = spec["hf_home"]
    t0 = time.time()
    import torch
    from diffusers import LCMScheduler, StableDiffusionPipeline
    from huggingface_hub import snapshot_download
    m = spec["model"]
    paths = []
    for f in m["fetch"]:
        paths.append(snapshot_download(f["repo"], allow_patterns=f.get("allow_patterns"), local_files_only=not spec.get("allow_download")))
    d = m["diffusers"]
    pipe = StableDiffusionPipeline.from_pretrained(paths[0], variant=d.get("variant"), torch_dtype=torch.float32, safety_checker=None,
                                                   feature_extractor=None, requires_safety_checker=False)
    if d.get("scheduler") == "LCMScheduler":
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
    elif d.get("scheduler") == "DPMSolverMultistepScheduler":  # bk-sdm-tiny: 8 steps (measured, registry)
        from diffusers import DPMSolverMultistepScheduler
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
    if d.get("lora") and len(paths) > 1:
        pipe.load_lora_weights(paths[1])
        pipe.fuse_lora()
    pipe.set_progress_bar_config(disable=True)
    load_s = time.time() - t0
    out = []
    for job in spec["images"]:
        g = torch.Generator("cpu").manual_seed(int(job["seed"]))
        t1 = time.time()
        img = pipe(job["prompt"], negative_prompt=job.get("negative") or None, num_inference_steps=int(job.get("steps", d.get("steps", 4))),
                   guidance_scale=float(d.get("guidance", 1.0)), width=int(job["w"]), height=int(job["h"]), generator=g).images[0]
        img.save(job["out"])
        out.append({"out": job["out"], "seconds": round(time.time() - t1, 2), "seed": job["seed"], "w": job["w"], "h": job["h"]})
    res = {"ok": True, "load_seconds": round(load_s, 2), "images": out, "torch": torch.__version__,
           "peak_rss_gb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20, 2)}
    json.dump(res, open(spec["result"], "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
