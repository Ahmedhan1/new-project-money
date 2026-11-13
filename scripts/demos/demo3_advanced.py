"""Account 2 Demo 3: advanced editing on licensed footage. Every section exercises a renderer capability, and the
rendered output is checked (technical QA + quality report) and inspected through labelled frames.

Run:  NEXUS_HOME=/tmp/a2-demo3 python scripts/demos/demo3_advanced.py
Writes docs/results/a2_demos/demo3_advanced.json + demo3_contact.jpg (small). The video stays in NEXUS_HOME.
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
from nexus import config, project, qa  # noqa: E402
from nexus.audio import music  # noqa: E402
from nexus.fx import sfx  # noqa: E402
from nexus.timeline import ops, render  # noqa: E402

B = Path(os.environ.get("NEXUS_BENCH", ROOT / ".nexus" / "benchmarks"))
IV, WT, BR, EX, DOC = (str(B / k / "source.mp4") for k in ("interview", "walktalk", "broll", "explainer", "documentary"))
OUT = ROOT / "docs" / "results" / "a2_demos"


def build() -> tuple[dict, list[tuple[str, str]]]:
    tl = project.new_timeline(1280, 720)
    tl.update({"grade": "none", "captions": {"style": "pop", "position": "bottom", "words_per_cue": 3, "highlight": "#FFD400"}})
    C = []  # (clip, label of the capability shown)
    C.append((ops.video_clip(BR, 20, 26, grade="teal_orange", effects=[{"type": "letterbox", "ratio": 2.39}, {"type": "vignette"}],
                             titles=[{"text": "Advanced editing", "style": "title", "start": 0.4, "end": 5.2, "anim": "rise"}], fade_in=0.6),
              "letterbox 2.39 + teal/orange grade + animated title"))
    C.append((ops.video_clip(BR, 60, 66, transition="crossfade", xfade="circleopen", transition_dur=0.8,
                             keyframes={"scale": [[0, 1.0], [6, 1.35, "ease"]], "x": [[0, 0.5], [6, 0.7, "ease"]]}),
              "circle-open transition + keyframed push-in/pan"))
    C.append((ops.video_clip(BR, 100, 104, speed=0.5, transition="crossfade", xfade="slideleft", transition_dur=0.5,
                             effects=[{"type": "grain", "amount": 10}]), "0.5x slow motion + film grain"))
    C.append((ops.video_clip(IV, 40, 47, transition="crossfade", xfade="fadeblack", transition_dur=0.6, audio_fx="dialogue_v2_ffmpeg",
                             keyframes={"opacity": [[0, 0.0], [1.0, 1.0]]},
                             # privacy-style blur INSIDE an ellipse that travels across the frame and settles on the face
                             mask={"shape": "ellipse", "x": 0.02, "y": 0.06, "w": 0.24, "h": 0.36, "effect": "blur", "amount": 0.9,
                                   "region": "inside", "keyframes": {"x": [[0, 0.02], [4.5, 0.38, "ease"]]}},
                             titles=[{"text": "Moving blur mask + opacity fade-in", "style": "lower_third", "start": 1.2, "end": 6.5, "anim": "slide"}]),
              "opacity keyframes + moving (keyframed) blur mask + dialogue chain"))
    C.append((ops.video_clip(WT, 30, 36, transition="crossfade", xfade="wipeleft", transition_dur=0.5,
                             split={"srcs": [{"src": BR, "in": 140}, {"src": EX, "in": 60}], "layout": "thirds_h", "gap": 6}),
              "three-way split screen (thirds)"))
    C.append((ops.video_clip(EX, 70, 76, freeze={"at": 2.0, "duration": 1.5}, transition="crossfade", xfade="dissolve", transition_dur=0.5,
                             effects=[{"type": "flash", "at": 2.0, "duration": 0.25}],
                             titles=[{"text": "Freeze frame", "style": "callout", "start": 2.0, "end": 3.6, "anim": "fade"}]),
              "freeze frame with flash"))
    C.append((ops.video_clip(DOC, 30, 36, transition="crossfade", xfade="zoomin", transition_dur=0.5,
                             keyframes={"rotation": [[0, -4], [6, 4]]}, perspective={"tl": [0.04, 0.0], "tr": [0.96, 0.03], "bl": [0.0, 1.0], "br": [1.0, 0.97]}),
              "rotation keyframes + perspective correction"))
    C.append((ops.video_clip(BR, 180, 186, transition="crossfade", xfade="hblur", transition_dur=0.6, grade="golden_hour",
                             effects=[{"type": "speed_ramp", "from": 1.0, "to": 0.4, "start": 2.0, "end": 3.5}], fade_out=1.0,
                             credits={"title": "NEXUS Account 2", "lines": ["Footage: NASA (public domain)", "Edited by the NEXUS renderer"]}),
              "speed ramp + golden-hour look + rolling credits"))
    tl["clips"] = [c for c, _ in C]
    # layer above the storyline: picture-in-picture of the interview over the b-roll push-in, fading in and out
    starts = render.clip_starts(tl)
    tl = ops.add_track(tl, "video", "V2")
    tl = ops.add_item(tl, "V2", {"type": "video", "src": IV, "in": 10, "out": 15, "x": 0.64, "y": 0.06, "w": 0.32, "h": 0.32,
                                 "keyframes": {"opacity": [[0, 0.0], [0.6, 1.0], [4.4, 1.0], [5.0, 0.0]]}}, start=starts[1] + 0.5)
    total = render.total_duration(tl)
    tl["music"] = {"src": str(music.music_bed(total + 2, "inspiring", 21)), "volume": 0.4, "duck": "keyframes"}
    tl["audio"] = [{"src": str(sfx.make("whoosh")), "start": round(starts[i] - 0.25, 3), "role": "sfx", "volume": 0.6,
                    "pan": 0.2 if i % 2 else 0.8} for i in range(1, len(C)) if starts[i] > 0.3]
    tl["audio"].append({"src": str(sfx.make("impact")), "start": round(starts[5] + 2.0, 3), "role": "sfx", "volume": 0.7})
    tl["subtitles"] = [{"start": round(starts[3] + 1.0, 3), "end": round(starts[3] + 3.4, 3), "text": "Captions pop word by word"},
                       {"start": round(starts[3] + 3.6, 3), "end": round(starts[3] + 6.0, 3), "text": "with a highlighted current word"}]
    return tl, [(lab, c["id"]) for c, lab in C]


def main() -> None:
    config.ensure_dirs()
    p = project.Project.create("Account 2 Demo 3 - advanced editing", "demo")
    for src in (IV, WT, BR, EX, DOC):  # declared public-domain sources (rights gate + provenance)
        m = p.import_media(src)
        m["rights"] = {"basis": "public-domain", "holder": "NASA", "evidence": json.loads((Path(src).parent / "provenance.json").read_text())["page"]}
    p.save()
    tl, labels = build()
    # the clips must reference the project's copies (rights gate + provenance look them up by path)
    path_map = {src: m["path"] for src, m in zip((IV, WT, BR, EX, DOC), p.data["media"])}
    def remap(d):
        if isinstance(d, dict):
            return {k: (path_map.get(v, v) if k == "src" and isinstance(v, str) else remap(v)) for k, v in d.items()}
        if isinstance(d, list):
            return [remap(x) for x in d]
        return d
    tl = remap(tl)
    p.save_timeline(tl, "demo 3 timeline")
    t0 = time.time()
    stats, report = qa.render_with_qa(p, tl, "demo3_advanced")
    wall = time.time() - t0
    out = Path(stats["output"])
    starts = render.clip_starts(tl)
    durs = [render.clip_duration(c) for c in tl["clips"]]
    frames = []
    for i, (lab, cid) in enumerate(labels):
        t = starts[i] + durs[i] * (0.62 if i != 5 else 0.0) + (2.6 if i == 5 else 0)  # freeze section: inside the hold
        f = OUT / f"_d3_{i}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(out), "-frames:v", "1",
                        "-vf", "scale=426:-2", str(f)], check=True)
        frames.append({"t": round(t, 2), "capability": lab, "frame": f.name})
    # contact sheet (2 rows x 4), labels drawn by ffmpeg
    ins = sum([["-i", str(OUT / fr["frame"])] for fr in frames], [])
    lay = "|".join(f"{(i % 4)}*w0_{(i // 4)}*h0".replace("0*w0", "0").replace("0*h0", "0") for i in range(len(frames)))
    lay = "|".join(f"{'+'.join(['w0'] * (i % 4)) or '0'}_{'+'.join(['h0'] * (i // 4)) or '0'}" for i in range(len(frames)))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *ins, "-filter_complex",
                    f"xstack=inputs={len(frames)}:layout={lay}", "-q:v", "6", str(OUT / "demo3_contact.jpg")], check=True)
    for fr in frames:
        (OUT / fr["frame"]).unlink()
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(out)],
                                     capture_output=True, text=True).stdout)
    res = {"demo": "3 - advanced editing", "project": p.id, "output": str(out), "duration": float(info["format"]["duration"]),
           "size_mb": round(int(info["format"]["size"]) / 1e6, 2), "wall_seconds": round(wall, 1), "render": {k: stats.get(k) for k in
           ("segments", "segments_from_cache", "pieces", "pieces_from_cache", "pipeline", "elapsed")},
           "qa_status": report["status"], "known_issues": report["known_issues"], "sections": frames,
           "checks": [f"{'PASS' if c['ok'] else 'FAIL'} {s['stage']}: {c['check']} - {c['detail']}" for s in report["stages"] for c in s["checks"]],
           "machine": "shared 4-vCPU container, no GPU (other agents running)"}
    (OUT / "demo3_advanced.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ("duration", "wall_seconds", "qa_status", "known_issues")}, indent=1))


if __name__ == "__main__":
    main()
