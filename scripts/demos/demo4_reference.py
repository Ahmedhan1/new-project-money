"""Account 2 Demo 4: reference-style adaptation. A public-domain reference's STRUCTURE is measured, a new piece is cut
from a DIFFERENT public-domain source with that structure, and the rendered piece is measured with the same analyser so
the two profiles can be compared number by number. Nothing of the reference (frames, words, sound) is used.

Run:  NEXUS_HOME=<dir> python scripts/demos/demo4_reference.py [reference id, default explainer] [source id, default walktalk]
Writes docs/results/a2_demos/demo4_reference.json + demo4_contact.jpg.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nexus import config, director, project, qa, quality, reference  # noqa: E402
from nexus.audio import measure  # noqa: E402

B = Path(os.environ.get("NEXUS_BENCH", ROOT / ".nexus" / "benchmarks"))
AN = Path(os.environ.get("NEXUS_ANALYSES", ROOT / ".nexus" / "analyses"))
OUT = ROOT / "docs" / "results" / "a2_demos"


def remap(d, m: dict):
    if isinstance(d, dict):
        return {k: (m.get(v, v) if k == "src" and isinstance(v, str) else remap(v, m)) for k, v in d.items()}
    if isinstance(d, list):
        return [remap(x, m) for x in d]
    return d


def flat(m: dict) -> dict:
    """The comparable numbers of a ReferenceStyleProfile."""
    g = lambda *ks: (lambda d: d)(_dig(m, ks))  # noqa: E731
    return {"shot_length_median_s": g("shot_length", "median"), "shot_length_mean_s": g("shot_length", "mean"),
            "cuts_per_min": g("cuts_per_min"), "first_10s_cuts": g("first_10s", "cuts"), "first_speech_s": g("hook", "first_speech_s"),
            "hook_t_s": g("hook", "t"), "pattern_interrupt_median_s": g("pattern_interrupt_s", "median"),
            "cutaways_per_min": g("cutaways_per_min"), "on_screen_text_share": g("on_screen_text_share"),
            "speech_share": g("speech_share"), "music_bed_share": g("music_bed_share"), "audio_accents_per_min": g("audio_accents_per_min"),
            "wpm": g("wpm"), "ending": g("ending", "kind"), "saturation": g("colour", "saturation"), "warmth": g("colour", "warmth"),
            "colourfulness": g("colour", "colourfulness"), "luma_contrast": g("colour", "luma_contrast"),
            "hard_cut_share": g("transitions", "hard_cut_share"), "gradual_share": g("transitions", "gradual_share"),
            "fade_through_black_share": g("transitions", "fade_through_black_share"),
            "shot_type_changes_per_min": g("shot_type_changes_per_min"), "motion": g("visual_density", "motion")}


def _dig(d, ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def main() -> None:
    rid = sys.argv[1] if len(sys.argv) > 1 else "explainer"
    sid = sys.argv[2] if len(sys.argv) > 2 else "walktalk"
    config.ensure_dirs()
    rsrc, ssrc = str(B / rid / "source.mp4"), str(B / sid / "source.mp4")
    rprov, sprov = (json.loads((B / k / "provenance.json").read_text()) for k in (rid, sid))
    t0 = time.time()
    ref = reference.analyze(rsrc, json.loads((AN / f"{rid}.json").read_text()), name=f"{rid} (NASA, public domain)",
                            rights={"basis": "public-domain", "evidence": rprov.get("page")})
    ref_s = time.time() - t0
    ov = reference.to_overrides(ref)
    san = json.loads((AN / f"{sid}.json").read_text())
    target = float(os.environ.get("DEMO4_TARGET", 90))
    plan = reference.apply(ref, {ssrc: san}, {"profile": "educational_explainer", "target_seconds": target,
                                              "title": "Inside the Booster Processing Facility"})
    tl = director.build(plan, (1920, 1080))
    for c in tl["clips"]:
        if c["type"] == "video":
            c["audio_fx"] = "dialogue_v2_ffmpeg"
    if tl.get("music"):
        tl["music"]["duck"] = "keyframes"
    p = project.Project.create(f"Account 2 Demo 4 - {sid} with the structure of {rid}", "demo")
    m = p.import_media(ssrc)
    m["rights"] = {"basis": "public-domain", "holder": "NASA", "evidence": sprov.get("page")}
    p.save()
    tl = remap(tl, {ssrc: m["path"]})
    p.save_timeline(tl, f"demo 4: structure of {rid}")
    t0 = time.time()
    stats, rep = qa.render_with_qa(p, tl, "demo4_reference")
    render_s = time.time() - t0
    out = Path(stats["output"])
    # measure the OUTPUT with the same analyser (fresh analysis of the rendered file: whisper, scenes, faces, visual)
    from nexus.media import analyze
    t0 = time.time()
    oan = analyze.analyze(str(out))
    gen = reference.analyze(str(out), oan, name="demo 4 output", rights={"basis": "owned"})
    measure_s = time.time() - t0
    fr, fg = flat(ref["metrics"]), flat(gen["metrics"])
    rows = []
    for k in fr:
        a, b = fr[k], fg[k]
        d = round(b - a, 3) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        rel = round(abs(b - a) / abs(a), 3) if d is not None and a else None
        rows.append({"metric": k, "reference": a, "generated": b, "diff": d, "rel_diff": rel})
    qs = quality.score(out, tl)
    loud = measure.loudness(measure.decode(out, 48000), 48000)
    # contact sheet: 4 reference frames (top) vs 4 output frames (bottom), same relative positions
    frames = []
    for row, (path, dur) in enumerate(((rsrc, ref["metrics"]["duration"]), (str(out), gen["metrics"]["duration"]))):
        for i in range(4):
            f = OUT / f"_d4_{row}_{i}.png"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{dur * (i + 0.5) / 4:.2f}", "-i", path,
                            "-frames:v", "1", "-vf", "scale=384:216:force_original_aspect_ratio=decrease,pad=384:216:(ow-iw)/2:(oh-ih)/2", str(f)], check=True)
            frames.append(f)
    ins = sum([["-i", str(f)] for f in frames], [])
    lay = "|".join(f"{'+'.join(['w0'] * (i % 4)) or '0'}_{'+'.join(['h0'] * (i // 4)) or '0'}" for i in range(8))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *ins, "-filter_complex", f"xstack=inputs=8:layout={lay}",
                    "-q:v", "5", str(OUT / "demo4_contact.jpg")], check=True)
    for f in frames:
        f.unlink()
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(out)],
                                     capture_output=True, text=True).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    res = {"demo": "4 - reference style adaptation",
           "reference": {"benchmark": rid, "page": rprov.get("page"), "licence": "public domain (NASA media guidelines)", "measure_s": round(ref_s, 1)},
           "source": {"benchmark": sid, "page": sprov.get("page"), "licence": "public domain (NASA media guidelines)"},
           "applied_overrides": ov["profile_overrides"], "why": ov["why"], "project": p.id, "output": str(out),
           "duration": float(info["format"]["duration"]), "resolution": f"{v['width']}x{v['height']}", "fps": v["r_frame_rate"],
           "codec": v["codec_name"], "render_with_qa_s": round(render_s, 1), "output_measure_s": round(measure_s, 1),
           "qa_status": rep["status"], "known_issues": rep.get("known_issues"),
           "quality": {k: qs.get(k) for k in ("score", "verdict", "categories")}, "loudness": loud,
           "comparison": rows, "reference_profile": ref, "generated_profile": gen,
           "not_copied": "no frame, transcript line, title, music or sound of the reference is in the output; the output's words and "
                         "pictures all come from the source benchmark",
           "machine": "shared 4-vCPU container, no GPU", "human_review": "PENDING"}
    (OUT / "demo4_reference.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    for r in rows:
        print(f"{r['metric']:30s} ref={r['reference']!s:10s} gen={r['generated']!s:10s} diff={r['diff']}")
    print(json.dumps({k: res[k] for k in ("duration", "qa_status", "quality")}, indent=1, default=str))


if __name__ == "__main__":
    main()
