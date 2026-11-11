"""Content Factory: honest capacity planning from MEASURED throughput, and a daily production schedule."""
from __future__ import annotations

import time

from . import compute, db

# Fallbacks are only used when a stage has never been measured on this machine; they are flagged in the output.
DEFAULTS = {"render_rtf": 0.9, "tts_rtf": 0.12, "asr_rtf": 0.45, "llm_tok_per_s": 6.5, "mt_sec_per_line": 1.6,
            "blender_sec_per_frame": 3.2, "manim_seconds": 9.0}

# What one master video of each profile needs. Durations in seconds of finished video.
PROFILES = {
    "narrated-explainer": {"label": "Narrated explainer (licensed stills, Ken Burns, captions, music)", "duration": 75, "scenes": 6,
                           "llm_tokens": 1100, "script_attempts": 1.6, "asset_fetch_s": 150, "mograph_s": 0, "manim_clips": 0, "blender_frames": 0,
                           "gpu_minutes": 0, "quality": "good for education/news-style channels; visual variety limited to stills + graphics"},
    "animated-explainer": {"label": "Animated explainer (Manim diagrams + data graphics + narration)", "duration": 90, "scenes": 6,
                           "llm_tokens": 1300, "script_attempts": 1.6, "asset_fetch_s": 60, "mograph_s": 10, "manim_clips": 3, "blender_frames": 0,
                           "gpu_minutes": 0, "quality": "strong for science/finance/tech topics"},
    "edit-or-remix": {"label": "Edit / remix of 10 min of supplied footage", "duration": 180, "scenes": 0, "source_minutes": 10,
                      "llm_tokens": 400, "script_attempts": 1.0, "asset_fetch_s": 0, "mograph_s": 0, "manim_clips": 0, "blender_frames": 0,
                      "gpu_minutes": 0, "quality": "depends on the footage; highest realism per CPU-minute"},
    "3d-showcase": {"label": "30 s of 3D animation (Cycles CPU, 720p) + narration", "duration": 45, "scenes": 3, "llm_tokens": 600,
                    "script_attempts": 1.3, "asset_fetch_s": 0, "mograph_s": 0, "manim_clips": 0, "blender_frames": 720, "blender_scale": 4.0,
                    "gpu_minutes": 0, "quality": "720p frames cost ~4x the measured 360p test; noisy below ~64 samples"},
    "generative-cinematic": {"label": "Fully AI-generated video scenes (open-weight T2V on a free GPU session)", "duration": 60, "scenes": 12,
                             "llm_tokens": 1100, "script_attempts": 1.6, "asset_fetch_s": 0, "mograph_s": 0, "manim_clips": 0, "blender_frames": 0,
                             "gpu_minutes": 72, "quality": "UNMEASURED ESTIMATE: ~6 GPU-min per 5 s 480p clip on a T4 class GPU; no model deployed yet"},
}


def measured() -> dict:
    out = {}
    for k, d in DEFAULTS.items():
        v = db.metric_avg(k)
        out[k] = {"value": round(v if v is not None else d, 3), "measured": v is not None}
    return out


def cost_per_master(profile: str, m: dict | None = None) -> dict:
    """CPU-seconds per stage for one master video, plus the per-language add-on."""
    m = m or measured()
    v = lambda k: m[k]["value"]
    p = PROFILES[profile]
    d = p["duration"]
    stages = {
        "research+script (LLM)": p["llm_tokens"] * p["script_attempts"] / v("llm_tok_per_s"),
        "asset search/download": p["asset_fetch_s"],
        "narration (TTS)": d * 0.85 * v("tts_rtf"),
        "source analysis (ASR + detection)": p.get("source_minutes", 0) * 60 * (v("asr_rtf") + 0.25),
        "2D animation (Manim)": p["manim_clips"] * v("manim_seconds") * 2.2,  # 720p30 is ~2.2x the measured clip
        "motion graphics": p["mograph_s"],
        "3D rendering (Blender Cycles CPU)": p["blender_frames"] * v("blender_sec_per_frame") * p.get("blender_scale", 1.0),
        "render + mix": d * v("render_rtf") * 1.3,  # 1080p costs more than the measured 720p
        "automated QA (decode, loudness, detectors)": d * 0.35 + 10,
    }
    lang = {
        "translation (MADLAD)": (p["scenes"] or 12) * 2 * v("mt_sec_per_line"),
        "back-translation check (LLM)": (p["scenes"] or 12) * 45 / v("llm_tok_per_s") * 1.0,
        "dub (TTS)": d * 0.9 * v("tts_rtf"),
        "dub verification (ASR)": d * v("asr_rtf"),
        "render + mix": d * v("render_rtf") * 1.3 * 0.6,  # video segments are reused from cache; audio/captions change
        "automated QA": d * 0.35 + 10,
    }
    platform = d * v("render_rtf") * 0.8 + 8  # one reframed export + thumbnail + metadata
    return {"profile": profile, "label": p["label"], "duration": d, "stages": {k: round(s) for k, s in stages.items() if s},
            "master_cpu_s": round(sum(stages.values())), "per_language_cpu_s": round(sum(lang.values())), "language_stages": {k: round(s) for k, s in lang.items()},
            "per_platform_cpu_s": round(platform), "gpu_minutes": p["gpu_minutes"], "quality_note": p["quality"]}


