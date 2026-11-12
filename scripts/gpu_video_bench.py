"""Real text/image-to-video benchmark for a GPU machine (the owner's PC, or a free Kaggle/Colab notebook the OWNER
signed into). Nothing here runs on CPU: without CUDA it stops and says so.

    pip install "diffusers>=0.33" transformers accelerate ftfy imageio imageio-ffmpeg psutil
    python scripts/gpu_video_bench.py --model wan21-t2v-1.3b [--frames 33] [--steps 30] [--out bench_out]

Models (Apache-2.0 weights, commercial use allowed per their cards; licence text re-checked at run time from the
registry entry, not assumed):
    wan21-t2v-1.3b   Wan-AI/Wan2.1-T2V-1.3B-Diffusers   ~8 GB VRAM (card), 832x480 @16 fps
    wan22-ti2v-5b    Wan-AI/Wan2.2-TI2V-5B-Diffusers    ~24 GB VRAM (card; offload), 1280x704 @24 fps

Writes <out>/<model>_<prompt#>.mp4 and <out>/gpu_video_bench.json with: GPU name, VRAM total, peak VRAM, peak RAM,
load time, generation time per clip, frames, resolution, fps, and two motion measures computed from the frames:
    motion       mean absolute frame difference (0 = frozen)
    flicker      mean |2nd difference| of per-frame brightness (temporal instability; lower is better)
A human still has to judge "usable": the JSON has a "human_verdict" field left empty on purpose.
Copy gpu_video_bench.json back into docs/results/ to record the result.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

PROMPTS = [
    "A slow aerial shot over a rocky desert canyon at sunrise, soft light, cinematic, realistic",
    "Close-up of ocean waves rolling onto a dark sand beach, overcast sky, natural colours",
    "A small red kite flying in a blue sky above green hills, gentle wind, handheld camera",
]
MODELS = {
    "wan21-t2v-1.3b": {"repo": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers", "size": (832, 480), "fps": 16, "steps": 30, "guidance": 5.0},
    "wan22-ti2v-5b": {"repo": "Wan-AI/Wan2.2-TI2V-5B-Diffusers", "size": (1280, 704), "fps": 24, "steps": 50, "guidance": 5.0},
}


def registry_licence(model_id: str) -> dict:
    try:
        import yaml
        reg = yaml.safe_load((Path(__file__).resolve().parents[1] / "nexus" / "registry" / "generation.yaml").read_text())
        items = reg if isinstance(reg, list) else next(v for v in reg.values() if isinstance(v, list))
        e = next(x for x in items if x.get("id") == model_id)
        return {k: e.get(k) for k in ("weights_licence", "commercial_use", "conditions", "source_url", "read_on")}
    except Exception as ex:  # noqa: BLE001
        return {"error": f"registry not readable: {ex}"}


def motion_stats(frames) -> dict:
    import numpy as np
    a = np.stack([np.asarray(f, dtype=np.float32) for f in frames])
    if a.max() <= 1.0:
        a = a * 255
    g = a.mean(axis=-1) if a.ndim == 4 else a
    diff = np.abs(np.diff(g, axis=0)).mean(axis=(1, 2))
    br = g.mean(axis=(1, 2))
    return {"motion": round(float(diff.mean()), 3), "motion_min": round(float(diff.min()), 3),
            "flicker": round(float(np.abs(np.diff(br, 2)).mean()) if len(br) > 2 else 0.0, 3)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="wan21-t2v-1.3b", choices=list(MODELS))
    ap.add_argument("--frames", type=int, default=33)
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--prompts", type=int, default=2)
    ap.add_argument("--out", default="bench_out")
    a = ap.parse_args()
    import psutil
    try:
        import torch
    except ImportError:
        print("PyTorch is not installed: pip install torch (CUDA build) plus the packages in this file's docstring.")
        return 2
    if not torch.cuda.is_available():
        print("No CUDA GPU: this benchmark only runs on a GPU (CPU video generation was measured as unusable).")
        return 2
    m = MODELS[a.model]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    from diffusers import AutoencoderKLWan, WanPipeline
    from diffusers.utils import export_to_video
    proc = psutil.Process()
    rss_peak = proc.memory_info().rss
    t0 = time.time()
    vae = AutoencoderKLWan.from_pretrained(m["repo"], subfolder="vae", torch_dtype=torch.float32)
    pipe = WanPipeline.from_pretrained(m["repo"], vae=vae, torch_dtype=torch.bfloat16)
    vram_total = torch.cuda.get_device_properties(0).total_memory / 2**30
    offload = vram_total < 20
    if offload:
        pipe.enable_model_cpu_offload()
    else:
        pipe.to("cuda")
    load_s = time.time() - t0
    rss_peak = max(rss_peak, proc.memory_info().rss)
    W, H = m["size"]
    runs = []
    for i, prompt in enumerate(PROMPTS[: a.prompts]):
        torch.cuda.reset_peak_memory_stats()
        g = torch.Generator("cuda").manual_seed(1234 + i)
        t = time.time()
        frames = pipe(prompt=prompt, negative_prompt="blurry, distorted, text, watermark", height=H, width=W,
                      num_frames=a.frames, num_inference_steps=a.steps or m["steps"], guidance_scale=m["guidance"],
                      generator=g).frames[0]
        gen_s = time.time() - t
        rss_peak = max(rss_peak, proc.memory_info().rss)
        f = out / f"{a.model}_{i}.mp4"
        export_to_video(frames, str(f), fps=m["fps"])
        runs.append({"prompt": prompt, "file": f.name, "seconds": round(gen_s, 1), "frames": len(frames),
                     "clip_seconds": round(len(frames) / m["fps"], 2), "resolution": f"{W}x{H}", "fps": m["fps"],
                     "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2), **motion_stats(frames)})
        print(json.dumps(runs[-1]))
    rep = {"model": a.model, "repo": m["repo"], "licence": registry_licence(a.model), "gpu": torch.cuda.get_device_name(0),
           "vram_total_gb": round(vram_total, 1), "cpu_offload": offload, "peak_ram_gb": round(rss_peak / 2**30, 2),
           "load_seconds": round(load_s, 1), "torch": torch.__version__, "python": sys.version.split()[0],
           "os": platform.platform(), "steps": a.steps or m["steps"], "runs": runs,
           "human_verdict": "", "note": "Generated video. Judge motion, temporal consistency and artefacts by eye before use."}
    (out / "gpu_video_bench.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print("wrote", out / "gpu_video_bench.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
