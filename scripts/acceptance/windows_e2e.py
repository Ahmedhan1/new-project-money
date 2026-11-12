"""Clean-machine end-to-end check driven through the running app's HTTP API (used by the windows-e2e CI job).
No models: the analysis is hand-built, so it proves installation, launch, jobs, rendering, UTF-8 and backup -
not transcription quality. Exit code 0 = every step passed; a JSON report is printed and written to e2e_report.json."""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = f"http://127.0.0.1:{os.environ.get('NEXUS_PORT', '8765')}"
report: dict = {"steps": []}


def step(name, ok, detail=""):
    report["steps"].append({"step": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""), flush=True)
    if not ok:
        Path("e2e_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        sys.exit(1)


def call(path, body=None, method=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_job(jid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = next(x for x in call("/api/jobs") if x["id"] == jid)
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(3)
    return {"status": "timeout"}


def main():
    home = Path(os.environ["NEXUS_HOME"])
    step("health", call("/api/health").get("ok"), call("/api/health"))
    src = Path(os.environ.get("RUNNER_TEMP", ".")) / "clip ünïcode 视频.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=20", "-f", "lavfi",
                    "-i", "sine=f=300:d=20:r=48000", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)], check=True)
    step("ffmpeg made a source file with a non-ASCII name", src.exists(), src)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from nexus import config, project as prj  # same NEXUS_HOME as the running app
    p = prj.Project.create("Windows e2e — اختبار 测试", "edit")
    m = p.import_media(src)
    segs, t = [], 0.5
    for sent in ["Solar panels convert sunlight into electricity.", "Heat reduces their output in summer.",
                 "Cleaning dust restores lost power.", "Batteries store the midday surplus for the evening."]:
        ws = []
        for w in sent.split():
            ws.append({"start": round(t, 2), "end": round(t + 0.3, 2), "word": w})
            t += 0.35
        segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": sent, "words": ws})
        t += 0.6
    an = {"path": m["path"], "probe": {"duration": 20.0, "video": {"width": 640, "height": 360}, "audio": {"channels": 2}},
          "scenes": [(0.0, 20.0)], "silences": [], "transcript": {"language": "en", "segments": segs, "text": " ".join(s["text"] for s in segs)},
          "repetitions": [], "loudness": {"lufs": -18.0}, "black": [], "freeze": [], "issues": [], "not_available": []}
    (p.dir / "reports" / f"analysis_{m['id']}.json").write_text(json.dumps(an), encoding="utf-8")
    step("project + media under a non-ASCII NEXUS_HOME", str(config.HOME) == str(home) and Path(m["path"]).exists(), m["path"])
    j = wait_job(call(f"/api/projects/{p.id}/direct", {"profile": "documentary", "target_seconds": 10, "size": "640x360"})["job"])
    step("director job", j["status"] == "done", j.get("error") or j.get("result", {}).get("summary"))
    tl = call(f"/api/projects/{p.id}")["timeline"]
    step("editable timeline", tl and tl["clips"], f"{len(tl['clips'])} clips, {tl['duration']} s")
    # UTF-8 round trip: an Arabic + Chinese caption timeline saved and read back by the server
    p = prj.Project(p.id)  # reload: the server saved the timeline after this process loaded the project
    full = p.timeline("main")
    full["subtitles"] = [{"start": 0.5, "end": 2.5, "text": "الطاقة الشمسية نظيفة"}, {"start": 3.0, "end": 5.0, "text": "太阳能很干净"}]
    full["lang"] = "ar"
    p.save_timeline(full, "utf-8 round trip", "utf8")
    back = p.timeline("utf8")
    step("Arabic/Chinese timeline round trip", back["subtitles"][0]["text"] == "الطاقة الشمسية نظيفة" and back["subtitles"][1]["text"] == "太阳能很干净")
    r = wait_job(call(f"/api/projects/{p.id}/render", {"branch": "utf8", "name": "utf8_render"})["job"])
    step("render job with burned Arabic/Chinese captions", r["status"] == "done", r.get("error") or r.get("result", {}).get("quality", {}).get("score"))
    out = p.dir / r["result"]["output"]
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(out)],
                                      capture_output=True, text=True).stdout)
    step("output MP4 is valid", float(probe["format"]["duration"]) > 5, probe["format"]["duration"])
    report["quality"] = r["result"].get("quality")
    Path("e2e_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
