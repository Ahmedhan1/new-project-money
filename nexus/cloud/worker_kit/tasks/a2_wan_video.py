# ruff: noqa: F821
# Account 2 task: Wan text/image-to-video on a GPU worker. GPU-READY, UNVERIFIED - never executed in development (no GPU).
# Appended to Account 1's nexus_worker.py: `task`, `Context`, `_torch` are that script's globals.

_WAN_REPOS = {"wan21-t2v-1.3b": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers", "wan22-ti2v-5b": "Wan-AI/Wan2.2-TI2V-5B-Diffusers"}


@task("a2_wan_video")
def task_a2_wan_video(ctx: Context) -> dict:
    """params: model (catalogue id), prompt, settings {frames, fps, width, height, steps, guidance, seed}. Refuses to run
    without CUDA (a CPU result would be mislabelled); the output is returned with the GPU name so Account 2's
    classifier can label it generated-video."""
    import time as _t
    torch = _torch()
    if torch is None or not torch.cuda.is_available():
        raise RuntimeError("RESOURCE_EXHAUSTED: a2_wan_video needs a CUDA GPU; refusing to produce a CPU stand-in")
    from diffusers import AutoencoderKLWan, WanPipeline
    from diffusers.utils import export_to_video
    repo = _WAN_REPOS[ctx.params["model"]]
    s = ctx.params.get("settings") or {}
    t0 = _t.time()
    vae = AutoencoderKLWan.from_pretrained(repo, subfolder="vae", torch_dtype=torch.float32)
    pipe = WanPipeline.from_pretrained(repo, vae=vae, torch_dtype=torch.bfloat16)
    pipe.enable_model_cpu_offload()
    load_s = _t.time() - t0
    g = torch.Generator("cuda").manual_seed(int(s.get("seed", 0)))
    t1 = _t.time()
    frames = pipe(prompt=ctx.params["prompt"], height=int(s.get("height", 480)), width=int(s.get("width", 832)),
                  num_frames=int(s.get("frames", 33)), num_inference_steps=int(s.get("steps", 30)),
                  guidance_scale=float(s.get("guidance", 5.0)), generator=g).frames[0]
    gen_s = _t.time() - t1
    out = ctx.work / "generated.mp4"
    export_to_video(frames, str(out), fps=int(s.get("fps", 16)))
    gpu = torch.cuda.get_device_name(0)
    ctx.artifact(out, "generated-video", model=ctx.params["model"], repo=repo, gpu=gpu, prompt=ctx.params["prompt"])
    return {"model": ctx.params["model"], "gpu": gpu, "load_s": round(load_s, 1), "generate_s": round(gen_s, 1),
            "frames": len(frames), "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1e9, 2)}