def capacity(target_masters: int = 5, languages: int = 2, platforms: int = 4, hours_per_day: float = 16.0, machine_factor: float = 1.25,
             free_gpu_hours_per_week: float = 0.0, mix: dict[str, int] | None = None, human_review_min_per_video: float = 6.0) -> dict:
    """Can this machine deliver `target_masters` per day?

    machine_factor: how much slower the production machine is than the machine the metrics were measured on
    (1.25 is our assumption for an HP ZBook G3 i7-6820HQ vs this 4-vCPU test container - UNVERIFIED until measured on the ZBook).
    languages = extra language versions per master (e.g. 2 = Arabic + German next to the English master).
    """
    m = measured()
    budget = hours_per_day * 3600
    rows = []
    for prof in PROFILES:
        c = cost_per_master(prof, m)
        per = (c["master_cpu_s"] + languages * c["per_language_cpu_s"] + platforms * (1 + languages) * c["per_platform_cpu_s"]) * machine_factor
        by_cpu = budget / per
        gpu_needed_h = c["gpu_minutes"] / 60
        by_gpu = (free_gpu_hours_per_week / 7 / gpu_needed_h) if gpu_needed_h else float("inf")
        n = int(min(by_cpu, by_gpu))
        bottleneck = max(c["stages"], key=c["stages"].get)
        if by_gpu < by_cpu:
            bottleneck = f"free GPU quota ({gpu_needed_h:.1f} GPU-h per video vs {free_gpu_hours_per_week / 7:.1f} GPU-h/day available)"
        rows.append({"profile": prof, "label": c["label"], "minutes_per_master_all_versions": round(per / 60, 1), "max_per_day": n,
                     "meets_target": n >= target_masters, "bottleneck": bottleneck, "gpu_hours_per_video": round(gpu_needed_h, 2),
                     "quality_note": c["quality_note"], "detail": c})
    outputs_per_master = (1 + languages) * platforms
    review_h = target_masters * (1 + languages) * human_review_min_per_video / 60
    plan_mix = mix or {"narrated-explainer": 2, "animated-explainer": 2, "edit-or-remix": 1}
    used = sum(next(r for r in rows if r["profile"] == k)["minutes_per_master_all_versions"] * n for k, n in plan_mix.items())
    unmeasured = [k for k, x in m.items() if not x["measured"]]
    return {"target_masters_per_day": target_masters, "assumptions": {"working_hours_per_day": hours_per_day, "machine_factor": machine_factor,
            "extra_languages": languages, "platform_exports_per_language": platforms, "free_gpu_hours_per_week": free_gpu_hours_per_week,
            "one_heavy_job_at_a_time": True, "metrics_not_yet_measured_on_this_machine": unmeasured},
            "throughput_metrics": m, "profiles": rows, "files_per_master": outputs_per_master,
            "recommended_mix": {"mix": plan_mix, "machine_hours": round(used / 60, 1), "fits_in_day": used / 60 <= hours_per_day,
                                "total_files_per_day": sum(plan_mix.values()) * outputs_per_master},
            "human_review_hours_per_day": round(review_h, 1),
            "verdict": _verdict(rows, target_masters, plan_mix, used / 60, hours_per_day, review_h)}


