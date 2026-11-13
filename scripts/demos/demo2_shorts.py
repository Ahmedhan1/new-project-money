"""Account 2 Demo 2: shorts factory. 5-10 distinct vertical shorts from one licensed long-form source, each rendered at
1080x1920 and measured (technical QA, platform quality score incl. caption safe zone, loudness), with the reason it
was selected and labelled frames.

Run:  NEXUS_HOME=<dir> python scripts/demos/demo2_shorts.py [benchmark id, default interview] [n, default 8]
Analyses are read from $NEXUS_ANALYSES/<id>.json (see demo1). Writes docs/results/a2_demos/demo2_shorts.json and
demo2_contact.jpg. Videos stay in NEXUS_HOME.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nexus import config, project, qa, quality, shorts  # noqa: E402
from nexus.audio import measure  # noqa: E402

B = Path(os.environ.get("NEXUS_BENCH", ROOT / ".nexus" / "benchmarks"))
AN = Path(os.environ.get("NEXUS_ANALYSES", ROOT / ".nexus" / "analyses"))
OUT = ROOT / "docs" / "results" / "a2_demos"
PLATFORM = "youtube_shorts"


def remap(d, m: dict):
    if isinstance(d, dict):
        return {k: (m.get(v, v) if k == "src" and isinstance(v, str) else remap(v, m)) for k, v in d.items()}
    if isinstance(d, list):
        return [remap(x, m) for x in d]
    return d


def main() -> None:
    bid = sys.argv[1] if len(sys.argv) > 1 else "interview"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    config.ensure_dirs()
    src = str(B / bid / "source.mp4")
    prov = json.loads((B / bid / "provenance.json").read_text())
    an = json.loads((AN / f"{bid}.json").read_text())
    p = project.Project.create(f"Account 2 Demo 2 - shorts from {bid}", "demo")
    m = p.import_media(src)
    m["rights"] = {"basis": "public-domain", "holder": "NASA", "evidence": prov.get("page")}
    p.save()
    t0 = time.time()
    items = shorts.factory(src, an, n=n, platforms=(PLATFORM,), durations=(15, 25, 40, 55), size=(1080, 1920))
    select_s = time.time() - t0
    sents = shorts.usable_sentences(shorts._norm(an))
    rows, frames = [], []
    for k, it in enumerate(items):
        mo, tl = it["moment"], remap(it["timeline"], {src: m["path"]})
        i, j = mo["sentences"]
        t1 = time.time()
        stats, rep = qa.render_with_qa(p, tl, f"demo2_short_{k + 1:02d}")
        out = Path(stats["output"])
        qs = quality.score(out, tl, platform=PLATFORM)
        loud = measure.loudness(measure.decode(out, 48000), 48000)
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(out)],
                                         capture_output=True, text=True).stdout)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        safe = next(({kk: c.get(kk) for kk in ("score", "ok", "detail", "value") if kk in c} for c in qs.get("checks") or []
                     if isinstance(c, dict) and c.get("id") == "editing.caption_safe_area"), None)
        rows.append({"n": k + 1, "rank": mo["rank"], "src_range_s": [round(mo["start"], 2), round(mo["end"], 2)],
                     "duration": round(float(info["format"]["duration"]), 2), "resolution": f"{v['width']}x{v['height']}",
                     "fps": v["r_frame_rate"], "title": tl["short"]["title"], "hook": sents[i]["text"][:140],
                     "ending": sents[j]["text"][-140:], "ending_terminal": bool(sents[j]["terminal"]),
                     "why_selected": it.get("why"), "scores": it.get("scores"), "context_check": (it.get("context_check") or {}).get("status"),
                     "reframe": [x for x in tl.get("growth_notes", []) if x.startswith("reframe")][:1],
                     "captions": (tl.get("captions") or {}).get("style"), "cues": len(tl.get("subtitles") or []),
                     "qa_status": rep["status"], "known_issues": rep.get("known_issues"),
                     "quality": {kk: qs.get(kk) for kk in ("score", "verdict", "categories")},
                     "caption_safe_area": safe, "loudness": {kk: loud.get(kk) for kk in ("lufs", "true_peak", "lra")},
                     "render_s": round(time.time() - t1, 1), "output": str(out),
                     "metadata": {"title": it.get("title"), "description": it.get("description"), "hashtags": it.get("hashtags")}})
        for q, t in enumerate((1.0, float(info["format"]["duration"]) * 0.6)):
            f = OUT / f"_d2_{k}_{q}.png"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(out), "-frames:v", "1",
                            "-vf", "scale=216:384", str(f)], check=True)
            frames.append(f)
        print(rows[-1]["n"], rows[-1]["duration"], rows[-1]["qa_status"], rows[-1]["quality"]["score"], rows[-1]["title"], flush=True)
    pairs = [max(0.0, min(a["src_range_s"][1], b["src_range_s"][1]) - max(a["src_range_s"][0], b["src_range_s"][0]))
             / max(1e-6, min(a["src_range_s"][1] - a["src_range_s"][0], b["src_range_s"][1] - b["src_range_s"][0]))
             for a, b in itertools.combinations(rows, 2)]
    cols = 8
    ins = sum([["-i", str(f)] for f in frames], [])
    lay = "|".join(f"{'+'.join(['w0'] * (i % cols)) or '0'}_{'+'.join(['h0'] * (i // cols)) or '0'}" for i in range(len(frames)))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *ins, "-filter_complex",
                    f"xstack=inputs={len(frames)}:layout={lay}:fill=black", "-q:v", "5", str(OUT / "demo2_contact.jpg")], check=True)
    for f in frames:
        f.unlink()
    res = {"demo": "2 - shorts factory", "source": {"benchmark": bid, "page": prov.get("page"), "licence": "public domain (NASA media guidelines)"},
           "project": p.id, "platform": PLATFORM, "requested": n, "built": len(rows), "selection_s": round(select_s, 1),
           "distinctness": {"max_pairwise_source_overlap": round(max(pairs), 3) if pairs else 0.0,
                            "pairs_over_10pct": sum(1 for x in pairs if x > 0.1)},
           "endings_terminal": sum(r["ending_terminal"] for r in rows), "qa_passed": sum(r["qa_status"].startswith("PASS") for r in rows),
           "shorts": rows, "selection_notes": items.notes, "contact_sheet": "demo2_contact.jpg (2 frames per short: 1 s and 60%)",
           "machine": "shared 4-vCPU container, no GPU",
           "human_review": "PENDING - hooks, titles and whether each short stands alone need a person"}
    (OUT / "demo2_shorts.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    print(json.dumps({k: res[k] for k in ("built", "distinctness", "endings_terminal", "qa_passed")}, indent=1))


if __name__ == "__main__":
    main()
