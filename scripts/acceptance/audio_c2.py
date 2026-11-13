"""Acceptance for Account 2 / Agent C audio work -> docs/results/a2_audio_acceptance.json (+ small spectrogram PNGs).

    python scripts/acceptance/audio_c2.py [--ids interview,panel,podcast,walktalk,ad] [--seconds 0 (= whole file)]

1. Dialogue chain v2 over each benchmark file (whole file by default): before/after measurement table +
   AudioMasterManifest; 30 s before/after excerpts (WAV + MP4 with the original picture) under /tmp/a2-c/accept
   (media is never committed); 480x200 spectrogram PNGs of the excerpts under docs/results/a2_audio/.
2. Sound design on the interview excerpt: procedural music bed, beat grid, cut-to-beat offsets, speech-density ducking
   rendered as volume keyframes and measured (music level under speech vs in pauses), density-capped SFX plan.
3. Audio QA on a real render: a 4-clip edit of the interview through the renderer, A/V sync per clip against the
   source, loudness, jumps, gaps.
Objective measurements on a shared 4-vCPU container. HUMAN LISTENING IS STILL REQUIRED.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BENCH = Path("/home/user/new-project-money/.nexus/benchmarks")
OUT = Path("/tmp/a2-c/accept")
SR = 48000
KEYS = ("lufs", "lra", "true_peak", "noise_floor_db", "noise_rel_lufs_db", "speech_band_snr_db", "clipped_samples",
        "dc_offset_db", "channel_imbalance_db", "speaker_spread_db", "hum_prominence_db", "bandwidth_hz")


def mux(video_src: Path, start: float, dur: float, wav: Path, out: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start}", "-t", f"{dur}", "-i", str(video_src), "-i", str(wav),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac",
                    "-b:a", "160k", "-shortest", str(out)], check=True)


def dialogue_part(ids, seconds, ex_start, ex_len, png_dir):
    from nexus.audio import design as S
    from nexus.audio import dialogue as G
    from nexus.audio import measure as M
    rows = {}
    for i in ids:
        src = BENCH / i / "source.mp4"
        x = M.decode(src, SR, 2, None, seconds or None)
        t0 = time.time()
        y, rep = G.process(x, SR, target_lufs=-16.0)
        el = time.time() - t0
        out_wav = OUT / f"{i}_full_after.wav"
        M.write_wav(out_wav, y, SR)
        man = G.manifest(src, out_wav, rep)
        a, b = int(ex_start * SR), int((ex_start + ex_len) * SR)
        bw, aw = OUT / f"{i}_before.wav", OUT / f"{i}_after.wav"
        M.write_wav(bw, x[a:b], SR)
        M.write_wav(aw, y[a:b], SR)
        mux(src, ex_start, ex_len, bw, OUT / f"{i}_before.mp4")
        mux(src, ex_start, ex_len, aw, OUT / f"{i}_after.mp4")
        S.spectrogram_png(x[a:b], SR, png_dir / f"{i}_before.png")
        S.spectrogram_png(y[a:b], SR, png_dir / f"{i}_after.png")
        rows[i] = {"seconds": round(len(x) / SR, 1), "processing_s": round(el, 1), "rtf": round(el / (len(x) / SR), 3),
                   "material": rep["material"], "before": {k: rep["before"].get(k) for k in KEYS},
                   "after": {k: rep["after"].get(k) for k in KEYS},
                   "steps_applied": [c["step"] for c in rep["chain"] if c["applied"]], "manifest": man,
                   "excerpt": {"start": ex_start, "seconds": ex_len, "files": [str(p) for p in (bw, aw, OUT / f"{i}_before.mp4", OUT / f"{i}_after.mp4")]},
                   "spectrograms": [f"docs/results/a2_audio/{i}_before.png", f"docs/results/a2_audio/{i}_after.png"]}
        print(i, rep["material"], rows[i]["steps_applied"], {k: (rows[i]["before"][k], rows[i]["after"][k]) for k in KEYS[:6]}, flush=True)
    return rows


def design_part(ex_start, ex_len):
    from nexus.audio import design as S
    from nexus.audio import measure as M
    from nexus.audio import music
    from nexus.audio.denoise import ffilter
    src = BENCH / "interview" / "source.mp4"
    x = M.decode(src, SR, 2, ex_start, ex_len)
    segs = M.active_segments(x, SR, min_len=0.3)
    bed = M.decode(music.music_bed(ex_len + 2, "inspiring", seed=3), SR, 2)[: len(x)]
    grid = S.beat_grid(bed, SR)
    cuts = [4.1, 9.7, 15.2, 21.9]
    snaps = S.snap_cuts(cuts, grid["beats"], 0.25, protect=[(s["start"], s["end"]) for s in segs])
    duck = S.ducking(segs, ex_len, base_db=-12.0)
    ducked = ffilter(bed, SR, f"volume='{S.volume_expr(duck['volume_keyframes'])}':eval=frame")
    sp_mask = np.zeros(len(x), bool)
    for s in segs:
        sp_mask[int(s["start"] * SR):int(s["end"] * SR)] = True
    lv = lambda m: float(20 * np.log10(np.sqrt(np.mean(ducked[m] ** 2)) + 1e-12)) if m.any() else None
    under, pause = lv(sp_mask), lv(~sp_mask)
    plan = S.sfx_plan([{"t": c, "kind": "transition"} for c in cuts] + [{"t": 1.0, "kind": "title", "x": 0.25}], "corporate", ex_len, segs)
    items = S.render_sfx_items(plan["placed"])
    q = {n: S.sfx_quality(n)["passed"] for n in ("whoosh", "impact", "riser", "pop", "click", "swell")}
    mix = ducked + x * 0.0
    for it in items:
        y = M.decode(it["src"], SR, 2)
        a = int(it["start"] * SR)
        mix[a:a + len(y)] += (y * it["volume"])[: len(mix) - a]
    mix = mix + x
    M.write_wav(OUT / "interview_design_mix.wav", mix / max(1.0, float(np.abs(mix).max()) / 0.9), SR)
    return {"music_bed": "procedural 'inspiring' (84 BPM pulse)", "beat_grid": {k: grid[k] for k in ("bpm", "confidence")},
            "cut_to_beat": snaps, "speech_density": duck["density"], "duck": {k: duck[k] for k in ("depth_db", "attack_s", "release_s", "hold_s", "spans")},
            "music_level_under_speech_db": round(under, 2) if under is not None else None,
            "music_level_in_pauses_db": round(pause, 2) if pause is not None else None,
            "sfx_plan": {"cap": plan["cap"], "placed": plan["placed"], "dropped": plan["dropped"]},
            "sfx_quality_pass": q, "mix_file": str(OUT / "interview_design_mix.wav")}


def qa_part():
    from nexus import config
    from nexus import project as prj
    from nexus.audio import qa as AQ
    from nexus.qa_continuity import _cut_times
    from nexus.timeline import ops, render
    src = str(BENCH / "interview" / "source.mp4")
    tl = prj.new_timeline(640, 360)
    tl["clips"] = [ops.video_clip(src, 61.0, 66.0), ops.video_clip(src, 80.0, 86.0), ops.video_clip(src, 120.0, 124.5),
                   ops.video_clip(src, 150.0, 156.0)]
    out = OUT / "qa_render.mp4"
    work = OUT / "qa_work"
    config.ensure_dirs()
    r = render.render(tl, out, work)
    rv = AQ.audio_review(out, tl, _cut_times(tl), target_lufs=float(tl.get("target_lufs", -14.0)))
    return {"render_s": r.get("elapsed"), "clips": len(tl["clips"]), "checks": rv["checks"], "sync": rv["measurements"].get("sync"),
            "loudness": rv["measurements"].get("loudness"), "passed": rv["passed"], "output": str(out)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="interview,panel,podcast,walktalk,ad")
    ap.add_argument("--seconds", type=float, default=0.0)
    ap.add_argument("--excerpt-start", type=float, default=60.0)
    ap.add_argument("--excerpt-s", type=float, default=30.0)
    ap.add_argument("--parts", default="dialogue,design,qa")
    ap.add_argument("--out", default=str(ROOT / "docs" / "results" / "a2_audio_acceptance.json"))
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    png_dir = ROOT / "docs" / "results" / "a2_audio"
    png_dir.mkdir(parents=True, exist_ok=True)
    out = Path(a.out)
    doc = json.loads(out.read_text()) if out.exists() else {}
    doc.update({"what": "Agent C audio acceptance: dialogue chain v2, sound design, audio QA on real benchmark media",
                "date": time.strftime("%Y-%m-%d"), "machine": "shared 4-vCPU container, no GPU",
                "human_listening": "REQUIRED - nobody listened; every number here is an objective measurement",
                "media_location": str(OUT) + " (not committed)"})
    parts = set(a.parts.split(","))
    if "dialogue" in parts:
        doc["dialogue"] = dialogue_part(a.ids.split(","), a.seconds, a.excerpt_start, a.excerpt_s, png_dir)
    if "design" in parts:
        doc["design"] = design_part(a.excerpt_start, a.excerpt_s)
        print("design", {k: doc["design"][k] for k in ("beat_grid", "duck", "music_level_under_speech_db", "music_level_in_pauses_db")}, flush=True)
    if "qa" in parts:
        doc["qa"] = qa_part()
        print("qa", doc["qa"]["sync"], doc["qa"]["passed"], flush=True)
    out.write_text(json.dumps(doc, indent=1, default=str) + "\n")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
