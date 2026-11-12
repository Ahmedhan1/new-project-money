"""Dispatcher-side task registry: what each worker task needs, so the dispatcher can route it.

Accounts 2/3: add a worker implementation under nexus/cloud/worker_kit/tasks/ and a TaskSpec here (see that package's
docstring). `gpu`: "required" (never runs on a CPU worker), "preferred" (GPU provider first, CPU fallback allowed),
"no" (CPU is enough: the local worker is preferred and Kaggle GPU is not spent on it).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TaskSpec:
    gpu: str = "no"                       # required | preferred | no
    min_vram_gb: float = 0.0
    internet: bool = False                # needs to download inputs/models on the worker
    resumable: bool = False               # writes checkpoints and skips finished steps when resumed
    description: str = ""
    providers: tuple[str, ...] = field(default_factory=tuple)  # restrict to these provider types (empty = any)


TASKS: dict[str, TaskSpec] = {
    "probe": TaskSpec(gpu="preferred", description="hardware inventory + matmul benchmark + NVENC/internet check"),
    "torch_bench": TaskSpec(gpu="preferred", description="PyTorch matmul throughput (fp32/fp16)"),
    "ffmpeg_transcode": TaskSpec(gpu="preferred", description="input -> H.264 MP4 (NVENC when available, else libx264)"),
    "chunked_transcode": TaskSpec(gpu="preferred", resumable=True,
                                  description="N time-chunks -> H.264, checkpoint after each chunk; resumes skipping finished chunks"),
}


def spec(task: str) -> TaskSpec | None:
    return TASKS.get(task)


def register(name: str, s: TaskSpec) -> None:
    TASKS[name] = s
