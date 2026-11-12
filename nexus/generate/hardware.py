"""What this machine can run: CPU, RAM, disk, GPUs and VRAM, detected at runtime (never read from configuration).

GPU detection does not need torch: `nvidia-smi` (NVIDIA), `rocm-smi` (AMD) and `vulkaninfo --summary` (any Vulkan device,
used by ncnn-vulkan builds) are asked in that order, each with a short timeout. The torch probe of the optional generation
environment (`nexus.generate.detect()`) is merged in when available so a CUDA build that sees the GPU is reported too.

    detect() -> {"cpu_count", "ram_gb", "ram_free_gb", "disk_free_gb", "gpus": [{"name","vram_gb","vram_free_gb","vendor",
                 "source"}], "best_vram_gb", "cuda": bool, "vulkan": bool, "tier": "cpu"|"vram_6_8"|"vram_12_16"|"vram_24"}
    satisfies(requirements, hw) -> (ok, [reasons])   requirements = RenderJobManifest.requirements
"""
from __future__ import annotations

import os
import shutil
import subprocess

from .. import config


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def nvidia_gpus(text: str | None = None) -> list[dict]:
    """Parse `nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader,nounits` (MiB)."""
    text = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"]) if text is None else text
    out = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                out.append({"name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 1),
                            "vram_free_gb": round(float(parts[2]) / 1024, 1), "vendor": "nvidia", "source": "nvidia-smi"})
            except ValueError:
                continue
    return out


def vulkan_devices(text: str | None = None) -> list[dict]:
    """Device names from `vulkaninfo --summary`; CPU (llvmpipe/lavapipe) devices are flagged as such."""
    text = _run(["vulkaninfo", "--summary"]) if text is None else text
    out = []
    for line in text.splitlines():
        if "deviceName" in line and "=" in line:
            name = line.split("=", 1)[1].strip()
            out.append({"name": name, "cpu_device": any(k in name.lower() for k in ("llvmpipe", "lavapipe", "swiftshader"))})
    return out


def _ram() -> tuple[float | None, float | None]:
    try:
        import psutil
        vm = psutil.virtual_memory()
        return round(vm.total / 2**30, 1), round(vm.available / 2**30, 1)
    except Exception:  # noqa: BLE001
        return None, None


def tier_for(best_vram_gb: float | None) -> str:
    v = best_vram_gb or 0
    if v >= 24:
        return "vram_24"
    if v >= 12:
        return "vram_12_16"
    if v >= 6:
        return "vram_6_8"
    return "cpu"


def detect(torch_probe: dict | None = None) -> dict:
    ram, ram_free = _ram()
    home = config.HOME if config.HOME.exists() else config.ROOT
    gpus = nvidia_gpus()
    if not gpus and torch_probe and torch_probe.get("cuda") and torch_probe.get("vram_gb"):
        gpus = [{"name": "CUDA device", "vram_gb": torch_probe["vram_gb"], "vram_free_gb": None, "vendor": "nvidia",
                 "source": "torch"}]
    vk = vulkan_devices()
    best = max((g["vram_gb"] for g in gpus if g.get("vram_gb")), default=None)
    return {"cpu_count": os.cpu_count(), "ram_gb": ram, "ram_free_gb": ram_free,
            "disk_free_gb": round(shutil.disk_usage(home).free / 2**30, 1), "gpus": gpus, "best_vram_gb": best,
            "cuda": bool(gpus) and any(g["vendor"] == "nvidia" for g in gpus), "vulkan": [d for d in vk if not d["cpu_device"]] != [],
            "vulkan_devices": vk, "tier": tier_for(best)}


def satisfies(req: dict, hw: dict) -> tuple[bool, list[str]]:
    """Can a job with these RenderJobManifest.requirements run on this hardware? Reasons name every shortfall."""
    why = []
    if req.get("gpu") and not hw.get("gpus"):
        why.append("needs a GPU; none detected (nvidia-smi / torch CUDA)")
    need_v = req.get("vram_gb")
    if need_v and (hw.get("best_vram_gb") or 0) < float(need_v):
        why.append(f"needs {need_v} GB VRAM; best device has {hw.get('best_vram_gb') or 0} GB")
    need_r = req.get("ram_gb")
    if need_r and hw.get("ram_gb") is not None and hw["ram_gb"] < float(need_r):
        why.append(f"needs {need_r} GB RAM; machine has {hw['ram_gb']} GB")
    need_d = req.get("disk_gb")
    if need_d and hw.get("disk_free_gb") is not None and hw["disk_free_gb"] < float(need_d):
        why.append(f"needs {need_d} GB free disk; {hw['disk_free_gb']} GB free")
    return (not why), why