def _verdict(rows, target, mix, used_h, hours, review_h) -> list[str]:
    by = {r["profile"]: r for r in rows}
    v = []
    ok = [r["profile"] for r in rows if r["meets_target"]]
    v.append(f"{target}/day is {'ACHIEVABLE' if ok else 'NOT achievable'} on CPU alone for: {', '.join(ok) or 'no profile'}.")
    no = [f"{r['profile']} (max {r['max_per_day']}/day; bottleneck: {r['bottleneck']})" for r in rows if not r["meets_target"]]
    if no:
        v.append("NOT achievable at this volume: " + "; ".join(no) + ".")
    v.append(f"Recommended daily mix {mix} needs ~{used_h:.1f} machine-hours of the {hours:.0f} available.")
    v.append(f"Human review is the real limit on quality: ~{review_h:.1f} h/day to watch every language version before publishing. "
             "Automated QA catches technical faults, not weak storytelling or mistranslations.")
    v.append(f"'Premium cinematic' fully generated footage is not available at 5/day: {by['generative-cinematic']['quality_note']}.")
    return v


def schedule(target_masters: int = 5, languages: list[str] | None = None, platforms: list[str] | None = None, topics: list[str] | None = None,
             mix: dict[str, int] | None = None, start_hour: float = 8.0, **cap_kw) -> dict:
    """Build today's production plan: one heavy job at a time on the local worker, with predicted start/finish times."""
    languages = languages if languages is not None else ["ar", "de"]
    platforms = platforms or ["youtube", "shorts", "tiktok", "instagram-reels"]
    cap = capacity(target_masters, len(languages), len(platforms), mix=mix, **cap_kw)
    fed = compute.Federation()
    worker, reasons = fed.route("encode")
    by = {r["profile"]: r for r in cap["profiles"]}
    queue = [p for p, n in cap["recommended_mix"]["mix"].items() for _ in range(n)][:target_masters]
    t, jobs = start_hour * 60, []
    day_end = (start_hour + cap["assumptions"]["working_hours_per_day"]) * 60
    for i, prof in enumerate(queue):
        dur = by[prof]["minutes_per_master_all_versions"]
        status = "scheduled" if t + dur <= day_end else "deferred to tomorrow (does not fit in today's hours)"
        jobs.append({"slot": i + 1, "topic": (topics or [])[i] if topics and i < len(topics) else f"topic {i + 1} (from research queue)",
                     "profile": prof, "worker": worker.id if worker else None, "start": _hhmm(t), "finish": _hhmm(t + dur), "minutes": dur,
                     "versions": ["en"] + languages, "platforms": platforms, "status": status,
                     "quality_gates": ["script review", "asset licence review", "technical QA", "language QA", "HUMAN APPROVAL"]})
        t += dur
    plan = {"date": time.strftime("%Y-%m-%d"), "target": target_masters, "scheduled": sum(j["status"] == "scheduled" for j in jobs),
            "jobs": jobs, "predicted_finish": _hhmm(t), "workers": fed.registry(), "capacity": cap,
            "realistic": cap["recommended_mix"]["fits_in_day"] and len(queue) >= target_masters}
    db.put_doc("schedule", plan, plan["date"])
    return plan


def _hhmm(minutes: float) -> str:
    return f"{int(minutes // 60) % 24:02d}:{int(minutes % 60):02d}" + (" (+1d)" if minutes >= 24 * 60 else "")


def dashboard() -> dict:
    """Numbers for the cost/quality/efficiency dashboard."""
    from . import costguard
    jobs = db.query("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
    qa_rows = db.query("SELECT AVG(value) rate, COUNT(*) n FROM metrics WHERE name='qa_pass'")
    ffs = db.query("SELECT COALESCE(SUM(value),0) s FROM metrics WHERE name='ffmpeg_seconds'")[0]["s"]
    hw = compute.detect_hardware()
    return {"spend": costguard.spend_report(), "jobs": {j["status"]: j["n"] for j in jobs},
            "projects": db.query("SELECT COUNT(*) n FROM projects")[0]["n"],
            "qa": {"renders_reviewed": qa_rows[0]["n"], "first_pass_rate": round(qa_rows[0]["rate"] or 0, 2)},
            "cpu_seconds_in_ffmpeg": round(ffs), "gpu_seconds": 0, "hardware": hw, "throughput": measured()}
