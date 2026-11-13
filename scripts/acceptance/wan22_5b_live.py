"""Live Wan 2.2 TI2V-5B acceptance on the authorised Kaggle GPU worker (`kaggle_primary`; no other account, no paid compute).

    python scripts/acceptance/wan22_5b_live.py --profile smoke     # 672x384, 25 frames, 20 steps: placement/precision check
    python scripts/acceptance/wan22_5b_live.py --profile smoke_i2v # same, image-to-video
    python scripts/acceptance/wan22_5b_live.py --profile t2v720    # 1280x704 @ 24 fps, 121 frames, 30 steps (then i2v720)

Weights (~23 GB, Apache-2.0, pinned revision) are downloaded inside the Kaggle kernel only. The MP4s come back through
the dispatcher (size + SHA-256 verified) and are re-checked here with ffprobe and a frame-difference motion test.
Evidence: docs/results/wan22_5b/<profile>.json (no secrets, no account ids).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nexus import vault  # noqa: E402
from nexus.cloud.compute import dispatcher as dsp  # noqa: E402
from nexus.contracts import ProductionJobManifest  # noqa: E402
from nexus.timeline import remote_render  # noqa: E402

T2V = ("A red fox trots through fresh snow in a birch forest at sunrise, its breath visible in the cold air; the camera "
       "tracks alongside at low height, soft golden backlight, shallow depth of field, cinematic, natural motion.")
I2V = "The cat slowly turns its head toward the camera and blinks while the background gently moves, natural motion."
PROFILES = {
    "smoke": [{"mode": "t2v", "prompt": T2V, "width": 672, "height": 384, "frames": 25, "fps": 24, "steps": 20, "seed": 7}],
    "smoke_i2v": [{"mode": "i2v", "prompt": I2V, "width": 672, "height": 384, "frames": 25, "fps": 24, "steps": 20, "seed": 11}],
    # 720p @ 24 fps, 121 frames (5 s): one job per mode so each fits a Kaggle session on its own
    "t2v720": [{"mode": "t2v", "prompt": T2V, "width": 1280, "height": 704, "frames": 121, "fps": 24, "steps": 30, "seed": 7}],
    "i2v720": [{"mode": "i2v", "prompt": I2V, "width": 1280, "height": 704, "frames": 121, "fps": 24, "steps": 30, "seed": 11}],
}


def _probe(p: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height,r_frame_rate,nb_read_frames,pix_fmt:format=duration,size", "-of", "json", str(p)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _motion(p: Path) -> dict:
    """Mean absolute difference between consecutive frames (64x36 grey) and luma stats, decoded locally."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-vf", "scale=64:36,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    n = len(raw) // (64 * 36)
    fr = [raw[i * 2304:(i + 1) * 2304] for i in range(n)]
    d = [sum(abs(a - b) for a, b in zip(fr[i], fr[i + 1])) / 2304 for i in range(n - 1)]
    luma = [sum(f) / 2304 for f in fr]
    return {"decoded_frames": n, "mean_abs_diff": round(sum(d) / len(d), 3) if d else 0, "static_pairs": sum(1 for x in d if x < 0.25),
            "luma_min": round(min(luma), 1), "luma_max": round(max(luma), 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=sorted(PROFILES), default="smoke")
    ap.add_argument("--dtype", choices=["fp16", "bf16"], default=None)
    ap.add_argument("--timeout", type=float, default=9 * 3600)
    ap.add_argument("--out-dir", default="docs/results/wan22_5b")
    ap.add_argument("--attach", help="poll an already submitted job id instead of submitting a new one")
    a = ap.parse_args()
    if "kaggle_primary" not in vault.names():
        print("kaggle_primary is not connected (store the token with nexus.vault.put; never paste it in chat)")
        return 2
    remote_render.register_account1_tasks()
    params = {"model": "wan22-ti2v-5b", "runs": PROFILES[a.profile]}
    if a.dtype:
        params["dtype"] = a.dtype
    m = ProductionJobManifest(task="a2_wan_video", owner_account="account2", max_runtime_s=8 * 3600 if a.profile.endswith("720") else 5400,
                              max_retries=0, params=params,
                              idempotency_key=f"acceptance:wan22_5b:{a.profile}:{a.dtype}:{time.strftime('%Y%m%d%H%M')}")
    r = {"job_id": a.attach} if a.attach else dsp.submit(m)
    print("attached" if a.attach else "submitted", r, flush=True)
    row = dsp.get().run_until_done(r["job_id"], timeout=a.timeout, interval=30)
    res = json.loads(row["result"]) if row.get("result") else {}
    if not res.get("metrics"):
        from nexus import config
        att = sorted((config.HOME / "compute" / "jobs" / row["id"]).glob("attempt_*/result.json"))
        if att:
            res = {**json.loads(att[-1].read_text(encoding="utf-8")), **{k: v for k, v in res.items() if k == "attempts"}}
    arts = dsp.job_artifacts(row["id"])
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bench, videos = None, []
    for x in arts:
        p = dsp.artifact_path(x)
        if x["name"] == "wan_benchmark.json":
            bench = json.loads(p.read_text(encoding="utf-8"))
        elif x["name"].endswith(".mp4"):
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            videos.append({"name": x["name"], "size": x["size"], "sha256": sha, "sha256_matches_worker": sha == x["sha256"],
                           "ffprobe": _probe(p), "motion": _motion(p), "local_path": str(p)})
    ev = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": "LIVE (real Kaggle GPU, real Wan 2.2 TI2V-5B)",
          "profile": a.profile, "job_id": row["id"], "status": row["status"], "error": row.get("error"),
          "accelerator": res.get("accelerator"), "duration_s": res.get("duration_s"),
          "worker_metrics": {k: (res.get("metrics") or {}).get(k) for k in ("peak_rss_mb", "ram_total_gb", "gpu_util_max", "gpu_util_mean",
                                                                            "gpu_mem_used_max_mb", "task_s", "per_gpu", "load_s", "download_s")},
          "benchmark": bench, "videos": videos, "log_tail": (res.get("log_tail") or "")[-2500:]}
    text = re.sub(r'"ref": "[^"/]+/', '"ref": "<kaggle-user>/', json.dumps(ev, indent=1, ensure_ascii=False))
    (out_dir / f"{a.profile}.json").write_text(vault.redact(text), encoding="utf-8")
    print(json.dumps({k: ev[k] for k in ("status", "error", "accelerator", "duration_s")}, indent=1))
    for v in videos:
        print(v["name"], v["ffprobe"]["streams"][0], v["motion"])
    return 0 if row["status"] == "succeeded" and videos else 1


if __name__ == "__main__":
    raise SystemExit(main())
