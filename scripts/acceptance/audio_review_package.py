"""Audio reviewer package (Account 2): short before/after excerpts a person can listen to, with the measurements and a
human-approval field per item. Nothing here approves anything: every item starts as PENDING_OWNER_REVIEW.

    python scripts/acceptance/audio_review_package.py [--master <Demo 1 master.mp4>]

Audio (never committed - the repository keeps no media): $NEXUS_HOME/review/a2_audio/<id>_before.ogg / <id>_after.ogg
(dialogue only, 20 s, Opus 96 kb/s) and programme_mix.ogg (Demo 1 master excerpt as mixed).
Committed: docs/results/a2_audio_review/review.json (measurements, chain steps, automated artefact notes, approval
fields) and REVIEW.md (the listening sheet). Needs the benchmark media: python scripts/benchmarks/fetch_media.py
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
BENCH = ROOT / ".nexus" / "benchmarks"
OUT = ROOT / "docs" / "results" / "a2_audio_review"
SR = 48000
ITEMS = {"interview": 60.0, "panel": 40.0, "podcast": 90.0, "walktalk": 30.0, "ad": 20.0, "documentary": 30.0}
LEN = 20.0
KEYS = ("lufs", "true_peak", "noise_floor_db", "speech_band_snr_db", "clipped_samples", "bandwidth_hz")


def ogg(wav: Path, out: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "libopus", "-b:a", "96k", str(out)], check=True)
    wav.unlink()


def notes(before: dict, after: dict, steps: list[str]) -> list[str]:
    """Automated hints of over-processing for the listener to check (not a verdict)."""
    n = []
    if (after.get("speech_band_snr_db") or 0) - (before.get("speech_band_snr_db") or 0) > 15:
        n.append("large SNR gain: listen for 'underwater'/musical-noise artefacts in pauses")
    if (after.get("bandwidth_hz") or 0) < 0.8 * (before.get("bandwidth_hz") or 1):
        n.append("bandwidth reduced by >20%: listen for dullness")
    if "dereverb" in " ".join(steps):
        n.append("de-reverb applied: listen for phasey or clipped word tails")
    if "deess" in " ".join(steps) or "de-ess" in " ".join(steps):
        n.append("de-essing applied: listen for lisping")
    if (after.get("true_peak") or -99) > -1.0:
        n.append("true peak above -1 dBTP")
    return n or ["no automated warning; listen anyway"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", help="Demo 1 master to excerpt as the programme-mix sample")
    a = ap.parse_args()
    from nexus import config
    from nexus.audio import dialogue as G
    from nexus.audio import measure as M
    OUT.mkdir(parents=True, exist_ok=True)
    AUD = config.HOME / "review" / "a2_audio"
    AUD.mkdir(parents=True, exist_ok=True)
    rows = []
    for k, start in ITEMS.items():
        src = BENCH / k / "source.mp4"
        prov = json.loads((BENCH / k / "provenance.json").read_text())
        x = M.decode(src, SR, 2, start, LEN)
        t0 = time.time()
        y, rep = G.process(x, SR, target_lufs=-16.0)
        el = time.time() - t0
        for tag, sig in (("before", x), ("after", y)):
            w = AUD / f"{k}_{tag}.wav"
            M.write_wav(w, sig, SR)
            ogg(w, AUD / f"{k}_{tag}.ogg")
        b, f = ({kk: rep[s].get(kk) for kk in KEYS} for s in ("before", "after"))
        steps = [c["step"] for c in rep["chain"] if c["applied"]]
        rows.append({"id": k, "kind": "dialogue only", "source": prov.get("page"), "licence": "public domain",
                     "excerpt": {"start_s": start, "seconds": LEN}, "material": rep["material"], "processing_s": round(el, 2),
                     "chain_applied": steps, "before": b, "after": f, "automated_notes": notes(b, f, steps),
                     "files": [f"{k}_before.ogg", f"{k}_after.ogg"],
                     "human_review": {"status": "PENDING_OWNER_REVIEW", "reviewer": None, "date": None,
                                      "verdict": None, "artefacts_heard": None, "comments": None}})
        print(k, rep["material"], steps, flush=True)
    if a.master and Path(a.master).exists():
        x = M.decode(a.master, SR, 2, 40.0, 25.0)
        w = AUD / "programme_mix.wav"
        M.write_wav(w, x, SR)
        ogg(w, AUD / "programme_mix.ogg")
        lo = M.loudness(M.decode(a.master, SR), SR)
        rows.append({"id": "programme_mix", "kind": "programme mix (Demo 1 master 40-65 s: cleaned narration + ducked music)",
                     "files": ["programme_mix.ogg"], "measurements_whole_master": lo,
                     "listen_for": ["music level under narration (should never mask words)", "music recovery in pauses (no pumping)",
                                    "level steps at cuts", "sync of words to picture"],
                     "human_review": {"status": "PENDING_OWNER_REVIEW", "reviewer": None, "date": None, "verdict": None, "comments": None}})
    doc = {"what": "audio reviewer package - before/after excerpts for human listening", "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "status": "PENDING_OWNER_REVIEW", "note": "No person has listened to these files yet. Automated measurements are not approval.",
           "audio_folder": "$NEXUS_HOME/review/a2_audio (regenerate: python scripts/acceptance/audio_review_package.py --master <demo1 master>)",
           "how_to_review": "Play each pair on headphones and on a phone speaker; fill human_review (verdict: approve / approve-with-notes / reject).",
           "items": rows}
    (OUT / "review.json").write_text(json.dumps(doc, indent=1, default=lambda v: float(v) if isinstance(v, np.floating) else str(v)))
    md = ["# Audio listening sheet (Account 2)", "", "Status: **PENDING OWNER REVIEW**. Nobody has listened to these files yet.", "",
          "The audio is not in Git (no media in the repository). Generate it on your PC (about 1 minute after the benchmark",
          "media is fetched): `python scripts/benchmarks/fetch_media.py` then `python scripts/acceptance/audio_review_package.py`",
          "(add `--master <Demo 1 master.mp4>` for the programme-mix sample). Files appear in `%NEXUS_HOME%/review/a2_audio/`.", "",
          "| Item | Files | Chain applied | Before → after (LUFS / SNR dB) | Check for | Verdict |", "|---|---|---|---|---|---|"]
    for r in rows:
        if r["kind"] == "dialogue only":
            md.append(f"| {r['id']} ({r['material']}) | {' / '.join(r['files'])} | {', '.join(r['chain_applied'])} | "
                      f"{r['before']['lufs']} → {r['after']['lufs']} / {r['before']['speech_band_snr_db']} → {r['after']['speech_band_snr_db']} | "
                      f"{'; '.join(r['automated_notes'])} | ☐ approve ☐ notes ☐ reject |")
        else:
            md.append(f"| programme mix | programme_mix.ogg | Demo 1 mix | whole master {r['measurements_whole_master'].get('lufs')} LUFS | "
                      f"{'; '.join(r['listen_for'])} | ☐ approve ☐ notes ☐ reject |")
    md += ["", "Reviewer: ____________  Date: ________  Headphones / speaker used: ____________"]
    (OUT / "REVIEW.md").write_text("\n".join(md) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
