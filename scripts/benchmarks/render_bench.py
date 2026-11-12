"""Renderer benchmark on the licensed benchmark media (Account 2, agent A).

Builds a realistic edit from interview + explainer + broll (16 clips, B-roll cutaways, crossfades, animated titles,
a lower third, burned captions, a music bed with ducking) and measures five scenarios:
  cold (empty cache), warm (same timeline again), one-clip edit (grade of one clip), caption-only edit (one cue's
  text), draft (cold, half resolution).
For every run: wall time, CPU utilisation of the FFmpeg children (rusage), peak RSS of the FFmpeg processes (psutil
sampler: largest single process and largest simultaneous sum), disk used by intermediates (cache + workdir),
segment/piece cache hits, and time per FFmpeg stage (ff.run is wrapped to time each `op`).

Usage (always under the heavy lock; the CPU is shared):
  flock /tmp/nexus-heavy.lock python scripts/benchmarks/render_bench.py --label baseline [--size 1280x720] [--clips 16]
Results are merged into docs/results/a2_render_bench.json under `runs[<label>]`.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NEXUS_EMBEDDED_WORKER", "0")

BENCH = Path(os.environ.get("NEXUS_BENCH_MEDIA", "/home/user/new-project-money/.nexus/benchmarks"))
OUT_JSON = ROOT / "docs" / "results" / "a2_render_bench.json"


def build_timeline(W: int, H: int, n_clips: int, music: str | None) -> dict:
    from nexus.timeline import ops
    iv, ex, br = (str(BENCH / k / "source.mp4") for k in ("interview", "explainer", "broll"))
    clips = []
    t_iv, t_ex, t_br = 20.0, 30.0, 10.0
    for i in range(n_clips):
        kind = ("iv", "iv", "ex", "br")[i % 4]
        if kind == "iv":
            c = ops.video_clip(iv, t_iv, t_iv + 4.5, audio_fx="voice")
            t_iv += 11.0
            if i % 8 == 1:   # B-roll cutaway over the interview audio
                c["pip"] = {"src": br, "in": 60.0 + i * 3, "full": True, "start": 1.0, "end": 3.5}
            elif i % 8 == 5:  # picture-in-picture window
                c["pip"] = {"src": ex, "in": 100.0 + i, "x": 0.66, "y": 0.06, "w": 0.3}
        elif kind == "ex":
            c = ops.video_clip(ex, t_ex, t_ex + 4.0)
            t_ex += 13.0
        else:
            c = ops.video_clip(br, t_br, t_br + 3.5, mute=True, effects=[{"type": "zoom", "from": 1.0, "to": 1.08}])
            t_br += 9.0
        if i and i % 5 == 0:
            c.update(transition="crossfade", xfade=("fade", "wipeleft", "dissolve")[i % 3], transition_dur=0.5)
        clips.append(c)
    clips[0]["titles"] = [{"text": "NEXUS renderer benchmark", "style": "title", "start": 0.3, "end": 3.5, "anim": "rise"}]
    clips[1]["titles"] = [{"text": "Interview guest", "style": "lower_third", "start": 0.2, "end": 3.0, "anim": "slide"}]
    clips[2]["grade"] = "cinematic"
    tl = {"version": 1, "width": W, "height": H, "fps": 30, "lang": "en", "grade": "none", "audio_fx": "none",
          "target_lufs": -14.0, "clips": clips, "audio": [], "music": None, "subtitles": [], "burn_subtitles": True,
          "markers": [], "captions": {"style": "boxed", "position": "bottom"}}
    from nexus.timeline import render
    total = render.total_duration(tl)
    words = "the crew trained for months before launch and every system was checked twice".split()
    t, k = 0.4, 0
    while t + 2.2 < total:
        tl["subtitles"].append({"start": round(t, 3), "end": round(t + 2.0, 3),
                                "text": " ".join(words[(k * 3) % len(words): (k * 3) % len(words) + 5]) or "launch"})
        t += 2.6
        k += 1
    if music:
        tl["music"] = {"src": music, "volume": 0.35, "duck": True}
    return tl


class Sampler:
    """Polls FFmpeg child processes for RSS while a render runs."""

    def __init__(self):
        import psutil
        self.ps = psutil
        self.me = psutil.Process()
        self.peak_one = 0
        self.peak_sum = 0
        self.max_procs = 0
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            tot, n = 0, 0
            for p in self.me.children(recursive=True):
                try:
                    if "ffmpeg" in p.name():
                        r = p.memory_info().rss
                        tot += r
                        n += 1
                        self.peak_one = max(self.peak_one, r)
                except (self.ps.NoSuchProcess, self.ps.AccessDenied):
                    pass
            self.peak_sum = max(self.peak_sum, tot)
            self.max_procs = max(self.max_procs, n)
            self._stop.wait(0.1)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self.t.join()


def _du(*paths: Path) -> int:
    n = 0
    for p in paths:
        if p.exists():
            for f in p.rglob("*"):
                if f.is_file():
                    n += f.stat().st_size
    return n


def run_one(name: str, tl: dict, out: Path, work: Path, draft: bool, op_times: dict) -> dict:
    from nexus import config
    from nexus.media import ff
    from nexus.timeline import render
    op_times.clear()
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.time()
    with Sampler() as s:
        st = render.render(tl, out, work, draft=draft)
    wall = time.time() - t0
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    stages = {k: round(v, 2) for k, v in sorted(op_times.items()) if not k.startswith("encoder-probe")}
    cpu_s = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    pr = ff.probe(out)
    vd = float((pr.get("video") or {}).get("duration") or 0)
    ad = float((pr.get("audio") or {}).get("duration") or 0)
    lo = ff.loudness(out)
    segs = st.get("segments", 0)
    pieces = st.get("pieces")
    hit_den = segs + (pieces or 0) + (1 if "audio_from_cache" in st else 0)
    hit_num = st.get("segments_from_cache", 0) + (st.get("pieces_from_cache") or 0) + (1 if st.get("audio_from_cache") else 0)
    r = {"wall_s": round(wall, 2), "programme_s": round(st["duration"], 3), "rtf": round(wall / max(st["duration"], 0.1), 3),
         "cpu_s": round(cpu_s, 1), "cpu_util_of_4": round(cpu_s / wall / (os.cpu_count() or 1), 3),
         "peak_rss_one_ffmpeg_mb": round(s.peak_one / 2**20, 1), "peak_rss_sum_ffmpeg_mb": round(s.peak_sum / 2**20, 1),
         "max_parallel_ffmpeg": s.max_procs,
         "disk_intermediates_mb": round(_du(config.CACHE, work) / 2**20, 1),
         "segments": segs, "segments_from_cache": st.get("segments_from_cache"), "pieces": pieces,
         "pieces_from_cache": st.get("pieces_from_cache"), "audio_from_cache": st.get("audio_from_cache"),
         "cache_hit_rate": round(hit_num / max(1, hit_den), 3),
         "stage_s": stages,
         "final_pass_s": round(sum(v for k, v in stages.items() if k in ("final-mix", "concat", "concat-xfade", "final-mux")), 2),
         "av_delta_s": round(abs(vd - ad), 3), "lufs": lo.get("lufs"),
         "timings": st.get("timings")}
    print(f"  {name:10s} {r['wall_s']:7.2f}s  cpu {r['cpu_util_of_4']:.2f}  hits {r['cache_hit_rate']:.2f}  "
          f"stages {r['stage_s']}", flush=True)
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--clips", type=int, default=16)
    ap.add_argument("--home", default="/tmp/a2-agent-a/bench_home")
    ap.add_argument("--scenarios", default="cold,warm,edit_one_clip,edit_caption,draft")
    a = ap.parse_args()
    W, H = (int(x) for x in a.size.split("x"))
    home = Path(a.home)
    if home.exists():
        shutil.rmtree(home)
    from nexus import config, db
    config.set_home(home)
    config.ensure_dirs()
    os.environ["NEXUS_HOME"] = str(home)
    db.reset_for_tests()
    from nexus.media import ff
    from nexus.timeline import render

    op_times: dict[str, float] = {}
    lock = threading.Lock()
    real_run = ff.run

    def timed_run(args, op="ffmpeg", *k, **kw):
        t0 = time.time()
        try:
            return real_run(args, op, *k, **kw)
        finally:
            with lock:
                op_times[op] = op_times.get(op, 0.0) + time.time() - t0

    ff.run = timed_run
    try:  # hardware-encoder detection runs once per process: do it before the cold run so it is not timed
        from nexus.media import encoders
        encoders.choose_encoder("h264", "auto")
    except Exception:  # noqa: BLE001
        pass
    from nexus.audio import music as amusic
    music = None
    try:
        music = str(amusic.music_bed(120.0, "calm"))
    except Exception as e:  # noqa: BLE001
        print("music bed unavailable:", e)
    tl = build_timeline(W, H, a.clips, music)
    keep = {"music": music} if music else {}
    work = home / "work"
    outd = home / "out"
    res: dict = {}
    sc = a.scenarios.split(",")

    def clear_cache():
        for p in (config.CACHE / "segments", config.CACHE / "segments_draft", config.CACHE / "pieces", work):
            shutil.rmtree(p, ignore_errors=True)
        _ = keep

    if a.scenarios == "slowmo":  # cost of the slow-motion profiles on a 2 s broll window at 0.5x (4 s of output)
        from nexus.timeline import ops
        res = {}
        for prof in ("none", "blend", "optical_flow"):
            stl = {**tl, "clips": [ops.video_clip(str(BENCH / "broll" / "source.mp4"), 40.0, 42.0, speed=0.5, mute=True)],
                   "subtitles": [], "music": None, "slowmo": prof}
            clear_cache()
            res[prof] = run_one(prof, stl, outd / f"slowmo_{prof}.mp4", work, False, op_times)
        doc = json.loads(OUT_JSON.read_text()) if OUT_JSON.exists() else {}
        doc.setdefault("slowmo", {})[a.label] = {"date": time.strftime("%Y-%m-%dT%H:%M:%S"), "size": f"{W}x{H}",
                                                 "clip": "broll 40-42 s at speed 0.5 (4 s output)",
                                                 "wall_s": {k: v["wall_s"] for k, v in res.items()},
                                                 "cpu_s": {k: v["cpu_s"] for k, v in res.items()}}
        OUT_JSON.write_text(json.dumps(doc, indent=1) + "\n")
        print("written", OUT_JSON)
        return
    print(f"timeline: {len(tl['clips'])} clips, {render.total_duration(tl):.1f} s, {W}x{H}, {len(tl['subtitles'])} cues", flush=True)
    if "cold" in sc:
        clear_cache()
        res["cold"] = run_one("cold", tl, outd / "cold.mp4", work, False, op_times)
    if "warm" in sc:
        res["warm"] = run_one("warm", tl, outd / "warm.mp4", work, False, op_times)
    if "edit_one_clip" in sc:
        tl2 = json.loads(json.dumps(tl))
        tl2["clips"][min(6, len(tl2["clips"]) - 1)]["grade"] = "warm"
        res["edit_one_clip"] = run_one("edit_clip", tl2, outd / "edit_clip.mp4", work, False, op_times)
    if "edit_caption" in sc:
        tl3 = json.loads(json.dumps(tl))
        tl3["subtitles"][3]["text"] = "an edited caption line"
        res["edit_caption"] = run_one("edit_cap", tl3, outd / "edit_cap.mp4", work, False, op_times)
    if "draft" in sc:
        res["draft"] = run_one("draft", tl, outd / "draft.mp4", work, True, op_times)

    doc = json.loads(OUT_JSON.read_text()) if OUT_JSON.exists() else {}
    doc.setdefault("description", "Renderer benchmark (scripts/benchmarks/render_bench.py) on licensed benchmark media: "
                   "interview + explainer + broll, B-roll cutaways (full-frame + PiP), crossfades, animated titles, "
                   "lower third, burned boxed captions, ducked music bed.")
    try:
        cpu = next(ln.split(":", 1)[1].strip() for ln in open("/proc/cpuinfo") if ln.startswith("model name"))
    except Exception:  # noqa: BLE001
        cpu = platform.processor()
    doc["machine"] = {"cpu": cpu, "os_cpu_count": os.cpu_count(), "platform": platform.platform(),
                      "ffmpeg": ff.version(), "note": "noisy shared 4-vCPU cloud container (5 agents share it; heavy jobs "
                      "serialised with flock, but tests of other agents still run); repeat runs vary by 10-40 %"}
    doc.setdefault("runs", {})[a.label] = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"), "git": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                                                          capture_output=True, text=True).stdout.strip(),
        "timeline": {"clips": len(tl["clips"]), "programme_s": round(render.total_duration(tl), 2), "size": f"{W}x{H}",
                     "cues": len(tl["subtitles"]), "crossfades": sum(1 for c in tl["clips"] if c.get("transition")),
                     "cutaways": sum(1 for c in tl["clips"] if c.get("pip"))},
        "load_avg_start": os.getloadavg()[0], "scenarios": res}
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(doc, indent=1) + "\n")
    print("written", OUT_JSON)


if __name__ == "__main__":
    main()
