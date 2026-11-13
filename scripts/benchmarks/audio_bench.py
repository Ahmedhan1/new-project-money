"""Audio benchmark (Account 2, Agent C): objective measurements of the licensed benchmark media.

    python scripts/benchmarks/audio_bench.py [--ids interview,panel,...] [--out docs/results/a2_audio_bench.json]

Per file (whole duration, 48 kHz stereo decode): integrated LUFS, LRA, true peak (BS.1770-4 in numpy, cross-checked
against FFmpeg ebur128), noise floor (quietest 10 % of 50 ms blocks), speech-band (300-3400 Hz) SNR estimate,
clipping, DC offset, channel imbalance/dead channel/dual mono, noise character (flatness, hum, hiss, bandwidth) and
per-speaker level spread. Speakers: silence-separated segments clustered by voice with the diariser's WeSpeaker
embeddings (26 MB ONNX, CC BY 4.0, downloaded on first use; Whisper + full diarisation on every file was not "cheap"
on the shared 4-core container). Definitions:
nexus/audio/measure.py. Measurements only: nobody listened to these files.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BENCH = Path("/home/user/new-project-money/.nexus/benchmarks")
IDS = ["interview", "panel", "podcast", "walktalk", "documentary", "ad"]


def main() -> int:
    from nexus.audio import measure as M
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=",".join(IDS))
    ap.add_argument("--bench", default=str(BENCH))
    ap.add_argument("--out", default=str(ROOT / "docs" / "results" / "a2_audio_bench.json"))
    a = ap.parse_args()
    rows = {}
    for i in a.ids.split(","):
        src = Path(a.bench) / i / "source.mp4"
        if not src.exists():
            rows[i] = {"error": "missing"}
            continue
        prov = json.loads((src.parent / "provenance.json").read_text())
        t0 = time.time()
        x = M.decode(src, 48000, 2)
        t1 = time.time()
        r = M.profile(x, 48000, neural=True)
        t2 = time.time()
        rows[i] = {"title": prov.get("title"), "kind": prov.get("kind"), "licence": prov.get("license"), **r,
                   "decode_s": round(t1 - t0, 2), "measure_s": round(t2 - t1, 2)}
        print(i, {k: r.get(k) for k in M.PROFILE_KEYS}, flush=True)
    doc = {"what": "objective audio measurements of the licensed benchmark media (no human listening)",
           "date": time.strftime("%Y-%m-%d"), "machine": f"shared 4-vCPU container, no GPU ({platform.machine()})",
           "definitions": "nexus/audio/measure.py docstring", "files": rows}
    Path(a.out).write_text(json.dumps(doc, indent=1) + "\n")
    print("wrote", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
