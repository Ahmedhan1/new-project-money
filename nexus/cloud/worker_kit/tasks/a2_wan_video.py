# ruff: noqa: F821
# Wan 2.2 TI2V-5B text-to-video / image-to-video on a GPU worker (Kaggle T4 x2 verified; see
# docs/results/wan22_5b/). Appended to Account 1's nexus_worker.py: `task`, `Context`, `_torch`, `ffprobe`, `_run`
# are that script's globals.
#
# Execution strategy (measured, not assumed): `enable_model_cpu_offload()` keeps one module on ONE GPU and does not use a
# second GPU. On 2 x 15 GB T4 the 5B transformer (FP16, ~10 GB) runs on cuda:0, the UMT5-XXL text encoder (~11 GB) on
# cuda:1; all prompts are encoded first, the text encoder is then freed and the VAE (FP32, untiled) decodes on cuda:1;
# prompt embeddings move between the GPUs. T4 (sm_75) has no
# native BF16, so the transformer runs in FP16 unless the GPU is sm_80+ (then BF16); the text encoder uses BF16 only if
# a BF16 matmul actually works on the device, else FP32 on the CPU. A NaN/Inf result is reported as a failure, never
# exported. On a single GPU every component shares cuda:0 with model offload.

_WAN_REPOS = {"wan21-t2v-1.3b": ("Wan-AI/Wan2.1-T2V-1.3B-Diffusers", None),
              "wan22-ti2v-5b": ("Wan-AI/Wan2.2-TI2V-5B-Diffusers", "b8fff7315c768468a5333511427288870b2e9635")}
_WAN_DIFFUSERS = "0.35.2"   # first stable line with WanPipeline expand_timesteps (TI2V); pinned for reproducibility
_WAN_NEG = ("blurry, low quality, worst quality, jpeg artifacts, static, still image, frozen frame, distorted, deformed, "
            "disfigured, extra limbs, watermark, text, subtitles, logo, overexposed, underexposed")


def _wan_env(torch) -> dict:
    n = torch.cuda.device_count()
    gpus = []
    for i in range(n):
        p = torch.cuda.get_device_properties(i)
        gpus.append({"index": i, "name": p.name, "vram_gb": round(p.total_memory / 2 ** 30, 2), "cc": f"{p.major}.{p.minor}"})
    bf16_native = n > 0 and torch.cuda.get_device_capability(0)[0] >= 8
    bf16_works = False
    if n:
        try:
            a = torch.randn(256, 256, device=f"cuda:{n - 1}", dtype=torch.bfloat16)
            bf16_works = bool(torch.isfinite(a @ a).all().item())
        except Exception:  # noqa: BLE001
            bf16_works = False
    vers = {}
    for m in ("torch", "diffusers", "transformers", "accelerate", "safetensors", "huggingface_hub"):
        try:
            vers[m] = __import__(m).__version__
        except Exception:  # noqa: BLE001
            vers[m] = None
    vers["cuda"] = torch.version.cuda
    return {"gpu_count": n, "gpus": gpus, "bf16_native": bf16_native, "bf16_matmul_works": bf16_works, "versions": vers}


def _wan_ensure_diffusers(ctx) -> None:
    try:
        import diffusers
        if diffusers.__version__ == _WAN_DIFFUSERS:
            return
    except Exception:  # noqa: BLE001
        pass
    ctx.log(f"installing diffusers=={_WAN_DIFFUSERS} (pinned)")
    r = _run([sys.executable, "-m", "pip", "install", "-q", f"diffusers=={_WAN_DIFFUSERS}", "ftfy"], timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"pip install diffusers=={_WAN_DIFFUSERS} failed: {r.stderr[-400:]}")
    for k in [k for k in sys.modules if k == "diffusers" or k.startswith("diffusers.")]:
        del sys.modules[k]


class _GpuPeak(threading.Thread):
    """Per-GPU utilisation / used-memory peaks from nvidia-smi (the worker's Sampler keeps only a pooled summary)."""

    def __init__(self, every=2.0):
        super().__init__(daemon=True)
        self.every, self.halt, self.rows = every, threading.Event(), []

    def run(self):
        while not self.halt.wait(self.every):
            try:
                r = _run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used", "--format=csv,noheader,nounits"], timeout=5)
                self.rows.append([[float(x) for x in ln.split(",")] for ln in r.stdout.strip().splitlines() if ln.strip()])
            except Exception:  # noqa: BLE001
                pass

    def summary(self, n):
        out = []
        for i in range(n):
            u = [r[i][1] for r in self.rows if len(r) > i]
            m = [r[i][2] for r in self.rows if len(r) > i]
            out.append({"index": i, "util_max": max(u) if u else None, "util_mean": round(sum(u) / len(u), 1) if u else None,
                        "smi_mem_used_max_mb": max(m) if m else None})
        return out


