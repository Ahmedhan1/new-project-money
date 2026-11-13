"""Generated-video benchmark protocol for a GPU machine (the owner's PC, or a free Kaggle/Colab notebook the OWNER signed
into). Video generation never runs on CPU: without CUDA the generation modes stop and say so.
EVERYTHING GPU-SIDE HERE IS UNVERIFIED: it has never run on a GPU in the build container (no GPU there).

    pip install "diffusers>=0.33" transformers accelerate ftfy imageio imageio-ffmpeg psutil opencv-python-headless pyyaml
    pip install open_clip_torch        # optional, for --clip (LAION ViT-B/32 weights, MIT)

    python scripts/gpu_video_bench.py --model wan21-t2v-1.3b --mode t2v [--frames 33] [--steps 30] [--clip]
    python scripts/gpu_video_bench.py --model wan22-ti2v-5b  --mode i2v --image still.png
    python scripts/gpu_video_bench.py --model wan21-t2v-1.3b --mode v2v --video ref.mp4 [--strength 0.7]
    python scripts/gpu_video_bench.py --model cogvideox-2b   --mode t2v
    python scripts/gpu_video_bench.py --request job_request.json      # RenderExecutionRequest from Account 1's scheduler
    python scripts/gpu_video_bench.py --metrics-only clip.mp4 [--prompt "..."]   # CPU-OK: score an existing clip

Per clip it records: model, mode, resolution, frames, fps, seconds, load time, peak VRAM, peak RAM, and from the frames
(nexus.generate.metrics): motion, flicker, frame-to-frame SSIM (mean/min), Farneback flow warp error (temporal
consistency; lower = better), the still-motion verdict (a clip explained by one still under zoom/pan is flagged), the
classification from nexus.generate.classify, and optionally a CLIP score (prompt adherence; mean cosine x 100 over 8
frames, LAION ViT-B/32 - OpenAI CLIP weights are not used because their card excludes deployed use).
`human_verdict` is left empty on purpose: a person judges usability. Copy the JSON into docs/results/.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROMPTS = [
    "A slow aerial shot over a rocky desert canyon at sunrise, soft light, cinematic, realistic",
    "Close-up of ocean waves rolling onto a dark sand beach, overcast sky, natural colours",
    "A small red kite flying in a blue sky above green hills, gentle wind, handheld camera",
]
NEG = "blurry, distorted, text, watermark, low quality"
MODELS = {
    "wan21-t2v-1.3b": {"repo": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers", "family": "wan", "size": (832, 480), "fps": 16, "steps": 30,
                       "guidance": 5.0, "modes": ("t2v", "v2v")},
    "wan22-ti2v-5b": {"repo": "Wan-AI/Wan2.2-TI2V-5B-Diffusers", "family": "wan", "size": (1280, 704), "fps": 24, "steps": 50,
                      "guidance": 5.0, "modes": ("t2v", "i2v")},
    "cogvideox-2b": {"repo": "zai-org/CogVideoX-2b", "family": "cogvideox", "size": (720, 480), "fps": 8, "steps": 50,
                     "guidance": 6.0, "modes": ("t2v",)},
}
TASK = {"t2v": "text_to_video", "i2v": "image_to_video", "v2v": "video_to_video"}


def registry_licence(model_id: str) -> dict:
    try:
        from nexus.generate import catalog
        m = catalog.model(model_id)
        return {k: m.get(k) for k in ("weights_licence", "commercial_use", "conditions", "source_url", "read_on")} | {
            "class": catalog.licence_class(m)}
    except Exception as ex:  # noqa: BLE001
        return {"error": f"registry not readable: {ex}"}


def frames_to_uint8(frames):
    import numpy as np
    out = []
    for f in frames:
        a = np.asarray(f)
        if a.dtype != np.uint8:
            a = (np.clip(a, 0, 1) * 255).astype(np.uint8) if a.max() <= 1.0 else np.clip(a, 0, 255).astype(np.uint8)
        out.append(a[..., :3])
    return np.stack(out)


def clip_score(frames, prompt: str, device: str = "cpu") -> float | None:
    """Mean CLIP cosine similarity x 100 between the prompt and 8 evenly spaced frames (LAION ViT-B/32, MIT)."""
    try:
        import numpy as np
        import open_clip
        import torch
        from PIL import Image
    except ImportError:
        return None
    model, _, pre = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k", device=device)
    tok = open_clip.get_tokenizer("ViT-B-32")
    idx = np.linspace(0, len(frames) - 1, min(8, len(frames))).astype(int)
    with torch.no_grad():
        im = torch.stack([pre(Image.fromarray(frames[i])) for i in idx]).to(device)
        fi = model.encode_image(im)
        ft = model.encode_text(tok([prompt]).to(device))
        fi, ft = fi / fi.norm(dim=-1, keepdim=True), ft / ft.norm(dim=-1, keepdim=True)
        return round(float((fi @ ft.T).mean()) * 100, 2)


def frame_metrics(frames, prompt: str | None, model_id: str | None, mode: str, use_clip: bool, device: str = "cpu") -> dict:
    from nexus.generate import classify, metrics
    t = metrics.temporal(frames)
    sm = metrics.still_motion(frames)
    rep = {**t, "still_motion": sm}
    try:
        rep["classification"] = classify.classify({"model": model_id, "task": TASK.get(mode, mode)}, frames=frames)["classification"]
    except classify.ClassificationError as e:
        rep["classification"] = f"refused: {e}"
    if use_clip and prompt:
        rep["clip_score"] = clip_score(frames, prompt, device)
    return rep


def load_pipeline(model_id: str, mode: str):
    import torch
    m = MODELS[model_id]
    if m["family"] == "wan":
        from diffusers import AutoencoderKLWan
        vae = AutoencoderKLWan.from_pretrained(m["repo"], subfolder="vae", torch_dtype=torch.float32)
        if mode == "i2v":
            from diffusers import WanImageToVideoPipeline as P
        elif mode == "v2v":
            from diffusers import WanVideoToVideoPipeline as P
        else:
            from diffusers import WanPipeline as P
        pipe = P.from_pretrained(m["repo"], vae=vae, torch_dtype=torch.bfloat16)
    else:
        from diffusers import CogVideoXPipeline
        pipe = CogVideoXPipeline.from_pretrained(m["repo"], torch_dtype=torch.float16)
        pipe.vae.enable_tiling()
    vram = torch.cuda.get_device_properties(0).total_memory / 2**30
    offload = vram < 20
    if offload:
        pipe.enable_model_cpu_offload()
    else:
        pipe.to("cuda")
    return pipe, vram, offload


def generate_clip(pipe, model_id: str, mode: str, prompt: str, frames: int, steps: int | None, seed: int,
                  image: str | None = None, video: str | None = None, strength: float = 0.7):
    import torch
    m = MODELS[model_id]
    W, H = m["size"]
    g = torch.Generator("cuda").manual_seed(seed)
    kw = {"prompt": prompt, "negative_prompt": NEG, "num_inference_steps": steps or m["steps"], "guidance_scale": m["guidance"],
          "generator": g}
    if m["family"] == "wan":
        kw |= {"height": H, "width": W}
    if mode == "i2v":
        from diffusers.utils import load_image
        kw["image"] = load_image(image).resize((W, H))
    if mode == "v2v":
        from diffusers.utils import load_video
        kw["video"] = [f.resize((W, H)) for f in load_video(video)[:frames]]
        kw["strength"] = strength
    else:
        kw["num_frames"] = frames
    return pipe(**kw).frames[0]


def run_generation(a) -> dict:
    import psutil
    import torch
    from diffusers.utils import export_to_video
    m = MODELS[a.model]
    if a.mode not in m["modes"]:
        raise SystemExit(f"{a.model} supports modes {m['modes']}, not {a.mode}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    proc = psutil.Process()
    rss = proc.memory_info().rss
    t0 = time.time()
    pipe, vram_total, offload = load_pipeline(a.model, a.mode)
    load_s = time.time() - t0
    runs = []
    prompts = [a.prompt] if a.prompt else PROMPTS[: a.prompts]
    for i, prompt in enumerate(prompts):
        torch.cuda.reset_peak_memory_stats()
        t = time.time()
        fr = generate_clip(pipe, a.model, a.mode, prompt, a.frames, a.steps, 1234 + i, a.image, a.video, a.strength)
        gen_s = time.time() - t
        rss = max(rss, proc.memory_info().rss)
        f = out / f"{a.model}_{a.mode}_{i}.mp4"
        export_to_video(fr, str(f), fps=m["fps"])
        u8 = frames_to_uint8(fr)
        runs.append({"prompt": prompt, "file": f.name, "seconds": round(gen_s, 1), "frames": len(u8),
                     "clip_seconds": round(len(u8) / m["fps"], 2), "resolution": f"{u8.shape[2]}x{u8.shape[1]}", "fps": m["fps"],
                     "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
                     **frame_metrics(u8, prompt, a.model, a.mode, a.clip, "cuda")})
        print(json.dumps(runs[-1]), flush=True)
    return {"model": a.model, "mode": a.mode, "task": TASK[a.mode], "repo": m["repo"], "licence": registry_licence(a.model),
            "gpu": torch.cuda.get_device_name(0), "vram_total_gb": round(vram_total, 1), "cpu_offload": offload,
            "peak_ram_gb": round(rss / 2**30, 2), "load_seconds": round(load_s, 1), "torch": torch.__version__,
            "steps": a.steps or m["steps"], "runs": runs, "verified_on_gpu": True, "human_verdict": "",
            "note": "Generated video. Judge motion, temporal consistency and artefacts by eye before use."}


def run_request(path: str, out_dir: str) -> dict:
    """Execute a RenderExecutionRequest (kind generate.t2v / generate.i2v / generate.v2v) and return a
    RenderExecutionResult. Interrupted runs report a retryable failure and the finished prompts as checkpoint."""
    from nexus import contracts
    req = contracts.check(json.loads(Path(path).read_text(encoding="utf-8")), "RenderExecutionRequest")
    man, s = req["manifest"], req["manifest"]["settings"]
    mode = {"generate.t2v": "t2v", "generate.i2v": "i2v", "generate.v2v": "v2v"}[man["kind"]]
    ns = argparse.Namespace(model=s["model"], mode=mode, frames=int(s.get("frames", 33)), steps=s.get("steps"), prompts=1,
                            prompt=s.get("prompt"), out=out_dir, clip=bool(s.get("clip")), strength=float(s.get("strength", 0.7)),
                            image=(man["inputs"][0]["uri"].removeprefix("file://") if mode == "i2v" and man["inputs"] else None),
                            video=(man["inputs"][0]["uri"].removeprefix("file://") if mode == "v2v" and man["inputs"] else None))
    try:
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("no CUDA")
    except (ImportError, RuntimeError) as e:
        return contracts.make("RenderExecutionResult", request_id=req["request_id"], status="deferred", outputs=[], metrics={},
                              error={"code": "RESOURCE_EXHAUSTED", "message": f"needs a CUDA GPU ({e})", "retryable": True})
    try:
        rep = run_generation(ns)
    except Exception as e:  # noqa: BLE001
        oom = "out of memory" in str(e).lower()
        return contracts.make("RenderExecutionResult", request_id=req["request_id"], status="failed", outputs=[], metrics={},
                              error={"code": "RESOURCE_EXHAUSTED" if oom else "INTERNAL", "message": str(e)[:500],
                                     "retryable": oom}, checkpoint=man.get("checkpoint"))
    r = rep["runs"][0]
    f = Path(out_dir) / r["file"]
    return contracts.make("RenderExecutionResult", request_id=req["request_id"], status="done",
                          outputs=[{**contracts.artifact(f, role="output", media_type="video/mp4"),
                                    "provenance": {"model": s["model"], "task": TASK[mode], "repo": rep["repo"]}}],
                          metrics={"seconds": r["seconds"], "peak_vram_gb": r["peak_vram_gb"], "gpu": rep["gpu"],
                                   "temporal": {k: r[k] for k in ("ssim_f2f_mean", "warp_error", "flicker", "motion")},
                                   "clip_score": r.get("clip_score")})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="wan21-t2v-1.3b", choices=list(MODELS))
    ap.add_argument("--mode", default="t2v", choices=["t2v", "i2v", "v2v"])
    ap.add_argument("--image")
    ap.add_argument("--video")
    ap.add_argument("--strength", type=float, default=0.7)
    ap.add_argument("--frames", type=int, default=33)
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--prompts", type=int, default=2)
    ap.add_argument("--prompt")
    ap.add_argument("--clip", action="store_true")
    ap.add_argument("--out", default="bench_out")
    ap.add_argument("--request")
    ap.add_argument("--metrics-only")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.metrics_only:
        from nexus.generate import metrics
        fr = metrics.read_frames(a.metrics_only)
        rep = {"file": a.metrics_only, "frames": len(fr), "resolution": f"{fr.shape[2]}x{fr.shape[1]}",
               **frame_metrics(fr, a.prompt, None, "t2v", a.clip)}
        (out / "metrics_only.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
        print(json.dumps(rep))
        return 0
    if a.request:
        res = run_request(a.request, a.out)
        dest = out / (Path(a.request).stem + ".result.json")
        dest.write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(res["status"], dest)
        return 0 if res["status"] in ("done", "deferred") else 1
    try:
        import torch
    except ImportError:
        print("PyTorch is not installed: pip install torch (CUDA build) plus the packages in this file's docstring.")
        return 2
    if not torch.cuda.is_available():
        print("No CUDA GPU: this benchmark only runs on a GPU (CPU video generation was measured as unusable, K-19).")
        return 2
    if a.mode == "i2v" and not a.image or a.mode == "v2v" and not a.video:
        print("--mode i2v needs --image, --mode v2v needs --video")
        return 2
    rep = run_generation(a) | {"python": sys.version.split()[0], "os": platform.platform()}
    (out / "gpu_video_bench.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print("wrote", out / "gpu_video_bench.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
