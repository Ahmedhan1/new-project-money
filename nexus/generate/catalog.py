"""Capability + licence view over nexus/registry/generation.yaml (one place that answers "may we use this model, for
what, and on which hardware").

    licence_class(m)        foss | restricted | research | excluded   (docs/ACCOUNT2_MEDIA_TOOL_CATALOG.md)
    allowed(m, accept=...)  raises LicenceRefused unless the class is accepted by the caller
    requirements(m, task)   {"gpu", "vram_gb", "ram_gb", "disk_gb", "models", "est_s"} for RenderJobManifest
    runs_here(m, hw)        (ok, reasons) - CPU feasibility from the registry + measured hardware
    choose(task, hw)        best FOSS model for this task on this hardware tier (registry `tiers`)
"""
from __future__ import annotations

from . import hardware, registry

CLASSES = ("foss", "restricted", "research", "excluded")
VIDEO_TASKS = {"text_to_video", "image_to_video", "video_to_video"}
# Conservative VRAM floor when the registry has no number: never claim a video model fits less than this.
DEFAULT_VRAM = {"text_to_video": 8.0, "image_to_video": 8.0, "video_to_video": 8.0}


class LicenceRefused(PermissionError):
    pass


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def model(mid: str) -> dict:
    m = next((x for x in registry()["models"] if x["id"] == mid), None)
    if m is None:
        raise KeyError(f"unknown generation model {mid}")
    return m


def licence_class(m: dict) -> str:
    cu = str(m.get("commercial_use", "unverified")).lower()
    why = str(m.get("why", "")).lower()
    if "territor" in why or ("territor" in str(m.get("conditions", "")).lower() and cu != "yes"):
        return "excluded"
    return {"yes": "foss", "restricted": "restricted", "no": "research"}.get(cu, "excluded")


def allowed(m: dict, accept: tuple[str, ...] = ("foss",)) -> dict:
    c = licence_class(m)
    if c not in accept:
        raise LicenceRefused(f"{m['name']}: weights licence class '{c}' ({m.get('weights_licence')}) is not accepted "
                             f"(accepted: {', '.join(accept)}); conditions: {m.get('conditions') or 'see licence'}")
    return {"class": c, "weights_licence": m.get("weights_licence"), "commercial_use": m.get("commercial_use"),
            "conditions": m.get("conditions"), "source_url": m.get("source_url"), "read_on": m.get("read_on")}


def cpu_feasible(m: dict) -> bool:
    return str((m.get("cpu") or {}).get("feasible", "no")).lower() in ("yes", "slow")


def requirements(m: dict, task: str) -> dict:
    gpu = task in VIDEO_TASKS or not cpu_feasible(m)
    vram = _num(m.get("min_vram_gb"))
    if gpu and not vram:
        vram = DEFAULT_VRAM.get(task, 6.0)
    dl = _num(m.get("download_gb"))
    return {"gpu": gpu, "vram_gb": vram if gpu else None, "ram_gb": _num(m.get("min_ram_gb")),
            "disk_gb": round(dl * 1.2, 2) if dl else None, "models": [m["id"]],
            "est_s": _num((m.get("cpu") or {}).get("seconds_per_output")) if not gpu else None,
            "vram_source": "registry" if _num(m.get("min_vram_gb")) else ("floor (unverified)" if gpu else None)}


def runs_here(m: dict, task: str, hw: dict | None = None) -> tuple[bool, list[str]]:
    hw = hw or hardware.detect()
    if task not in (m.get("tasks") or []):
        return False, [f"{m['id']} does not do {task}"]
    return hardware.satisfies(requirements(m, task), hw)


def choose(task: str, hw: dict | None = None, accept: tuple[str, ...] = ("foss",)) -> dict | None:
    """Registry tier recommendation for this hardware, falling back down the tiers; None when nothing fits."""
    hw = hw or hardware.detect()
    tiers = registry().get("tiers") or {}
    order = ["vram_24", "vram_12_16", "vram_6_8", "cpu"]
    start = order.index(hw.get("tier", "cpu")) if hw.get("tier", "cpu") in order else 3
    key = "video" if task in VIDEO_TASKS else ("image" if task == "image" else task)
    for t in order[start:]:
        mid = (tiers.get(t) or {}).get(key)
        if not mid:
            continue
        try:
            m = model(mid)
        except KeyError:
            continue
        if licence_class(m) in accept and runs_here(m, task, hw)[0]:
            return m
    return None


def table(task: str | None = None) -> list[dict]:
    """Flat view for the UI / API: every model with class, tasks, CPU feasibility and requirements."""
    rows = []
    for m in registry()["models"]:
        for t in m.get("tasks") or []:
            if task and t != task:
                continue
            rows.append({"id": m["id"], "name": m["name"], "task": t, "class": licence_class(m),
                         "cpu": (m.get("cpu") or {}).get("feasible"), "download_gb": m.get("download_gb"),
                         "requirements": requirements(m, t), "status": m.get("status")})
    return rows