def _wan_quality(frames) -> dict:
    """Motion / emptiness checks on the decoded frames (uint8 HxWx3 numpy arrays)."""
    import numpy as np
    arr = np.stack([np.asarray(f, dtype=np.float32) for f in frames])
    luma = arr.mean(axis=(1, 2, 3))
    diffs = np.abs(arr[1:] - arr[:-1]).mean(axis=(1, 2, 3)) if len(arr) > 1 else np.zeros(1)
    return {"frames": int(len(arr)), "mean_luma": round(float(luma.mean()), 2), "min_frame_luma": round(float(luma.min()), 2),
            "mean_frame_std": round(float(arr.std(axis=(1, 2, 3)).mean()), 2),
            "mean_abs_frame_diff": round(float(diffs.mean()), 3), "max_abs_frame_diff": round(float(diffs.max()), 3),
            "first_last_diff": round(float(np.abs(arr[-1] - arr[0]).mean()), 3),
            "static_pairs": int((diffs < 0.5).sum()), "black": bool(luma.max() < 8), "flat": bool(arr.std() < 2)}


def _wan_write_mp4(frames, path, fps: int) -> None:
    import numpy as np
    h, w = np.asarray(frames[0]).shape[:2]
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps),
                          "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
                          "-movflags", "+faststart", str(path)], stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(np.ascontiguousarray(np.asarray(f, dtype=np.uint8)).tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError("ffmpeg encode failed")


@task("a2_wan_video")
def task_a2_wan_video(ctx: Context) -> dict:
    """params: model (catalogue id), runs: [{mode: t2v|i2v, prompt, image_url?, width, height, frames, fps, steps,
    guidance, seed}] (or a single run as prompt + settings, the original Account 2 shape). One MP4 artifact per run plus
    wan_benchmark.json. Refuses to run without CUDA: a CPU stand-in would be mislabelled as generated video."""
    import time as _t
    torch = _torch()
    if torch is None or not torch.cuda.is_available():
        raise RuntimeError("RESOURCE_EXHAUSTED: a2_wan_video needs a CUDA GPU; refusing to produce a CPU stand-in")
    os.environ.setdefault("HF_HOME", "/tmp/hf_home")      # weights never land in the job's output directory
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    _wan_ensure_diffusers(ctx)
    env = _wan_env(torch)
    ctx.log(f"wan env: {json.dumps(env)}")
    repo, rev = _WAN_REPOS[ctx.params["model"]]
    rev = ctx.params.get("revision") or rev
    runs = ctx.params.get("runs") or [{"mode": "t2v", "prompt": ctx.params["prompt"], **(ctx.params.get("settings") or {})}]
    done = dict(ctx.resume_state.get("done") or {})
    n = env["gpu_count"]
    dev_main = torch.device("cuda:0")
    dev_aux = torch.device(f"cuda:{n - 1}")
    tdtype = {"fp16": torch.float16, "bf16": torch.bfloat16}.get(ctx.params.get("dtype") or ("bf16" if env["bf16_native"] else "fp16"))
    te_mode = ctx.params.get("text_encoder") or ("gpu-bf16" if env["bf16_matmul_works"] and n > 1 else "cpu-fp32")

    import diffusers
    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline, WanPipeline, WanTransformer3DModel
    from transformers import AutoTokenizer, UMT5EncoderModel

    class _T2V(WanPipeline):
        @property
        def _execution_device(self):
            return dev_main

    class _I2V(WanImageToVideoPipeline):
        @property
        def _execution_device(self):
            return dev_main

    peak = _GpuPeak()
    peak.start()
    t0 = _t.time()
    from huggingface_hub import snapshot_download
    local = snapshot_download(repo, revision=rev, allow_patterns=["model_index.json", "scheduler/*", "text_encoder/*", "tokenizer/*",
                                                                  "transformer/*", "vae/*", "examples/*"])
    dl_s = _t.time() - t0
    ctx.log(f"snapshot {repo}@{rev} in {dl_s:.0f}s")
    t1 = _t.time()
    tok = AutoTokenizer.from_pretrained(local, subfolder="tokenizer")
    if te_mode == "gpu-bf16":
        te = UMT5EncoderModel.from_pretrained(local, subfolder="text_encoder", torch_dtype=torch.bfloat16, low_cpu_mem_usage=True).to(dev_aux)
    else:
        te = UMT5EncoderModel.from_pretrained(local, subfolder="text_encoder", torch_dtype=torch.float32, low_cpu_mem_usage=True)
    te.eval()
    tr = WanTransformer3DModel.from_pretrained(local, subfolder="transformer", torch_dtype=tdtype, low_cpu_mem_usage=True).to(dev_main)
    vae = AutoencoderKLWan.from_pretrained(local, subfolder="vae", torch_dtype=torch.float32).to(dev_main)
    load_s = _t.time() - t1
    ctx.log(f"loaded in {load_s:.0f}s: transformer {tdtype} on {dev_main}, text encoder {te_mode}, vae fp32")
    common = dict(tokenizer=tok, text_encoder=te, transformer=tr, vae=vae,
                  scheduler=diffusers.UniPCMultistepScheduler.from_pretrained(local, subfolder="scheduler"))
    t2v = _T2V(**common, expand_timesteps=True)
    i2v = _I2V(**common, image_encoder=None, image_processor=None, expand_timesteps=True)
    te_dev = dev_aux if te_mode == "gpu-bf16" else torch.device("cpu")
    embeds, te_s = {}, {}
    for k, r in enumerate(runs):
        if f"run{k}" in done:
            continue
        te_t = _t.time()
        with torch.no_grad():
            pe, ne = t2v.encode_prompt(r["prompt"], r.get("negative_prompt", _WAN_NEG), do_classifier_free_guidance=float(r.get("guidance", 5.0)) > 1.0,
                                       max_sequence_length=512, device=te_dev, dtype=te.dtype)
        embeds[k] = (pe.to("cpu"), ne.to("cpu") if ne is not None else None)
        te_s[k] = round(_t.time() - te_t, 1)
    # the text encoder is not needed again: free its ~11 GB so the VAE can decode untiled on the auxiliary GPU
    # (diffusers 0.35.2 Wan 2.2 VAE tiled_decode fails: avg_shortcut shape mismatch, measured on Kaggle 2026-11-13)
    t2v.text_encoder = i2v.text_encoder = None
    common.clear()          # the constructor kwargs held a second reference to the encoder (measured: 14.1 GB stayed on cuda:1)
    del te
    import gc
    gc.collect()
    torch.cuda.empty_cache()
    ctx.log(f"text encoder freed: cuda:{dev_aux.index} free {torch.cuda.mem_get_info(dev_aux)[0] / 2 ** 30:.1f} GB")
    results = []
    for k, r in enumerate(runs):
        key = f"run{k}"
        if key in done:
            results.append(done[key])
            ctx.log(f"{key}: already done (resumed)")
            continue
        mode = r.get("mode", "t2v")
        W, H, F = int(r.get("width", 1280)), int(r.get("height", 704)), int(r.get("frames", 121))
        steps, gs, seed, fps = int(r.get("steps", 50)), float(r.get("guidance", 5.0)), int(r.get("seed", 42)), int(r.get("fps", 24))
        for i in range(n):
            torch.cuda.reset_peak_memory_stats(i)
        pe, ne = embeds[k]
        pe, ne = pe.to(dev_main, tdtype), (ne.to(dev_main, tdtype) if ne is not None else None)
        vae.to(dev_main)
        g = torch.Generator(device=dev_main).manual_seed(seed)
        g_t = _t.time()
        step_times = []

        def _cb(pipe, i, t, kw, _st=step_times):
            _st.append(_t.time())
            return kw
        kw = dict(prompt_embeds=pe, negative_prompt_embeds=ne, height=H, width=W, num_frames=F, num_inference_steps=steps,
                  guidance_scale=gs, generator=g, output_type="latent", callback_on_step_end=_cb)
        image_info = None
        if mode == "i2v":
            from diffusers.utils import load_image
            src = r.get("image_url") or str(Path(local) / "examples" / "i2v_input.JPG")
            img = load_image(src).convert("RGB")
            image_info = {"source": r.get("image_url") or f"{repo}@{rev}:examples/i2v_input.JPG", "size": list(img.size)}
            with torch.no_grad():
                lat = i2v(image=img, **kw).frames
        else:
            with torch.no_grad():
                lat = t2v(**kw).frames
        gen_s = _t.time() - g_t
        if not torch.isfinite(lat).all():
            raise RuntimeError(f"{key}: non-finite latents with transformer dtype {tdtype} (precision overflow); not exported")
        # decode on the auxiliary GPU (FP32, tiled) so the transformer can stay resident on cuda:0
        d_t = _t.time()
        vae.to(dev_aux)
        free_aux = torch.cuda.mem_get_info(dev_aux)[0] / 2 ** 30
        with torch.no_grad():
            z = lat.to(dev_aux, torch.float32)
            mean = torch.tensor(vae.config.latents_mean).view(1, vae.config.z_dim, 1, 1, 1).to(z)
            inv_std = 1.0 / torch.tensor(vae.config.latents_std).view(1, vae.config.z_dim, 1, 1, 1).to(z)
            video = vae.decode(z / inv_std + mean, return_dict=False)[0]
            frames = t2v.video_processor.postprocess_video(video, output_type="np")[0]
        dec_s = _t.time() - d_t
        frames = [(f * 255).round().clip(0, 255).astype("uint8") for f in frames]
        q = _wan_quality(frames)
        if q["black"] or q["flat"]:
            raise RuntimeError(f"{key}: decoded video is black/flat {q}; not exported")
        out = ctx.work / f"wan22_5b_{mode}_{W}x{H}_{F}f_{key}.mp4"
        _wan_write_mp4(frames, out, fps)
        probe = ffprobe(out)
        vram = [{"index": i, "peak_allocated_mb": round(torch.cuda.max_memory_allocated(i) / 2 ** 20, 1),
                 "peak_reserved_mb": round(torch.cuda.max_memory_reserved(i) / 2 ** 20, 1)} for i in range(n)]
        sp = [b - a for a, b in zip([g_t] + step_times[:-1], step_times)]
        res = {"run": key, "mode": mode, "prompt": r["prompt"], "image": image_info, "width": W, "height": H, "frames": F, "fps": fps,
               "steps": steps, "guidance": gs, "seed": seed, "scheduler": "UniPCMultistepScheduler",
               "text_encode_s": te_s.get(k), "generate_s": round(gen_s, 1), "decode_s": round(dec_s, 1),
               "s_per_step_mean": round(sum(sp) / len(sp), 2) if sp else None, "aux_free_gb_before_decode": round(free_aux, 2), "vram": vram, "quality": q, "ffprobe": probe}
        a = ctx.artifact(out, "generated-video", model=ctx.params["model"], repo=repo, revision=rev, mode=mode,
                         gpu=env["gpus"][0]["name"], prompt=r["prompt"], seed=seed, steps=steps)
        res["artifact"] = {"name": a["name"], "sha256": a["sha256"], "size": a["size"]}
        results.append(res)
        done[key] = res
        ctx.checkpoint(f"wan-{key}", {"done": done})
        ctx.log(f"{key}: {mode} {W}x{H}x{F} {steps} steps gen {gen_s:.0f}s decode {dec_s:.0f}s motion {q['mean_abs_frame_diff']}")
        vae.to(dev_main)
        torch.cuda.empty_cache()
    peak.halt.set()
    peak.join(timeout=3)
    bench = {"model": ctx.params["model"], "repo": repo, "revision": rev, "licence": "apache-2.0", "diffusers_pinned": _WAN_DIFFUSERS,
             "env": env, "strategy": {"transformer": f"{tdtype} on {dev_main}", "text_encoder": te_mode + (f" on {dev_aux}" if te_mode == "gpu-bf16" else ""),
                                      "vae": f"fp32 encode on {dev_main}, untiled fp32 decode on {dev_aux} (text encoder freed after encoding)", "cpu_offload": False},
             "download_s": round(dl_s, 1), "load_s": round(load_s, 1), "runs": results, "per_gpu": peak.summary(n)}
    ctx.write_json_artifact("wan_benchmark.json", bench)
    return {"model": ctx.params["model"], "gpu": env["gpus"][0]["name"], "gpu_count": n, "load_s": round(load_s, 1),
            "download_s": round(dl_s, 1), "runs_ok": len(results), "per_gpu": peak.summary(n)}
