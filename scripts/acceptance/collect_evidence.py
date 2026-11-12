"""Collect acceptance evidence from the job ledger and rendered files into docs/results/demos/acceptance_*.json.

    NEXUS_HOME=.nexus python scripts/acceptance/collect_evidence.py <kind> <job_id> [...]

kinds: cinematic <render job> | recovery <job>... | shorts <shorts job> | platforms <deliver job> | dub <localize job>
       | subtitles <project> <branch>

Media stays in NEXUS_HOME (outside Git); only small JSON, a contact sheet and checksums are written to the repo.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from nexus import config, db  # noqa: E402

OUT = ROOT / "docs" / "results" / "demos"


def _job(jid: str) -> dict:
    r = db.query("SELECT * FROM jobs WHERE id=?", (jid,))[0]
    for k in ("result", "stages", "params"):
        if isinstance(r.get(k), str):
            try:
                r[k] = json.loads(r[k])
            except ValueError:
                pass
    return r


def _probe(path: Path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,duration:format=duration,size",
                        "-of", "json", str(path)], capture_output=True, text=True)
    d = json.loads(p.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), {})
    a = next((s for s in d.get("streams", []) if s["codec_type"] == "audio"), {})
    return {"video": f"{v.get('codec_name')} {v.get('width')}x{v.get('height')}", "video_s": float(v.get("duration", 0)),
            "audio": a.get("codec_name"), "audio_s": float(a.get("duration", 0)),
            "bytes": int(d.get("format", {}).get("size", 0))}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _sheet(video: Path, out: Path, n: int = 8, w: int = 320) -> str:
    dur = _probe(video)["video_s"] or 1
    cols = 4
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
                    f"fps={n / dur},scale={w}:-2,tile={cols}x{(n + cols - 1) // cols}", "-frames:v", "1", "-q:v", "5", str(out)],
                   check=True)
    return str(out.relative_to(ROOT))


def _stages(j: dict) -> dict:
    return {k: {x: v.get(x) for x in ("status", "seconds", "runs")} for k, v in (j.get("stages") or {}).items()} \
        if isinstance(j.get("stages"), dict) else j.get("stages")


def _write(name: str, data: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    data = {"collected_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **data}
    p = OUT / f"acceptance_{name}.json"
    p.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(p.relative_to(ROOT))
    return p


def _project_dir(j: dict) -> Path:
    return config.PROJECTS / j["project_id"]


def cinematic(jid: str) -> None:
    j = _job(jid)
    r = j["result"]
    video = _project_dir(j) / r["output"]
    q = r.get("quality") or {}
    _write("cinematic", {
        "demo": 1, "what": "3-5 minute cinematic edit from licensed source footage (director + B-roll + post)",
        "job": jid, "status": j["status"], "stages": _stages(j), "retries": j["retries"], "recovery": j["recovery"],
        "file": _probe(video), "sha256": _sha(video), "segments": r.get("segments"), "segments_from_cache": r.get("segments_from_cache"),
        "qa_status": r.get("status"), "qa_issues": r.get("issues"),
        "quality": {k: q.get(k) for k in ("score", "verdict", "categories", "not_measured", "note")},
        "contact_sheet": _sheet(video, OUT / "acceptance_cinematic_contact.jpg", 12),
        "licences": "source: NASA interview (public domain, NASA media guidelines); B-roll: per-item credits in the timeline "
                    "(NASA Image and Video Library / Openverse CC licences); music: none",
    })


def recovery(*jids: str) -> None:
    rows = []
    for jid in jids:
        j = _job(jid)
        r = j.get("result") or {}
        rows.append({"job": jid, "kind": j["kind"], "status": j["status"], "retries": j["retries"], "recovery": j["recovery"],
                     "stages": _stages(j), "segments": r.get("segments"), "segments_from_cache": r.get("segments_from_cache")})
    _write("recovery", {"demo": 7, "what": "hard-kill (SIGKILL) the app during real production jobs, restart, verify resume",
                        "jobs": rows})


def shorts(jid: str) -> None:
    j = _job(jid)
    r = j["result"]
    pdir = _project_dir(j)
    items = []
    for s in r.get("shorts", []):
        vid = pdir / "renders" / f"{s['branch']}.mp4"
        it = {k: s.get(k) for k in ("branch", "title", "duration", "start", "end", "variant", "platform", "context_status", "hashtags")}
        it["scores"] = s.get("scores")
        it["quality"] = {k: (s.get("quality") or {}).get(k) for k in ("score", "verdict")} if s.get("quality") else s.get("quality_score")
        if vid.exists():
            it["file"] = _probe(vid)
            it["sha256"] = _sha(vid)
        items.append(it)
    first = next((pdir / "renders" / f"{s['branch']}.mp4" for s in r.get("shorts", [])
                  if (pdir / "renders" / f"{s['branch']}.mp4").exists()), None)
    _write("shorts", {"demo": 2, "what": "5-10 distinct shorts from the same master", "job": jid, "status": j["status"],
                      "stages": _stages(j), "moments_considered": r.get("moments"), "rendered": r.get("rendered"),
                      "not_rendered": r.get("not_rendered"), "shorts": items,
                      "overlap_check": _overlaps(items),
                      "contact_sheet": _sheet(first, OUT / "acceptance_shorts_contact.jpg", 8, 180) if first else None,
                      "note": "scores are editorial heuristics measured on the transcript; not a prediction of views or reach"})


def _overlaps(items: list[dict]) -> list:
    out = []
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            x, y = items[a], items[b]
            if x.get("start") is None or y.get("start") is None:
                continue
            ov = min(x["end"], y["end"]) - max(x["start"], y["start"])
            if ov > 0:
                out.append({"a": x["branch"], "b": y["branch"], "overlap_s": round(ov, 2)})
    return out


def platforms(jid: str) -> None:
    j = _job(jid)
    r = j["result"]
    pdir = _project_dir(j)
    groups = []
    for g in r.get("groups", []):
        v = pdir / g["video"]
        name = "_".join(g["platforms"])
        groups.append({**g, "file": _probe(v), "sha256": _sha(v),
                       "frame_sheet": _sheet(v, OUT / f"acceptance_platform_{name}.jpg", 4, 240)})
    pk = {n: {k: v.get(k) for k in ("check", "title", "hashtags", "safe_zone", "files", "manual_upload")} if isinstance(v, dict) else v
          for n, v in (r.get("packages") or {}).items()}
    _write("platforms", {"demo": 3, "what": "platform adaptations (aspect, safe zones, framing, limits) from one master",
                         "job": jid, "status": j["status"], "stages": _stages(j), "groups": groups, "packages": pk,
                         "note": "nothing was uploaded; packages are for manual upload (no account authorisation)"})


def dub(jid: str) -> None:
    j = _job(jid)
    r = j["result"]
    video = _project_dir(j) / r["output"]
    _write("dub", {"demo": 5, "what": "dubbed version with a commercially cleared synthetic voice", "job": jid,
                   "status": j["status"], "params": j.get("params"), "stages": _stages(j), "file": _probe(video), "sha256": _sha(video),
                   "qa_status": r.get("status"), "issues": r.get("issues"), "dub": r.get("dub"), "label": r.get("label"),
                   "sample_lines": (r.get("lines") or [])[:8],
                   "contact_sheet": _sheet(video, OUT / "acceptance_dub_contact.jpg", 4, 320)})


def subtitles(pid: str, branch: str = "main") -> None:
    from nexus import project as prj, publish
    r = publish.subtitle_package(prj.Project(pid), branch)
    _write("subtitles", {"demo": 4, "what": "multilingual subtitle package with readiness manifest", "project": pid,
                         "branch": branch, "languages": r["languages"], "subtitle_ready": r["subtitle_ready"],
                         "manifest": r["manifest"], "zip": Path(r["zip"]).name})


if __name__ == "__main__":
    kind, *args = sys.argv[1:]
    {"cinematic": cinematic, "recovery": recovery, "shorts": shorts, "platforms": platforms, "dub": dub,
     "subtitles": subtitles}[kind](*args)
